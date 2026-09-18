"""Convert provider financial statements into the compact SEC-facts shape.

Yahoo-style statements carry period end dates but no filing dates, so each
row receives an estimated ``filed`` date from configurable reporting
deadlines (Canadian issuers must file quarterly statements within 45 or 60
days and annual statements within 90 or 120 days). The resulting extract
feeds ``FundamentalHistory`` exactly like an SEC extract, with tags prefixed
``statement:`` and ``data_quality`` set to ``estimated_filing_dates`` so
downstream consumers never mistake it for point-in-time filing data.
"""

from __future__ import annotations

from datetime import date, timedelta
import math

EXTRACT_VERSION = "statements-1"

DEFAULT_CONFIG = {
    "quarterly_filing_lag_days": 60,
    "annual_filing_lag_days": 120,
}

# Concept -> ordered candidate statement labels (first present wins per period).
STATEMENT_LABELS: dict[str, dict] = {
    "revenue": {"statement": "income", "labels": ["Total Revenue", "Operating Revenue"]},
    "gross_profit": {"statement": "income", "labels": ["Gross Profit"]},
    "operating_income": {
        "statement": "income",
        "labels": ["Operating Income", "Total Operating Income As Reported"],
    },
    "net_income": {"statement": "income", "labels": ["Net Income", "Net Income Common Stockholders"]},
    "operating_cash_flow": {"statement": "cashflow", "labels": ["Operating Cash Flow"]},
    "capital_expenditure": {
        "statement": "cashflow", "labels": ["Capital Expenditure"], "negate": True,
    },
    "cash": {
        "statement": "balance",
        "labels": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
        "instant": True,
    },
    "long_term_debt": {
        "statement": "balance",
        "labels": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
        "instant": True,
    },
    "short_term_debt": {
        "statement": "balance",
        "labels": ["Current Debt", "Current Debt And Capital Lease Obligation"],
        "instant": True,
    },
    "stockholders_equity": {
        "statement": "balance", "labels": ["Stockholders Equity", "Common Stock Equity"],
        "instant": True,
    },
    "shares_outstanding": {
        "statement": "balance", "labels": ["Ordinary Shares Number", "Share Issued"],
        "instant": True,
    },
}


def extract_from_statements(
    ticker: str,
    statements: dict,
    config: dict | None = None,
) -> dict:
    """Build a compact extract from ``{"currency", "quarterly", "annual"}``.

    ``quarterly`` and ``annual`` each map statement names (``income``,
    ``balance``, ``cashflow``) to ``{period_end_iso: {label: value}}``.
    """
    config = {**DEFAULT_CONFIG, **(config or {})}
    quarterly = statements.get("quarterly") or {}
    annual = statements.get("annual") or {}
    currency = statements.get("currency")
    facts: dict[str, list[dict]] = {}
    tags_found: dict[str, list[str]] = {}

    for concept, spec in STATEMENT_LABELS.items():
        rows: list[dict] = []
        found: list[str] = []
        for frequency, tables, lag_key in (
            ("quarterly", quarterly, "quarterly_filing_lag_days"),
            ("annual", annual, "annual_filing_lag_days"),
        ):
            if spec.get("instant") and frequency == "annual":
                continue  # balance-sheet instants come from the quarterly table
            table = tables.get(spec["statement"]) or {}
            for end_text, values in table.items():
                end = _parse_date(end_text)
                if end is None or not isinstance(values, dict):
                    continue
                label, value = _first_value(values, spec["labels"])
                if value is None:
                    continue
                if spec.get("negate"):
                    value = -value
                tag = f"statement:{label}"
                if tag not in found:
                    found.append(tag)
                filed = end + timedelta(days=int(config[lag_key]))
                if spec.get("instant"):
                    start = None
                else:
                    start = _period_start(end, frequency)
                rows.append({
                    "start": start.isoformat() if start else None,
                    "end": end.isoformat(),
                    "filed": filed.isoformat(),
                    "form": f"statement-{frequency}",
                    "fy": end.year,
                    "fp": None,
                    "val": float(value),
                    "tag": tag,
                    "unit": currency,
                })
        rows.sort(key=lambda row: (row["end"], row["filed"], row.get("start") or ""))
        facts[concept] = rows
        tags_found[concept] = found

    return {
        "_extract_version": EXTRACT_VERSION,
        "ticker": str(ticker).upper(),
        "cik": "",
        "entity_name": None,
        "currency": currency,
        "data_quality": "estimated_filing_dates",
        "filing_lag_days": {
            "quarterly": int(config["quarterly_filing_lag_days"]),
            "annual": int(config["annual_filing_lag_days"]),
        },
        "tags_found": tags_found,
        "facts": facts,
    }


def _first_value(values: dict, labels: list[str]) -> tuple[str, float | None]:
    for label in labels:
        number = _number(values.get(label))
        if number is not None:
            return label, number
    return labels[0], None


def _period_start(end: date, frequency: str) -> date:
    """Approximate period start: one quarter or one year before the end."""
    months = 3 if frequency == "quarterly" else 12
    year, month = end.year, end.month - months
    while month <= 0:
        month += 12
        year -= 1
    # Start is the day after the previous period end; approximate with the
    # same day-of-month clamped to the month length, then add one day.
    day = min(end.day, _days_in_month(year, month))
    return date(year, month, day) + timedelta(days=1)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
