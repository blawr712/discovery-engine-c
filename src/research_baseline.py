"""Freeze a run's research queue as a forward-test cohort.

Score v3's weights were chosen from a backtest, which makes every in-sample
result suspect. This module freezes the queue a completed run produced,
labelled into cohorts, so later price history can answer the only question
that matters: did the ordering work on data the weights never saw?

The cohorts are carved from the v3-scored population so the comparison is
like for like. The top of the queue and the top quintile are the selected
side; the bottom quintile and the names v3's filters excluded are the
control side. Excluded names are deliberately kept, because a filter that
works should leave them behind.

Baselines are immutable once written. Resolution is a separate step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from src.config import BENCHMARKS, RESEARCH_BASELINE_CONFIG


DEFAULT_CONFIG = {
    "model_version": "v3.0-shadow-composite",
    "horizon_days": {"1M": 30, "3M": 91, "6M": 182, "1Y": 365},
    "top_n": 25,
    "quantiles": 5,
}

SELECTED_CLASSIFICATIONS = ("queue_top_n", "v3_quintile_5")
CONTROL_CLASSIFICATIONS = ("v3_quintile_1", "v3_excluded")

INTERPRETATION_WARNING = (
    "This immutable cohort supports future observation of Score v3 on data "
    "its weights never saw. It is not a backtest, forecast, or investment "
    "recommendation."
)


def build_research_baseline(
    queue: list[dict],
    run_id: str,
    baseline_as_of: str,
    config: dict | None = None,
) -> dict:
    """Freeze a research queue into labelled cohorts with fixed horizons."""
    config = validated_config(config)
    anchor = _parse_time(baseline_as_of)
    scored = [
        row for row in queue
        if str(row.get("ranking_basis")) == "score_v3"
        and _number(row.get("score_v3")) is not None
    ]
    scored.sort(
        key=lambda row: (-_number(row["score_v3"]), str(row.get("ticker", "")))
    )
    classifications = _classify(scored, queue, config)

    candidates = []
    for row in queue:
        ticker = str(row.get("ticker", ""))
        classification = classifications.get(ticker)
        if classification is None:
            continue
        candidates.append({
            "ticker": ticker,
            "company_name": row.get("company_name"),
            "country": row.get("country"),
            "sector": row.get("sector"),
            "classification": classification,
            "research_rank": row.get("research_rank"),
            "ranking_basis": row.get("ranking_basis"),
            "score_v3": _number(row.get("score_v3")),
            "score_v3_confidence": _number(row.get("score_v3_confidence")),
            "score_v3_excluded": bool(row.get("score_v3_excluded")),
            "score_v2": _number(row.get("score_v2")),
            "discovery_rank": row.get("discovery_rank"),
            "benchmark": BENCHMARKS.get(str(row.get("country") or ""), "SPY"),
            "baseline_as_of": baseline_as_of,
            "tracking_status": "pending",
            "horizons": {
                label: {
                    "eligible_at": (anchor + timedelta(days=days)).isoformat(),
                    "status": "pending",
                }
                for label, days in sorted(config["horizon_days"].items())
            },
        })
    candidates.sort(key=lambda row: (
        row["research_rank"] is None, row["research_rank"], row["ticker"],
    ))

    baseline = {
        "schema_version": 1,
        "kind": "research",
        "run_id": run_id,
        "model_version": config["model_version"],
        "baseline_as_of": baseline_as_of,
        "candidate_count": len(candidates),
        "cohort_counts": _counts(candidates),
        "selected_classifications": list(SELECTED_CLASSIFICATIONS),
        "control_classifications": list(CONTROL_CLASSIFICATIONS),
        "config": config,
        "interpretation_warning": INTERPRETATION_WARNING,
        "official_scores_and_ranks_unchanged": True,
        "candidates": candidates,
    }
    baseline["baseline_id"] = _baseline_id(baseline)
    return baseline


def export_research_baseline(
    baseline: dict,
    output_directory: Path,
) -> dict[str, str]:
    """Write the frozen cohort as JSON plus a flat CSV. Never overwrite."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"research_forward_baseline_{baseline['run_id']}_"
        f"{baseline['model_version']}"
    )
    json_path = output_directory / f"{stem}.json"
    csv_path = output_directory / f"{stem}.csv"
    if json_path.exists():
        raise FileExistsError(
            f"A research baseline already exists for this run and model: "
            f"{json_path.name}. Baselines are immutable; delete it "
            "deliberately if it must be rebuilt."
        )
    _atomic_json(json_path, baseline)
    _atomic_csv(csv_path, [
        {key: value for key, value in candidate.items() if key != "horizons"}
        | {
            f"eligible_{label}": horizon["eligible_at"]
            for label, horizon in sorted(candidate["horizons"].items())
        }
        for candidate in baseline["candidates"]
    ])
    return {
        "research_baseline_json_path": str(json_path),
        "research_baseline_csv_path": str(csv_path),
    }


def validated_config(config: dict | None = None) -> dict:
    """Merge research-baseline settings over defaults and validate them."""
    source = config if config is not None else RESEARCH_BASELINE_CONFIG
    if "horizon_days" in source and not source["horizon_days"]:
        raise ValueError("Research baseline horizon_days must not be empty.")
    # Horizons are replaced wholesale, never merged: a configured set of
    # horizons is the complete set, so defaults cannot creep back in.
    merged = {
        **DEFAULT_CONFIG,
        **source,
        "horizon_days": dict(
            source.get("horizon_days") or DEFAULT_CONFIG["horizon_days"]
        ),
    }
    horizons = merged["horizon_days"]
    for label, days in horizons.items():
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ValueError(
                f"Research baseline horizon {label!r} must be a positive integer."
            )
    for name in ("top_n", "quantiles"):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Research baseline {name} must be a positive integer.")
    merged["model_version"] = str(merged["model_version"])
    return merged


def _classify(
    scored: list[dict],
    queue: list[dict],
    config: dict,
) -> dict[str, str]:
    """Label every queue row with exactly one cohort, or leave it out."""
    labels: dict[str, str] = {}
    top_n = int(config["top_n"])
    quantiles = int(config["quantiles"])
    total = len(scored)

    for index, row in enumerate(scored):
        ticker = str(row.get("ticker", ""))
        if index < top_n:
            labels[ticker] = "queue_top_n"
            continue
        # Quintiles run over the whole v3-scored population so the bands stay
        # comparable; the carved-out top names simply vacate their places.
        bucket = min(quantiles - 1, index * quantiles // total) if total else 0
        labels[ticker] = f"v3_quintile_{quantiles - bucket}"

    for row in queue:
        ticker = str(row.get("ticker", ""))
        if ticker in labels:
            continue
        if row.get("score_v3_excluded"):
            labels[ticker] = "v3_excluded"
        elif str(row.get("ranking_basis")) == "score_v2":
            labels[ticker] = "v2_fallback"
        else:
            labels[ticker] = "official_fallback"
    return labels


def _counts(candidates: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        key = str(candidate["classification"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _baseline_id(baseline: dict) -> str:
    payload = json.dumps(
        [
            [row["ticker"], row["classification"], row["research_rank"]]
            for row in baseline["candidates"]
        ] + [baseline["baseline_as_of"], baseline["model_version"]],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = _temporary(path)
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True, default=str)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_csv(path: Path, rows: list[dict]) -> None:
    import csv

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
