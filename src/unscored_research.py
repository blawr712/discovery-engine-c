"""The tradeable names Score v3 never scores: are they better, and can they be sorted?

An earlier grouping showed names v3 never scores out-compounding its scored
names (324% against 183% in 2016-21). Compounded means in this universe are
carried by a few extreme rebounds, so this module asks the question the robust
way, with the questions fixed before results are read:

1. Each month-end, how do never-scored names compare with the screen (v3
   quintiles 3-5) on median excess return and win rate, in each window? The
   group is split by why v3 skipped it: financials (v3 is not built for
   them), no fundamentals at all, and the rest.
2. Inside the never-scored group, does any signal rank outcomes? The bar is
   the screen scan's: the same sign with |t| >= 2 in both windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.model_tests import SPLIT
from src.screen_research import (
    COMPOSITES, HORIZONS, PRIMARY_HORIZON, SIGNALS, _clean, _ic_series, _windows,
    _yearly, classify, screen_frame,
)

FINANCIALS = "Financial Services"
GROUPS = ("screen", "low_v3_quintile", "rejected_by_filters", "never_scored")
NEVER_SCORED_PARTS = ("financials", "no_fundamentals", "other")


def label_groups(observations: pd.DataFrame) -> pd.DataFrame:
    """Every tradeable observation with the screen group it would fall in."""
    frame = observations.copy()
    frame["period"] = frame["period"].astype(str)
    excluded = frame["score_v3_excluded"].astype(str).str.lower() == "true"
    frame["group"] = np.where(
        frame["score_v3"].notna(), "low_v3_quintile",
        np.where(excluded, "rejected_by_filters", "never_scored"),
    )
    passing = screen_frame(observations)
    frame.loc[passing.index, "group"] = "screen"
    no_fundamentals = frame["pit_revenue_ttm"].isna() if "pit_revenue_ttm" in frame else True
    financial = frame["sector"].astype(str) == FINANCIALS
    frame["never_scored_part"] = None
    never = frame["group"] == "never_scored"
    frame.loc[never & financial, "never_scored_part"] = "financials"
    frame.loc[never & ~financial & no_fundamentals, "never_scored_part"] = "no_fundamentals"
    frame.loc[never & ~financial & ~no_fundamentals, "never_scored_part"] = "other"
    return frame


def _group_stats(frame: pd.DataFrame, mask: pd.Series, horizon: str, split: str) -> dict:
    """Per-period median, win rate, and mean, averaged within each window."""
    excess = f"excess_{horizon}"
    part = frame[mask & frame[excess].notna()]
    rows = []
    for period, group in part.groupby("period", sort=True):
        if len(group) < 10:
            continue
        rows.append({
            "period": period, "names": len(group),
            "median": group[excess].median(),
            "win": (group[excess] > 0).mean() * 100,
            "mean": group[excess].mean(),
        })
    table = pd.DataFrame(rows)
    result = {}
    for name, window in (("fit", lambda t: t[t["period"] < split]),
                         ("holdout", lambda t: t[t["period"] >= split])):
        chosen = window(table) if len(table) else table
        result[name] = None if chosen.empty else {
            "periods": int(len(chosen)),
            "median_names": float(chosen["names"].median()),
            "median_excess": float(chosen["median"].mean()),
            "win_rate": float(chosen["win"].mean()),
            "mean_excess": float(chosen["mean"].mean()),
        }
    return {"windows": result, "by_period": table}


def compare_groups(frame: pd.DataFrame, horizon: str = PRIMARY_HORIZON, split: str = SPLIT) -> dict:
    """Question 1: each group against the screen, month by month."""
    masks = {name: frame["group"] == name for name in GROUPS}
    masks.update({
        f"never_scored:{part}": frame["never_scored_part"] == part for part in NEVER_SCORED_PARTS
    })
    stats = {name: _group_stats(frame, mask, horizon, split) for name, mask in masks.items()}
    screen = stats["screen"]["by_period"].set_index("period") if len(stats["screen"]["by_period"]) else None
    result = {}
    for name, entry in stats.items():
        summary = {"windows": entry["windows"]}
        table = entry["by_period"]
        if screen is not None and name != "screen" and len(table):
            joined = table.set_index("period").join(screen, rsuffix="_screen", how="inner")
            for window, chosen in (("fit", joined[joined.index < split]),
                                   ("holdout", joined[joined.index >= split])):
                if chosen.empty:
                    continue
                beat = chosen["median"] > chosen["median_screen"]
                summary.setdefault("against_screen", {})[window] = {
                    "periods": int(len(chosen)),
                    "median_minus_screen": float((chosen["median"] - chosen["median_screen"]).mean()),
                    "win_rate_minus_screen": float((chosen["win"] - chosen["win_screen"]).mean()),
                    "periods_median_above_screen": int(beat.sum()),
                }
        result[name] = summary
    return result


def scan_never_scored(frame: pd.DataFrame, split: str = SPLIT) -> dict:
    """Question 2: which signals rank names inside the never-scored group."""
    group = frame[frame["group"] == "never_scored"]
    scan = {}
    for column in [c for c in ("score_v2", "technical_score") + SIGNALS if c in group.columns]:
        if column in COMPOSITES and column not in ("score_v2", "technical_score"):
            continue
        entry = {"horizons": {}}
        for horizon in HORIZONS:
            series, coverage = _ic_series(group, column, horizon)
            windows = _windows(series, horizon, split)
            entry["horizons"][horizon] = {**windows, "verdict": classify(windows)}
            if horizon == PRIMARY_HORIZON:
                entry["coverage"] = float(coverage.mean()) if len(coverage) else 0.0
        primary = entry["horizons"][PRIMARY_HORIZON]
        entry["verdict"] = primary["verdict"]
        direction = 1.0 if (primary["fit"]["mean_ic"] or 0) >= 0 else -1.0
        entry["direction"] = "higher is better" if direction > 0 else "lower is better"
        entry["years"] = _yearly(group, column, PRIMARY_HORIZON, direction)
        scan[column] = entry
    return scan


def unscored_research(observations: pd.DataFrame, split: str = SPLIT) -> dict:
    frame = label_groups(observations)
    counts = frame.groupby("group").size().to_dict()
    parts = frame.groupby("never_scored_part").size().to_dict()
    return {
        "split": split,
        "primary_horizon": PRIMARY_HORIZON,
        "observations": {str(k): int(v) for k, v in counts.items()},
        "never_scored_parts": {str(k): int(v) for k, v in parts.items()},
        "groups": compare_groups(frame, PRIMARY_HORIZON, split),
        "groups_6M": compare_groups(frame, "6M", split),
        "never_scored_signals": scan_never_scored(frame, split),
    }


def _f(value, digits=1, sign=True):
    if value is None or not np.isfinite(value):
        return "—"
    return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"


def render_markdown(result: dict, run_id: str) -> str:
    lines = [
        f"# The names Score v3 never scores — {run_id}",
        "",
        "Tradeable names only. Groups at each month-end: the screen (v3 quintiles 3–5), "
        "v3's bottom two quintiles, names its filters reject, and names it never scores, "
        "split into financials, names with no fundamentals, and the rest.",
        "",
        "Observations: " + ", ".join(f"{k} {v:,}" for k, v in sorted(result["observations"].items()))
        + "; never scored: " + ", ".join(f"{k} {v:,}" for k, v in sorted(result["never_scored_parts"].items())) + ".",
        "",
        "## 1Y excess return by group (per-period figures averaged over each window)",
        "",
        "| Group | Window | Names a month | Median | Win % | Mean | Median vs screen | Win % vs screen | Months above screen |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, entry in result["groups"].items():
        for window in ("fit", "holdout"):
            stats = entry["windows"].get(window)
            if not stats:
                continue
            against = (entry.get("against_screen") or {}).get(window) or {}
            lines.append(
                f"| {name} | {window} | {stats['median_names']:.0f} | {_f(stats['median_excess'])} | "
                f"{_f(stats['win_rate'], sign=False)} | {_f(stats['mean_excess'])} | "
                f"{_f(against.get('median_minus_screen'))} | {_f(against.get('win_rate_minus_screen'))} | "
                + (f"{against['periods_median_above_screen']}/{against['periods']}" if against else "—")
                + " |"
            )
    lines += [
        "", "## Signals inside the never-scored group (1Y)", "",
        "Rule: separates: same sign in both windows and |t| >= 2 in both; "
        "suggestive: same sign and |t| >= 2 in one.",
        "",
        "| Signal | Verdict | Direction | Fit IC (t) | Holdout IC (t) | Years agreeing | Coverage |",
        "|---|---|---|---|---|---|---|",
    ]
    order = {"separates": 0, "suggestive": 1, "no": 2}
    for name, entry in sorted(result["never_scored_signals"].items(),
                              key=lambda item: (order[item[1]["verdict"]], item[0])):
        one = entry["horizons"]["1Y"]
        lines.append(
            f"| {name} | {entry['verdict']} | {entry['direction']} | "
            f"{_f(one['fit']['mean_ic'], 3)} ({_f(one['fit']['newey_west_t'])}) | "
            f"{_f(one['holdout']['mean_ic'], 3)} ({_f(one['holdout']['newey_west_t'])}) | "
            f"{entry['years']['agreeing']}/{entry['years']['years']} | {entry['coverage'] * 100:.0f}% |"
        )
    lines += [
        "", "## Caveats", "",
        "- Delisted companies are still missing; small, weak names are the most flattered.",
        "- Canadian names carry no fundamentals in the backtest, so they all fall in "
        "`never_scored:no_fundamentals` and are measured against a Canadian benchmark.",
        "- Not a return forecast.",
    ]
    return "\n".join(lines) + "\n"


def export_unscored_research(result: dict, run_id: str, output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"unscored_research_{run_id}.json"
    markdown_path = output_directory / f"unscored_research_{run_id}.md"
    json_path.write_text(json.dumps(_clean({"run_id": run_id, **result}), indent=2, sort_keys=True),
                         encoding="utf-8")
    markdown_path.write_text(render_markdown(result, run_id), encoding="utf-8")
    return json_path, markdown_path
