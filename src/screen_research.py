"""What separates winners inside the Score v3 screen.

Score v3 is used as a screen: its bottom two fifths and its filter rejections
did worst, while its top three fifths did alike. This module asks which
signals, measured the same point-in-time way, rank names *inside* the screen.

The rules are fixed before results are read:

- The population at each month-end is what the live screen would pass:
  v3-ranked (tradeable, not filter-rejected) names in v3 quintiles 3-5,
  assigned with the formula the research queue and frozen cohorts use.
- A signal's evidence is its per-period Spearman correlation with forward
  excess return inside that population, with Newey-West t-statistics because
  monthly 1Y returns overlap by eleven months.
- Because many signals are scanned, a signal *separates* winners only when
  its correlation has the same sign in the fitting window and the holdout and
  |t| >= 2 in both. Same sign with |t| >= 2 in one window is *suggestive*.
- A combined tie-breaker is built from signals chosen on the fitting window
  alone and judged on the holdout alone against v3's own order.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.model_tests import MINIMUM_PAIRS, NEWEY_WEST_LAGS, SPLIT, newey_west_t

PRIMARY_HORIZON = "1Y"
HORIZONS = ("1Y", "6M", "3M")
SCREEN_QUANTILES = 5
SCREEN_MINIMUM_QUINTILE = 3
T_THRESHOLD = 2.0

# Composite scores, then raw signals. Composites are tested too, as a check
# on how much ordering inside the screen each model still carries.
# discovery_score_static is left out: its market-cap points come from the
# source run's current market cap, which encodes each name's later returns
# (inside the screen that part alone shows a 1Y IC of -0.18, t -20).
COMPOSITES = ("score_v3", "score_v3_challenger", "score_v2", "technical_score")
SIGNALS = (
    # Price and trading
    "v2_momentum_long", "v2_momentum_medium", "v2_short_term_reversal",
    "v2_high_proximity", "v2_volatility", "v2_volume_trend",
    "relative_strength_6m", "volume_ratio", "volume_score", "trend_score",
    "relative_strength_score", "liquidity_score", "average_dollar_volume_30d",
    "universe_market_cap",
    # Fundamentals
    "pit_revenue_ttm", "pit_revenue_growth_ttm", "pit_revenue_acceleration",
    "pit_gross_margin_ttm", "pit_gross_margin_change",
    "pit_operating_margin_ttm", "pit_ocf_margin_ttm", "pit_fcf_margin_ttm",
    "pit_cash_conversion", "pit_share_change_1y", "pit_net_cash_to_market_cap",
    "pit_fcf_yield", "pit_earnings_yield", "pit_sales_yield",
    "pit_report_age_days",
    # Insiders
    "ins_purchase_count_short", "ins_sale_count_short", "ins_net_count_short",
    "ins_distinct_buyers_short", "ins_officer_purchase_count_short",
    "ins_net_value_to_market_cap_short", "ins_cluster_buy_short",
    "ins_purchase_count_long", "ins_net_value_to_market_cap_long",
    "ins_days_since_last_purchase",
)


def screen_frame(observations: pd.DataFrame,
                 quantiles: int = SCREEN_QUANTILES,
                 minimum_quintile: int = SCREEN_MINIMUM_QUINTILE) -> pd.DataFrame:
    """Observations the live screen would pass, with their v3 quintile.

    Quintiles follow ``research_ranking._apply_screen``: ranked rows ordered by
    v3 score (ticker breaks ties), bucket ``index * quantiles // total``,
    quintile 5 at the top.
    """
    frame = observations.copy()
    frame["period"] = frame["period"].astype(str)
    ranked = frame[frame["score_v3"].notna()]
    if "score_v3_excluded" in ranked:
        ranked = ranked[ranked["score_v3_excluded"].astype(str).str.lower() != "true"]
    parts = []
    for _, group in ranked.groupby("period", sort=True):
        group = group.sort_values(["score_v3", "ticker"], ascending=[False, True])
        total = len(group)
        index = np.arange(total)
        bucket = np.minimum(quantiles - 1, index * quantiles // total)
        group = group.assign(v3_quintile=quantiles - bucket)
        parts.append(group)
    if not parts:
        return ranked.assign(v3_quintile=pd.Series(dtype=int))
    screened = pd.concat(parts)
    return screened[screened["v3_quintile"] >= minimum_quintile]


def _ic_series(frame: pd.DataFrame, column: str, horizon: str) -> tuple[pd.Series, pd.Series]:
    """Per-period Spearman IC and coverage of one column inside the screen."""
    excess = f"excess_{horizon}"
    values, coverage = {}, {}
    for period, group in frame.groupby("period", sort=True):
        usable = group[group[excess].notna()]
        if usable.empty:
            continue
        pairs = usable[[column, excess]].dropna()
        coverage[period] = len(pairs) / len(usable)
        if len(pairs) >= MINIMUM_PAIRS and pairs[column].nunique() > 1:
            values[period] = pairs[column].rank().corr(pairs[excess].rank())
    # String-indexed even when empty, so window splits by period label work.
    return (pd.Series(values, dtype=float, index=pd.Index(list(values), dtype=object)).sort_index(),
            pd.Series(coverage, dtype=float, index=pd.Index(list(coverage), dtype=object)).sort_index())


def _windows(series: pd.Series, horizon: str, split: str = SPLIT) -> dict:
    lags = NEWEY_WEST_LAGS.get(horizon, 0)
    result = {}
    for name, part in (("fit", series[series.index < split]),
                       ("holdout", series[series.index >= split])):
        result[name] = {
            "periods": int(len(part)),
            "mean_ic": float(part.mean()) if len(part) else float("nan"),
            "newey_west_t": newey_west_t(part, lags) if len(part) else float("nan"),
        }
    return result


def classify(windows: dict, threshold: float = T_THRESHOLD) -> str:
    """'separates', 'suggestive', or 'no', by the rule fixed in advance."""
    fit, holdout = windows["fit"], windows["holdout"]
    stats = (fit["newey_west_t"], holdout["newey_west_t"])
    means = (fit["mean_ic"], holdout["mean_ic"])
    if any(not np.isfinite(value) for value in stats + means):
        return "no"
    if np.sign(means[0]) != np.sign(means[1]) or means[0] == 0:
        return "no"
    strong = [abs(value) >= threshold and np.sign(value) == np.sign(means[0]) for value in stats]
    if all(strong):
        return "separates"
    if any(strong):
        return "suggestive"
    return "no"


def half_split(frame: pd.DataFrame, column: str, direction: float, horizon: str,
               split: str = SPLIT) -> dict:
    """Median excess and win rate of the better half against the worse half.

    Means in this universe are dominated by a few extreme rebounds, so the
    robust check is per-period medians and hit rates, averaged per window.
    """
    excess = f"excess_{horizon}"
    rows = []
    for period, group in frame.groupby("period", sort=True):
        pairs = group[[column, excess]].dropna()
        if len(pairs) < MINIMUM_PAIRS or pairs[column].nunique() < 2:
            continue
        ranks = (pairs[column] * direction).rank(pct=True, method="average")
        better, worse = pairs[ranks > 0.5][excess], pairs[ranks <= 0.5][excess]
        if better.empty or worse.empty:
            continue
        rows.append({
            "period": period,
            "better_median": better.median(), "worse_median": worse.median(),
            "better_win": (better > 0).mean() * 100, "worse_win": (worse > 0).mean() * 100,
        })
    table = pd.DataFrame(rows)
    result = {}
    for name, part in (("fit", table[table["period"] < split] if len(table) else table),
                       ("holdout", table[table["period"] >= split] if len(table) else table)):
        if part.empty:
            result[name] = None
            continue
        result[name] = {
            "better_median_excess": float(part["better_median"].mean()),
            "worse_median_excess": float(part["worse_median"].mean()),
            "better_win_rate": float(part["better_win"].mean()),
            "worse_win_rate": float(part["worse_win"].mean()),
        }
    return result


def _yearly(frame: pd.DataFrame, column: str, horizon: str, direction: float) -> dict:
    """Mean IC by calendar year of the month-end, and how many years agree.

    Monthly 1Y returns overlap, so the holdout holds only about four
    independent years; a sign that holds year after year is the plainer test.
    """
    series, _ = _ic_series(frame, column, horizon)
    if series.empty:
        return {"by_year": {}, "agreeing": 0, "years": 0}
    by_year = series.groupby(series.index.str[:4]).mean()
    agreeing = int((np.sign(by_year) == np.sign(direction)).sum())
    return {"by_year": {year: float(value) for year, value in by_year.items()},
            "agreeing": agreeing, "years": int(len(by_year))}


def _within_correlation(frame: pd.DataFrame, column: str, other: str) -> float:
    if column == other:
        return 1.0
    values = []
    for _, group in frame.groupby("period", sort=True):
        pairs = group[[column, other]].dropna()
        if len(pairs) >= MINIMUM_PAIRS and pairs[column].nunique() > 1 and pairs[other].nunique() > 1:
            values.append(pairs[column].rank().corr(pairs[other].rank()))
    return float(np.mean(values)) if values else float("nan")


def tiebreaker(frame: pd.DataFrame, chosen: dict[str, float]) -> pd.Series:
    """Equal-weight mean of signed within-period percentile ranks.

    A name missing a signal is scored on the signals it has; a name missing
    all of them gets the period's middle (0.5), so coverage does not decide
    the order.
    """
    if not chosen:
        return pd.Series(np.nan, index=frame.index)
    parts = []
    for column, direction in chosen.items():
        parts.append((frame[column] * direction).groupby(frame["period"]).rank(pct=True))
    combined = pd.concat(parts, axis=1).mean(axis=1)
    return combined.fillna(0.5)


def out_of_sample_tiebreaker(frame: pd.DataFrame, scan: dict, horizon: str = PRIMARY_HORIZON,
                             split: str = SPLIT, threshold: float = T_THRESHOLD) -> dict:
    """Choose signals on the fitting window only; judge them on the holdout only."""
    chosen = {}
    for column, entry in scan.items():
        if column in COMPOSITES:
            continue
        fit = entry["horizons"][horizon]["fit"]
        if np.isfinite(fit["newey_west_t"]) and abs(fit["newey_west_t"]) >= threshold:
            chosen[column] = 1.0 if fit["mean_ic"] > 0 else -1.0
    working = frame.assign(_tiebreaker=tiebreaker(frame, chosen))
    combined, _ = _ic_series(working, "_tiebreaker", horizon)
    base, _ = _ic_series(working, "score_v3", horizon)
    difference = (combined - base).dropna()
    lags = NEWEY_WEST_LAGS.get(horizon, 0)
    holdout = combined[combined.index >= split]
    holdout_difference = difference[difference.index >= split]
    return {
        "horizon": horizon,
        "chosen_on_fit_window": chosen,
        "holdout": {
            "periods": int(len(holdout)),
            "mean_ic": float(holdout.mean()) if len(holdout) else float("nan"),
            "newey_west_t": newey_west_t(holdout, lags) if len(holdout) else float("nan"),
            "mean_ic_minus_v3": float(holdout_difference.mean()) if len(holdout_difference) else float("nan"),
            "difference_newey_west_t": newey_west_t(holdout_difference, lags) if len(holdout_difference) else float("nan"),
        },
        "holdout_half_split": half_split(working, "_tiebreaker", 1.0, horizon, split)["holdout"],
    }


def scan_screen(observations: pd.DataFrame, split: str = SPLIT) -> dict:
    """The whole study: every signal's within-screen evidence and the tie-breaker."""
    frame = screen_frame(observations)
    columns = [c for c in COMPOSITES + SIGNALS if c in frame.columns]
    scan = {}
    for column in columns:
        entry = {"horizons": {}}
        for horizon in HORIZONS:
            series, coverage = _ic_series(frame, column, horizon)
            windows = _windows(series, horizon, split)
            entry["horizons"][horizon] = {**windows, "verdict": classify(windows)}
            if horizon == PRIMARY_HORIZON:
                entry["coverage"] = float(coverage.mean()) if len(coverage) else 0.0
        primary = entry["horizons"][PRIMARY_HORIZON]
        entry["verdict"] = primary["verdict"]
        direction = 1.0 if primary["fit"]["mean_ic"] >= 0 else -1.0
        entry["years"] = _yearly(frame, column, PRIMARY_HORIZON, direction)
        entry["direction"] = "higher is better" if direction > 0 else "lower is better"
        if entry["verdict"] != "no":
            entry["half_split"] = half_split(frame, column, direction, PRIMARY_HORIZON, split)
            entry["correlation_with_v3"] = _within_correlation(frame, column, "score_v3")
            entry["correlation_with_size"] = _within_correlation(frame, column, "universe_market_cap")
        scan[column] = entry
    periods = sorted(frame["period"].unique())
    return {
        "split": split,
        "primary_horizon": PRIMARY_HORIZON,
        "rule": ("separates: same sign in both windows and |t| >= 2 in both; "
                 "suggestive: same sign and |t| >= 2 in one"),
        "population": {
            "observations": int(len(frame)),
            "periods": len(periods),
            "first_period": periods[0] if periods else None,
            "last_period": periods[-1] if periods else None,
            "median_names_per_period": float(frame.groupby("period").size().median()) if len(frame) else 0.0,
        },
        "signals": scan,
        "tiebreaker": out_of_sample_tiebreaker(frame, scan, PRIMARY_HORIZON, split),
    }


def _fmt(value, digits=3):
    return "—" if value is None or not np.isfinite(value) else f"{value:+.{digits}f}"


def render_markdown(result: dict, run_id: str) -> str:
    population = result["population"]
    lines = [
        f"# What separates winners inside the v3 screen — {run_id}",
        "",
        f"Population: names the live screen would pass (v3 quintiles 3–5, tradeable, "
        f"not filter-rejected), {population['observations']:,} observations over "
        f"{population['periods']} month-ends ({population['first_period']} to "
        f"{population['last_period']}), median {population['median_names_per_period']:.0f} "
        f"names a month. Fitting window before {result['split']}, holdout from it.",
        "",
        f"Rule fixed in advance — {result['rule']}.",
        "",
        "| Signal | Verdict | Direction | Fit 1Y IC (t) | Holdout 1Y IC (t) | Years agreeing | 6M verdict | 3M verdict | Coverage |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    order = {"separates": 0, "suggestive": 1, "no": 2}
    entries = sorted(result["signals"].items(),
                     key=lambda item: (order[item[1]["verdict"]],
                                       -abs(item[1]["horizons"]["1Y"]["holdout"]["newey_west_t"] or 0)
                                       if np.isfinite(item[1]["horizons"]["1Y"]["holdout"]["newey_west_t"]) else 0))
    for column, entry in entries:
        one = entry["horizons"]["1Y"]
        lines.append(
            f"| {column} | {entry['verdict']} | {entry['direction']} | "
            f"{_fmt(one['fit']['mean_ic'])} ({_fmt(one['fit']['newey_west_t'], 1)}) | "
            f"{_fmt(one['holdout']['mean_ic'])} ({_fmt(one['holdout']['newey_west_t'], 1)}) | "
            f"{entry['years']['agreeing']}/{entry['years']['years']} | "
            f"{entry['horizons']['6M']['verdict']} | {entry['horizons']['3M']['verdict']} | "
            f"{entry['coverage'] * 100:.0f}% |"
        )
    lines += ["", "## Better half against worse half (1Y, per-period medians)", "",
              "| Signal | Window | Better median | Worse median | Better win % | Worse win % | Corr. with v3 | Corr. with size |",
              "|---|---|---|---|---|---|---|---|"]
    for column, entry in entries:
        if entry["verdict"] == "no":
            continue
        for window in ("fit", "holdout"):
            half = (entry.get("half_split") or {}).get(window)
            if not half:
                continue
            lines.append(
                f"| {column} | {window} | {half['better_median_excess']:+.1f} | "
                f"{half['worse_median_excess']:+.1f} | {half['better_win_rate']:.1f} | "
                f"{half['worse_win_rate']:.1f} | {_fmt(entry['correlation_with_v3'], 2)} | "
                f"{_fmt(entry['correlation_with_size'], 2)} |"
            )
    tie = result["tiebreaker"]
    holdout = tie["holdout"]
    chosen = ", ".join(f"{name} ({'+' if sign > 0 else '−'})" for name, sign in tie["chosen_on_fit_window"].items()) or "none"
    lines += [
        "", "## Out-of-sample tie-breaker", "",
        f"Signals chosen on the fitting window alone (|t| ≥ 2 at 1Y): {chosen}.",
        "",
        f"Holdout within-screen IC {_fmt(holdout['mean_ic'])} (t {_fmt(holdout['newey_west_t'], 1)}) "
        f"over {holdout['periods']} periods; against v3's own order "
        f"{_fmt(holdout['mean_ic_minus_v3'])} (t {_fmt(holdout['difference_newey_west_t'], 1)}).",
    ]
    half = tie.get("holdout_half_split")
    if half:
        lines.append(
            f"Holdout better half median {half['better_median_excess']:+.1f} against "
            f"{half['worse_median_excess']:+.1f}; win rate {half['better_win_rate']:.1f}% against "
            f"{half['worse_win_rate']:.1f}%."
        )
    lines += [
        "", "## Caveats", "",
        "- Delisted companies are still missing from the backtest; signals that favour "
        "smaller or weaker companies are the most flattered by that (see the size column).",
        "- Insider counts are measured only where a company has Form 4 history; coverage "
        "is the share of screened names with a value.",
        "- This ranks research attention inside the screen. It is not a return forecast.",
    ]
    return "\n".join(lines) + "\n"


def _clean(value):
    """JSON has no NaN; write missing statistics as null."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def export_screen_research(result: dict, run_id: str, output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"screen_research_{run_id}.json"
    markdown_path = output_directory / f"screen_research_{run_id}.md"
    json_path.write_text(json.dumps(_clean({"run_id": run_id, **result}), indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown(result, run_id), encoding="utf-8")
    return json_path, markdown_path
