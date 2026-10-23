"""Recompute the backtest's group returns with delisted companies included.

Observed rows keep the groups the backtest gave them. Dead rows are placed
by running v3's own ``score_frame`` over each month's cross-section with them
appended, which applies the filters exactly as the backtest does. Returns are
equal-weighted monthly means compounded at the 1M rebalance, the same
construction as the comparison they bound.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from src.survivorship import SCENARIOS, assign_groups, compounded, monthly_rates

GROUPS = ("filter_excluded", "scored", "never_scored")
SPLIT = "2022-01"
WINDOWS = (("2016-2021", lambda p: p < SPLIT), ("2022-2026", lambda p: p >= SPLIT))


def observed_groups(frame: pd.DataFrame) -> pd.Series:
    """What the backtest did with each observed row."""
    excluded = (
        frame["score_v3_excluded"].fillna(False).astype(str).str.lower().isin({"true", "1"})
        if "score_v3_excluded" in frame else pd.Series(False, index=frame.index)
    )
    groups = pd.Series("never_scored", index=frame.index)
    groups[frame["score_v3"].notna()] = "scored"
    groups[excluded] = "filter_excluded"
    return groups


def build_report(
    observations: pd.DataFrame,
    dead: pd.DataFrame,
    model: dict,
    score_frame,
    funnel: dict,
    scenarios: dict[str, dict[str, float]] | None = None,
    minimum_cross_section: int = 50,
    minimum_scored: int = 100,
) -> dict:
    scenarios = scenarios or SCENARIOS
    frame = observations.dropna(subset=["return_1M", "benchmark_return_1M"]).copy()
    frame["period"] = frame["period"].astype(str)
    frame["group"] = observed_groups(frame)

    dead = dead.copy()
    if not dead.empty:
        dead["period"] = dead["period"].astype(str)
        dead["group"] = None
        for period, rows in dead.groupby("period"):
            month = frame[frame["period"] == period]
            combined = pd.concat([month.drop(columns=["group"]), rows], ignore_index=True)
            groups = assign_groups(combined, model, score_frame)
            dead.loc[rows.index, "group"] = groups.iloc[len(month):].to_numpy()

    periods = []
    for period, month in frame.groupby("period", sort=True):
        if len(month) < minimum_cross_section or (month["group"] == "scored").sum() < minimum_scored:
            continue
        periods.append(period)

    result = {"funnel": funnel, "windows": {}}
    for window, inside in WINDOWS:
        chosen = [p for p in periods if inside(p)]
        month_rows = frame[frame["period"].isin(chosen)]
        dead_rows = dead[dead["period"].isin(chosen)] if not dead.empty else dead
        entry = {"months": len(chosen), "counts": {}, "observed": {}, "scenarios": {}}
        for group in GROUPS:
            observed = month_rows[month_rows["group"] == group]
            missing = dead_rows[dead_rows["group"] == group] if not dead_rows.empty else dead_rows
            entry["counts"][group] = {
                "observed_per_month": round(len(observed) / max(len(chosen), 1), 1),
                "dead_per_month": round(len(missing) / max(len(chosen), 1), 1),
                "dead_share_percent": round(
                    100.0 * len(missing) / max(len(observed) + len(missing), 1), 2,
                ),
            }
        entry["observed"] = _compounded_by_group(month_rows, chosen, None, None)
        entry["observed"]["universe"] = compounded(
            month_rows.groupby("period")["return_1M"].mean().reindex(chosen)
        )
        for name, scenario in scenarios.items():
            rates = monthly_rates(dead_rows, scenario) if not dead_rows.empty else pd.Series(dtype=float)
            entry["scenarios"][name] = _compounded_by_group(month_rows, chosen, dead_rows, rates)
        if not dead_rows.empty:
            entry["dead_exit_types"] = dict(Counter(
                dead_rows.drop_duplicates("ticker")["exit_type"]
            ))
        result["windows"][window] = entry
    return result


def _compounded_by_group(month_rows, chosen, dead_rows, rates) -> dict:
    out = {}
    universe_parts = []
    for group in GROUPS:
        observed = month_rows[month_rows["group"] == group][["period", "return_1M"]]
        parts = [observed.rename(columns={"return_1M": "r"})]
        if dead_rows is not None and not dead_rows.empty:
            mask = dead_rows["group"] == group
            parts.append(pd.DataFrame({"period": dead_rows.loc[mask, "period"], "r": rates[mask]}))
        joined = pd.concat(parts, ignore_index=True)
        universe_parts.append(joined)
        out[group] = compounded(joined.groupby("period")["r"].mean().reindex(chosen))
    if dead_rows is not None:
        everything = pd.concat(universe_parts, ignore_index=True)
        out["universe"] = compounded(everything.groupby("period")["r"].mean().reindex(chosen))
    return out


def build_markdown(report: dict, run_id: str, basis: str) -> str:
    funnel = report["funnel"]
    lines = [
        f"# Survivorship bounds for run `{run_id}`",
        "",
        f"Backtest basis: {basis}. Compounded equal-weight 1M returns by what v3 did "
        "with each name, observed and with delisted companies added under three "
        "return scenarios. A range, not a measurement.",
        "",
        "## Delisted companies found",
        "",
    ]
    for key, value in funnel.items():
        lines.append(f"- {key.replace('_', ' ')}: {value}")
    for window, entry in report["windows"].items():
        lines += ["", f"## {window} ({entry['months']} months)", "",
                  "| | Filter-excluded | Scored | Never scored | Universe |",
                  "|---|---|---|---|---|"]
        rows = [("Observed", entry["observed"])] + [
            (f"With delisted, {name}", values) for name, values in entry["scenarios"].items()
        ]
        for label, values in rows:
            cells = " | ".join(_pct(values.get(group)) for group in GROUPS + ("universe",))
            lines.append(f"| {label} | {cells} |")
        lines += ["", "Delisted observations as a share of each group:", ""]
        for group in GROUPS:
            counts = entry["counts"][group]
            lines.append(
                f"- {group.replace('_', ' ')}: {counts['dead_per_month']} a month beside "
                f"{counts['observed_per_month']} observed ({counts['dead_share_percent']}%)"
            )
        if entry.get("dead_exit_types"):
            lines.append("- exit types: " + ", ".join(
                f"{name} {count}" for name, count in sorted(entry["dead_exit_types"].items())
            ))
    lines += [
        "",
        "Scenario lifetime returns by exit type:",
        "",
    ] + [
        f"- {name}: " + ", ".join(f"{k} {v:+.0%}" for k, v in scenario.items())
        for name, scenario in SCENARIOS.items()
    ] + ["", "Not a return forecast or an investment recommendation.", ""]
    return "\n".join(lines)


def _pct(value) -> str:
    return f"{value:.0f}%" if isinstance(value, (int, float)) and np.isfinite(value) else "-"
