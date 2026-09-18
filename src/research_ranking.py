"""Research-queue ordering driven by validated shadow models.

The official Discovery Score remains the provenance ranking, but the research
queue that decides which companies get analyst time is ordered by the model
with the best validated evidence: Score v3 first, Score v2 for companies that
lack fundamentals, and the official score for anything left. Each queue row
says which basis ranked it and why, and nothing here is a return forecast or
an investment recommendation.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import tempfile

from src.config import RESEARCH_RANKING_CONFIG


DEFAULT_CONFIG = {
    "primary_model": "score_v3",
    "fallback_model": "score_v2",
    "final_fallback": "discovery_score",
    "top_stocks": 100,
    "packet_source": "research_queue",
}

KEY_RATIOS = (
    "pit_sales_yield",
    "pit_net_cash_to_market_cap",
    "pit_fcf_margin_ttm",
    "pit_operating_margin_ttm",
    "pit_share_change_1y",
    "pit_revenue_growth_ttm",
)


def validated_config(config: dict | None = None) -> dict:
    """Merge configured research-ranking settings over defaults."""
    merged = {**DEFAULT_CONFIG, **(config if config is not None else RESEARCH_RANKING_CONFIG)}
    for name in ("primary_model", "fallback_model", "final_fallback"):
        value = merged[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"Research ranking {name} must be a non-empty field name.")
    top = merged["top_stocks"]
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise ValueError("Research ranking top_stocks must be a positive integer.")
    if merged["packet_source"] not in {"research_queue", "calibration_scenario"}:
        raise ValueError(
            "Research ranking packet_source must be research_queue or calibration_scenario."
        )
    return merged


def build_research_queue(
    results: list[dict],
    config: dict | None = None,
) -> tuple[list[dict], dict]:
    """Order successful rows by the best available validated basis."""
    config = validated_config(config)
    rows = [row for row in results if row.get("status") == "OK"]
    discovery_ranks = _ranks(rows, config["final_fallback"])

    tiers = (
        ("primary", config["primary_model"]),
        ("fallback", config["fallback_model"]),
        ("final", config["final_fallback"]),
    )
    queued: list[tuple[str, str, dict]] = []
    used: set[str] = set()
    for tier, field in tiers:
        eligible = [
            row for row in rows
            if str(row.get("ticker")) not in used and _number(row.get(field)) is not None
        ]
        eligible.sort(
            key=lambda row: (-_number(row.get(field)), str(row.get("ticker", "")))
        )
        for row in eligible:
            used.add(str(row.get("ticker")))
            queued.append((tier, field, row))

    queue = []
    for rank, (tier, field, row) in enumerate(queued, start=1):
        queue.append({
            "research_rank": rank,
            "ranking_basis": field,
            "ranking_tier": tier,
            "ranking_score": _number(row.get(field)),
            "ticker": row.get("ticker"),
            "company_name": row.get("company_name"),
            "country": row.get("country"),
            "exchange": row.get("exchange"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "market_cap": row.get("market_cap"),
            "latest_close": row.get("latest_close"),
            "discovery_score": row.get("discovery_score"),
            "discovery_rank": discovery_ranks.get(str(row.get("ticker"))),
            "score_v3": row.get("score_v3"),
            "score_v3_rank": row.get("score_v3_rank"),
            "score_v3_confidence": row.get("score_v3_confidence"),
            "score_v3_excluded": row.get("score_v3_excluded"),
            "score_v3_exclusion_reasons": row.get("score_v3_exclusion_reasons"),
            "score_v2": row.get("score_v2"),
            "score_v2_rank": row.get("score_v2_rank"),
            "fundamentals_status": row.get("fundamentals_status"),
            **{name: row.get(name) for name in KEY_RATIOS},
            "strongest_drivers": _drivers(row, f"{field}_breakdown", best=True),
            "weakest_drivers": _drivers(row, f"{field}_breakdown", best=False),
        })

    summary = {
        "queued": len(queue),
        "by_basis": _counts(queue, "ranking_basis"),
        "by_tier": _counts(queue, "ranking_tier"),
        "excluded_by_primary": sum(
            1 for row in rows if bool(row.get(f"{config['primary_model']}_excluded"))
        ),
        "exclusion_reasons": _reason_counts(rows, config["primary_model"]),
        "config": {
            key: value for key, value in config.items() if not key.startswith("_")
        },
        "note": (
            "The queue orders research attention by validated evidence; it is "
            "not a return forecast or an investment recommendation."
        ),
    }
    return queue, summary


def export_research_queue(
    results: list[dict],
    run_id: str,
    output_directory: Path,
    config: dict | None = None,
) -> dict[str, str]:
    """Write the research queue as CSV (top N) and JSON (complete)."""
    config = validated_config(config)
    queue, summary = build_research_queue(results, config)
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_path = output_directory / f"research_queue_{run_id}.csv"
    json_path = output_directory / f"research_queue_{run_id}.json"
    _atomic_csv(csv_path, queue[: config["top_stocks"]])
    _atomic_json(json_path, {
        "run_id": run_id,
        "summary": summary,
        "queue": queue,
    })
    return {
        "research_queue_csv_path": str(csv_path),
        "research_queue_json_path": str(json_path),
    }


def _ranks(rows: list[dict], field: str) -> dict[str, int]:
    ordered = sorted(
        (row for row in rows if _number(row.get(field)) is not None),
        key=lambda row: (-_number(row.get(field)), str(row.get("ticker", ""))),
    )
    return {str(row.get("ticker")): rank for rank, row in enumerate(ordered, start=1)}


def _drivers(row: dict, breakdown_field: str, *, best: bool, limit: int = 3) -> str:
    breakdown = row.get(breakdown_field)
    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except json.JSONDecodeError:
            return ""
    if not isinstance(breakdown, dict):
        return ""
    factors = []
    for name, factor in breakdown.items():
        if not isinstance(factor, dict) or not factor.get("available", True):
            continue
        maximum = _number(factor.get("max_points"))
        points = _number(factor.get("points"))
        if maximum is None or points is None or maximum <= 0:
            continue
        factors.append((points / maximum, name, factor.get("explanation", "")))
    # Strongest: highest points ratio first. Weakest: lowest ratio first.
    factors.sort(key=lambda item: (item[0], item[1]), reverse=best)
    return "; ".join(
        f"{name}: {explanation}" if explanation else name
        for _, name, explanation in factors[:limit]
    )


def _reason_counts(rows: list[dict], model: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reasons = row.get(f"{model}_exclusion_reasons")
        if not isinstance(reasons, str) or not reasons:
            continue
        for reason in reasons.split("; "):
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _counts(rows: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


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


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = _temporary(path)
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True, default=str)
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
