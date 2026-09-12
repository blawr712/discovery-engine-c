"""Deterministic offline Moonshot Discovery shadow analysis."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from src.config import MOONSHOT_CONFIG


CLASSIFICATION_ORDER = {
    "priority_research": 0,
    "asymmetric_watch": 1,
    "speculative_watch": 2,
    "market_data_required": 3,
    "insufficient_evidence": 4,
    "low_upside_signal": 5,
}

FUNDAMENTAL_RISK_FACTORS = {
    "cash_runway", "balance_sheet", "profitability", "leverage",
}
MARKET_RISK_FACTORS = {
    "liquidity", "volatility", "maximum_drawdown", "dilution",
    "reverse_splits",
}

CSV_FIELDS = (
    "moonshot_rank", "ticker", "company_name", "country", "exchange",
    "sector", "market_cap", "size_tier", "classification", "risk_band",
    "upside_score", "risk_of_ruin_score", "moonshot_confidence",
    "upside_confidence", "risk_confidence", "market_risk_confidence",
    "fundamental_data_quality", "market_data_status", "market_data_source",
    "market_data_captured_at", "market_data_errors",
    "average_dollar_volume_30d", "annualized_volatility_percent",
    "maximum_drawdown_percent", "share_count_change_percent",
    "dilution_percent", "reverse_split_count_1y",
    "source_status", "source_reason_flags", "missing_inputs",
    "upside_factor_breakdown", "risk_factor_breakdown",
)


def build_moonshot_analysis(
    results: list[dict],
    run_id: str,
    source_completed_at: str,
    config: dict | None = None,
    market_evidence: dict[str, dict] | None = None,
) -> dict:
    """Build a separate, non-ranking shadow analysis from a completed run."""
    settings = _validated_config(config or MOONSHOT_CONFIG)
    as_of = _timestamp(source_completed_at)
    candidates = []
    for row in results:
        market_cap = _positive_number(row.get("market_cap"))
        if not _eligible(row, market_cap, settings):
            continue
        ticker = str(row.get("ticker") or "")
        candidates.append(_score_candidate(
            row,
            market_cap,
            as_of,
            settings,
            (market_evidence or {}).get(ticker, {}),
        ))
    candidates.sort(key=lambda row: (
        CLASSIFICATION_ORDER[row["classification"]],
        -row["upside_score"], row["risk_of_ruin_score"],
        -row["moonshot_confidence"], row["ticker"],
    ))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["moonshot_rank"] = rank
    return {
        "schema_version": 2,
        "model_version": settings["model_version"],
        "run_id": run_id,
        "source_completed_at": source_completed_at,
        "official_scores_and_ranks_unchanged": True,
        "mode": "offline_shadow_analysis",
        "analysis_warning": (
            "Moonshot scores organize speculative research. They are not return "
            "forecasts, price targets, or investment recommendations."
        ),
        "market_cap_policy": settings["market_cap"],
        "pending_data_requirements": settings["pending_data_requirements"],
        "summary": _summary(candidates),
        "market_evidence_summary": _market_evidence_summary(
            candidates, settings,
        ),
        "factor_coverage": {
            "upside": _factor_coverage(candidates, "upside_factors"),
            "risk": _factor_coverage(candidates, "risk_factors"),
        },
        "candidates": candidates,
    }


def export_moonshot_analysis(
    analysis: dict,
    output_directory: Path,
) -> tuple[Path, Path]:
    """Atomically export review-ready CSV and complete JSON artifacts."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_id = str(analysis["run_id"])
    csv_path = output_directory / f"moonshot_candidates_{run_id}.csv"
    json_path = output_directory / f"moonshot_analysis_{run_id}.json"
    _atomic_json(json_path, analysis)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{csv_path.stem}-", suffix=".tmp", dir=output_directory,
    )
    temporary_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for candidate in analysis["candidates"]:
                row = {field: candidate.get(field) for field in CSV_FIELDS}
                row["missing_inputs"] = "|".join(candidate["missing_inputs"])
                row["market_data_errors"] = "|".join(
                    candidate["market_data_errors"]
                )
                row["upside_factor_breakdown"] = json.dumps(
                    candidate["upside_factors"], sort_keys=True,
                )
                row["risk_factor_breakdown"] = json.dumps(
                    candidate["risk_factors"], sort_keys=True,
                )
                writer.writerow(row)
        os.replace(temporary_path, csv_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return csv_path, json_path


def _score_candidate(row, market_cap, as_of, config, market_evidence):
    quality = _fundamental_quality(
        row.get("fundamental_data_timestamp"), as_of,
        config["maximum_fundamental_age_days"],
    )
    fundamental_usable = quality in {"fresh", "undated"}
    upside = _upside_factors(row, market_cap, config, fundamental_usable)
    risk = _risk_factors(
        row, market_cap, config, fundamental_usable, market_evidence,
    )
    confidence_ratio = (
        config["undated_confidence_ratio"] if quality == "undated"
        else 1.0 if quality == "fresh" else 0.0
    )
    upside_score, upside_confidence = _factor_score(upside, confidence_ratio)
    risk_score, risk_confidence = _factor_score(
        risk,
        confidence_ratio,
        quality_adjusted_names=FUNDAMENTAL_RISK_FACTORS,
    )
    market_risk_confidence = _subset_confidence(risk, MARKET_RISK_FACTORS)
    confidence = round(min(upside_confidence, risk_confidence), 2)
    classification = _classification(
        upside_score,
        risk_score,
        confidence,
        market_risk_confidence,
        config,
    )
    missing = sorted({
        factor["label"] for factor in upside + risk
        if factor["applicable"] and not factor["available"]
    })
    return {
        "moonshot_rank": None,
        "ticker": str(row.get("ticker") or ""),
        "company_name": row.get("company_name"),
        "country": row.get("country"),
        "exchange": row.get("exchange"),
        "sector": row.get("sector"),
        "market_cap": market_cap,
        "size_tier": "nano_cap" if market_cap < config["market_cap"]["nano_cap_maximum"] else "micro_cap",
        "classification": classification,
        "risk_band": _risk_band(
            risk_score,
            risk_confidence,
            config["minimum_classification_confidence"],
            market_risk_confidence,
            config["minimum_market_risk_confidence"],
        ),
        "upside_score": upside_score,
        "risk_of_ruin_score": risk_score,
        "moonshot_confidence": confidence,
        "upside_confidence": upside_confidence,
        "risk_confidence": risk_confidence,
        "market_risk_confidence": market_risk_confidence,
        "fundamental_data_quality": quality,
        "market_data_status": market_evidence.get("data_status", "unavailable"),
        "market_data_source": market_evidence.get("source"),
        "market_data_captured_at": market_evidence.get("captured_at"),
        "average_dollar_volume_30d": market_evidence.get(
            "average_dollar_volume_30d"
        ),
        "annualized_volatility_percent": market_evidence.get(
            "annualized_volatility_percent"
        ),
        "maximum_drawdown_percent": market_evidence.get(
            "maximum_drawdown_percent"
        ),
        "share_count_change_percent": market_evidence.get(
            "share_count_change_percent"
        ),
        "dilution_percent": market_evidence.get("dilution_percent"),
        "reverse_split_count_1y": market_evidence.get(
            "reverse_split_count_1y"
        ),
        "market_data_errors": market_evidence.get("errors", []),
        "source_status": row.get("status"),
        "source_reason_flags": row.get("reason_flags"),
        "missing_inputs": missing,
        "upside_factors": upside,
        "risk_factors": risk,
    }


def _upside_factors(row, market_cap, config, usable):
    weights = config["upside_weights"]
    revenue = _number(row.get("revenue_growth")) if usable else None
    earnings = _number(row.get("earnings_growth")) if usable else None
    margin = _first_number(row.get("operating_margin"), row.get("profit_margin")) if usable else None
    cash_flow = _number(row.get("operating_cash_flow")) if usable else None
    price_to_sales = _positive_number(row.get("price_to_sales")) if usable else None
    return [
        _factor("revenue_growth", "Revenue growth", revenue, weights,
                _higher_ratio(revenue, ((.50, 1), (.30, .8), (.15, .55), (0, .2))),
                "Recent revenue growth rewards demonstrated commercial expansion."),
        _factor("earnings_growth", "Earnings growth", earnings, weights,
                _higher_ratio(earnings, ((.50, 1), (.30, .8), (.15, .55), (0, .2))),
                "Earnings growth can indicate improving operating leverage."),
        _factor("profitability", "Operating profitability", margin, weights,
                _higher_ratio(margin, ((.20, 1), (.10, .75), (0, .45), (-.10, .2))),
                "Positive and expanding margins improve self-funded growth capacity."),
        _factor("cash_generation", "Operating cash generation", (
                    cash_flow / market_cap if cash_flow is not None else None
                ), weights,
                _higher_ratio(
                    cash_flow / market_cap if cash_flow is not None else None,
                    ((.20, 1), (.10, .75), (0, .4)),
                ), "Operating cash flow is scaled to market capitalization."),
        _factor("sales_valuation", "Sales valuation", price_to_sales, weights,
                _lower_ratio(price_to_sales, ((1, 1), (2, .75), (4, .4))),
                "Lower positive price-to-sales can leave more valuation headroom."),
        _factor("market_cap_headroom", "Market-cap headroom", market_cap, weights,
                1 if market_cap < 10_000_000 else .67 if market_cap < 25_000_000 else .33,
                "Small size provides mathematical headroom but is not growth evidence."),
    ]


def _risk_factors(row, market_cap, config, usable, market_evidence):
    weights = config["risk_weights"]
    cash = _number(row.get("total_cash")) if usable else None
    debt = _number(row.get("total_debt")) if usable else None
    free_cash_flow = _number(row.get("free_cash_flow")) if usable else None
    margin = _first_number(row.get("operating_margin"), row.get("profit_margin")) if usable else None
    leverage = _number(row.get("debt_to_equity")) if usable else None
    financial = "financial" in str(row.get("sector") or "").lower()
    runway = None
    if cash is not None and free_cash_flow is not None:
        runway = math.inf if free_cash_flow >= 0 else cash / -free_cash_flow
    net_debt_ratio = (
        (debt - cash) / market_cap if cash is not None and debt is not None else None
    )
    disclosure_values = (
        row.get("revenue_growth"), row.get("earnings_growth"),
        _first_number(row.get("operating_margin"), row.get("profit_margin")),
        row.get("free_cash_flow"), row.get("total_cash"), row.get("total_debt"),
        row.get("price_to_sales"),
    )
    missing_ratio = sum(_number(value) is None for value in disclosure_values) / len(disclosure_values)
    dollar_volume = _nonnegative_number(
        market_evidence.get("average_dollar_volume_30d")
    )
    volatility = _nonnegative_number(
        market_evidence.get("annualized_volatility_percent")
    )
    drawdown = _nonnegative_number(
        market_evidence.get("maximum_drawdown_percent")
    )
    dilution = _number(market_evidence.get("dilution_percent"))
    reverse_splits = _nonnegative_number(
        market_evidence.get("reverse_split_count_1y")
    )
    return [
        _factor("size_fragility", "Size fragility", market_cap, weights,
                1 if market_cap < 5_000_000 else .67 if market_cap < 10_000_000 else .33 if market_cap < 25_000_000 else .13,
                "Smaller issuers generally have less financing and operating resilience."),
        _factor("cash_runway", "Cash runway", runway, weights,
                _runway_risk(runway),
                "Cash is compared with annual free-cash-flow burn; positive cash flow receives no burn penalty."),
        _factor("balance_sheet", "Balance-sheet pressure", net_debt_ratio, weights,
                _higher_risk_ratio(net_debt_ratio, ((.75, 1), (.25, .6), (0, .25))),
                "Net debt is scaled to market capitalization.", applicable=not financial),
        _factor("profitability", "Profitability risk", margin, weights,
                0 if margin is not None and margin >= 0 else .33 if margin is not None and margin >= -.10 else .67 if margin is not None and margin >= -.30 else 1 if margin is not None else None,
                "Deeper operating losses increase dependence on outside capital."),
        _factor("leverage", "Leverage risk", leverage, weights,
                _higher_risk_ratio(leverage, ((150, 1), (75, .6), (25, .3))),
                "Debt-to-equity is excluded for financial-sector business models.", applicable=not financial),
        _factor("disclosure_gaps", "Disclosure gaps", missing_ratio, weights,
                missing_ratio,
                "Missing core financial fields are treated as risk, not neutral evidence."),
        _factor("liquidity", "Trading liquidity", dollar_volume, weights,
                _lower_ratio(dollar_volume, ((25_000, 1), (100_000, .75),
                                             (250_000, .5), (1_000_000, .25))),
                "Thirty-session average close times volume estimates trading capacity."),
        _factor("volatility", "Realized volatility", volatility, weights,
                _higher_risk_ratio(volatility, ((120, 1), (80, .75),
                                                (50, .4), (30, .15))),
                "Annualized daily volatility measures historical price instability."),
        _factor("maximum_drawdown", "Maximum drawdown", drawdown, weights,
                _higher_risk_ratio(drawdown, ((70, 1), (50, .75),
                                              (30, .4), (15, .15))),
                "One-year peak-to-trough loss measures realized downside severity."),
        _factor("dilution", "Share-count dilution", dilution, weights,
                _higher_risk_ratio(dilution, ((100, 1), (50, .75),
                                              (20, .4), (5, .15))),
                "Reported shares outstanding are compared across at least 180 days."),
        _factor("reverse_splits", "Reverse-split history", reverse_splits,
                weights,
                1 if reverse_splits is not None and reverse_splits >= 2
                else .75 if reverse_splits is not None and reverse_splits >= 1
                else 0 if reverse_splits is not None else None,
                "Reported reverse splits in the one-year price window increase risk."),
    ]


def _factor(name, label, raw_value, weights, ratio, explanation, applicable=True):
    maximum = float(weights[name])
    available = applicable and raw_value is not None and ratio is not None
    return {
        "name": name, "label": label, "raw_value": _finite_or_text(raw_value),
        "points": round(maximum * ratio, 2) if available else 0.0,
        "max_points": maximum, "available": available,
        "applicable": applicable, "explanation": explanation,
    }


def _factor_score(factors, quality_ratio, quality_adjusted_names=None):
    applicable_max = sum(row["max_points"] for row in factors if row["applicable"])
    available = [row for row in factors if row["applicable"] and row["available"]]
    available_max = sum(row["max_points"] for row in available)
    score = (
        round(sum(row["points"] for row in available) / available_max * 100, 2)
        if available_max else 0.0
    )
    quality_adjusted_names = quality_adjusted_names or {
        row["name"] for row in factors
    }
    confident_max = sum(
        row["max_points"] * (
            quality_ratio if row["name"] in quality_adjusted_names else 1
        )
        for row in available
    )
    confidence = confident_max / applicable_max * 100 if applicable_max else 0.0
    return score, round(confidence, 2)


def _subset_confidence(factors, names):
    applicable = [
        row for row in factors if row["name"] in names and row["applicable"]
    ]
    maximum = sum(row["max_points"] for row in applicable)
    available = sum(row["max_points"] for row in applicable if row["available"])
    return round(available / maximum * 100, 2) if maximum else 0.0


def _classification(upside, risk, confidence, market_confidence, config):
    if confidence < config["minimum_classification_confidence"]:
        return "insufficient_evidence"
    if market_confidence < config["minimum_market_risk_confidence"]:
        return "market_data_required"
    if (
        confidence >= config["priority_confidence_minimum"]
        and upside >= config["priority_upside_minimum"]
        and risk <= config["priority_risk_maximum"]
    ):
        return "priority_research"
    if upside >= config["watch_upside_minimum"] and risk <= config["watch_risk_maximum"]:
        return "asymmetric_watch"
    if upside >= config["speculative_upside_minimum"]:
        return "speculative_watch"
    return "low_upside_signal"


def _eligible(row, market_cap, config):
    if market_cap is None:
        return False
    policy = config["market_cap"]
    if not policy["minimum"] <= market_cap <= policy["maximum"]:
        return False
    return (
        str(row.get("asset_type") or "") == "operating_equity"
        and str(row.get("quote_type") or "").upper() == "EQUITY"
    )


def _summary(candidates):
    return {
        "candidate_count": len(candidates),
        "nano_cap_count": sum(row["size_tier"] == "nano_cap" for row in candidates),
        "micro_cap_count": sum(row["size_tier"] == "micro_cap" for row in candidates),
        "countries": _counts(row.get("country") or "UNKNOWN" for row in candidates),
        "classifications": _counts(row["classification"] for row in candidates),
        "risk_bands": _counts(row["risk_band"] for row in candidates),
        "market_data_status": _counts(
            row["market_data_status"] for row in candidates
        ),
        "fundamental_data_quality": _counts(
            row["fundamental_data_quality"] for row in candidates
        ),
    }


def _market_evidence_summary(candidates, config):
    covered = [
        row for row in candidates
        if row["market_data_status"] != "unavailable"
    ]
    excluded = sorted([
        {
            "ticker": row["ticker"],
            "reason": "unavailable_market_evidence",
        }
        for row in candidates
        if row["market_data_status"] == "unavailable"
    ], key=lambda row: row["ticker"])
    timestamps = []
    for row in covered:
        try:
            timestamps.append(_timestamp(row["market_data_captured_at"]))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    first = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    span_days = (
        round((last - first).total_seconds() / 86400, 2)
        if first is not None and last is not None else None
    )
    coverage_percent = (
        round(len(covered) / len(candidates) * 100, 2)
        if candidates else 0.0
    )
    minimum_coverage = float(
        config["minimum_cross_section_coverage_percent"]
    )
    synchronized = len(timestamps) == len(covered)
    return {
        "covered_candidates": len(covered),
        "coverage_percent": coverage_percent,
        "minimum_coverage_percent": minimum_coverage,
        "excluded_candidates": excluded,
        "sources": _counts(
            row.get("market_data_source") or "UNKNOWN" for row in covered
        ),
        "first_captured_at": first.isoformat() if first is not None else None,
        "last_captured_at": last.isoformat() if last is not None else None,
        "capture_span_days": span_days,
        "cross_section_comparable": (
            bool(candidates)
            and coverage_percent >= minimum_coverage
            and synchronized
            and span_days is not None
            and span_days <= 7
        ),
    }


def _factor_coverage(candidates, field):
    names = sorted({factor["name"] for row in candidates for factor in row[field]})
    coverage = {}
    for name in names:
        available = sum(
            factor["available"] for row in candidates
            for factor in row[field] if factor["name"] == name
        )
        applicable = sum(
            factor["applicable"] for row in candidates
            for factor in row[field] if factor["name"] == name
        )
        coverage[name] = {
            "available": available,
            "applicable": applicable,
            "coverage_percent": (
                round(available / applicable * 100, 2) if applicable else None
            ),
        }
    return coverage


def _validated_config(config):
    required = {
        "model_version", "market_cap", "maximum_fundamental_age_days",
        "undated_confidence_ratio", "minimum_classification_confidence",
        "minimum_market_risk_confidence",
        "minimum_cross_section_coverage_percent",
        "priority_confidence_minimum",
        "priority_upside_minimum", "priority_risk_maximum",
        "watch_upside_minimum", "watch_risk_maximum",
        "speculative_upside_minimum", "upside_weights", "risk_weights",
        "pending_data_requirements",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"Moonshot configuration missing: {', '.join(sorted(missing))}")
    policy = config["market_cap"]
    if not policy["minimum"] < policy["nano_cap_maximum"] < policy["maximum"]:
        raise ValueError("Moonshot market-cap thresholds must increase.")
    coverage_minimum = float(config["minimum_cross_section_coverage_percent"])
    if not 0 < coverage_minimum <= 100:
        raise ValueError(
            "Moonshot cross-section coverage threshold must be within (0, 100]."
        )
    for name in ("upside_weights", "risk_weights"):
        if round(sum(config[name].values()), 6) != 100:
            raise ValueError(f"Moonshot {name} must total 100.")
    return config


def _fundamental_quality(value, as_of, maximum_age_days):
    if value is None:
        return "undated"
    try:
        timestamp = _timestamp(value)
    except (TypeError, ValueError, OSError, OverflowError):
        return "invalid"
    age = (as_of - timestamp).total_seconds() / 86400
    if age < -7:
        return "invalid"
    return "fresh" if age <= maximum_age_days else "stale"


def _higher_ratio(value, thresholds):
    if value is None:
        return None
    return next((ratio for minimum, ratio in thresholds if value >= minimum), 0)


def _lower_ratio(value, thresholds):
    if value is None:
        return None
    return next((ratio for maximum, ratio in thresholds if value <= maximum), 0)


def _higher_risk_ratio(value, thresholds):
    if value is None:
        return None
    return next((ratio for minimum, ratio in thresholds if value > minimum), 0)


def _runway_risk(runway):
    if runway is None:
        return None
    if math.isinf(runway) or runway >= 2:
        return 0
    if runway >= 1:
        return .33
    if runway >= .5:
        return .67
    return 1


def _risk_band(
    value,
    confidence,
    minimum_confidence,
    market_confidence,
    minimum_market_confidence,
):
    if (
        confidence <= minimum_confidence
        or market_confidence < minimum_market_confidence
    ):
        return "unresolved"
    return "controlled" if value <= 30 else "elevated" if value <= 55 else "severe"


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_number(value):
    number = _number(value)
    return number if number is not None and number > 0 else None


def _nonnegative_number(value):
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _first_number(*values):
    return next((number for value in values if (number := _number(value)) is not None), None)


def _finite_or_text(value):
    if isinstance(value, float) and math.isinf(value):
        return "positive_cash_flow"
    return value


def _counts(values):
    return dict(sorted(Counter(values).items()))


def _atomic_json(path, payload):
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent,
    )
    temporary_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
