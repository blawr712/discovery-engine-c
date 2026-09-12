"""Human-readable, deterministic explanations for stored scoring results."""

from __future__ import annotations

import json


SCORE_GLOSSARY = {
    "discovery_score": {
        "label": "Discovery Score",
        "definition": (
            "A 0–100 technical opportunity score combining volume acceleration, "
            "benchmark-relative strength, trend, market capitalization, sector, "
            "and trading liquidity. It is not a predicted return."
        ),
    },
    "fundamental_score_normalized": {
        "label": "Fundamental Score",
        "definition": (
            "A normalized 0–100 view of available growth, profitability, cash-flow, "
            "valuation, liquidity, leverage, and balance-sheet factors."
        ),
    },
    "score_confidence": {
        "label": "Technical Confidence",
        "definition": (
            "The percentage of configured technical factor weight supported by "
            "available data; it measures coverage, not certainty of a price increase."
        ),
    },
    "fundamental_confidence": {
        "label": "Fundamental Confidence",
        "definition": (
            "Quality-adjusted coverage of applicable fundamental factors, including "
            "missing, stale, invalid, and capped inputs."
        ),
    },
}


def score_percentiles(rows: list[dict], field: str) -> dict[str, float]:
    """Return within-run percentiles for successful rows with numeric values."""
    values = [
        (str(row.get("ticker")), float(row[field]))
        for row in rows
        if row.get("status") == "OK" and _number(row.get(field)) is not None
    ]
    if not values:
        return {}
    numeric = [value for _, value in values]
    if len(numeric) == 1:
        return {values[0][0]: 100.0}
    return {
        ticker: round(
            (sum(other <= value for other in numeric) - 1)
            / (len(numeric) - 1) * 100,
            2,
        )
        for ticker, value in values
    }


def percentile_descriptor(percentile: float | None) -> str:
    """Describe relative standing without implying an investment outcome."""
    if percentile is None:
        return "Not ranked"
    if percentile >= 95:
        return "Top tier"
    if percentile >= 80:
        return "Strong"
    if percentile >= 60:
        return "Above average"
    if percentile >= 40:
        return "Middle"
    if percentile >= 20:
        return "Below average"
    return "Lower tier"


def confidence_descriptor(value: object) -> str:
    number = _number(value)
    if number is None:
        return "Unavailable"
    if number >= 75:
        return "High coverage"
    if number >= 50:
        return "Moderate coverage"
    if number >= 25:
        return "Limited coverage"
    return "Very limited coverage"


def explain_candidate(
    row: dict,
    rank: int | None,
    discovery_percentile: float | None,
    fundamental_percentile: float | None,
    candidate_count: int | None = None,
    discovery_median: float | None = None,
) -> dict:
    """Build a dashboard-safe explanation from one stored result row."""
    status = str(row.get("status") or "UNKNOWN")
    technical_factors = _factor_rows(row.get("factor_breakdown"))
    fundamental_factors = _factor_rows(row.get("fundamental_breakdown"))
    technical_confidence = _confidence_card(
        row.get("score_confidence"), SCORE_GLOSSARY["score_confidence"]
    )
    fundamental_confidence = _confidence_card(
        row.get("fundamental_confidence"),
        SCORE_GLOSSARY["fundamental_confidence"],
    )
    return {
        "ticker": row.get("ticker"),
        "company_name": row.get("company_name"),
        "country": row.get("country"),
        "sector": row.get("sector"),
        "exchange": row.get("exchange"),
        "status": status,
        "status_explanation": _status_explanation(status, row.get("reason_flags")),
        "rank": rank,
        "scores": {
            "discovery": _score_card(
                row.get("discovery_score"), discovery_percentile,
                SCORE_GLOSSARY["discovery_score"],
            ),
            "fundamental": _score_card(
                row.get("fundamental_score_normalized"), fundamental_percentile,
                SCORE_GLOSSARY["fundamental_score_normalized"],
            ),
        },
        "confidence": {
            "technical": technical_confidence,
            "fundamental": fundamental_confidence,
        },
        "technical_factors": technical_factors,
        "fundamental_factors": fundamental_factors,
        "score_story": _score_story(
            row, rank, candidate_count, discovery_median,
            technical_factors, fundamental_factors,
            technical_confidence, fundamental_confidence,
        ),
        "fundamental_data_quality": row.get("fundamental_data_quality"),
        "fundamental_data_as_of": row.get("fundamental_data_as_of"),
        "reason_flags": [
            flag.strip() for flag in str(row.get("reason_flags") or "").split(";")
            if flag.strip()
        ],
        "interpretation_warning": (
            "Scores describe alignment with configured discovery factors and data "
            "coverage. They are not recommendations, price targets, or return forecasts."
        ),
    }


def _score_story(
    row, rank, candidate_count, discovery_median,
    technical_factors, fundamental_factors,
    technical_confidence, fundamental_confidence,
):
    technical = _factor_context(technical_factors)
    fundamental = _factor_context(fundamental_factors)
    score = _number(row.get("discovery_score"))
    difference = (
        round(score - discovery_median, 2)
        if score is not None and discovery_median is not None else None
    )
    if rank is not None and candidate_count:
        rank_context = (
            f"Ranks #{rank:,} of {candidate_count:,} successful candidates"
        )
    else:
        rank_context = "Not ranked in the successful candidate pool"
    if difference is not None:
        rank_context += (
            " and is at the run median"
            if difference == 0 else
            f" and scores {abs(difference):g} points "
            f"{'above' if difference > 0 else 'below'} the run median"
        )
    driver_labels = [item["label"] for item in technical["drivers"]]
    constraint_labels = [item["label"] for item in technical["constraints"]]
    why = (
        f"{rank_context}. The strongest technical contributions came from "
        f"{_join_labels(driver_labels)}."
        if driver_labels else f"{rank_context}. Technical driver detail is unavailable."
    )
    holds_back = (
        "The largest unused technical scoring capacity is in "
        f"{_join_labels(constraint_labels)}."
        if constraint_labels else
        "No material technical constraint is visible in the available factors."
    )
    gaps = list(dict.fromkeys(technical["gaps"] + fundamental["gaps"]))
    reliability = (
        f"Technical coverage is {_coverage_phrase(technical_confidence)}. "
        f"Fundamental coverage is {_coverage_phrase(fundamental_confidence)}."
    )
    if gaps:
        reliability += f" Review missing or limited inputs in {_join_labels(gaps[:3])}."
    return {
        "why_it_ranks": why,
        "what_holds_it_back": holds_back,
        "reliability": reliability,
        "rank_context": rank_context,
        "successful_candidate_count": candidate_count,
        "discovery_median": discovery_median,
        "score_vs_median": difference,
        "technical_points": technical["points"],
        "technical_max_points": technical["max_points"],
        "drivers": technical["drivers"],
        "constraints": technical["constraints"],
        "fundamental_drivers": fundamental["drivers"],
        "data_gaps": gaps,
    }


def _factor_context(factors):
    applicable = [
        factor for factor in factors
        if factor["applicable"] and factor["max_points"] not in (None, 0)
    ]
    scored = [factor for factor in applicable if factor["points"] is not None]
    enriched = [
        {
            "name": factor["name"], "label": factor["label"],
            "points": factor["points"], "max_points": factor["max_points"],
            "percent": round(factor["points"] / factor["max_points"] * 100, 2),
            "explanation": factor["explanation"],
            "data_quality": factor["data_quality"],
        }
        for factor in scored
    ]
    ranked = sorted(
        enriched, key=lambda factor: (-factor["percent"], -factor["points"], factor["name"])
    )
    drivers = [factor for factor in ranked if factor["percent"] >= 50][:3]
    if not drivers and ranked:
        drivers = ranked[:1]
    driver_names = {factor["name"] for factor in drivers}
    constraints = sorted(
        (factor for factor in enriched if factor["name"] not in driver_names),
        key=lambda factor: (factor["percent"], -factor["max_points"], factor["name"]),
    )[:3]
    gaps = [
        factor["label"] for factor in applicable
        if not factor["available"] or factor["points"] is None
        or factor["data_quality"] in {"missing", "invalid", "stale"}
    ]
    return {
        "points": round(sum(factor["points"] for factor in scored), 2),
        "max_points": round(sum(factor["max_points"] for factor in applicable), 2),
        "drivers": drivers,
        "constraints": constraints,
        "gaps": gaps,
    }


def _join_labels(labels):
    if not labels:
        return "no available factors"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _coverage_phrase(card):
    descriptor = card["descriptor"].lower()
    value = card["value"]
    return descriptor if value is None else f"{descriptor} ({value:g}%)"


def _score_card(value, percentile, glossary):
    return {
        "value": _number(value),
        "maximum": 100,
        "percentile": percentile,
        "descriptor": percentile_descriptor(percentile),
        **glossary,
    }


def _confidence_card(value, glossary):
    return {
        "value": _number(value),
        "descriptor": confidence_descriptor(value),
        **glossary,
    }


def _factor_rows(payload) -> list[dict]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(payload, dict):
        return []
    return [
        {
            "name": str(factor.get("name") or name),
            "label": str(factor.get("name") or name).replace("_", " ").title(),
            "points": _number(factor.get("points")),
            "max_points": _number(factor.get("max_points")),
            "raw_value": factor.get("raw_value"),
            "available": bool(factor.get("available")),
            "applicable": factor.get("applicable") is not False,
            "data_quality": factor.get("data_quality") or "unknown",
            "as_of": factor.get("as_of"),
            "explanation": factor.get("explanation") or "No explanation available",
        }
        for name, factor in payload.items()
        if isinstance(factor, dict)
    ]


def _status_explanation(status: str, reason_flags) -> str:
    reason = str(reason_flags or "").strip()
    if status == "OK":
        return "Passed structural screening and received a complete technical score."
    if status == "FILTERED":
        return reason or "Excluded by a configured structural or metadata filter."
    if status == "FAILED":
        return reason or "Could not receive a valid score from the available inputs."
    return reason or "No status explanation is available."


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 4)
