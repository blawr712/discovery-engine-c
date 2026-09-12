"""Point-in-time backtest of Discovery Score technical factors.

The backtest replays the configured technical scoring functions at historical
month-ends using only price data available on each date, then measures how the
resulting scores related to subsequent benchmark-relative returns. It never
changes official scores or ranks, and it reports its own structural
limitations so results are not mistaken for a return forecast.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable

import numpy as np
import pandas as pd

from src.config import BACKTEST_CONFIG, SCORING_CONFIG
from src.data_sources.base import MarketDataSource
from src.scoring import (
    score_liquidity,
    score_market_cap,
    score_relative_strength,
    score_sector_bonus,
    score_trend_strength,
    score_volume_acceleration,
)
from src.scoring_v2 import (
    RAW_PREFIX,
    compute_raw_signals,
    score_frame,
    validated_config as validated_v2_config,
)


ProgressCallback = Callable[[str, int, int, str], None]

COMPOSITE_COLUMNS = ("technical_score", "discovery_score_static", "score_v2")
FACTOR_POINT_COLUMNS = (
    "volume_score",
    "relative_strength_score",
    "trend_score",
    "liquidity_score",
)
FACTOR_RAW_COLUMNS = ("volume_ratio", "relative_strength_6m")
V2_RAW_COLUMNS = tuple(
    f"{RAW_PREFIX}{name}" for name in validated_v2_config()["signals"]
)
IC_COLUMNS = (
    COMPOSITE_COLUMNS + FACTOR_POINT_COLUMNS + FACTOR_RAW_COLUMNS + V2_RAW_COLUMNS
)

LIMITATIONS = (
    "Survivorship bias: the universe is taken from a recent completed run, so "
    "companies that delisted before that run are absent from every period.",
    "Market-cap and sector points use the saved run's current values rather "
    "than historical values; technical_score excludes them for that reason.",
    "Returns use adjusted closes in each listing's local currency, ignore "
    "transaction costs, spreads, and slippage, and treat halted names as "
    "unavailable after the configured stale tolerance.",
    "Horizons longer than the rebalance interval overlap, so their period "
    "statistics are not independent observations.",
    "Ties in coarse scores are broken by relative strength and then ticker; "
    "top-N results therefore depend on that deterministic tie-break.",
    "This is a structural validation of the configured screen, not a return "
    "forecast or an investment recommendation.",
)


def build_backtest_universe(
    results: list[dict],
    config: dict | None = None,
) -> list[dict]:
    """Select saved-run rows that passed metadata screening."""
    config = _validated_config(config)
    statuses = set(config["source_statuses"])
    universe = {}
    for row in results:
        if str(row.get("status")) not in statuses:
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker in universe:
            continue
        universe[ticker] = {
            "ticker": ticker,
            "country": str(row.get("country") or "US").upper(),
            "sector": row.get("sector"),
            "market_cap": _number(row.get("market_cap")),
            "source_status": str(row.get("status")),
        }
    return [universe[ticker] for ticker in sorted(universe)]


def collect_price_histories(
    universe: list[dict],
    benchmarks: dict[str, str],
    source: MarketDataSource,
    *,
    period: str,
    max_workers: int = 5,
    progress_callback: ProgressCallback | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, str]]:
    """Collect long price histories with per-company failure isolation."""
    if max_workers < 1:
        raise ValueError("Backtest price workers must be at least 1.")

    benchmark_histories: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    benchmark_tickers = sorted({
        benchmarks.get(row["country"], "SPY") for row in universe
    })
    for completed, ticker in enumerate(benchmark_tickers, start=1):
        try:
            benchmark_histories[ticker] = source.get_price_history(ticker, period)
        except Exception as error:  # noqa: BLE001 - isolate provider failures
            errors[ticker] = f"{type(error).__name__}: {error}"
        if progress_callback is not None:
            progress_callback(
                "backtest-benchmarks", completed, len(benchmark_tickers), ticker,
            )

    histories: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(source.get_price_history, row["ticker"], period):
                row["ticker"]
            for row in universe
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            try:
                histories[ticker] = future.result()
            except Exception as error:  # noqa: BLE001 - isolate provider failures
                errors[ticker] = f"{type(error).__name__}: {error}"
            if progress_callback is not None:
                progress_callback(
                    "backtest-prices", completed, len(futures), ticker,
                )
    return histories, benchmark_histories, errors


def build_backtest(
    universe: list[dict],
    histories: dict[str, pd.DataFrame],
    benchmark_histories: dict[str, pd.DataFrame],
    benchmarks: dict[str, str],
    run_id: str,
    *,
    config: dict | None = None,
    collection_errors: dict[str, str] | None = None,
) -> dict:
    """Replay technical scoring at historical month-ends and measure outcomes."""
    config = _validated_config(config)
    collection_errors = dict(collection_errors or {})
    horizons = config["forward_horizons_days"]

    calendars: dict[str, pd.DatetimeIndex] = {}
    benchmark_frames: dict[str, pd.DataFrame] = {}
    for ticker, frame in benchmark_histories.items():
        normalized = _normalize_history(frame)
        if normalized is None:
            collection_errors[ticker] = "Benchmark history is empty or invalid"
            continue
        benchmark_frames[ticker] = normalized
        calendars[ticker] = normalized.index

    periods = _rebalance_periods(calendars)
    observations: list[dict] = []
    usable_tickers: set[str] = set()
    skipped: dict[str, int] = {
        "missing_history": 0,
        "missing_benchmark": 0,
        "invalid_history": 0,
    }

    benchmark_slices: dict[tuple[str, str], pd.DataFrame] = {}
    for row in universe:
        ticker = row["ticker"]
        benchmark_ticker = benchmarks.get(row["country"], "SPY")
        if benchmark_ticker not in benchmark_frames:
            skipped["missing_benchmark"] += 1
            continue
        raw = histories.get(ticker)
        if raw is None:
            skipped["missing_history"] += 1
            continue
        history = _normalize_history(raw)
        if history is None:
            skipped["invalid_history"] += 1
            continue

        calendar = calendars[benchmark_ticker]
        benchmark_frame = benchmark_frames[benchmark_ticker]
        aligned_close = history["Close"].reindex(calendar).ffill(
            limit=config["maximum_stale_trading_days"],
        )
        benchmark_close = benchmark_frame["Close"]
        static_market_cap_score = (
            score_market_cap(row["market_cap"])
            if row["market_cap"] is not None
            else 0.0
        )
        static_sector_score = score_sector_bonus(row["sector"])

        for period_label, period_dates in periods:
            date = period_dates.get(benchmark_ticker)
            if date is None:
                continue
            position = calendar.get_loc(date)
            history_slice = history.loc[:date]
            if len(history_slice) < config["minimum_history_days"]:
                continue
            stale_boundary = calendar[
                max(0, position - config["maximum_stale_trading_days"])
            ]
            if history_slice.index[-1] < stale_boundary:
                continue

            key = (benchmark_ticker, period_label)
            if key not in benchmark_slices:
                benchmark_slices[key] = benchmark_frame.loc[:date]
            benchmark_slice = benchmark_slices[key]

            volume_score, volume_ratio = score_volume_acceleration(history_slice)
            relative_strength_score, relative_strength = score_relative_strength(
                history_slice,
                benchmark_slice,
            )
            trend_score = score_trend_strength(history_slice)
            liquidity_score = score_liquidity(history_slice)
            technical_score = (
                volume_score + relative_strength_score
                + trend_score + liquidity_score
            )
            observation = {
                "period": period_label,
                "date": date.date().isoformat(),
                "ticker": ticker,
                "country": row["country"],
                "sector": row["sector"],
                "benchmark": benchmark_ticker,
                "volume_score": float(volume_score),
                "volume_ratio": _finite(volume_ratio),
                "relative_strength_score": float(relative_strength_score),
                "relative_strength_6m": _finite(relative_strength),
                "trend_score": float(trend_score),
                "liquidity_score": float(liquidity_score),
                "technical_score": float(technical_score),
                "discovery_score_static": float(
                    technical_score + static_market_cap_score + static_sector_score
                ),
                **compute_raw_signals(history_slice),
            }
            start_close = aligned_close.iloc[position]
            for label, days in horizons.items():
                end_position = position + days
                stock_return = None
                benchmark_return = None
                if end_position < len(calendar) and _positive(start_close):
                    end_close = aligned_close.iloc[end_position]
                    if _positive(end_close):
                        stock_return = (end_close / start_close - 1.0) * 100.0
                    benchmark_start = benchmark_close.iloc[position]
                    benchmark_end = benchmark_close.iloc[end_position]
                    if _positive(benchmark_start) and _positive(benchmark_end):
                        benchmark_return = (
                            benchmark_end / benchmark_start - 1.0
                        ) * 100.0
                excess = (
                    stock_return - benchmark_return
                    if stock_return is not None and benchmark_return is not None
                    else None
                )
                observation[f"return_{label}"] = stock_return
                observation[f"benchmark_return_{label}"] = benchmark_return
                observation[f"excess_{label}"] = excess
            observations.append(observation)
            usable_tickers.add(ticker)

    frame = pd.DataFrame(observations)
    frame = _apply_score_v2(frame)
    period_metrics = _period_metrics(frame, config)
    aggregate = _aggregate_metrics(period_metrics, frame, config)

    return {
        "model_version": config["model_version"],
        "source_run_id": run_id,
        "config": config,
        "limitations": list(LIMITATIONS),
        "coverage": {
            "universe_tickers": len(universe),
            "collected_tickers": len(histories),
            "usable_tickers": len(usable_tickers),
            "collection_errors": len(collection_errors),
            "skipped": skipped,
            "benchmarks": sorted(benchmark_frames),
            "periods": len(periods),
            "first_period": periods[0][0] if periods else None,
            "last_period": periods[-1][0] if periods else None,
            "observations": int(len(frame)),
            "countries": _counts(frame, "country"),
        },
        "collection_errors": dict(sorted(collection_errors.items())),
        "aggregate": aggregate,
        "periods": period_metrics,
        "official_scores_and_ranks_unchanged": True,
        "_observations": frame,
    }


def export_backtest(
    analysis: dict,
    output_directory: Path,
) -> dict[str, str]:
    """Write deterministic JSON, CSV, and Markdown backtest artifacts."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_id = analysis["source_run_id"]
    frame = analysis["_observations"]
    payload = {
        key: value for key, value in analysis.items() if key != "_observations"
    }

    json_path = output_directory / f"backtest_{run_id}.json"
    periods_path = output_directory / f"backtest_periods_{run_id}.csv"
    observations_path = (
        output_directory / f"backtest_observations_{run_id}.csv.gz"
    )
    summary_path = output_directory / f"backtest_summary_{run_id}.md"

    _atomic_json(json_path, payload)
    _atomic_csv(periods_path, _flatten_periods(analysis["periods"]))
    _atomic_observations(observations_path, frame)
    _atomic_text(summary_path, build_backtest_markdown(payload))
    return {
        "backtest_json_path": str(json_path),
        "backtest_periods_csv_path": str(periods_path),
        "backtest_observations_csv_path": str(observations_path),
        "backtest_summary_markdown_path": str(summary_path),
    }


def build_backtest_markdown(analysis: dict) -> str:
    """Render a readable summary of aggregate backtest statistics."""
    coverage = analysis["coverage"]
    aggregate = analysis["aggregate"]
    config = analysis["config"]
    lines = [
        "# Discovery Engine Technical Backtest",
        "",
        f"- Source run: `{analysis['source_run_id']}`",
        f"- Model: `{analysis['model_version']}`",
        f"- Periods: {coverage['periods']} "
        f"({coverage['first_period']} to {coverage['last_period']})",
        f"- Universe / collected / usable tickers: "
        f"{coverage['universe_tickers']} / {coverage['collected_tickers']} / "
        f"{coverage['usable_tickers']}",
        f"- Observations: {coverage['observations']}",
        "",
        "## Rank correlation with forward excess return (Spearman IC)",
        "",
        "| Signal | Horizon | Periods | Mean IC | t-stat | Positive share |",
        "|---|---|---|---|---|---|",
    ]
    for column in IC_COLUMNS:
        for horizon in config["forward_horizons_days"]:
            stats = aggregate["information_coefficient"].get(column, {}).get(horizon)
            if not stats:
                continue
            lines.append(
                f"| {column} | {horizon} | {stats['periods']} | "
                f"{_fmt(stats['mean'])} | {_fmt(stats['t_stat'])} | "
                f"{_fmt(stats['positive_share_percent'])}% |"
            )
    lines += [
        "",
        "## Mean forward excess return by score quantile (percentage points)",
        "",
    ]
    for column in COMPOSITE_COLUMNS:
        lines.append(f"### {column}")
        lines.append("")
        header = "| Horizon | " + " | ".join(
            f"Q{index + 1}" for index in range(config["quantiles"])
        ) + " | Spread |"
        lines.append(header)
        lines.append("|" + "---|" * (config["quantiles"] + 2))
        for horizon in config["forward_horizons_days"]:
            stats = aggregate["quantiles"].get(column, {}).get(horizon)
            if not stats:
                continue
            cells = " | ".join(_fmt(value) for value in stats["mean_excess"])
            lines.append(
                f"| {horizon} | {cells} | {_fmt(stats['spread'])} |"
            )
        lines.append("")
    lines += [
        "## Top-N selections",
        "",
        "| Signal | N | Horizon | Periods | Mean excess | Hit rate | Turnover |",
        "|---|---|---|---|---|---|---|",
    ]
    for column in COMPOSITE_COLUMNS:
        for top_n in config["top_n"]:
            for horizon in config["forward_horizons_days"]:
                stats = (
                    aggregate["top_n"].get(column, {}).get(str(top_n), {})
                    .get(horizon)
                )
                if not stats:
                    continue
                turnover = aggregate["turnover"].get(column, {}).get(str(top_n))
                lines.append(
                    f"| {column} | {top_n} | {horizon} | {stats['periods']} | "
                    f"{_fmt(stats['mean_excess'])} | "
                    f"{_fmt(stats['hit_rate_percent'])}% | "
                    f"{_fmt(turnover)}% |"
                )
    compounded = aggregate.get("compounded")
    if compounded:
        horizon = config["compounding_horizon"]
        lines += [
            "",
            f"## Compounded equal-weight top-N portfolios ({horizon} rebalance)",
            "",
            "| Signal | N | Periods | Portfolio | Matched benchmark | Difference |",
            "|---|---|---|---|---|---|",
        ]
        for column in COMPOSITE_COLUMNS:
            for top_n in config["top_n"]:
                stats = compounded.get(column, {}).get(str(top_n))
                if not stats:
                    continue
                lines.append(
                    f"| {column} | {top_n} | {stats['periods']} | "
                    f"{_fmt(stats['portfolio_return_percent'])}% | "
                    f"{_fmt(stats['benchmark_return_percent'])}% | "
                    f"{_fmt(stats['difference_percent'])}% |"
                )
    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in analysis["limitations"]]
    lines.append("")
    return "\n".join(lines)


def _period_metrics(frame: pd.DataFrame, config: dict) -> list[dict]:
    if frame.empty:
        return []
    metrics = []
    for period_label, group in frame.groupby("period", sort=True):
        entry = {"period": str(period_label), "observations": int(len(group))}
        for horizon in config["forward_horizons_days"]:
            entry[horizon] = _horizon_metrics(group, horizon, config)
        metrics.append(entry)
    return metrics


def _horizon_metrics(
    group: pd.DataFrame,
    horizon: str,
    config: dict,
) -> dict | None:
    excess_column = f"excess_{horizon}"
    valid = group.dropna(subset=[excess_column])
    if len(valid) < config["minimum_cross_section"]:
        return None
    result = {
        "observations": int(len(valid)),
        "mean_excess": _finite(valid[excess_column].mean()),
        "mean_return": _finite(valid[f"return_{horizon}"].mean()),
        "mean_benchmark_return": _finite(
            valid[f"benchmark_return_{horizon}"].mean()
        ),
        "information_coefficient": {},
        "quantiles": {},
        "top_n": {},
    }
    for column in IC_COLUMNS:
        pair = valid[[column, excess_column]].dropna()
        if len(pair) < config["minimum_cross_section"]:
            result["information_coefficient"][column] = None
            continue
        if pair[column].nunique() < 2:
            result["information_coefficient"][column] = None
            continue
        value = _spearman(pair[column], pair[excess_column])
        result["information_coefficient"][column] = _finite(value)

    for column in COMPOSITE_COLUMNS:
        ranked = _ranked(valid.dropna(subset=[column]), column)
        quantile_count = config["quantiles"]
        if len(ranked) >= quantile_count:
            # Quantiles follow the tie-broken ranking, so the best-ranked row
            # receives the highest order value and lands in the top quantile.
            order = pd.Series(
                np.arange(len(ranked), 0, -1),
                index=ranked.index,
                dtype=float,
            )
            labels = pd.qcut(order, quantile_count, labels=False)
            means = ranked.groupby(labels, observed=True)[excess_column].mean()
            mean_excess = [
                _finite(means.get(index)) for index in range(quantile_count)
            ]
            spread = (
                mean_excess[-1] - mean_excess[0]
                if mean_excess[-1] is not None and mean_excess[0] is not None
                else None
            )
            result["quantiles"][column] = {
                "mean_excess": mean_excess,
                "spread": spread,
            }
        top_entries = {}
        for top_n in config["top_n"]:
            if len(ranked) < top_n:
                continue
            top = ranked.head(top_n)
            top_entries[str(top_n)] = {
                "mean_excess": _finite(top[excess_column].mean()),
                "mean_return": _finite(top[f"return_{horizon}"].mean()),
                "mean_benchmark_return": _finite(
                    top[f"benchmark_return_{horizon}"].mean()
                ),
                "hit_rate_percent": _finite(
                    (top[excess_column] > 0).mean() * 100.0
                ),
                "tickers": top["ticker"].tolist(),
            }
        result["top_n"][column] = top_entries
    return result


def _spearman(left: pd.Series, right: pd.Series) -> float:
    """Spearman rank correlation as Pearson correlation of average ranks."""
    return float(
        left.rank(method="average").corr(right.rank(method="average"))
    )


def _aggregate_metrics(
    period_metrics: list[dict],
    frame: pd.DataFrame,
    config: dict,
) -> dict:
    horizons = list(config["forward_horizons_days"])
    ic_summary: dict[str, dict] = {}
    for column in IC_COLUMNS:
        ic_summary[column] = {}
        for horizon in horizons:
            values = [
                entry[horizon]["information_coefficient"].get(column)
                for entry in period_metrics
                if entry.get(horizon)
            ]
            values = [value for value in values if value is not None]
            ic_summary[column][horizon] = _series_summary(values)

    quantile_summary: dict[str, dict] = {}
    top_summary: dict[str, dict] = {}
    for column in COMPOSITE_COLUMNS:
        quantile_summary[column] = {}
        top_summary[column] = {}
        for horizon in horizons:
            rows = [
                entry[horizon]["quantiles"].get(column)
                for entry in period_metrics
                if entry.get(horizon)
            ]
            rows = [row for row in rows if row]
            if rows:
                columns = list(zip(*(row["mean_excess"] for row in rows)))
                quantile_summary[column][horizon] = {
                    "periods": len(rows),
                    "mean_excess": [
                        _mean([value for value in values if value is not None])
                        for values in columns
                    ],
                    "spread": _mean([
                        row["spread"] for row in rows
                        if row["spread"] is not None
                    ]),
                }
        for top_n in config["top_n"]:
            key = str(top_n)
            top_summary[column][key] = {}
            for horizon in horizons:
                rows = [
                    entry[horizon]["top_n"].get(column, {}).get(key)
                    for entry in period_metrics
                    if entry.get(horizon)
                ]
                rows = [row for row in rows if row]
                if not rows:
                    continue
                top_summary[column][key][horizon] = {
                    "periods": len(rows),
                    "mean_excess": _mean([
                        row["mean_excess"] for row in rows
                        if row["mean_excess"] is not None
                    ]),
                    "hit_rate_percent": _mean([
                        row["hit_rate_percent"] for row in rows
                        if row["hit_rate_percent"] is not None
                    ]),
                }

    return {
        "information_coefficient": ic_summary,
        "quantiles": quantile_summary,
        "top_n": top_summary,
        "turnover": _turnover(period_metrics, config),
        "compounded": _compounded(period_metrics, config),
    }


def _turnover(period_metrics: list[dict], config: dict) -> dict:
    horizon = config["compounding_horizon"]
    summary: dict[str, dict] = {}
    for column in COMPOSITE_COLUMNS:
        summary[column] = {}
        for top_n in config["top_n"]:
            key = str(top_n)
            previous: set[str] | None = None
            rates = []
            for entry in period_metrics:
                stats = (
                    (entry.get(horizon) or {}).get("top_n", {})
                    .get(column, {}).get(key)
                )
                if not stats:
                    continue
                current = set(stats["tickers"])
                if previous is not None and current:
                    rates.append(
                        (1.0 - len(previous & current) / len(current)) * 100.0
                    )
                previous = current
            summary[column][key] = _mean(rates)
    return summary


def _compounded(period_metrics: list[dict], config: dict) -> dict:
    horizon = config["compounding_horizon"]
    summary: dict[str, dict] = {}
    for column in COMPOSITE_COLUMNS:
        summary[column] = {}
        for top_n in config["top_n"]:
            key = str(top_n)
            portfolio = 1.0
            benchmark = 1.0
            periods = 0
            for entry in period_metrics:
                stats = (
                    (entry.get(horizon) or {}).get("top_n", {})
                    .get(column, {}).get(key)
                )
                if (
                    not stats
                    or stats["mean_return"] is None
                    or stats["mean_benchmark_return"] is None
                ):
                    continue
                portfolio *= 1.0 + stats["mean_return"] / 100.0
                benchmark *= 1.0 + stats["mean_benchmark_return"] / 100.0
                periods += 1
            if periods == 0:
                continue
            summary[column][key] = {
                "periods": periods,
                "portfolio_return_percent": round((portfolio - 1.0) * 100.0, 2),
                "benchmark_return_percent": round((benchmark - 1.0) * 100.0, 2),
                "difference_percent": round((portfolio - benchmark) * 100.0, 2),
            }
    return summary


def _apply_score_v2(frame: pd.DataFrame) -> pd.DataFrame:
    """Score each period's cross-section with the shadow v2 model."""
    frame = frame.copy()
    frame["score_v2"] = np.nan
    frame["score_v2_confidence"] = np.nan
    if frame.empty:
        return frame
    for _, group in frame.groupby("period", sort=True):
        scored = score_frame(group)
        frame.loc[group.index, "score_v2"] = scored["score_v2"]
        frame.loc[group.index, "score_v2_confidence"] = scored[
            "score_v2_confidence"
        ]
    return frame


def _rebalance_periods(
    calendars: dict[str, pd.DatetimeIndex],
) -> list[tuple[str, dict[str, pd.Timestamp]]]:
    """Return month-end trading dates per benchmark, dropping the open month."""
    by_label: dict[str, dict[str, pd.Timestamp]] = {}
    for ticker, calendar in calendars.items():
        if len(calendar) == 0:
            continue
        series = pd.Series(calendar, index=calendar)
        month_ends = series.groupby(calendar.to_period("M")).max()
        last_label = str(calendar[-1].to_period("M"))
        for period, date in month_ends.items():
            label = str(period)
            if label == last_label:
                continue
            by_label.setdefault(label, {})[ticker] = pd.Timestamp(date)
    return [(label, by_label[label]) for label in sorted(by_label)]


def _normalize_history(frame: pd.DataFrame) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None
    if "Date" in frame.columns:
        dates = frame["Date"]
    elif isinstance(frame.index, pd.DatetimeIndex):
        dates = frame.index.to_series()
    else:
        return None
    if "Close" not in frame.columns:
        return None
    normalized = pd.DataFrame({
        "Close": pd.to_numeric(frame["Close"].to_numpy(), errors="coerce"),
        "Volume": pd.to_numeric(
            frame["Volume"].to_numpy() if "Volume" in frame.columns
            else np.zeros(len(frame)),
            errors="coerce",
        ),
    })
    parsed = pd.to_datetime(dates.to_numpy(), errors="coerce", utc=True)
    normalized.index = pd.DatetimeIndex(parsed.tz_localize(None)).normalize()
    normalized = normalized[~normalized.index.isna()]
    normalized = normalized.dropna(subset=["Close"])
    normalized = normalized[normalized["Close"] > 0]
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    normalized = normalized.sort_index()
    normalized["Volume"] = normalized["Volume"].fillna(0.0)
    if normalized.empty:
        return None
    return normalized


def _ranked(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    return frame.sort_values(
        [column, "relative_strength_6m", "ticker"],
        ascending=[False, False, True],
        na_position="last",
        kind="mergesort",
    )


def _flatten_periods(period_metrics: list[dict]) -> list[dict]:
    rows = []
    for entry in period_metrics:
        for horizon, stats in entry.items():
            if horizon in {"period", "observations"} or not stats:
                continue
            row = {
                "period": entry["period"],
                "horizon": horizon,
                "observations": stats["observations"],
                "mean_excess": stats["mean_excess"],
                "mean_benchmark_return": stats["mean_benchmark_return"],
            }
            for column, value in stats["information_coefficient"].items():
                row[f"ic_{column}"] = value
            for column, quantiles in stats["quantiles"].items():
                row[f"spread_{column}"] = quantiles["spread"]
            for column, tops in stats["top_n"].items():
                for top_n, top in tops.items():
                    row[f"top{top_n}_excess_{column}"] = top["mean_excess"]
                    row[f"top{top_n}_hit_rate_{column}"] = top["hit_rate_percent"]
            rows.append(row)
    return rows


def _validated_config(config: dict | None) -> dict:
    merged = {**BACKTEST_CONFIG, **(config or {})}
    merged.setdefault("model_version", "v0.1-technical-point-in-time")
    merged.setdefault("price_history_period", "10y")
    merged.setdefault("source_statuses", ["OK", "FAILED"])
    merged.setdefault("maximum_stale_trading_days", 5)
    merged.setdefault(
        "forward_horizons_days",
        {"1M": 21, "3M": 63, "6M": 126, "1Y": 252},
    )
    merged.setdefault("compounding_horizon", "1M")
    merged.setdefault("top_n", [10, 25, 50, 100])
    merged.setdefault("quantiles", 5)
    merged.setdefault("minimum_cross_section", 50)
    merged.setdefault(
        "minimum_history_days",
        int(SCORING_CONFIG.get("minimum_price_history_days", 200)),
    )

    horizons = merged["forward_horizons_days"]
    if not isinstance(horizons, dict) or not horizons:
        raise ValueError("Backtest forward_horizons_days must be a non-empty mapping.")
    for label, days in horizons.items():
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ValueError(f"Backtest horizon {label!r} must be a positive integer.")
    if merged["compounding_horizon"] not in horizons:
        raise ValueError("Backtest compounding_horizon must be a configured horizon.")
    for name in ("quantiles", "minimum_cross_section", "minimum_history_days"):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ValueError(f"Backtest {name} must be an integer of at least 2.")
    stale = merged["maximum_stale_trading_days"]
    if isinstance(stale, bool) or not isinstance(stale, int) or stale < 0:
        raise ValueError("Backtest maximum_stale_trading_days cannot be negative.")
    top_n = merged["top_n"]
    if (
        not isinstance(top_n, list)
        or not top_n
        or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in top_n)
    ):
        raise ValueError("Backtest top_n must be a non-empty list of positive integers.")
    merged["top_n"] = sorted(set(top_n))
    merged["source_statuses"] = [str(value) for value in merged["source_statuses"]]
    return merged


def _series_summary(values: list[float]) -> dict | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    mean = float(array.mean())
    std = float(array.std(ddof=1)) if len(array) > 1 else None
    t_stat = (
        mean / std * math.sqrt(len(array))
        if std not in (None, 0.0)
        else None
    )
    return {
        "periods": int(len(array)),
        "mean": round(mean, 4),
        "std": round(std, 4) if std is not None else None,
        "t_stat": round(t_stat, 2) if t_stat is not None else None,
        "positive_share_percent": round(float((array > 0).mean() * 100.0), 2),
    }


def _mean(values: list[float]) -> float | None:
    values = [value for value in values if value is not None]
    return round(float(np.mean(values)), 4) if values else None


def _counts(frame: pd.DataFrame, column: str) -> dict:
    if frame.empty or column not in frame:
        return {}
    grouped = frame.drop_duplicates("ticker")[column].value_counts()
    return {str(key): int(value) for key, value in sorted(grouped.items())}


def _positive(value: object) -> bool:
    number = _number(value)
    return number is not None and number > 0


def _finite(value: object) -> float | None:
    number = _number(value)
    return round(number, 4) if number is not None else None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: object) -> str:
    number = _number(value)
    return f"{number:.2f}" if number is not None else "n/a"


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = _temporary_path(path, ".json")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True, default=_json_default)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, text: str) -> None:
    temporary = _temporary_path(path, path.suffix)
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_csv(path: Path, rows: list[dict]) -> None:
    temporary = _temporary_path(path, ".csv")
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_observations(path: Path, frame: pd.DataFrame) -> None:
    temporary = _temporary_path(path, ".csv.gz")
    try:
        frame.to_csv(temporary, index=False, compression="gzip")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_path(path: Path, suffix: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=suffix, dir=path.parent,
    )
    os.close(descriptor)
    return Path(name)


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
