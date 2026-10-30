"""Point-in-time fundamental signals derived from SEC fact extracts.

Every derived value answers one question: what would an analyst have known
on a given calendar date from filings made on or before that date? Quarterly
flows are taken directly when reported, or derived by differencing
year-to-date and annual figures, and each derived value inherits the latest
filing date of its inputs. Restated values only become visible from the date
of the restating filing onward.

Share counts are restated into the price series' basis before use, because a
split between the filing and the price puts the two in different units; see
``src/split_adjustment.py``.

Valuation ratios need a market capitalisation, and the obvious construction
-- the filing's share count times the traded price -- is wrong whenever the
two are denominated differently. An ADS that bundles 20 ordinary shares, or a
reverse split between the filing date and the price date, leaves the share
count on a different basis from the price and silently scales every yield by
that factor. When an independently sourced market capitalisation is supplied
and the two cannot be reconciled, the ratios are built from the supplied
figure and the result is labelled, rather than quietly published.

Many companies with more than one share class report period-end shares per
class only, which SEC company facts omit, so they carry no usable count. For
them the share count falls back to the latest quarter's weighted-average basic
shares, which they report as one total; ``pit_shares_basis`` records which was
used.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math

from src.data_sources.sec_xbrl_source import CONCEPTS
from src.split_adjustment import SplitHistory

# Lower is better: the position of a taxonomy tag in its concept's alias list.
TAG_PRIORITY = {
    concept: {tag: index for index, tag in enumerate(spec["tags"])}
    for concept, spec in CONCEPTS.items()
}


DEFAULT_CONFIG = {
    "quarter_days": [75, 105],
    "annual_days": [340, 380],
    # Half-year reports, used by filers that report twice a year (6-K).
    "half_year_days": [170, 195],
    "period_tolerance_days": 12,
    "maximum_report_age_days": 400,
    # A filer that reports only annually (most 20-F and 40-F filers) files up
    # to four months after its year ends, so its latest figures are
    # legitimately up to about sixteen months old.
    "maximum_annual_report_age_days": 550,
    # A filing's share count and today's price may legitimately disagree by
    # the issuance or buybacks since the filing. Beyond this factor the
    # disagreement is a unit mismatch (ADS ratio or split), not a flow.
    "market_cap_reconciliation_tolerance": 2.0,
}

DURATION_CONCEPTS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capital_expenditure",
)
# Debt concepts, combined into one figure by FundamentalHistory.total_debt.
DEBT_CONCEPTS = (
    "debt_total_reported",
    "debt_broad",
    "long_term_debt",
    "short_term_debt",
    "debt_noncurrent_parts",
    "debt_current_parts",
    "debt_credit_lines",
    "debt_notes",
)
# Debt must come from the same balance sheet as cash. A few days' slack
# covers filers whose balance-sheet tags carry slightly different dates.
DEBT_DATE_TOLERANCE_DAYS = 7
INSTANT_CONCEPTS = (
    "cash",
    *DEBT_CONCEPTS,
    "stockholders_equity",
    "shares_outstanding",
)
# Share counts never take part in currency selection or filtering.
SHARE_CONCEPTS = ("shares_outstanding", "weighted_average_shares")
WEIGHTED_SHARES_MAX_DAYS = 380

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
    # Free cash flow over two years (DECISIONS.md 2026-10-29): the trailing
    # four quarters a year before the current window, and two variants that
    # damp a one-off year. Each falls back to the one-year figure when the
    # prior year is missing.
    "pit_free_cash_flow_prior_ttm",
    "pit_fcf_margin_2y",
    "pit_fcf_margin_low",
    "pit_cash_conversion",
    "pit_cash",
    "pit_total_debt",
    # How pit_total_debt was found: "reported_total", "components", or
    # "none_found" (no debt tag on that balance sheet, so debt counts as 0).
    "pit_debt_coverage",
    "pit_net_cash",
    "pit_shares_outstanding",
    "pit_shares_basis",
    "pit_share_change_1y",
    "pit_split_factor",
    "pit_split_ambiguous",
    "pit_market_cap",
    "pit_market_cap_basis",
    "pit_net_cash_to_market_cap",
    "pit_fcf_yield",
    "pit_fcf_yield_2y",
    "pit_fcf_yield_low",
    "pit_earnings_yield",
    "pit_sales_yield",
    "pit_report_age_days",
    "pit_reporting_currency",
    "pit_data_quality",
)


# The weighted-average share count replaces a period-end count it exceeds by
# more than this factor (was 2.0 until 2026-10-29).
WEIGHTED_SHARE_RATIO = 1.2
# A weighted average ten times the period-end count is a scale error in the
# filing, not a share class: GLBS's 2025 20-F tags 20.7 billion for 20.7
# million (Oct 2026).
WEIGHTED_SHARE_SCALE_ERROR = 10.0
# Trailing sums rescaled onto the newest filing's basis after a restatement.
RESCALED_CONCEPTS = ("revenue",)
RESTATEMENT_THRESHOLD = 0.05


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
        facts = _without_proxy_figures(facts)
        self.data_quality = str(
            (extract.get("data_quality") if isinstance(extract, dict) else None)
            or "filed"
        )
        self.currency = _dominant_currency(
            facts, extract.get("currency") if isinstance(extract, dict) else None,
        )
        facts = _single_currency(facts, self.currency)
        self._annuals: dict[str, list[list[PeriodValue]]] = {}
        self._halves: dict[str, list[list[PeriodValue]]] = {}
        self._quarters: dict[str, dict[date, list[PeriodValue]]] = {}
        self._instants: dict[str, dict[date, list[PeriodValue]]] = {}
        self._quarter_ends: dict[str, list[date]] = {}
        self._instant_ends: dict[str, list[date]] = {}

        for concept in DURATION_CONCEPTS:
            values = _duration_values(facts.get(concept, []), concept)
            self._annuals[concept] = _annual_values(values, self.config)
            self._halves[concept] = _annual_values(
                values, {"annual_days": self.config["half_year_days"]},
            )
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
            # Concepts whose tags add up keep every tag; the rest keep one.
            self._instants[concept] = (
                instants if _combine(concept) != "alias" else _prefer_best_tag(instants)
            )
            self._instant_ends[concept] = sorted(self._instants[concept])
        self._share_averages = _weighted_share_values(
            facts.get("weighted_average_shares", []), self.config,
        )
        self._share_average_ends = sorted(self._share_averages)

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
        return (as_of - latest).days > self.maximum_age_as_of(as_of)

    def reports_quarterly(self, as_of: date) -> bool:
        """True unless the latest known fiscal year was reported annually only.

        A filer is annual when its most recent annual figure known on
        ``as_of`` has no quarterly figures inside that fiscal year. A
        quarterly filer that has gone silent is still a quarterly filer, so
        its figures go stale at the ordinary limit.
        """
        latest_annual = None
        for concept in DURATION_CONCEPTS:
            annuals = self.annuals_as_of(concept, as_of)
            if annuals and (latest_annual is None or annuals[-1].end > latest_annual):
                latest_annual = annuals[-1].end
        if latest_annual is None:
            return True
        year_start = latest_annual - timedelta(days=int(self.config["annual_days"][1]))
        for concept in DURATION_CONCEPTS:
            for quarter in self.quarters_as_of(concept, as_of):
                if year_start < quarter.end <= latest_annual:
                    return True
        return False

    def maximum_age_as_of(self, as_of: date) -> int:
        """The report-age limit for this filer on this date."""
        if self.reports_quarterly(as_of):
            return int(self.config["maximum_report_age_days"])
        return int(self.config["maximum_annual_report_age_days"])

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

    def debt_component(
        self,
        concept: str,
        balance_end: date,
        as_of: date,
    ) -> float | None:
        """One debt concept on a balance-sheet date, its tags combined.

        Each tag contributes its latest value known on ``as_of`` dated within
        a few days before ``balance_end``. Older balance sheets never count.
        """
        earliest = balance_end - timedelta(days=DEBT_DATE_TOLERANCE_DAYS)
        by_tag: dict[int, PeriodValue] = {}
        for end, entries in self._instants.get(concept, {}).items():
            if not earliest <= end <= balance_end:
                continue
            for entry in entries:
                if entry.filed > as_of:
                    continue
                kept = by_tag.get(entry.priority)
                if kept is None or (entry.end, entry.filed) > (kept.end, kept.filed):
                    by_tag[entry.priority] = entry
        if not by_tag:
            return None
        mode = _combine(concept)
        if mode == "sum":
            return sum(item.value for item in by_tag.values())
        if mode == "max":
            return max(item.value for item in by_tag.values())
        return by_tag[min(by_tag)].value

    def total_debt(self, balance_end: date, as_of: date) -> tuple[float, str]:
        """Total debt on a balance-sheet date, and how it was found.

        The filer's own total wins. Otherwise three readings that each cover
        a different slice are compared and the largest is used: the broad
        total tags, noncurrent plus current debt (each side the larger of its
        one-figure tag and its instruments added up), and credit lines plus
        notes reported without a split. A balance sheet with no debt tag at
        all gives 0 with coverage "none_found": the filer is debt free or
        uses a tag this extract does not read.
        """
        def part(concept: str) -> float | None:
            return self.debt_component(concept, balance_end, as_of)

        reported = part("debt_total_reported")
        if reported is not None:
            return reported, "reported_total"
        noncurrent = _largest(part("long_term_debt"), part("debt_noncurrent_parts"))
        current = _largest(part("short_term_debt"), part("debt_current_parts"))
        lines, notes = part("debt_credit_lines"), part("debt_notes")
        readings = [
            part("debt_broad"),
            _total(noncurrent, current),
            _total(lines, notes),
        ]
        known = [reading for reading in readings if reading is not None]
        if not known:
            return 0.0, "none_found"
        return max(known), "components"

    def weighted_shares_as_of(
        self,
        as_of: date,
        latest_end: date | None = None,
    ) -> PeriodValue | None:
        """Latest weighted-average basic share count known on ``as_of``."""
        cap = min(as_of, latest_end) if latest_end is not None else as_of
        position = bisect_right(self._share_average_ends, cap)
        for index in range(position - 1, -1, -1):
            chosen = _latest_known(self._share_averages[self._share_average_ends[index]], as_of)
            if chosen is not None:
                return chosen
        return None

    def share_count_as_of(
        self,
        as_of: date,
        latest_end: date | None = None,
        basis: str | None = None,
    ) -> tuple[PeriodValue | None, str | None]:
        """The share count to value the company with, and its basis.

        The period-end count is preferred. The weighted-average count is used
        when the period-end count is missing or older than the report-age
        limit, or when the weighted-average count is more than 20% larger.
        That is what one class of a multi-class company looks like (GTEC,
        Oct 2026: Class A alone, 20.5M against 26.5M in total), and what
        pre-funded warrants look like, which count as shares in the average
        and are shares in substance. A buyback of that size in one quarter is
        rare, and splits are restated after the count is chosen. ``basis`` forces
        one source, so a year-ago count can match the current one's basis.
        """
        maximum_age = self.maximum_age_as_of(as_of)
        reference = latest_end or as_of
        period_end = self.instant_as_of("shares_outstanding", as_of, latest_end=latest_end)
        if period_end is not None and (
            (reference - period_end.end).days > maximum_age or period_end.value <= 0
        ):
            period_end = None
        average = self.weighted_shares_as_of(as_of, latest_end=latest_end)
        if average is not None and (
            (reference - average.end).days > maximum_age or average.value <= 0
        ):
            average = None
        if basis == "period_end":
            return period_end, "period_end" if period_end else None
        if basis == "weighted_average":
            return average, "weighted_average" if average else None
        if (
            period_end is not None and average is not None
            and average.value > period_end.value * WEIGHTED_SHARE_SCALE_ERROR
        ):
            average = None
        if period_end is not None and not (
            average is not None and period_end.value * WEIGHTED_SHARE_RATIO < average.value
        ):
            return period_end, "period_end"
        if average is not None:
            return average, "weighted_average"
        return None, None

    def ttm(self, concept: str, as_of: date, offset: int = 0) -> tuple[tuple[date, ...], float] | None:
        """Sum of four consecutive quarters ending ``offset`` quarters back.

        Falls back to a reported annual figure when quarterly data is absent
        (annual-only filers); ``offset`` must then be a multiple of four.
        """
        quarters = self.quarters_as_of(concept, as_of)
        end_index = len(quarters) - offset
        start_index = end_index - 4
        quarterly = None
        if start_index >= 0:
            window = quarters[start_index:end_index]
            if _consecutive(window, self.config) and _same_tag(window):
                ratio, restated_on = self._restatement_ratio(concept, quarters)
                total = sum(
                    item.value * (ratio if item.filed < restated_on else 1.0)
                    for item in window
                ) if ratio is not None else sum(item.value for item in window)
                quarterly = tuple(item.end for item in window), float(total)
        annual = None
        if offset % 4 == 0:
            annuals = self.annuals_as_of(concept, as_of)
            index = len(annuals) - 1 - offset // 4
            if index >= 0:
                annual = (annuals[index].end,), float(annuals[index].value)
        half = self._half_year_ttm(concept, as_of, offset // 4) if offset % 4 == 0 else None
        # A filer that stopped reporting quarters (or reported them years ago)
        # must not have a stale quarterly window beat a current annual figure.
        # On equal end dates the quarters win, then the annual figure.
        candidates = [result for result in (quarterly, annual, half) if result is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda result: result[0][-1])

    def halves_as_of(self, concept: str, as_of: date) -> list[PeriodValue]:
        """Half-year values known on ``as_of``, latest filing per period."""
        known = []
        for entries in self._halves.get(concept, []):
            chosen = _latest_known(entries, as_of)
            if chosen is not None and chosen.end <= as_of:
                known.append(chosen)
        return known

    def _half_year_ttm(
        self, concept: str, as_of: date, years_back: int = 0,
    ) -> tuple[tuple[date, ...], float] | None:
        """Trailing year for a filer that reports half-years.

        The fiscal year's first half plus the year before it, less the same
        half a year earlier (GLBS, Oct 2026: 44.2 + 26.9 - 18.2 = $52.9M, as
        its own report states). The half must start the day after a fiscal
        year ends, and all three figures must share one tag.
        """
        tolerance = int(self.config["period_tolerance_days"])
        annuals = self.annuals_as_of(concept, as_of)
        halves = self.halves_as_of(concept, as_of)
        built = []
        for half in halves:
            year = next((
                annual for annual in reversed(annuals)
                if abs((half.start - annual.end).days - 1) <= tolerance
            ), None)
            if year is None:
                continue
            prior = next((
                other for other in halves
                if abs((half.end - other.end).days - 365) <= tolerance
                and abs((half.start - other.start).days - 365) <= tolerance
            ), None)
            if prior is None or len({year.priority, half.priority, prior.priority}) > 1:
                continue
            built.append(((half.end,), float(year.value + half.value - prior.value)))
        if not built:
            return None
        latest_end = built[-1][0][-1]
        target = latest_end - timedelta(days=365 * years_back)
        for result in reversed(built):
            if abs((result[0][-1] - target).days) <= tolerance:
                return result
        return None

    def _restatement_ratio(
        self, concept: str, quarters: list[PeriodValue],
    ) -> tuple[float | None, date | None]:
        """How far the newest filing restated last year's same quarter.

        When a filing moves a business to discontinued operations it restates
        the comparable quarter a year back, but quarters filed earlier stay on
        the old basis, so a trailing sum mixes the two (CNDT, Oct 2026: $2.79B
        against about $2.33B on one basis). Quarters filed before the
        restating filing are scaled by the comparable quarter's new/old ratio.
        Only revenue is rescaled, only for a change above 5%, and only when
        both values are positive.
        """
        if concept not in RESCALED_CONCEPTS or len(quarters) < 5:
            return None, None
        latest, comparable = quarters[-1], quarters[-5]
        if comparable.filed != latest.filed or not _consecutive(quarters[-5:], self.config):
            return None, None
        before = _latest_known(
            self._quarters[concept][comparable.end], latest.filed - timedelta(days=1),
        )
        if before is None or before.value <= 0 or comparable.value <= 0:
            return None, None
        ratio = comparable.value / before.value
        if abs(ratio - 1.0) <= RESTATEMENT_THRESHOLD:
            return None, None
        return ratio, latest.filed

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
        splits: SplitHistory | None = None,
    ) -> dict:
        """Derive every point-in-time signal for a calendar date.

        Valuation ratios use ``shares_outstanding * price`` when a price is
        given; otherwise an explicit ``market_cap`` is used when supplied.
        Share counts are restated into the price's basis when ``splits`` is
        supplied, since the price series is split-adjusted and the filing's
        count is not.
        """
        as_of = as_of.date() if isinstance(as_of, datetime) else as_of
        signals: dict[str, object] = {name: None for name in SIGNAL_NAMES}
        signals["pit_reporting_currency"] = self.currency
        signals["pit_data_quality"] = self.data_quality
        maximum_age = self.maximum_age_as_of(as_of)

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
            self._two_year_fcf(signals, as_of, ocf[0], free_cash_flow)

        cash = self.instant_as_of("cash", as_of)
        if cash is not None and (as_of - cash.end).days <= maximum_age:
            signals["pit_cash"] = cash.value
            debt, coverage = self.total_debt(cash.end, as_of)
            signals["pit_total_debt"] = debt
            signals["pit_debt_coverage"] = coverage
            signals["pit_net_cash"] = cash.value - debt

        shares, shares_basis = self.share_count_as_of(as_of)
        if shares is not None:
            splits = splits or SplitHistory()
            # Restate into the price series' basis. The whole series is
            # adjusted to its download date, not to the as-of date, so every
            # split after the share count counts -- including ones that fall
            # after as_of, which the historical closes already reflect.
            factor = splits.factor_after(shares.end)
            adjusted_shares = shares.value * factor
            signals["pit_shares_outstanding"] = adjusted_shares
            signals["pit_shares_basis"] = shares_basis
            signals["pit_split_factor"] = factor
            signals["pit_split_ambiguous"] = splits.has_event_between(
                shares.end, shares.filed,
            )
            # The year-ago count comes from the same basis, or a switch from
            # weighted to period-end counts would read as issuance.
            year_ago, _ = self.share_count_as_of(
                as_of, latest_end=shares.end - timedelta(days=350), basis=shares_basis,
            )
            if year_ago is not None and year_ago.value > 0 and (
                shares.end - year_ago.end
            ).days <= maximum_age + 30:
                # Both counts go onto one basis, or a split inside the year
                # reads as dilution that never happened.
                prior_factor = splits.factor_after(year_ago.end)
                signals["pit_share_change_1y"] = (
                    adjusted_shares / (year_ago.value * prior_factor) - 1.0
                )
            resolved, basis = self._reconciled_market_cap(
                adjusted_shares, price, market_cap,
            )
            if resolved is not None:
                signals["pit_market_cap"] = resolved
                signals["pit_market_cap_basis"] = basis
                if basis == "reported_shares_unreconciled":
                    # The share count is on a different basis from the price,
                    # so the year-over-year share ratio spans that break too.
                    signals["pit_share_change_1y"] = None
                two_year = signals.pop("_fcf_2y", None)
                low = signals.pop("_fcf_low", None)
                if two_year is not None:
                    signals["pit_fcf_yield_2y"] = two_year / resolved
                    signals["pit_fcf_yield_low"] = low / resolved
                for name, source in (
                    ("pit_net_cash_to_market_cap", "pit_net_cash"),
                    ("pit_fcf_yield", "pit_free_cash_flow_ttm"),
                    ("pit_earnings_yield", "pit_net_income_ttm"),
                    ("pit_sales_yield", "pit_revenue_ttm"),
                ):
                    value = signals[source]
                    if value is not None:
                        signals[name] = value / resolved

        signals.pop("_fcf_2y", None)
        signals.pop("_fcf_low", None)
        return {
            name: (round(value, 6) if isinstance(value, float) else value)
            for name, value in signals.items()
        }

    def _two_year_fcf(
        self, signals: dict, as_of: date, ends: tuple[date, ...], free_cash_flow: float,
    ) -> None:
        """Free cash flow a year earlier and the two variants built on it.

        Temporary ``_fcf_2y`` and ``_fcf_low`` carry the money amounts until
        a market cap is known. Without a usable prior year every variant
        equals the one-year figure, so coverage matches ``pit_fcf_*``.
        """
        revenue = self._aligned_ttm("revenue", as_of, ends)
        margin = free_cash_flow / revenue if revenue is not None and revenue > 0 else None
        prior_ocf = self.ttm("operating_cash_flow", as_of, offset=4)
        prior_fcf = prior_revenue = None
        if prior_ocf is not None and 330 <= (ends[-1] - prior_ocf[0][-1]).days <= 400:
            prior_capex = self.ttm("capital_expenditure", as_of, offset=4)
            capex = prior_capex[1] if prior_capex is not None and prior_capex[0] == prior_ocf[0] else 0.0
            prior_fcf = prior_ocf[1] - capex
            prior_sales = self.ttm("revenue", as_of, offset=4)
            if prior_sales is not None and prior_sales[0] == prior_ocf[0]:
                prior_revenue = prior_sales[1]
        signals["pit_free_cash_flow_prior_ttm"] = prior_fcf
        if prior_fcf is None:
            signals["_fcf_2y"] = signals["_fcf_low"] = free_cash_flow
            signals["pit_fcf_margin_2y"] = signals["pit_fcf_margin_low"] = margin
            return
        signals["_fcf_2y"] = (free_cash_flow + prior_fcf) / 2.0
        signals["_fcf_low"] = min(free_cash_flow, prior_fcf)
        if margin is None:
            return
        if prior_revenue is not None and prior_revenue > 0:
            signals["pit_fcf_margin_2y"] = (free_cash_flow + prior_fcf) / (revenue + prior_revenue)
            signals["pit_fcf_margin_low"] = min(margin, prior_fcf / prior_revenue)
        else:
            signals["pit_fcf_margin_2y"] = signals["pit_fcf_margin_low"] = margin

    def _reconciled_market_cap(
        self,
        shares: float,
        price: float | None,
        supplied: float | None,
    ) -> tuple[float | None, str | None]:
        """Pick a market cap and say where it came from.

        ``shares * price`` is the only construction that produces a market
        cap, because it is contemporaneous with the price and available in
        historical replay. The supplied figure is a cross-check, not a
        substitute: when the two disagree by more than the configured factor
        the share count is on a different basis from the price and the
        supplied figure replaces it, but with no price there is no market cap
        and no valuation ratios. A name nobody can price is a name this model
        was never fitted on and nobody can trade.
        """
        derived = (
            shares * price
            if price is not None and math.isfinite(price) and price > 0
            and math.isfinite(shares) and shares > 0
            else None
        )
        supplied = (
            supplied
            if supplied is not None and math.isfinite(supplied) and supplied > 0
            else None
        )
        if derived is None:
            return None, None
        if supplied is None:
            return derived, "reported_shares"
        tolerance = float(self.config["market_cap_reconciliation_tolerance"])
        ratio = derived / supplied
        if ratio >= tolerance or ratio <= 1.0 / tolerance:
            return supplied, "reported_shares_unreconciled"
        return derived, "reported_shares"

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


def is_proxy_form(form: object) -> bool:
    """A proxy or information statement (DEF 14A, PRER14A, DEFM14A, PRE 14C...).

    Proxies carry a pay-versus-performance table tagged with the same
    concepts as the financial statements, sometimes with the sign or scale
    changed. Being filed after the annual report, such a figure would read as
    a restatement and replace the real one, as it did at INGN, where the
    proxy's +$22.7M replaced a -$22.7M annual loss.
    """
    text = str(form or "").strip().upper()
    if text.endswith("/A"):
        text = text[:-2]
    return text.endswith(("14A", "14C"))


def _without_proxy_figures(facts: dict) -> dict:
    """Every concept's facts minus those taken from proxy filings."""
    if not isinstance(facts, dict):
        return {}
    return {
        concept: [row for row in rows if not is_proxy_form(row.get("form"))]
        if isinstance(rows, list) else rows
        for concept, rows in facts.items()
    }


def _dominant_currency(facts: dict, declared: object) -> str | None:
    """Pick the money unit with the most facts, or the declared currency."""
    counts: dict[str, int] = {}
    for concept, rows in facts.items():
        if concept in SHARE_CONCEPTS:
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
        if concept in SHARE_CONCEPTS:
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


def _weighted_share_values(rows: list[dict], config: dict) -> dict[date, list[PeriodValue]]:
    """Weighted-average share counts keyed by period end.

    A quarter's figure is kept over a year-to-date or annual one ending on the
    same date, because it is the most recent average. Averages are never
    differenced the way flows are: the difference of two averages is not an
    average of anything.
    """
    quarter_min, quarter_max = config["quarter_days"]
    by_end: dict[date, list[PeriodValue]] = {}
    for item in _duration_values(rows, "weighted_average_shares"):
        if (item.end - item.start).days > WEIGHTED_SHARES_MAX_DAYS:
            continue
        by_end.setdefault(item.end, []).append(item)
    chosen: dict[date, list[PeriodValue]] = {}
    for end, entries in by_end.items():
        quarters = [
            entry for entry in entries
            if quarter_min <= (entry.end - entry.start).days <= quarter_max
        ]
        pool = quarters or entries
        best = min(entry.priority for entry in pool)
        kept = [entry for entry in pool if entry.priority == best]
        kept.sort(key=lambda entry: entry.filed)
        chosen[end] = kept
    return chosen


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


def _combine(concept: str) -> str:
    """How a concept's tags combine for one date: alias, sum or max."""
    return str(CONCEPTS.get(concept, {}).get("combine", "alias"))


def _largest(*values: float | None) -> float | None:
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _total(*values: float | None) -> float | None:
    """Sum of the known values, or None when none is known."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


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
