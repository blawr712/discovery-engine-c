"""Point-in-time fundamental signals derived from SEC fact extracts.

Every derived value answers one question: what would an analyst have known
on a given calendar date from filings made on or before that date? Quarterly
flows are taken directly when reported, or derived by differencing
year-to-date and annual figures, and each derived value inherits the latest
filing date of its inputs. Restated values only become visible from the date
of the restating filing onward.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math

from src.data_sources.sec_xbrl_source import CONCEPTS

# Lower is better: the position of a taxonomy tag in its concept's alias list.
TAG_PRIORITY = {
    concept: {tag: index for index, tag in enumerate(spec["tags"])}
    for concept, spec in CONCEPTS.items()
}


DEFAULT_CONFIG = {
    "quarter_days": [75, 105],
    "annual_days": [340, 380],
    "period_tolerance_days": 12,
    "maximum_report_age_days": 400,
}

DURATION_CONCEPTS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capital_expenditure",
)
INSTANT_CONCEPTS = (
    "cash",
    "long_term_debt",
    "short_term_debt",
    "stockholders_equity",
    "shares_outstanding",
)

SIGNAL_NAMES = (
    "pit_revenue_ttm",
    "pit_revenue_growth_ttm",
    "pit_revenue_acceleration",
    "pit_gross_margin_ttm",
    "pit_gross_margin_change",
    "pit_operating_margin_ttm",
    "pit_net_income_ttm",
    "pit_operating_cash_flow_ttm",
    "pit_ocf_margin_ttm",
    "pit_free_cash_flow_ttm",
    "pit_fcf_margin_ttm",
    "pit_cash_conversion",
    "pit_cash",
    "pit_total_debt",
    "pit_net_cash",
    "pit_shares_outstanding",
    "pit_share_change_1y",
    "pit_market_cap",
    "pit_net_cash_to_market_cap",
    "pit_fcf_yield",
    "pit_earnings_yield",
    "pit_sales_yield",
    "pit_report_age_days",
    "pit_reporting_currency",
    "pit_data_quality",
)


@dataclass(frozen=True)
class PeriodValue:
    """One reported or derived value for a period, with its filing date."""

    end: date
    filed: date
    value: float
    start: date | None = None
    priority: int = 0


class FundamentalHistory:
    """Point-in-time queryable fundamentals for one company."""

    def __init__(self, extract: dict, config: dict | None = None) -> None:
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.ticker = str(extract.get("ticker", ""))
        facts = extract.get("facts", {}) if isinstance(extract, dict) else {}
        self.data_quality = str(
            (extract.get("data_quality") if isinstance(extract, dict) else None)
            or "filed"
        )
        self.currency = _dominant_currency(
            facts, extract.get("currency") if isinstance(extract, dict) else None,
        )
        facts = _single_currency(facts, self.currency)
        self._annuals: dict[str, list[list[PeriodValue]]] = {}
        self._quarters: dict[str, dict[date, list[PeriodValue]]] = {}
        self._instants: dict[str, dict[date, list[PeriodValue]]] = {}
        self._quarter_ends: dict[str, list[date]] = {}
        self._instant_ends: dict[str, list[date]] = {}

        for concept in DURATION_CONCEPTS:
            values = _duration_values(facts.get(concept, []), concept)
            self._annuals[concept] = _annual_values(values, self.config)
            quarters = _quarterly_values(values, self.config)
            self._quarters[concept] = _prefer_best_tag(quarters)
            self._quarter_ends[concept] = sorted(self._quarters[concept])
        for concept in INSTANT_CONCEPTS:
            instants: dict[date, list[PeriodValue]] = {}
            for row in facts.get(concept, []):
                value = _instant_value(row, concept)
                if value is not None:
                    instants.setdefault(value.end, []).append(value)
            for entries in instants.values():
                entries.sort(key=lambda item: item.filed)
            self._instants[concept] = _prefer_best_tag(instants)
            self._instant_ends[concept] = sorted(self._instants[concept])

    @property
    def has_data(self) -> bool:
        return any(self._quarter_ends.values()) or any(self._instant_ends.values())

    def latest_period_end(self, as_of: date | None = None) -> date | None:
        """Most recent period end known on ``as_of`` across every concept."""
        latest = None
        for concept in DURATION_CONCEPTS:
            for entries in self._annuals.get(concept, []):
                chosen = _latest_known(entries, as_of) if as_of else entries[-1]
                if chosen is not None and (latest is None or chosen.end > latest):
                    latest = chosen.end
            for end, entries in self._quarters.get(concept, {}).items():
                chosen = _latest_known(entries, as_of) if as_of else entries[-1]
                if chosen is not None and (latest is None or end > latest):
                    latest = end
        for concept in INSTANT_CONCEPTS:
            for end, entries in self._instants.get(concept, {}).items():
                chosen = _latest_known(entries, as_of) if as_of else entries[-1]
                if chosen is not None and (latest is None or end > latest):
                    latest = end
        return latest

    def is_stale(self, as_of: date | datetime) -> bool:
        """True when nothing known on ``as_of`` is within the maximum report age."""
        as_of = as_of.date() if isinstance(as_of, datetime) else as_of
        latest = self.latest_period_end(as_of)
        if latest is None:
            return True
        return (as_of - latest).days > int(self.config["maximum_report_age_days"])

    def quarters_as_of(self, concept: str, as_of: date) -> list[PeriodValue]:
        """Quarterly values known on ``as_of``, latest filing per period."""
        known = []
        for end in self._quarter_ends.get(concept, []):
            if end > as_of:
                break
            chosen = _latest_known(self._quarters[concept][end], as_of)
            if chosen is not None:
                known.append(chosen)
        return known

    def instant_as_of(
        self,
        concept: str,
        as_of: date,
        latest_end: date | None = None,
    ) -> PeriodValue | None:
        """Most recent instant value known on ``as_of``, optionally capped."""
        ends = self._instant_ends.get(concept, [])
        cap = min(as_of, latest_end) if latest_end is not None else as_of
        position = bisect_right(ends, cap)
        for index in range(position - 1, -1, -1):
            chosen = _latest_known(self._instants[concept][ends[index]], as_of)
            if chosen is not None:
                return chosen
        return None

    def ttm(self, concept: str, as_of: date, offset: int = 0) -> tuple[tuple[date, ...], float] | None:
        """Sum of four consecutive quarters ending ``offset`` quarters back.

        Falls back to a reported annual figure when quarterly data is absent
        (annual-only filers); ``offset`` must then be a multiple of four.
        """
        quarters = self.quarters_as_of(concept, as_of)
        end_index = len(quarters) - offset
        start_index = end_index - 4
        if start_index >= 0:
            window = quarters[start_index:end_index]
            if _consecutive(window, self.config) and _same_tag(window):
                return tuple(item.end for item in window), float(sum(item.value for item in window))
        if offset % 4 != 0:
            return None
        annuals = self.annuals_as_of(concept, as_of)
        index = len(annuals) - 1 - offset // 4
        if index < 0:
            return None
        item = annuals[index]
        return (item.end,), float(item.value)

    def annuals_as_of(self, concept: str, as_of: date) -> list[PeriodValue]:
        """Annual values known on ``as_of``, latest filing per fiscal year."""
        known = []
        for entries in self._annuals.get(concept, []):
            chosen = _latest_known(entries, as_of)
            if chosen is not None and chosen.end <= as_of:
                known.append(chosen)
        return known

    def signals_as_of(
        self,
        as_of: date | datetime,
        price: float | None = None,
        market_cap: float | None = None,
    ) -> dict:
        """Derive every point-in-time signal for a calendar date.

        Valuation ratios use ``shares_outstanding * price`` when a price is
        given; otherwise an explicit ``market_cap`` is used when supplied.
        """
        as_of = as_of.date() if isinstance(as_of, datetime) else as_of
        signals: dict[str, object] = {name: None for name in SIGNAL_NAMES}
        signals["pit_reporting_currency"] = self.currency
        signals["pit_data_quality"] = self.data_quality
        maximum_age = int(self.config["maximum_report_age_days"])

        revenue = self.ttm("revenue", as_of)
        if revenue is not None and (as_of - revenue[0][-1]).days > maximum_age:
            revenue = None
        if revenue is not None:
            ends, revenue_ttm = revenue
            signals["pit_revenue_ttm"] = revenue_ttm
            signals["pit_report_age_days"] = float((as_of - ends[-1]).days)
            prior = self.ttm("revenue", as_of, offset=4)
            if prior is not None and prior[1] > 0:
                signals["pit_revenue_growth_ttm"] = revenue_ttm / prior[1] - 1.0
            signals["pit_revenue_acceleration"] = self._acceleration(as_of)

            gross = self._aligned_ttm("gross_profit", as_of, ends)
            if gross is not None and revenue_ttm > 0:
                margin = gross / revenue_ttm
                signals["pit_gross_margin_ttm"] = margin
                prior_gross = self.ttm("gross_profit", as_of, offset=4)
                if (
                    prior is not None and prior_gross is not None
                    and prior_gross[0] == prior[0] and prior[1] > 0
                ):
                    signals["pit_gross_margin_change"] = margin - prior_gross[1] / prior[1]
            operating = self._aligned_ttm("operating_income", as_of, ends)
            if operating is not None and revenue_ttm > 0:
                signals["pit_operating_margin_ttm"] = operating / revenue_ttm

        net_income = self.ttm("net_income", as_of)
        if net_income is not None and (as_of - net_income[0][-1]).days <= maximum_age:
            signals["pit_net_income_ttm"] = net_income[1]
        ocf = self.ttm("operating_cash_flow", as_of)
        if ocf is not None and (as_of - ocf[0][-1]).days <= maximum_age:
            ocf_ttm = ocf[1]
            signals["pit_operating_cash_flow_ttm"] = ocf_ttm
            capex = self._aligned_ttm("capital_expenditure", as_of, ocf[0])
            free_cash_flow = ocf_ttm - (capex if capex is not None else 0.0)
            signals["pit_free_cash_flow_ttm"] = free_cash_flow
            revenue_ttm = signals["pit_revenue_ttm"]
            if revenue is not None and revenue[0] == ocf[0] and revenue_ttm and revenue_ttm > 0:
                signals["pit_ocf_margin_ttm"] = ocf_ttm / revenue_ttm
                signals["pit_fcf_margin_ttm"] = free_cash_flow / revenue_ttm
            if (
                net_income is not None and net_income[0] == ocf[0]
                and net_income[1] > 0
            ):
                signals["pit_cash_conversion"] = ocf_ttm / net_income[1]

        cash = self.instant_as_of("cash", as_of)
        if cash is not None and (as_of - cash.end).days <= maximum_age:
            signals["pit_cash"] = cash.value
            debt = 0.0
            for concept in ("long_term_debt", "short_term_debt"):
                item = self.instant_as_of(concept, as_of, latest_end=cash.end)
                if item is not None and (cash.end - item.end).days <= maximum_age:
                    debt += item.value
            signals["pit_total_debt"] = debt
            signals["pit_net_cash"] = cash.value - debt

        shares = self.instant_as_of("shares_outstanding", as_of)
        if shares is not None and (as_of - shares.end).days <= maximum_age:
            signals["pit_shares_outstanding"] = shares.value
            year_ago = self.instant_as_of(
                "shares_outstanding", as_of, latest_end=shares.end - timedelta(days=350),
            )
            if year_ago is not None and year_ago.value > 0 and (
                shares.end - year_ago.end
            ).days <= maximum_age + 30:
                signals["pit_share_change_1y"] = shares.value / year_ago.value - 1.0
            derived_cap = (
                shares.value * price
                if price is not None and math.isfinite(price) and price > 0
                else market_cap
            )
            if derived_cap is not None and math.isfinite(derived_cap) and derived_cap > 0:
                market_cap = derived_cap
                if market_cap > 0:
                    signals["pit_market_cap"] = market_cap
                    for name, source in (
                        ("pit_net_cash_to_market_cap", "pit_net_cash"),
                        ("pit_fcf_yield", "pit_free_cash_flow_ttm"),
                        ("pit_earnings_yield", "pit_net_income_ttm"),
                        ("pit_sales_yield", "pit_revenue_ttm"),
                    ):
                        value = signals[source]
                        if value is not None:
                            signals[name] = value / market_cap

        return {
            name: (round(value, 6) if isinstance(value, float) else value)
            for name, value in signals.items()
        }

    def _aligned_ttm(self, concept: str, as_of: date, ends: tuple[date, ...]) -> float | None:
        result = self.ttm(concept, as_of)
        if result is None or result[0] != ends:
            return None
        return result[1]

    def _acceleration(self, as_of: date) -> float | None:
        quarters = self.quarters_as_of("revenue", as_of)
        if (
            len(quarters) < 6
            or not _consecutive(quarters[-6:], self.config)
            or not _same_tag(quarters[-6:])
        ):
            return None
        latest, previous = quarters[-1], quarters[-2]
        latest_base, previous_base = quarters[-5], quarters[-6]
        if latest_base.value <= 0 or previous_base.value <= 0:
            return None
        return (latest.value / latest_base.value) - (previous.value / previous_base.value)


def _dominant_currency(facts: dict, declared: object) -> str | None:
    """Pick the money unit with the most facts, or the declared currency."""
    counts: dict[str, int] = {}
    for concept, rows in facts.items():
        if concept == "shares_outstanding":
            continue
        for row in rows:
            unit = row.get("unit")
            if isinstance(unit, str) and unit:
                counts[unit] = counts.get(unit, 0) + 1
    if counts:
        return max(sorted(counts), key=counts.get)
    if isinstance(declared, str) and declared:
        return str(declared)
    # Extracts written before units were recorded hold only US-GAAP facts.
    if any(
        str(row.get("tag", "")).startswith("us-gaap:")
        for rows in facts.values() for row in rows
    ):
        return "USD"
    return None


def _single_currency(facts: dict, currency: str | None) -> dict:
    """Drop money facts reported in a currency other than the dominant one."""
    if currency is None:
        return facts
    filtered = {}
    for concept, rows in facts.items():
        if concept == "shares_outstanding":
            filtered[concept] = rows
            continue
        filtered[concept] = [
            row for row in rows if not row.get("unit") or row.get("unit") == currency
        ]
    return filtered


def _annual_values(values: list[PeriodValue], config: dict) -> list[list[PeriodValue]]:
    """Group reported annual figures by fiscal-year end, versions sorted by filing."""
    annual_min, annual_max = config["annual_days"]
    by_end: dict[date, list[PeriodValue]] = {}
    for item in values:
        if annual_min <= (item.end - item.start).days <= annual_max:
            by_end.setdefault(item.end, []).append(item)
    groups = []
    for end in sorted(by_end):
        entries = by_end[end]
        best = min(entry.priority for entry in entries)
        groups.append(sorted(
            (entry for entry in entries if entry.priority == best),
            key=lambda entry: entry.filed,
        ))
    return groups


def _duration_values(rows: list[dict], concept: str) -> list[PeriodValue]:
    values = []
    for row in rows:
        start = _parse_date(row.get("start"))
        end = _parse_date(row.get("end"))
        filed = _parse_date(row.get("filed"))
        value = _number(row.get("val"))
        if start is None or end is None or filed is None or value is None:
            continue
        if end <= start:
            continue
        values.append(PeriodValue(
            end=end, filed=filed, value=value, start=start,
            priority=_tag_priority(concept, row.get("tag")),
        ))
    return values


def _instant_value(row: dict, concept: str) -> PeriodValue | None:
    end = _parse_date(row.get("end"))
    filed = _parse_date(row.get("filed"))
    value = _number(row.get("val"))
    if end is None or filed is None or value is None:
        return None
    return PeriodValue(
        end=end, filed=filed, value=value,
        priority=_tag_priority(concept, row.get("tag")),
    )


def _tag_priority(concept: str, tag: object) -> int:
    return TAG_PRIORITY.get(concept, {}).get(str(tag), 0)


def _prefer_best_tag(
    periods: dict[date, list[PeriodValue]],
) -> dict[date, list[PeriodValue]]:
    """Keep only the best-priority tag's values for each period.

    Filers often report a total and a component under different tags for
    the same period; mixing them would silently swap totals for components.
    """
    preferred: dict[date, list[PeriodValue]] = {}
    for end, entries in periods.items():
        best = min(entry.priority for entry in entries)
        preferred[end] = [entry for entry in entries if entry.priority == best]
    return preferred


def _quarterly_values(
    values: list[PeriodValue],
    config: dict,
) -> dict[date, list[PeriodValue]]:
    """Collect direct and derived quarterly values keyed by period end."""
    quarter_min, quarter_max = config["quarter_days"]
    tolerance = int(config["period_tolerance_days"])
    quarters: dict[date, list[PeriodValue]] = {}

    def add(item: PeriodValue) -> None:
        entries = quarters.setdefault(item.end, [])
        if any(
            existing.filed == item.filed and existing.value == item.value
            and existing.priority == item.priority
            for existing in entries
        ):
            return
        entries.append(item)

    direct_ends: set[date] = set()
    for item in values:
        days = (item.end - item.start).days
        if quarter_min <= days <= quarter_max:
            add(item)
            direct_ends.add(item.end)

    # Differencing: a cumulative period minus the same-start period that ends
    # about one quarter earlier yields the latest quarter.
    by_start: dict[date, list[PeriodValue]] = {}
    for item in values:
        by_start.setdefault(item.start, []).append(item)
    for start, items in by_start.items():
        items = sorted(items, key=lambda item: item.end)
        for longer in items:
            if longer.end in direct_ends:
                continue
            for shorter in items:
                if shorter.priority != longer.priority:
                    continue
                gap = (longer.end - shorter.end).days
                if abs(gap - 91) <= tolerance:
                    add(PeriodValue(
                        end=longer.end,
                        filed=max(longer.filed, shorter.filed),
                        value=longer.value - shorter.value,
                        start=shorter.end,
                        priority=max(longer.priority, shorter.priority),
                    ))

    # Annual minus three reported quarters covers filers that report no
    # nine-month cumulative figure.
    annual_min, annual_max = config["annual_days"]
    for item in values:
        days = (item.end - item.start).days
        if not annual_min <= days <= annual_max or item.end in direct_ends:
            continue
        if any(
            existing.start == item.start or existing.start is None
            for existing in quarters.get(item.end, [])
        ):
            continue
        # Use each quarter as it was known when the annual report was filed,
        # so restated quarters pair with the annual figure that reflects them.
        components = []
        for end, entries in quarters.items():
            if not item.start < end < item.end - timedelta(days=quarter_min - tolerance):
                continue
            known = _latest_known(entries, item.filed) or entries[0]
            if (
                known.start is not None
                and known.start >= item.start - timedelta(days=tolerance)
                and known.priority == item.priority
            ):
                components.append(known)
        components.sort(key=lambda entry: entry.end)
        if len(components) != 3:
            continue
        span = (components[-1].end - item.start).days
        if abs(span - 273) > tolerance * 2:
            continue
        add(PeriodValue(
            end=item.end,
            filed=max([item.filed] + [entry.filed for entry in components]),
            value=item.value - sum(entry.value for entry in components),
            start=components[-1].end,
            priority=max([item.priority] + [entry.priority for entry in components]),
        ))

    for entries in quarters.values():
        entries.sort(key=lambda entry: entry.filed)
    return quarters


def _latest_known(entries: list[PeriodValue], as_of: date) -> PeriodValue | None:
    chosen = None
    for entry in entries:
        if entry.filed <= as_of:
            chosen = entry
    return chosen


def _same_tag(window: list[PeriodValue]) -> bool:
    """True when every value in the window came from the same taxonomy tag."""
    return len({item.priority for item in window}) <= 1


def _consecutive(window: list[PeriodValue], config: dict) -> bool:
    quarter_min, quarter_max = config["quarter_days"]
    tolerance = int(config["period_tolerance_days"])
    for previous, current in zip(window, window[1:]):
        gap = (current.end - previous.end).days
        if not (quarter_min - tolerance) <= gap <= (quarter_max + tolerance):
            return False
    return True


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
