"""Point-in-time insider-trading signals from Form 4 open-market activity.

Signals are computed from transactions whose filing date is on or before the
scoring date, so historical evaluations see only what was public at the
time. Purchases carry most of the information in the literature; sales are
kept for net measures but are noisier because of compensation-driven
selling.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math


DEFAULT_CONFIG = {
    "short_window_days": 180,
    "long_window_days": 365,
    "minimum_transaction_value": 0,
}

SIGNAL_NAMES = (
    "ins_purchase_count_short",
    "ins_sale_count_short",
    "ins_net_count_short",
    "ins_distinct_buyers_short",
    "ins_officer_purchase_count_short",
    "ins_purchase_value_short",
    "ins_net_value_short",
    "ins_net_value_to_market_cap_short",
    "ins_cluster_buy_short",
    "ins_purchase_count_long",
    "ins_purchase_value_long",
    "ins_net_value_long",
    "ins_net_value_to_market_cap_long",
    "ins_days_since_last_purchase",
    "ins_data_through",
)


@dataclass(frozen=True)
class InsiderTransaction:
    """One open-market insider transaction as filed."""

    filed: date
    kind: str
    shares: float
    value: float | None
    owner_ciks: tuple[str, ...]
    officer_or_director: bool


class InsiderHistory:
    """Point-in-time queryable insider transactions for one issuer."""

    def __init__(
        self,
        transactions: list[dict],
        config: dict | None = None,
        data_through: date | None = None,
    ) -> None:
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        minimum_value = float(self.config["minimum_transaction_value"])
        parsed = []
        for row in transactions:
            filed = _parse_date(row.get("filed"))
            kind = str(row.get("kind") or "")
            shares = _number(row.get("shares"))
            if filed is None or kind not in {"purchase", "sale"} or shares is None:
                continue
            value = _number(row.get("value"))
            if value is not None and value < minimum_value:
                continue
            parsed.append(InsiderTransaction(
                filed=filed,
                kind=kind,
                shares=shares,
                value=value,
                owner_ciks=tuple(str(cik) for cik in row.get("owner_ciks") or ()),
                officer_or_director=bool(row.get("officer_or_director")),
            ))
        parsed.sort(key=lambda item: item.filed)
        self._transactions = parsed
        self._filed = [item.filed for item in parsed]
        self.data_through = data_through

    @property
    def has_data(self) -> bool:
        return bool(self._transactions)

    def transactions_between(self, start: date, end: date) -> list[InsiderTransaction]:
        """Transactions filed after ``start`` and on or before ``end``."""
        left = bisect_right(self._filed, start)
        right = bisect_right(self._filed, end)
        return self._transactions[left:right]

    def signals_as_of(
        self,
        as_of: date | datetime,
        market_cap: float | None = None,
    ) -> dict:
        """Derive every insider signal using filings public on ``as_of``."""
        as_of = as_of.date() if isinstance(as_of, datetime) else as_of
        signals: dict[str, object] = {name: None for name in SIGNAL_NAMES}
        signals["ins_data_through"] = (
            self.data_through.isoformat() if self.data_through else None
        )
        if not self._transactions:
            return signals
        if self.data_through is not None and as_of > self.data_through + timedelta(days=400):
            # The data set ends long before the scoring date: nothing reliable.
            return signals

        short = self.transactions_between(
            as_of - timedelta(days=int(self.config["short_window_days"])), as_of,
        )
        long = self.transactions_between(
            as_of - timedelta(days=int(self.config["long_window_days"])), as_of,
        )
        purchases_short = [item for item in short if item.kind == "purchase"]
        sales_short = [item for item in short if item.kind == "sale"]
        purchases_long = [item for item in long if item.kind == "purchase"]
        sales_long = [item for item in long if item.kind == "sale"]

        buyers = {cik for item in purchases_short for cik in item.owner_ciks}
        signals["ins_purchase_count_short"] = float(len(purchases_short))
        signals["ins_sale_count_short"] = float(len(sales_short))
        signals["ins_net_count_short"] = float(len(purchases_short) - len(sales_short))
        signals["ins_distinct_buyers_short"] = float(len(buyers))
        signals["ins_officer_purchase_count_short"] = float(
            sum(1 for item in purchases_short if item.officer_or_director)
        )
        purchase_value_short = _total_value(purchases_short)
        net_value_short = purchase_value_short - _total_value(sales_short)
        signals["ins_purchase_value_short"] = purchase_value_short
        signals["ins_net_value_short"] = net_value_short
        signals["ins_cluster_buy_short"] = 1.0 if len(buyers) >= 2 else 0.0

        signals["ins_purchase_count_long"] = float(len(purchases_long))
        purchase_value_long = _total_value(purchases_long)
        net_value_long = purchase_value_long - _total_value(sales_long)
        signals["ins_purchase_value_long"] = purchase_value_long
        signals["ins_net_value_long"] = net_value_long

        if market_cap is not None and math.isfinite(market_cap) and market_cap > 0:
            signals["ins_net_value_to_market_cap_short"] = net_value_short / market_cap
            signals["ins_net_value_to_market_cap_long"] = net_value_long / market_cap

        last_purchase = next(
            (item for item in reversed(self._transactions)
             if item.kind == "purchase" and item.filed <= as_of),
            None,
        )
        if last_purchase is not None:
            signals["ins_days_since_last_purchase"] = float((as_of - last_purchase.filed).days)
        return {
            name: (round(value, 6) if isinstance(value, float) else value)
            for name, value in signals.items()
        }


def build_insider_index(
    transactions: list[dict],
    config: dict | None = None,
    data_through: date | None = None,
) -> dict[str, InsiderHistory]:
    """Group compact transactions by issuer CIK into histories."""
    grouped: dict[str, list[dict]] = {}
    for row in transactions:
        cik = str(row.get("issuer_cik") or "").strip()
        if cik:
            grouped.setdefault(cik.zfill(10), []).append(row)
    return {
        cik: InsiderHistory(rows, config, data_through=data_through)
        for cik, rows in grouped.items()
    }


def quarter_end(label: str) -> date:
    """Return the calendar end date of a ``YYYYqN`` label."""
    year_text, quarter_text = str(label).lower().split("q")
    year, quarter = int(year_text), int(quarter_text)
    month = quarter * 3
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _total_value(items: list[InsiderTransaction]) -> float:
    return float(sum(item.value for item in items if item.value is not None))


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
