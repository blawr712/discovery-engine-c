"""Resolve frozen Moonshot forward baselines against realized prices.

A forward baseline records what the model claimed about a cohort on a fixed
date. This module reads one back, measures each candidate's benchmark-relative
return over every horizon that has since become eligible, and compares the
selected cohorts with the control cohorts.

Two rules protect the result from flattering itself. Both prices for a return
come from the same provider series, so a split cannot silently inflate it. And
candidates whose prices stop before a horizon are reported as unresolved
rather than dropped, because a vanished price is usually a delisting, which is
an outcome rather than missing data.

One baseline resolved at one date is a single observation, not a backtest.
"""

from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile

import pandas as pd


DEFAULT_CONFIG = {
    "maximum_price_staleness_days": 10,
    "minimum_cohort_size": 5,
    "selected_classifications": [
        "priority_research", "asymmetric_watch", "speculative_watch",
    ],
    "control_classifications": ["low_upside_signal", "insufficient_evidence"],
    # Frozen scores compared head to head on each resolved horizon. A rank
    # correlation over a handful of names is noise, so comparisons need more
    # resolved names than a cohort summary does.
    "compared_scores": ["score_v3", "score_v3_challenger"],
    "minimum_comparison_size": 50,
}

LIMITATIONS = (
    "One baseline resolved on one date is a single observation, not a "
    "backtest: it cannot separate model skill from the market regime.",
    "Candidates whose price series stops before a horizon are reported as "
    "unresolved, not excluded. Treating them as missing data would bias "
    "results upward, because a vanished price is usually a delisting.",
    "Returns use adjusted closes in each listing's local currency and ignore "
    "transaction costs, spreads, and slippage.",
    "Both prices for a return come from the same provider series, so a "
    "series refetched later may revise an earlier resolution.",
    "This measures what happened after the cohort was frozen. It is not a "
    "return forecast or an investment recommendation.",
)


BASELINE_KINDS = {
    "moonshot": ("moonshot_forward_baseline", "--calibrate-moonshot"),
    "research": ("research_forward_baseline", "--freeze-research-baseline"),
}


def available_baseline_kinds(output_directory: Path, run_id: str) -> list[str]:
    """Kinds of frozen baseline exported for a run, in a stable order."""
    output_directory = Path(output_directory)
    return [
        kind for kind, (prefix, _) in sorted(BASELINE_KINDS.items())
        if any(output_directory.glob(f"{prefix}_{run_id}_*.json"))
    ]


def load_forward_baseline(
    output_directory: Path,
    run_id: str,
    kind: str = "moonshot",
) -> dict:
    """Load one kind of frozen forward baseline exported for a run."""
    if kind not in BASELINE_KINDS:
        raise ValueError(f"Unknown baseline kind: {kind}")
    prefix, command = BASELINE_KINDS[kind]
    output_directory = Path(output_directory)
    matches = sorted(output_directory.glob(f"{prefix}_{run_id}_*.json"))
    if not matches:
        raise FileNotFoundError(
            f"No {kind} forward baseline is exported for run {run_id}. "
            f"Run {command} first."
        )
    with matches[-1].open("r", encoding="utf-8") as file:
        baseline = json.load(file)
    if not isinstance(baseline, dict) or not baseline.get("candidates"):
        raise ValueError(f"Forward baseline for {run_id} is empty or unreadable.")
    return baseline


def load_price_series(
    cache_directory: Path,
    tickers: list[str],
) -> tuple[dict[str, list[dict]], dict]:
    """Read cached one-year price series directly, ignoring cache freshness.

    Historical closes do not expire, so the normal TTL does not apply here.
    Each ticker yields one series; returns are only ever computed within it.
    """
    cache_directory = Path(cache_directory)
    series: dict[str, list[dict]] = {}
    stats = {"loaded": 0, "missing": 0, "read_errors": 0}
    for ticker in dict.fromkeys(tickers):
        digest = hashlib.sha256(f"{ticker}|1y".encode("utf-8")).hexdigest()
        path = cache_directory / "prices" / f"{digest}.json"
        if not path.is_file():
            stats["missing"] += 1
            continue
        try:
            frame = pd.read_json(path, orient="table")
            rows = _series_rows(frame)
        except (OSError, ValueError, TypeError, KeyError):
            stats["read_errors"] += 1
            continue
        if not rows:
            stats["read_errors"] += 1
            continue
        series[str(ticker)] = rows
        stats["loaded"] += 1
    return series, stats


def resolve_baseline(
    baseline: dict,
    series: dict[str, list[dict]],
    as_of: datetime | date | None = None,
    config: dict | None = None,
) -> dict:
    """Measure every eligible horizon of a frozen baseline."""
    config = validated_config(_cohorts_from(baseline, config))
    as_of_date = _as_date(as_of or datetime.now(timezone.utc))
    baseline_date = _as_date(_parse_time(baseline["baseline_as_of"]))
    tolerance = int(config["maximum_price_staleness_days"])

    candidates = []
    for row in baseline["candidates"]:
        ticker = str(row["ticker"])
        benchmark = str(row.get("benchmark") or "")
        horizons = {}
        for label, horizon in sorted((row.get("horizons") or {}).items()):
            eligible = _as_date(_parse_time(horizon["eligible_at"]))
            if eligible > as_of_date:
                horizons[label] = {
                    "status": "pending",
                    "eligible_at": horizon["eligible_at"],
                }
                continue
            horizons[label] = _resolve_horizon(
                series.get(ticker),
                series.get(benchmark),
                baseline_date,
                eligible,
                tolerance,
                horizon["eligible_at"],
            )
        candidates.append({
            "ticker": ticker,
            "company_name": row.get("company_name"),
            "country": row.get("country"),
            "sector": row.get("sector"),
            "size_tier": row.get("size_tier"),
            "classification": row.get("classification"),
            "screen_status": row.get("screen_status"),
            "moonshot_rank": row.get("moonshot_rank"),
            "upside_score": row.get("upside_score"),
            "risk_of_ruin_score": row.get("risk_of_ruin_score"),
            "scores": _frozen_scores(row),
            "benchmark": benchmark,
            "horizons": horizons,
        })

    eligible_labels = sorted({
        label
        for candidate in candidates
        for label, horizon in candidate["horizons"].items()
        if horizon["status"] != "pending"
    })
    return {
        "run_id": baseline.get("run_id"),
        "kind": baseline.get("kind", "moonshot"),
        "baseline_id": baseline.get("baseline_id"),
        "baseline_as_of": baseline.get("baseline_as_of"),
        "model_version": baseline.get("model_version"),
        "resolved_at": as_of_date.isoformat(),
        "config": config,
        "candidate_count": len(candidates),
        "eligible_horizons": eligible_labels,
        "horizons": {
            label: _horizon_report(candidates, label, config)
            for label in eligible_labels
        },
        "pending_horizons": sorted({
            label
            for candidate in candidates
            for label, horizon in candidate["horizons"].items()
            if horizon["status"] == "pending"
        }),
        "limitations": list(LIMITATIONS),
        "official_scores_and_ranks_unchanged": True,
        "candidates": candidates,
    }


def export_baseline_resolution(
    resolution: dict,
    output_directory: Path,
) -> dict[str, str]:
    """Write the resolution as JSON plus a flat per-horizon CSV."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_id = resolution["run_id"]
    stem = f"baseline_resolution_{resolution.get('kind', 'moonshot')}_{run_id}"
    json_path = output_directory / f"{stem}.json"
    csv_path = output_directory / f"{stem}.csv"
    _atomic_json(json_path, resolution)
    _atomic_csv(csv_path, _flatten(resolution))
    return {
        "baseline_resolution_json_path": str(json_path),
        "baseline_resolution_csv_path": str(csv_path),
    }


def validated_config(config: dict | None = None) -> dict:
    """Merge resolution settings over defaults and validate them."""
    merged = {**DEFAULT_CONFIG, **(config or {})}
    for name in (
        "maximum_price_staleness_days", "minimum_cohort_size", "minimum_comparison_size",
    ):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Baseline resolution {name} must be a positive integer.")
    for name in ("selected_classifications", "control_classifications"):
        value = merged[name]
        if not isinstance(value, list) or not value:
            raise ValueError(f"Baseline resolution {name} must be a non-empty list.")
        merged[name] = [str(item) for item in value]
    if not isinstance(merged["compared_scores"], list):
        raise ValueError("Baseline resolution compared_scores must be a list.")
    merged["compared_scores"] = [str(item) for item in merged["compared_scores"]]
    overlap = set(merged["selected_classifications"]) & set(
        merged["control_classifications"]
    )
    if overlap:
        raise ValueError(
            f"Classifications cannot be both selected and control: {sorted(overlap)}"
        )
    return merged


def _resolve_horizon(
    ticker_series: list[dict] | None,
    benchmark_series: list[dict] | None,
    baseline_date: date,
    eligible_date: date,
    tolerance: int,
    eligible_at: str,
) -> dict:
    """Measure one horizon, or explain precisely why it cannot be measured."""
    result = {"status": "unresolved", "eligible_at": eligible_at}
    if not ticker_series:
        result["reason"] = "no cached price series"
        return result

    anchor = _close_on_or_before(ticker_series, baseline_date, tolerance)
    if anchor is None:
        result["reason"] = "no price within tolerance of the baseline date"
        return result
    final = _close_on_or_before(ticker_series, eligible_date, tolerance)
    if final is None:
        last = ticker_series[-1]
        result["reason"] = (
            f"price series stops at {last['date']}, more than {tolerance} days "
            "before the horizon; the listing may have been delisted or halted"
        )
        result["last_price_date"] = last["date"]
        result["status"] = "unresolved_stale"
        return result

    ticker_return = (final["close"] / anchor["close"] - 1.0) * 100.0
    result.update({
        "status": "resolved",
        "anchor_date": anchor["date"],
        "anchor_close": round(anchor["close"], 6),
        "final_date": final["date"],
        "final_close": round(final["close"], 6),
        "ticker_return_percent": round(ticker_return, 4),
    })

    benchmark_anchor = (
        _close_on_or_before(benchmark_series, baseline_date, tolerance)
        if benchmark_series else None
    )
    benchmark_final = (
        _close_on_or_before(benchmark_series, eligible_date, tolerance)
        if benchmark_series else None
    )
    if benchmark_anchor is None or benchmark_final is None:
        result["status"] = "resolved_without_benchmark"
        result["benchmark_note"] = (
            "benchmark series does not cover this window; refresh prices with "
            "--collect-prices to compute the excess return"
        )
        return result
    benchmark_return = (
        benchmark_final["close"] / benchmark_anchor["close"] - 1.0
    ) * 100.0
    result["benchmark_return_percent"] = round(benchmark_return, 4)
    result["excess_return_percent"] = round(ticker_return - benchmark_return, 4)
    return result


def _cohorts_from(baseline: dict, config: dict | None) -> dict:
    """Prefer the cohort split a baseline declares over the global default."""
    merged = dict(config or {})
    for field in ("selected_classifications", "control_classifications"):
        declared = baseline.get(field)
        if declared:
            merged[field] = list(declared)
    return merged


def tickers_needing_prices(
    baseline: dict,
    series: dict[str, list[dict]],
    as_of: datetime | date | None = None,
    config: dict | None = None,
) -> list[str]:
    """Tickers and benchmarks whose cached prices stop short of a horizon.

    Lets a refresh fetch only what is actually missing instead of the whole
    cohort, which is usually just the benchmarks.
    """
    config = validated_config(config)
    as_of_date = _as_date(as_of or datetime.now(timezone.utc))
    tolerance = int(config["maximum_price_staleness_days"])
    needed: list[str] = []
    latest_eligible: date | None = None

    for row in baseline["candidates"]:
        eligible_dates = [
            _as_date(_parse_time(horizon["eligible_at"]))
            for horizon in (row.get("horizons") or {}).values()
        ]
        eligible_dates = [day for day in eligible_dates if day <= as_of_date]
        if not eligible_dates:
            continue
        target = max(eligible_dates)
        latest_eligible = (
            target if latest_eligible is None else max(latest_eligible, target)
        )
        if _covers(series.get(str(row["ticker"])), target, tolerance):
            continue
        needed.append(str(row["ticker"]))

    if latest_eligible is not None:
        for benchmark in sorted({
            str(row.get("benchmark") or "") for row in baseline["candidates"]
            if row.get("benchmark")
        }):
            if not _covers(series.get(benchmark), latest_eligible, tolerance):
                needed.append(benchmark)
    return list(dict.fromkeys(needed))


def _covers(series: list[dict] | None, target: date, tolerance: int) -> bool:
    """True when a series has a close close enough to ``target`` to use."""
    return _close_on_or_before(series, target, tolerance) is not None


def _horizon_report(candidates: list[dict], label: str, config: dict) -> dict:
    """Summarize one horizon by cohort, keeping unresolved names visible."""
    cohorts: dict[str, list[dict]] = {}
    for candidate in candidates:
        horizon = candidate["horizons"].get(label)
        if horizon is None or horizon["status"] == "pending":
            continue
        cohorts.setdefault(str(candidate["classification"]), []).append(horizon)

    by_classification = {
        name: _cohort_summary(rows, config)
        for name, rows in sorted(cohorts.items())
    }
    selected = [
        row for name in config["selected_classifications"]
        for row in cohorts.get(name, [])
    ]
    control = [
        row for name in config["control_classifications"]
        for row in cohorts.get(name, [])
    ]
    selected_summary = _cohort_summary(selected, config)
    control_summary = _cohort_summary(control, config)
    report = {
        "by_classification": by_classification,
        "selected": selected_summary,
        "control": control_summary,
        "selected_minus_control": _difference(selected_summary, control_summary),
        "comparable": bool(
            selected_summary["resolved"] >= config["minimum_cohort_size"]
            and control_summary["resolved"] >= config["minimum_cohort_size"]
        ),
    }
    comparison = _model_comparison(candidates, label, config)
    if comparison:
        report["model_comparison"] = comparison
    screen = _screen_comparison(candidates, label, config)
    if screen:
        report["screen_comparison"] = screen
    return report


SCREEN_SCORES = ("score_defensive", "score_v3")


def _screen_comparison(candidates: list[dict], label: str, config: dict) -> dict:
    """Inside the screen: the defensive ordering against v3's own order.

    Only names that passed the screen when the cohort froze are used, and
    only those that carry a frozen defensive score, so both orderings are
    measured on the same names. One cohort is one observation; the verdict
    comes from pooling many monthly cohorts, not from any single one.
    """
    rows = []
    for candidate in candidates:
        if candidate.get("screen_status") != "passes":
            continue
        scores = candidate.get("scores") or {}
        horizon = candidate["horizons"].get(label) or {}
        if "score_defensive" not in scores or "score_v3" not in scores:
            continue
        if horizon.get("status") != "resolved" or "excess_return_percent" not in horizon:
            continue
        rows.append((scores, horizon["excess_return_percent"], str(candidate.get("ticker"))))
    if not rows:
        return {}
    minimum = int(config["minimum_comparison_size"])
    enough = len(rows) >= minimum
    coefficients = {
        name: (_spearman([scores[name] for scores, _, _ in rows], [e for _, e, _ in rows])
               if enough else None)
        for name in SCREEN_SCORES
    }
    difference = (
        round(coefficients["score_defensive"] - coefficients["score_v3"], 4)
        if None not in coefficients.values() else None
    )
    terciles = []
    if enough:
        ordered = sorted(rows, key=lambda row: (row[0]["score_defensive"], row[2]))
        buckets: list[list[float]] = [[], [], []]
        for index, (_, excess, _) in enumerate(ordered):
            buckets[min(2, index * 3 // len(ordered))].append(excess)
        for number, values in enumerate(buckets, start=1):
            terciles.append({
                "tercile": number,
                "resolved": len(values),
                "median_excess_percent": round(statistics.median(values), 2) if values else None,
                "win_rate_percent": (
                    round(sum(1 for v in values if v > 0) / len(values) * 100.0, 2)
                    if values else None
                ),
            })
    spread = (
        round(terciles[2]["median_excess_percent"] - terciles[0]["median_excess_percent"], 2)
        if terciles and None not in (terciles[0]["median_excess_percent"], terciles[2]["median_excess_percent"])
        else None
    )
    return {
        "resolved": len(rows),
        "information_coefficient": coefficients,
        "defensive_minus_v3_information_coefficient": difference,
        "defensive_terciles": terciles,
        "most_minus_least_defensive_median_percent": spread,
    }


def _frozen_scores(row: dict) -> dict:
    """Every numeric model score frozen on a baseline candidate."""
    scores = {}
    for name, value in row.items():
        if not name.startswith("score_") or name.endswith(
            ("_confidence", "_rank", "_excluded", "_breakdown", "_reasons", "_version")
        ):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value == value:
            scores[name] = float(value)
    return scores


def _model_comparison(candidates: list[dict], label: str, config: dict) -> dict:
    """How well each frozen score ranked the realized outcomes of one horizon.

    Each model is measured on the names it scored, and the models are also
    compared on the names every one of them scored, so a difference in
    coverage cannot pass for a difference in skill. Only scores frozen at the
    baseline date are used, so nothing here could have seen the outcome.
    """
    names = [name for name in config["compared_scores"]]
    rows = []
    for candidate in candidates:
        horizon = candidate["horizons"].get(label) or {}
        if horizon.get("status") != "resolved" or "excess_return_percent" not in horizon:
            continue
        rows.append((candidate.get("scores") or {}, horizon["excess_return_percent"]))
    present = [name for name in names if any(name in scores for scores, _ in rows)]
    if not present:
        return {}
    minimum = int(config["minimum_comparison_size"])
    comparison = {}
    for name in present:
        pairs = [(scores[name], excess) for scores, excess in rows if name in scores]
        comparison[name] = {
            "resolved": len(pairs),
            "information_coefficient": (
                _spearman([a for a, _ in pairs], [b for _, b in pairs])
                if len(pairs) >= minimum else None
            ),
            "quintiles": _score_quintiles(pairs, config) if len(pairs) >= minimum else [],
        }
    if len(present) > 1:
        common = [
            (scores, excess) for scores, excess in rows
            if all(name in scores for name in present)
        ]
        coefficients = {
            name: (
                _spearman([scores[name] for scores, _ in common], [e for _, e in common])
                if len(common) >= minimum else None
            )
            for name in present
        }
        comparison["common"] = {
            "resolved": len(common),
            "information_coefficient": coefficients,
        }
    return comparison


def _score_quintiles(pairs: list[tuple[float, float]], config: dict) -> list[dict]:
    """Outcome ladder by score quintile, quintile 5 the highest scores."""
    ordered = sorted(pairs, key=lambda pair: pair[0])
    count = len(ordered)
    buckets: list[list[float]] = [[] for _ in range(5)]
    for index, (_, excess) in enumerate(ordered):
        buckets[min(4, index * 5 // count)].append(excess)
    ladder = []
    for number, values in enumerate(buckets, start=1):
        ladder.append({
            "quintile": number,
            "resolved": len(values),
            "median_excess_percent": round(statistics.median(values), 2) if values else None,
            "win_rate_percent": (
                round(sum(1 for v in values if v > 0) / len(values) * 100.0, 2)
                if values else None
            ),
        })
    return ladder


def _spearman(left: list[float], right: list[float]) -> float | None:
    """Rank correlation as the Pearson correlation of average ranks."""
    if len(left) < 3:
        return None
    a, b = _average_ranks(left), _average_ranks(right)
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    spread = (
        sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b)
    ) ** 0.5
    return round(covariance / spread, 4) if spread else None


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        rank = (position + end) / 2.0 + 1.0
        for index in order[position:end + 1]:
            ranks[index] = rank
        position = end + 1
    return ranks


def _cohort_summary(rows: list[dict], config: dict) -> dict:
    """Describe one cohort, counting unresolved names alongside outcomes."""
    excess = [
        row["excess_return_percent"] for row in rows
        if row["status"] == "resolved" and "excess_return_percent" in row
    ]
    unresolved = [row for row in rows if row["status"] != "resolved"]
    stale = [row for row in unresolved if row["status"] == "unresolved_stale"]
    no_benchmark = [
        row for row in rows if row["status"] == "resolved_without_benchmark"
    ]
    summary = {
        "candidates": len(rows),
        "resolved": len(excess),
        "unresolved": len(unresolved),
        "unresolved_stale": len(stale),
        "resolved_without_benchmark": len(no_benchmark),
        "resolution_rate_percent": (
            round(len(excess) / len(rows) * 100.0, 2) if rows else None
        ),
        "median_excess_percent": None,
        "mean_excess_percent": None,
        "trimmed_mean_excess_percent": None,
        "win_rate_percent": None,
    }
    if not excess:
        return summary
    summary["median_excess_percent"] = round(statistics.median(excess), 2)
    summary["mean_excess_percent"] = round(statistics.fmean(excess), 2)
    summary["trimmed_mean_excess_percent"] = round(_trimmed_mean(excess), 2)
    summary["win_rate_percent"] = round(
        sum(1 for value in excess if value > 0) / len(excess) * 100.0, 2
    )
    return summary


def _difference(selected: dict, control: dict) -> dict:
    """Selected minus control for each statistic both cohorts produced."""
    fields = (
        "median_excess_percent", "mean_excess_percent",
        "trimmed_mean_excess_percent", "win_rate_percent",
    )
    difference = {}
    for field in fields:
        left, right = selected.get(field), control.get(field)
        difference[field] = (
            round(left - right, 2)
            if left is not None and right is not None else None
        )
    return difference


def _close_on_or_before(
    series: list[dict] | None,
    target: date,
    tolerance: int,
) -> dict | None:
    """Last close on or before ``target``, if it is recent enough to count."""
    if not series:
        return None
    dates = [row["date"] for row in series]
    position = bisect_right(dates, target.isoformat())
    if position == 0:
        return None
    row = series[position - 1]
    if (target - _parse_date(row["date"])).days > tolerance:
        return None
    return row


def _series_rows(frame: pd.DataFrame) -> list[dict]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    if "Date" not in frame.columns or "Close" not in frame.columns:
        return []
    rows = []
    for record in frame.to_dict("records"):
        close = record.get("Close")
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        day = _date_text(record.get("Date"))
        if day is None:
            continue
        rows.append({"date": day, "close": close})
    rows.sort(key=lambda row: row["date"])
    return rows


def _flatten(resolution: dict) -> list[dict]:
    rows = []
    for candidate in resolution["candidates"]:
        for label, horizon in sorted(candidate["horizons"].items()):
            if horizon["status"] == "pending":
                continue
            rows.append({
                "run_id": resolution["run_id"],
                "kind": resolution.get("kind", "moonshot"),
                "baseline_as_of": resolution["baseline_as_of"],
                "resolved_at": resolution["resolved_at"],
                "horizon": label,
                "ticker": candidate["ticker"],
                "company_name": candidate["company_name"],
                "country": candidate["country"],
                "sector": candidate["sector"],
                "size_tier": candidate["size_tier"],
                "classification": candidate["classification"],
                "moonshot_rank": candidate["moonshot_rank"],
                "upside_score": candidate["upside_score"],
                "risk_of_ruin_score": candidate["risk_of_ruin_score"],
                "benchmark": candidate["benchmark"],
                "status": horizon["status"],
                "anchor_date": horizon.get("anchor_date"),
                "final_date": horizon.get("final_date"),
                "ticker_return_percent": horizon.get("ticker_return_percent"),
                "benchmark_return_percent": horizon.get("benchmark_return_percent"),
                "excess_return_percent": horizon.get("excess_return_percent"),
                "reason": horizon.get("reason"),
            })
    return rows


def _trimmed_mean(values: list[float], tail: float = 0.02) -> float:
    if len(values) < 3:
        return statistics.fmean(values)
    ordered = sorted(values)
    cut = max(1, int(len(ordered) * tail))
    trimmed = ordered[cut:-cut] or ordered
    return statistics.fmean(trimmed)


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _date_text(value: object) -> str | None:
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = _temporary(path)
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True, default=str)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_csv(path: Path, rows: list[dict]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    temporary = _temporary(path)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=path.suffix, dir=path.parent,
    )
    os.close(descriptor)
    return Path(name)
