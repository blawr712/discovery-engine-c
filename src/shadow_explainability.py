"""Dashboard-safe explanations of shadow models, fundamentals, and insiders.

Everything here reshapes fields already stored on a result row into
plain-language panels. Nothing is a return forecast or a recommendation, and
every panel says where its data came from and how fresh it is.
"""

from __future__ import annotations

import json
import math

from src.scoring_v2 import SIGNAL_LABELS, model_configs, raw_column


SHADOW_GLOSSARY = {
    "score_v2": {
        "label": "Score v2",
        "definition": (
            "Continuous price-only ranking: each signal is a percentile across "
            "the run's successful candidates (within sector when the group is "
            "large enough), blended by configured weights into 0-100."
        ),
        "not": "Not a return forecast. Validated by the backtest; see Evidence.",
    },
    "score_v3": {
        "label": "Score v3",
        "definition": (
            "Value-and-quality composite: exclusion filters remove weak margins, "
            "heavy dilution, and hype growth, then the remainder ranks on sales "
            "yield, net cash, cash-flow yield and margins, low dilution, insider "
            "buying, and a price tilt. Names without enough fundamentals get no "
            "v3 score and fall back to v2 in the research queue."
        ),
        "not": "Not a return forecast. Validated by the backtest; see Evidence.",
    },
}

FUNDAMENTAL_ROWS = (
    ("pit_revenue_ttm", "Revenue, trailing 12 months", "money"),
    ("pit_revenue_growth_ttm", "Revenue growth, TTM over prior TTM", "percent"),
    ("pit_revenue_acceleration", "Revenue acceleration (latest YoY minus prior YoY)", "points"),
    ("pit_gross_margin_ttm", "Gross margin, TTM", "percent"),
    ("pit_gross_margin_change", "Gross margin change vs prior TTM", "points"),
    ("pit_operating_margin_ttm", "Operating margin, TTM", "percent"),
    ("pit_net_income_ttm", "Net income, TTM", "money"),
    ("pit_operating_cash_flow_ttm", "Operating cash flow, TTM", "money"),
    ("pit_ocf_margin_ttm", "Operating cash flow margin", "percent"),
    ("pit_free_cash_flow_ttm", "Free cash flow, TTM", "money"),
    ("pit_fcf_margin_ttm", "Free cash flow margin", "percent"),
    ("pit_cash_conversion", "Cash conversion (OCF / net income)", "multiple"),
    ("pit_cash", "Cash and equivalents", "money"),
    ("pit_total_debt", "Total debt", "money"),
    ("pit_net_cash", "Net cash", "money"),
    ("pit_shares_outstanding", "Shares outstanding", "count"),
    ("pit_share_change_1y", "Share count change, one year", "percent"),
    ("pit_market_cap", "Market cap used for ratios", "money"),
    ("pit_net_cash_to_market_cap", "Net cash to market cap", "percent"),
    ("pit_sales_yield", "Sales yield (revenue / market cap)", "multiple"),
    ("pit_fcf_yield", "Free cash flow yield", "percent"),
    ("pit_earnings_yield", "Earnings yield", "percent"),
)

INSIDER_ROWS = (
    ("ins_purchase_count_short", "Open-market purchases", "count"),
    ("ins_sale_count_short", "Open-market sales", "count"),
    ("ins_net_count_short", "Net purchases minus sales", "count"),
    ("ins_distinct_buyers_short", "Distinct buyers", "count"),
    ("ins_officer_purchase_count_short", "Officer and director purchases", "count"),
    ("ins_purchase_value_short", "Purchase value", "money"),
    ("ins_net_value_short", "Net purchase value", "money"),
    ("ins_net_value_to_market_cap_short", "Net purchase value to market cap", "percent"),
    ("ins_cluster_buy_short", "Cluster buying (two or more buyers)", "flag"),
)
INSIDER_LONG_ROWS = (
    ("ins_purchase_count_long", "Open-market purchases", "count"),
    ("ins_purchase_value_long", "Purchase value", "money"),
    ("ins_net_value_long", "Net purchase value", "money"),
    ("ins_net_value_to_market_cap_long", "Net purchase value to market cap", "percent"),
)


def explain_shadow_models(row: dict) -> list[dict]:
    """Describe each configured shadow model's result for one row."""
    models = []
    for config in model_configs():
        prefix = config["output_prefix"]
        if prefix not in row and f"{prefix}_breakdown" not in row:
            continue
        breakdown = _json_dict(row.get(f"{prefix}_breakdown"))
        signals = []
        for name, spec in config["signals"].items():
            factor = breakdown.get(name, {})
            signals.append({
                "name": name,
                "label": SIGNAL_LABELS.get(name, name),
                "weight": spec["weight"],
                "direction": spec["direction"],
                "points": _number(factor.get("points")),
                "percentile": _percentile_from(factor, spec["weight"]),
                "raw_value": row.get(raw_column(name)),
                "available": bool(factor.get("available", False)),
                "applicable": bool(factor.get("applicable", True)),
                "explanation": factor.get("explanation") or "Unavailable",
            })
        reasons = row.get(f"{prefix}_exclusion_reasons")
        models.append({
            "model": prefix,
            "label": SHADOW_GLOSSARY.get(prefix, {}).get("label", prefix),
            "version": row.get(f"{prefix}_model_version"),
            "score": _number(row.get(prefix)),
            "rank": row.get(f"{prefix}_rank"),
            "confidence": _number(row.get(f"{prefix}_confidence")),
            "minimum_confidence": config["minimum_confidence"],
            "excluded": bool(row.get(f"{prefix}_excluded", False)),
            "exclusion_reasons": (
                [item for item in str(reasons).split("; ") if item]
                if isinstance(reasons, str) and reasons else []
            ),
            "exclusion_rules": [rule["reason"] for rule in config["exclusions"]],
            "sector_exclusions": config["sector_exclusions"],
            "signals": signals,
            "status": _model_status(row, prefix, config),
            "definition": SHADOW_GLOSSARY.get(prefix, {}).get("definition"),
            "not": SHADOW_GLOSSARY.get(prefix, {}).get("not"),
        })
    return models


def explain_fundamentals(row: dict) -> dict:
    """Describe point-in-time fundamentals stored on the row."""
    values = [
        {"key": key, "label": label, "kind": kind, "value": row.get(key)}
        for key, label, kind in FUNDAMENTAL_ROWS
        if row.get(key) is not None
    ]
    quality = row.get("pit_data_quality")
    status = str(row.get("fundamentals_status") or "")
    if quality is None and values:
        # Rows scored before the quality label existed came from SEC filings
        # unless their status says they were built from provider statements.
        quality = "estimated_filing_dates" if status.startswith("statements") else "filed"
    return {
        "available": bool(values),
        "status": row.get("fundamentals_status"),
        "reporting_currency": row.get("pit_reporting_currency"),
        "trading_currency": row.get("currency"),
        "data_quality": quality,
        "data_quality_note": _quality_note(quality),
        "report_age_days": row.get("pit_report_age_days"),
        "values": values,
    }


def explain_insiders(row: dict) -> dict:
    """Describe Form 4 insider activity stored on the row."""
    short = [
        {"key": key, "label": label, "kind": kind, "value": row.get(key)}
        for key, label, kind in INSIDER_ROWS if row.get(key) is not None
    ]
    long = [
        {"key": key, "label": label, "kind": kind, "value": row.get(key)}
        for key, label, kind in INSIDER_LONG_ROWS if row.get(key) is not None
    ]
    return {
        "available": bool(short or long),
        "status": row.get("insiders_status"),
        "data_through": row.get("ins_data_through"),
        "days_since_last_purchase": row.get("ins_days_since_last_purchase"),
        "short_window": short,
        "long_window": long,
        "note": (
            "Open-market Form 4 transactions from the SEC's quarterly insider "
            "data sets, which publish with a lag; activity after the data-through "
            "date is not yet visible."
        ),
    }


def _model_status(row: dict, prefix: str, config: dict) -> str:
    if row.get(f"{prefix}_excluded"):
        return "excluded"
    score = _number(row.get(prefix))
    confidence = _number(row.get(f"{prefix}_confidence"))
    if score is None:
        if confidence is not None and confidence < float(config["minimum_confidence"]):
            return "below_confidence_gate"
        return "not_scored"
    return "scored"


def _percentile_from(factor: dict, weight: float) -> float | None:
    points = _number(factor.get("points"))
    if points is None or not factor.get("available", False) or weight <= 0:
        return None
    return round(points / float(weight) * 100.0, 1)


def _quality_note(quality: object) -> str:
    if quality == "estimated_filing_dates":
        return (
            "Derived from provider statements without filing dates; availability "
            "dates were estimated from Canadian filing deadlines."
        )
    if quality == "filed":
        return "Derived from SEC filings using only facts public on the scoring date."
    return "No fundamental data is attached to this row."


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
