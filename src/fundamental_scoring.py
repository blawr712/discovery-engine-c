"""Explainable shadow scoring for company fundamentals."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
import math
from numbers import Real

from src.config import (
    FUNDAMENTAL_DATA_POLICY,
    FUNDAMENTAL_SCORING_CONFIG,
    FUNDAMENTAL_WEIGHTS,
)
from src.factors import FactorResult, score_confidence


def calculate_fundamental_scores(
    stock_data: dict,
    as_of: datetime | None = None,
) -> dict:
    """Calculate fundamental factors without changing Discovery Score yet."""
    quality, timestamp, quality_explanation = _data_quality(
        stock_data.get("fundamental_data_timestamp"),
        as_of or datetime.now(timezone.utc),
    )
    factors = [
        _percentage_factor(
            "revenue_growth",
            stock_data.get("revenue_growth"),
            "Revenue growth",
            quality,
            timestamp,
            quality_explanation,
        ),
        _percentage_factor(
            "earnings_growth",
            stock_data.get("earnings_growth"),
            "Earnings growth",
            quality,
            timestamp,
            quality_explanation,
        ),
        _profitability_factor(stock_data, quality, timestamp, quality_explanation),
        _free_cash_flow_factor(stock_data, quality, timestamp, quality_explanation),
        _balance_sheet_factor(stock_data, quality, timestamp, quality_explanation),
        _inverse_positive_factor(
            "earnings_yield",
            stock_data.get("trailing_pe"),
            "Trailing earnings yield",
            quality,
            timestamp,
            quality_explanation,
        ),
        _inverse_positive_factor(
            "sales_yield",
            stock_data.get("price_to_sales"),
            "Trailing sales yield",
            quality,
            timestamp,
            quality_explanation,
        ),
        _direct_factor(
            "enterprise_value_ebitda",
            stock_data.get("enterprise_to_ebitda"),
            "Enterprise value to EBITDA",
            quality,
            timestamp,
            quality_explanation,
            positive=True,
        ),
        _direct_factor(
            "liquidity",
            stock_data.get("current_ratio"),
            "Current ratio",
            quality,
            timestamp,
            quality_explanation,
            positive=True,
        ),
        _direct_factor(
            "leverage",
            stock_data.get("debt_to_equity"),
            "Debt to equity",
            quality,
            timestamp,
            quality_explanation,
            non_negative=True,
        ),
        _earnings_quality_factor(
            stock_data,
            quality,
            timestamp,
            quality_explanation,
        ),
    ]
    factors = _apply_sector_applicability(
        factors,
        stock_data.get("sector"),
    )
    score = round(sum(factor.points for factor in factors), 2)
    available_max = sum(
        factor.max_points
        for factor in factors
        if factor.available and factor.applicable
    )
    normalized_score = (
        round((score / available_max) * 100, 2)
        if available_max > 0
        else None
    )

    return {
        "fundamental_score": score,
        "fundamental_score_max": sum(FUNDAMENTAL_WEIGHTS.values()),
        "fundamental_score_normalized": normalized_score,
        "fundamental_confidence": score_confidence(factors),
        "fundamental_data_quality": quality,
        "fundamental_data_as_of": timestamp,
        "fundamental_breakdown": json.dumps(
            {factor.name: factor.to_dict() for factor in factors},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _percentage_factor(
    name: str,
    value: object,
    label: str,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    numeric_value = _number(value)
    return _threshold_factor(
        name,
        numeric_value,
        (
            f"{label} is {numeric_value:.1%}"
            if numeric_value is not None
            else f"{label} is unavailable"
        ),
        quality,
        timestamp,
        quality_explanation,
    )


def _profitability_factor(
    stock_data: dict,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    operating_margin = _number(stock_data.get("operating_margin"))
    profit_margin = _number(stock_data.get("profit_margin"))
    value = operating_margin if operating_margin is not None else profit_margin
    source = "Operating margin" if operating_margin is not None else "Profit margin"
    return _threshold_factor(
        "profitability",
        value,
        (
            f"{source} is {value:.1%}"
            if value is not None
            else "Operating and profit margins are unavailable"
        ),
        quality,
        timestamp,
        quality_explanation,
    )


def _free_cash_flow_factor(
    stock_data: dict,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    free_cash_flow = _number(stock_data.get("free_cash_flow"))
    market_cap = _positive_number(stock_data.get("market_cap"))
    value = (
        free_cash_flow / market_cap
        if free_cash_flow is not None and market_cap is not None
        else None
    )
    return _threshold_factor(
        "free_cash_flow",
        value,
        (
            f"Free-cash-flow yield is {value:.1%}"
            if value is not None
            else "Free cash flow or market capitalization is unavailable"
        ),
        quality,
        timestamp,
        quality_explanation,
    )


def _balance_sheet_factor(
    stock_data: dict,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    total_cash = _number(stock_data.get("total_cash"))
    total_debt = _number(stock_data.get("total_debt"))
    market_cap = _positive_number(stock_data.get("market_cap"))
    value = (
        (total_cash - total_debt) / market_cap
        if total_cash is not None
        and total_debt is not None
        and market_cap is not None
        else None
    )
    return _threshold_factor(
        "balance_sheet",
        value,
        (
            f"Net cash is {value:.1%} of market capitalization"
            if value is not None
            else "Cash, debt, or market capitalization is unavailable"
        ),
        quality,
        timestamp,
        quality_explanation,
    )


def _threshold_factor(
    name: str,
    value: float | None,
    explanation: str,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    max_points = float(FUNDAMENTAL_WEIGHTS[name])
    points = 0.0
    source_value = value
    value, policy_quality, policy_explanation = _apply_input_policy(
        name,
        value,
    )
    if policy_quality is not None:
        if policy_quality == "invalid":
            quality = "invalid"
        elif quality == "fresh":
            quality = policy_quality
        quality_explanation = (
            f"{quality_explanation}; {policy_explanation}"
        )
    available = value is not None and quality not in {
        "stale",
        "invalid",
        "invalid_date",
    }

    if available:
        factor_config = FUNDAMENTAL_SCORING_CONFIG[name]
        direction = factor_config.get("direction", "higher")
        for threshold in factor_config["thresholds"]:
            boundary = "maximum" if direction == "lower" else "minimum"
            matches = (
                value <= float(threshold[boundary])
                if direction == "lower"
                else value >= float(threshold[boundary])
            )
            if matches:
                points = max_points * float(threshold["points_ratio"])
                break

    return FactorResult(
        name=name,
        raw_value=value,
        points=points,
        max_points=max_points,
        available=available,
        explanation=f"{explanation}; {quality_explanation}",
        data_quality=quality if quality in {"stale", "invalid", "invalid_date"} else (
            "missing" if value is None else quality
        ),
        as_of=timestamp,
        source_value=source_value,
    )


def _apply_input_policy(
    name: str,
    value: float | None,
) -> tuple[float | None, str | None, str]:
    if value is None:
        return None, None, ""
    policies = FUNDAMENTAL_DATA_POLICY.get("factor_input_policies", {})
    policy = policies.get(name)
    if not isinstance(policy, dict):
        return value, None, ""
    minimum = _number(policy.get("minimum"))
    maximum = _number(policy.get("maximum"))
    outside = (
        (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    )
    if not outside:
        return value, None, ""
    action = str(policy.get("action", "flag"))
    bounds = f"configured range [{minimum}, {maximum}]"
    if action == "invalid":
        return None, "invalid", f"source value {value:g} is outside {bounds}"
    if action == "cap":
        capped = value
        if minimum is not None:
            capped = max(capped, minimum)
        if maximum is not None:
            capped = min(capped, maximum)
        return capped, "capped", (
            f"source value {value:g} was capped to {capped:g}"
        )
    return value, "flagged", f"source value {value:g} is outside {bounds}"


def _apply_sector_applicability(
    factors: list[FactorResult],
    sector: object,
) -> list[FactorResult]:
    exclusions = FUNDAMENTAL_DATA_POLICY.get("sector_exclusions", {})
    excluded = set(exclusions.get(str(sector), []))
    if not excluded:
        return factors
    return [
        replace(
            factor,
            points=0.0,
            available=False,
            applicable=False,
            data_quality="not_applicable",
            explanation=(
                f"{factor.explanation}; not applicable to {sector}"
            ),
        )
        if factor.name in excluded
        else factor
        for factor in factors
    ]


def _direct_factor(
    name: str,
    raw_value: object,
    label: str,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> FactorResult:
    value = _number(raw_value)
    invalid = (
        value is not None
        and ((positive and value <= 0) or (non_negative and value < 0))
    )
    if invalid:
        value = None
        quality = "invalid"
        quality_explanation = f"{label} is invalid"
    explanation = f"{label} is {value:.2f}" if value is not None else f"{label} is unavailable"
    return _threshold_factor(
        name,
        value,
        explanation,
        quality,
        timestamp,
        quality_explanation,
    )


def _inverse_positive_factor(
    name: str,
    denominator: object,
    label: str,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    raw_numeric = _number(denominator)
    numeric = raw_numeric if raw_numeric is not None and raw_numeric > 0 else None
    value = 1 / numeric if numeric is not None else None
    if raw_numeric is not None and raw_numeric <= 0:
        quality = "invalid"
        quality_explanation = f"{label} denominator is not positive"
    return _threshold_factor(
        name,
        value,
        f"{label} is {value:.1%}" if value is not None else f"{label} is unavailable",
        quality,
        timestamp,
        quality_explanation,
    )


def _earnings_quality_factor(
    stock_data: dict,
    quality: str,
    timestamp: str | None,
    quality_explanation: str,
) -> FactorResult:
    operating_cash_flow = _number(stock_data.get("operating_cash_flow"))
    net_income = _positive_number(stock_data.get("net_income"))
    value = (
        operating_cash_flow / net_income
        if operating_cash_flow is not None and net_income is not None
        else None
    )
    return _threshold_factor(
        "earnings_quality",
        value,
        (
            f"Operating cash flow is {value:.2f}x net income"
            if value is not None
            else "Positive net income or operating cash flow is unavailable"
        ),
        quality,
        timestamp,
        quality_explanation,
    )


def _data_quality(
    value: object,
    as_of: datetime,
) -> tuple[str, str | None, str]:
    timestamp = _timestamp(value)
    if timestamp is None:
        if value is None:
            return "undated", None, "reporting date is unavailable"
        return "invalid_date", None, "reporting date is invalid"

    as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    age_days = max(0, (as_of - timestamp).days)
    maximum_age = int(FUNDAMENTAL_DATA_POLICY.get("maximum_age_days", 550))
    formatted = timestamp.date().isoformat()
    if age_days > maximum_age:
        return "stale", formatted, f"reported {age_days} days ago and is stale"
    return "fresh", formatted, f"reported {age_days} days ago"


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return numeric


def _positive_number(value: object) -> float | None:
    numeric = _number(value)
    return numeric if numeric is not None and numeric > 0 else None
