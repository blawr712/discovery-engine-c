"""Coverage and distribution analytics for Discovery Engine runs."""

from __future__ import annotations

from collections import Counter
import json
import math
import os
from pathlib import Path
import tempfile


def build_run_analytics(results: list[dict]) -> dict:
    """Summarize factor coverage, exclusions, and score distributions."""
    successful = [row for row in results if row.get("status") == "OK"]
    return {
        "total_results": len(results),
        "successful_results": len(successful),
        "asset_types": _counts(results, "asset_type"),
        "filter_reasons": dict(sorted(Counter(
            str(row.get("reason_flags") or "Unknown")
            for row in results
            if row.get("status") == "FILTERED"
        ).items())),
        "structural_exclusions": sum(
            1
            for row in results
            if row.get("status") == "FILTERED"
            and row.get("asset_type")
            not in {None, "unknown", "operating_equity"}
        ),
        "fundamental_confidence_buckets": _confidence_buckets(successful),
        "fundamental_data_quality": _counts(
            successful,
            "fundamental_data_quality",
        ),
        "factor_coverage": _factor_coverage(successful),
        "coverage_by_country": _coverage_by_dimension(successful, "country"),
        "coverage_by_sector": _coverage_by_dimension(successful, "sector"),
        "score_distributions": {
            "discovery_score": _distribution(
                row.get("discovery_score") for row in successful
            ),
            "fundamental_score_normalized": _distribution(
                row.get("fundamental_score_normalized") for row in successful
            ),
            "fundamental_confidence": _distribution(
                row.get("fundamental_confidence") for row in successful
            ),
        },
    }


def export_run_analytics(
    results: list[dict],
    run_id: str,
    output_directory: Path,
) -> str:
    """Atomically export a JSON analytics artifact for a completed run."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"discovery_analytics_{run_id}.json"
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-",
        suffix=".tmp",
        dir=output_directory,
    )
    temporary_path = Path(name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(
                build_run_analytics(results),
                file,
                indent=2,
                sort_keys=True,
            )
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return str(output_path)


def _factor_coverage(rows: list[dict]) -> dict:
    factor_rows = [_fundamental_factors(row) for row in rows]
    factor_names = sorted({name for factors in factor_rows for name in factors})
    total = len(rows)
    coverage = {}

    for name in factor_names:
        applicable = sum(
            1
            for factors in factor_rows
            if factors.get(name, {}).get("applicable", True) is not False
        )
        available = sum(
            1
            for factors in factor_rows
            if factors.get(name, {}).get("available") is True
        )
        coverage[name] = {
            "available": available,
            "applicable": applicable,
            "not_applicable": total - applicable,
            "total": total,
            "percentage": (
                round((available / applicable) * 100, 2)
                if applicable
                else 0
            ),
        }

    return coverage


def _coverage_by_dimension(rows: list[dict], dimension: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get(dimension) or "UNKNOWN")
        groups.setdefault(key, []).append(row)

    return {
        key: {
            "companies": len(group),
            "average_fundamental_confidence": _average(
                row.get("fundamental_confidence") for row in group
            ),
            "factor_coverage": _factor_coverage(group),
        }
        for key, group in sorted(groups.items())
    }


def _confidence_buckets(rows: list[dict]) -> dict:
    buckets = Counter()
    for row in rows:
        value = _numeric(row.get("fundamental_confidence")) or 0
        if value >= 100:
            bucket = "100"
        elif value >= 75:
            bucket = "75-99"
        elif value >= 50:
            bucket = "50-74"
        elif value >= 25:
            bucket = "25-49"
        else:
            bucket = "0-24"
        buckets[bucket] += 1
    return {key: buckets.get(key, 0) for key in ("0-24", "25-49", "50-74", "75-99", "100")}


def _distribution(values) -> dict:
    numbers = sorted(
        value
        for value in (_numeric(item) for item in values)
        if value is not None
    )
    if not numbers:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None}
    return {
        "count": len(numbers),
        "min": round(numbers[0], 2),
        "p25": round(_percentile(numbers, 0.25), 2),
        "median": round(_percentile(numbers, 0.50), 2),
        "p75": round(_percentile(numbers, 0.75), 2),
        "max": round(numbers[-1], 2),
    }


def _percentile(numbers: list[float], percentile: float) -> float:
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    fraction = position - lower
    return numbers[lower] + (numbers[upper] - numbers[lower]) * fraction


def _average(values) -> float | None:
    numbers = [value for value in (_numeric(item) for item in values) if value is not None]
    return round(sum(numbers) / len(numbers), 2) if numbers else None


def _counts(rows: list[dict], field: str) -> dict:
    return dict(sorted(Counter(str(row.get(field) or "UNKNOWN") for row in rows).items()))


def _fundamental_factors(row: dict) -> dict:
    value = row.get("fundamental_breakdown")
    if not value:
        return {}
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
