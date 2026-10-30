from datetime import date
import unittest

from src.fundamentals_pit import FundamentalHistory, SIGNAL_NAMES, is_proxy_form
from src.split_adjustment import SplitEvent, SplitHistory


def _fact(start, end, filed, val):
    return {"start": start, "end": end, "filed": filed, "val": val}


def _instant(end, filed, val):
    return {"start": None, "end": end, "filed": filed, "val": val}


def _extract():
    """A filer with two fiscal years of quarterly revenue plus balance data.

    FY2024 reports Q1-Q3 directly and a nine-month cumulative figure, so Q4 is
    derived by differencing FY - 9M. FY2025 reports Q1-Q3 directly with no
    cumulative figure, so Q4 is derived as FY - (Q1 + Q2 + Q3). Q2 2025 is
    restated upward in a later filing.
    """
    revenue = [
        # FY2024 quarters: 100, 110, 120, and derived Q4 = 470 - 330 = 140
        _fact("2024-01-01", "2024-03-31", "2024-05-01", 100),
        _fact("2024-01-01", "2024-06-30", "2024-08-01", 210),
        _fact("2024-04-01", "2024-06-30", "2024-08-01", 110),
        _fact("2024-01-01", "2024-09-30", "2024-11-01", 330),
        _fact("2024-07-01", "2024-09-30", "2024-11-01", 120),
        _fact("2024-01-01", "2024-12-31", "2025-02-15", 470),
        # FY2025 quarters: 130, 150 (restated to 160), 170, derived Q4 = 640 - 450
        _fact("2025-01-01", "2025-03-31", "2025-05-01", 130),
        _fact("2025-04-01", "2025-06-30", "2025-08-01", 150),
        _fact("2025-04-01", "2025-06-30", "2025-11-01", 160),
        _fact("2025-07-01", "2025-09-30", "2025-11-01", 170),
        _fact("2025-01-01", "2025-12-31", "2026-02-15", 640),
    ]
    gross_profit = [
        _fact(start, end, filed, val * 0.5)
        for start, end, filed, val in [
            ("2024-01-01", "2024-03-31", "2024-05-01", 100),
            ("2024-04-01", "2024-06-30", "2024-08-01", 110),
            ("2024-07-01", "2024-09-30", "2024-11-01", 120),
            ("2024-10-01", "2024-12-31", "2025-02-15", 140),
            ("2025-01-01", "2025-03-31", "2025-05-01", 130),
            ("2025-04-01", "2025-06-30", "2025-08-01", 150),
            ("2025-07-01", "2025-09-30", "2025-11-01", 170),
            ("2025-10-01", "2025-12-31", "2026-02-15", 190),
        ]
    ]
    net_income = [
        _fact(start, end, filed, 10)
        for start, end, filed in [
            ("2025-01-01", "2025-03-31", "2025-05-01"),
            ("2025-04-01", "2025-06-30", "2025-08-01"),
            ("2025-07-01", "2025-09-30", "2025-11-01"),
            ("2025-10-01", "2025-12-31", "2026-02-15"),
        ]
    ]
    ocf = [
        _fact(start, end, filed, 15)
        for start, end, filed in [
            ("2025-01-01", "2025-03-31", "2025-05-01"),
            ("2025-04-01", "2025-06-30", "2025-08-01"),
            ("2025-07-01", "2025-09-30", "2025-11-01"),
            ("2025-10-01", "2025-12-31", "2026-02-15"),
        ]
    ]
    capex = [
        _fact(start, end, filed, 5)
        for start, end, filed in [
            ("2025-01-01", "2025-03-31", "2025-05-01"),
            ("2025-04-01", "2025-06-30", "2025-08-01"),
            ("2025-07-01", "2025-09-30", "2025-11-01"),
            ("2025-10-01", "2025-12-31", "2026-02-15"),
        ]
    ]
    return {
        "ticker": "TEST",
        "facts": {
            "revenue": revenue,
            "gross_profit": gross_profit,
            "net_income": net_income,
            "operating_cash_flow": ocf,
            "capital_expenditure": capex,
            "cash": [
                _instant("2024-12-31", "2025-02-15", 500),
                _instant("2025-12-31", "2026-02-15", 800),
            ],
            "long_term_debt": [_instant("2025-12-31", "2026-02-15", 200)],
            "short_term_debt": [_instant("2025-12-31", "2026-02-15", 50)],
            "shares_outstanding": [
                _instant("2024-12-31", "2025-02-15", 1000),
                _instant("2025-12-31", "2026-02-15", 1100),
            ],
        },
    }


class QuarterDerivationTests(unittest.TestCase):
    def setUp(self):
        self.history = FundamentalHistory(_extract())

    def test_derives_fourth_quarters_by_both_methods(self):
        quarters = self.history.quarters_as_of("revenue", date(2026, 3, 1))
        values = {item.end.isoformat(): item.value for item in quarters}
        self.assertEqual(values["2024-12-31"], 140.0)  # FY - 9M
        self.assertEqual(values["2025-12-31"], 180.0)  # FY - (Q1+Q2+Q3) with restated Q2
        self.assertEqual(len(quarters), 8)

    def test_restatements_are_only_visible_after_their_filing(self):
        before = self.history.quarters_as_of("revenue", date(2025, 10, 1))
        after = self.history.quarters_as_of("revenue", date(2025, 11, 2))
        q2_before = next(item for item in before if item.end == date(2025, 6, 30))
        q2_after = next(item for item in after if item.end == date(2025, 6, 30))
        self.assertEqual(q2_before.value, 150.0)
        self.assertEqual(q2_after.value, 160.0)
        # Q3 2025 was filed on 2025-11-01, so it is unknown on 2025-10-01.
        self.assertEqual(before[-1].end, date(2025, 6, 30))

    def test_derived_quarter_inherits_latest_input_filing_date(self):
        # FY2024 filed 2025-02-15: derived Q4 2024 must be invisible before then.
        self.assertEqual(
            self.history.quarters_as_of("revenue", date(2025, 2, 14))[-1].end,
            date(2024, 9, 30),
        )
        self.assertEqual(
            self.history.quarters_as_of("revenue", date(2025, 2, 15))[-1].end,
            date(2024, 12, 31),
        )


class TagPrecedenceTests(unittest.TestCase):
    def test_total_tag_beats_component_tag_for_the_same_period(self):
        extract = {"ticker": "T", "facts": {"revenue": [
            {**_fact("2025-01-01", "2025-03-31", "2025-05-01", 6_000_000),
             "tag": "us-gaap:Revenues"},
            {**_fact("2025-01-01", "2025-03-31", "2025-05-01", 200_000),
             "tag": "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax"},
            # A period reported only under the lower-priority tag still counts.
            {**_fact("2025-04-01", "2025-06-30", "2025-08-01", 250_000),
             "tag": "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax"},
            # Cumulative and component cumulative must not be differenced together.
            {**_fact("2025-01-01", "2025-09-30", "2025-11-01", 18_000_000),
             "tag": "us-gaap:Revenues"},
            {**_fact("2025-01-01", "2025-06-30", "2025-08-01", 400_000),
             "tag": "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax"},
        ]}}
        quarters = FundamentalHistory(extract).quarters_as_of("revenue", date(2026, 1, 1))
        values = {q.end.isoformat(): q.value for q in quarters}
        self.assertEqual(values["2025-03-31"], 6_000_000.0)
        self.assertEqual(values["2025-06-30"], 250_000.0)
        # 9M total minus H1 component is not a valid quarter.
        self.assertNotIn("2025-09-30", values)


def _debt_history(debt_facts, cash_end="2026-06-30", filed="2026-08-05"):
    """A filer with cash and the given debt facts: {concept: [(tag, end, val)]}."""
    facts = {"cash": [{**_instant(cash_end, filed, 100.0), "tag": "us-gaap:Cash"}]}
    for concept, rows in debt_facts.items():
        facts[concept] = [
            {**_instant(end, row_filed, val), "tag": tag}
            for tag, end, val, *rest in rows
            for row_filed in [rest[0] if rest else filed]
        ]
    return FundamentalHistory({"ticker": "T", "facts": facts})


def _debt(history, as_of=date(2026, 9, 1)):
    signals = history.signals_as_of(as_of)
    return signals["pit_total_debt"], signals["pit_debt_coverage"]


class DebtTests(unittest.TestCase):
    """Cases from the AI review dry run, where debt was missing at 14 of 45 names."""

    END = "2026-06-30"

    def test_filers_own_total_wins_over_a_current_portion(self):
        # CNDT and PKOH: only the $21M current portion was read before.
        history = _debt_history({
            "debt_total_reported": [("us-gaap:DebtAndCapitalLeaseObligations", self.END, 722.0)],
            "debt_current_parts": [("us-gaap:LongTermDebtCurrent", self.END, 21.0)],
            "debt_notes": [("us-gaap:OtherLongTermDebt", self.END, 12.0)],
        })
        self.assertEqual(_debt(history), (722.0, "reported_total"))

    def test_credit_facility_tag_counts(self):
        # TTEC: everything sits in LongTermLineOfCredit.
        history = _debt_history({
            "debt_noncurrent_parts": [("us-gaap:LongTermLineOfCredit", self.END, 855.0)],
        })
        self.assertEqual(_debt(history), (855.0, "components"))

    def test_convertibles_on_both_sides_add_up(self):
        # RPD: $598M current and $296M noncurrent convertible notes.
        history = _debt_history({
            "debt_current_parts": [("us-gaap:ConvertibleDebtCurrent", self.END, 598.2)],
            "debt_noncurrent_parts": [("us-gaap:ConvertibleDebtNoncurrent", self.END, 296.0)],
        })
        self.assertAlmostEqual(_debt(history)[0], 894.2)

    def test_credit_line_and_notes_without_a_split_add_up(self):
        # RMR: a $25M revolver and $138.8M of mortgage notes.
        history = _debt_history({
            "debt_credit_lines": [("us-gaap:LineOfCredit", self.END, 25.0)],
            "debt_notes": [("us-gaap:NotesPayable", self.END, 138.8)],
            "debt_noncurrent_parts": [("us-gaap:SecuredLongTermDebt", self.END, 0.0)],
            "debt_current_parts": [("us-gaap:SecuredDebtCurrent", self.END, 0.0)],
        })
        self.assertAlmostEqual(_debt(history)[0], 163.8)

    def test_overlapping_note_tags_are_not_double_counted(self):
        history = _debt_history({
            "debt_notes": [
                ("us-gaap:NotesPayable", self.END, 100.0),
                ("us-gaap:SeniorNotes", self.END, 80.0),
            ],
        })
        self.assertEqual(_debt(history)[0], 100.0)

    def test_ifrs_parts_beat_a_narrow_borrowings_total(self):
        # HITI: Borrowings (13.4) leaves out debentures and short-term loans.
        history = _debt_history({
            "debt_broad": [("ifrs-full:Borrowings", self.END, 13.4)],
            "debt_noncurrent_parts": [
                ("ifrs-full:LongtermBorrowings", self.END, 11.9),
                ("ifrs-full:NoncurrentDebtInstrumentsIssued", self.END, 17.9),
            ],
            "debt_current_parts": [
                ("ifrs-full:CurrentPortionOfLongtermBorrowings", self.END, 1.5),
                ("ifrs-full:ShorttermBorrowings", self.END, 16.2),
            ],
        })
        self.assertAlmostEqual(_debt(history)[0], 47.5)

    def test_current_portion_and_short_term_borrowings_add_up(self):
        # Before, the short-term side kept only the first of these tags.
        history = _debt_history({
            "long_term_debt": [("us-gaap:LongTermDebtNoncurrent", self.END, 300.0)],
            "debt_current_parts": [
                ("us-gaap:LongTermDebtCurrent", self.END, 20.0),
                ("us-gaap:ShortTermBorrowings", self.END, 30.0),
            ],
        })
        self.assertEqual(_debt(history)[0], 350.0)

    def test_one_figure_side_beats_smaller_instrument_sum(self):
        # NTWK: the current aggregate (8.2) already includes both parts.
        history = _debt_history({
            "long_term_debt": [("us-gaap:LongTermDebtNoncurrent", self.END, 0.2)],
            "short_term_debt": [
                ("us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent", self.END, 8.2),
            ],
            "debt_current_parts": [
                ("us-gaap:LongTermDebtCurrent", self.END, 0.1),
                ("us-gaap:LoansPayableCurrent", self.END, 8.1),
            ],
            "debt_noncurrent_parts": [("us-gaap:LongTermLoansPayable", self.END, 0.2)],
            "debt_notes": [("us-gaap:LoansPayable", self.END, 8.3)],
        })
        self.assertAlmostEqual(_debt(history)[0], 8.4)

    def test_debt_from_an_older_balance_sheet_is_ignored(self):
        # RMR: a nine-month-old LongTermDebt figure was used before.
        history = _debt_history({
            "debt_broad": [("us-gaap:LongTermDebt", "2025-09-30", 44.6, "2025-11-05")],
        })
        self.assertEqual(_debt(history), (0.0, "none_found"))

    def test_no_debt_tag_counts_as_zero_and_says_so(self):
        self.assertEqual(_debt(_debt_history({})), (0.0, "none_found"))

    def test_debt_filed_after_the_date_is_unknown(self):
        history = _debt_history({
            "debt_noncurrent_parts": [
                ("us-gaap:LongTermLineOfCredit", self.END, 855.0, "2026-10-01"),
            ],
        })
        self.assertEqual(_debt(history, as_of=date(2026, 9, 1)), (0.0, "none_found"))
        self.assertEqual(_debt(history, as_of=date(2026, 10, 2))[0], 855.0)

    def test_restated_part_uses_its_latest_known_filing(self):
        history = _debt_history({
            "debt_current_parts": [
                ("us-gaap:LongTermDebtCurrent", self.END, 20.0, "2026-08-05"),
                ("us-gaap:LongTermDebtCurrent", self.END, 25.0, "2026-09-15"),
                ("us-gaap:ShortTermBorrowings", self.END, 30.0, "2026-08-05"),
            ],
        })
        self.assertEqual(_debt(history, as_of=date(2026, 9, 1))[0], 50.0)
        self.assertEqual(_debt(history, as_of=date(2026, 9, 20))[0], 55.0)

    def test_reported_zero_is_coverage_not_absence(self):
        history = _debt_history({
            "short_term_debt": [("us-gaap:DebtCurrent", self.END, 0.0)],
        })
        self.assertEqual(_debt(history), (0.0, "components"))


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.history = FundamentalHistory(_extract())

    def test_ttm_growth_margins_and_acceleration(self):
        signals = self.history.signals_as_of(date(2026, 3, 1), price=2.0)

        self.assertEqual(signals["pit_revenue_ttm"], 640.0)
        self.assertAlmostEqual(signals["pit_revenue_growth_ttm"], 640 / 470 - 1, places=6)
        # Q4 YoY (180/140) minus Q3 YoY (170/120).
        self.assertAlmostEqual(
            signals["pit_revenue_acceleration"], 180 / 140 - 170 / 120, places=6,
        )
        self.assertAlmostEqual(signals["pit_gross_margin_ttm"], 0.5, places=6)
        self.assertAlmostEqual(signals["pit_gross_margin_change"], 0.0, places=6)
        self.assertEqual(signals["pit_net_income_ttm"], 40.0)
        self.assertEqual(signals["pit_operating_cash_flow_ttm"], 60.0)
        self.assertEqual(signals["pit_free_cash_flow_ttm"], 40.0)
        self.assertAlmostEqual(signals["pit_fcf_margin_ttm"], 40 / 640, places=6)
        self.assertAlmostEqual(signals["pit_cash_conversion"], 1.5, places=6)
        self.assertEqual(signals["pit_report_age_days"], 60.0)

    def test_balance_sheet_shares_and_market_cap_ratios(self):
        signals = self.history.signals_as_of(date(2026, 3, 1), price=2.0)

        self.assertEqual(signals["pit_cash"], 800.0)
        self.assertEqual(signals["pit_total_debt"], 250.0)
        self.assertEqual(signals["pit_net_cash"], 550.0)
        self.assertEqual(signals["pit_shares_outstanding"], 1100.0)
        self.assertAlmostEqual(signals["pit_share_change_1y"], 0.1, places=6)
        self.assertEqual(signals["pit_market_cap"], 2200.0)
        self.assertAlmostEqual(signals["pit_net_cash_to_market_cap"], 550 / 2200, places=6)
        self.assertAlmostEqual(signals["pit_fcf_yield"], 40 / 2200, places=6)
        self.assertAlmostEqual(signals["pit_earnings_yield"], 40 / 2200, places=6)
        self.assertAlmostEqual(signals["pit_sales_yield"], 640 / 2200, places=6)

    def test_signals_use_only_filings_known_on_the_date(self):
        signals = self.history.signals_as_of(date(2025, 6, 1), price=2.0)

        # Known quarters: Q1-Q4 2024 and Q1 2025 -> TTM ends 2025-03-31.
        self.assertEqual(signals["pit_revenue_ttm"], 100 + 110 + 120 + 140 - 100 + 130)
        self.assertIsNone(signals["pit_revenue_growth_ttm"])  # prior TTM incomplete
        self.assertIsNone(signals["pit_net_income_ttm"])  # only one quarter known
        self.assertEqual(signals["pit_cash"], 500.0)
        self.assertEqual(signals["pit_total_debt"], 0.0)  # no debt reported yet
        self.assertEqual(signals["pit_debt_coverage"], "none_found")
        self.assertEqual(signals["pit_shares_outstanding"], 1000.0)
        self.assertIsNone(signals["pit_share_change_1y"])

    def test_stale_reports_produce_no_signals(self):
        signals = self.history.signals_as_of(date(2027, 6, 1), price=2.0)
        self.assertIsNone(signals["pit_revenue_ttm"])
        self.assertIsNone(signals["pit_cash"])
        self.assertIsNone(signals["pit_market_cap"])

    def test_missing_price_skips_market_cap_ratios_only(self):
        signals = self.history.signals_as_of(date(2026, 3, 1))
        self.assertIsNone(signals["pit_market_cap"])
        self.assertIsNone(signals["pit_fcf_yield"])
        self.assertEqual(signals["pit_free_cash_flow_ttm"], 40.0)

    def test_two_year_fcf_falls_back_to_one_year_without_a_prior_year(self):
        signals = self.history.signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertIsNone(signals["pit_free_cash_flow_prior_ttm"])
        for name in ("pit_fcf_yield_2y", "pit_fcf_yield_low"):
            self.assertAlmostEqual(signals[name], signals["pit_fcf_yield"], places=6)
        for name in ("pit_fcf_margin_2y", "pit_fcf_margin_low"):
            self.assertAlmostEqual(signals[name], signals["pit_fcf_margin_ttm"], places=6)
        self.assertNotIn("_fcf_2y", signals)

    def test_two_year_fcf_damps_a_one_off_year(self):
        extract = _extract()
        quarters_2024 = [
            ("2024-01-01", "2024-03-31", "2024-05-01"),
            ("2024-04-01", "2024-06-30", "2024-08-01"),
            ("2024-07-01", "2024-09-30", "2024-11-01"),
            ("2024-10-01", "2024-12-31", "2025-02-15"),
        ]
        # 2024 operating cash flow 5 a quarter, capex 5: prior FCF is 0.
        extract["facts"]["operating_cash_flow"] = [
            _fact(*dates, 10) for dates in quarters_2024
        ] + extract["facts"]["operating_cash_flow"]
        extract["facts"]["capital_expenditure"] = [
            _fact(*dates, 10) for dates in quarters_2024
        ] + extract["facts"]["capital_expenditure"]
        # A one-off: Q3 2025 operating cash flow jumps from 15 to 415.
        extract["facts"]["operating_cash_flow"] = [
            _fact(start, end, filed, 415 if end == "2025-09-30" else val)
            if start.startswith("2025") else _fact(start, end, filed, val)
            for start, end, filed, val in (
                (row["start"], row["end"], row["filed"], row["val"])
                for row in extract["facts"]["operating_cash_flow"]
            )
        ]
        signals = FundamentalHistory(extract).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_free_cash_flow_ttm"], 440.0)
        self.assertEqual(signals["pit_free_cash_flow_prior_ttm"], 0.0)
        self.assertAlmostEqual(signals["pit_fcf_yield"], 440 / 2200, places=6)
        self.assertAlmostEqual(signals["pit_fcf_yield_2y"], 220 / 2200, places=6)
        self.assertAlmostEqual(signals["pit_fcf_yield_low"], 0.0, places=6)
        self.assertAlmostEqual(signals["pit_fcf_margin_2y"], 440 / (640 + 470), places=6)
        self.assertAlmostEqual(signals["pit_fcf_margin_low"], 0.0, places=6)

    def test_trailing_revenue_is_rescaled_after_a_restatement(self):
        # Q2 2025's filing moves a business to discontinued operations and
        # restates Q2 2024 from 100 to 80; earlier quarters stay on the old basis.
        quarters = [
            ("2023-07-01", "2023-09-30", "2023-11-01", 100),
            ("2023-10-01", "2023-12-31", "2024-02-15", 100),
            ("2024-01-01", "2024-03-31", "2024-05-01", 100),
            ("2024-04-01", "2024-06-30", "2024-08-01", 100),
            ("2024-07-01", "2024-09-30", "2024-11-01", 100),
            ("2024-10-01", "2024-12-31", "2025-02-15", 100),
            ("2025-01-01", "2025-03-31", "2025-05-01", 100),
            ("2024-04-01", "2024-06-30", "2025-08-01", 80),
            ("2025-04-01", "2025-06-30", "2025-08-01", 80),
        ]
        extract = {"ticker": "R", "facts": {
            "revenue": [_fact(*row) for row in quarters],
            "shares_outstanding": [_instant("2025-06-30", "2025-08-01", 100)],
        }}
        history = FundamentalHistory(extract)
        signals = history.signals_as_of(date(2025, 9, 1), price=1.0)
        # Q3 2024, Q4 2024 and Q1 2025 scale by 0.8; Q2 2025 is already restated.
        self.assertAlmostEqual(signals["pit_revenue_ttm"], 320.0, places=6)
        # The year-ago window is on the same basis, so growth is flat, not -20%.
        self.assertAlmostEqual(signals["pit_revenue_growth_ttm"], 0.0, places=6)
        # Before the restating filing, nothing is rescaled.
        self.assertEqual(history.signals_as_of(date(2025, 7, 1))["pit_revenue_ttm"], 400.0)

    def test_proxy_figures_never_replace_the_annual_report(self):
        # A later proxy restates FY2025 net income with the sign flipped.
        extract = _extract()
        extract["facts"]["net_income"] = extract["facts"]["net_income"] + [
            {**_fact("2025-01-01", "2025-12-31", "2026-04-01", -40), "form": "DEF 14A"},
        ]
        extract["facts"]["net_income"].append(
            {**_fact("2025-01-01", "2025-12-31", "2026-04-02", -40), "form": "PRER14A/A"},
        )
        signals = FundamentalHistory(extract).signals_as_of(date(2026, 6, 1), price=2.0)
        self.assertEqual(signals["pit_net_income_ttm"], 40.0)

    def test_proxy_form_names(self):
        for form in ("DEF 14A", "PRE 14A", "PRER14A", "DEFM14A", "DEFA14A", "DEF 14C", "DEFC14A/A"):
            self.assertTrue(is_proxy_form(form), form)
        for form in ("10-K", "10-Q", "10-K/A", "20-F", "8-K", "", None):
            self.assertFalse(is_proxy_form(form), form)

    def test_empty_extract_yields_all_none(self):
        history = FundamentalHistory({"ticker": "X", "facts": {}})
        self.assertFalse(history.has_data)
        signals = history.signals_as_of(date(2026, 1, 1), price=1.0)
        self.assertEqual(set(signals), set(SIGNAL_NAMES))
        self.assertEqual(signals["pit_data_quality"], "filed")
        self.assertIsNone(signals["pit_reporting_currency"])
        numeric = {k: v for k, v in signals.items()
                   if k not in ("pit_data_quality", "pit_reporting_currency")}
        self.assertTrue(all(value is None for value in numeric.values()))

    def test_non_consecutive_quarters_do_not_form_ttm(self):
        extract = _extract()
        extract["facts"]["revenue"] = [
            f for f in extract["facts"]["revenue"] if f["end"] != "2025-03-31"
        ]
        history = FundamentalHistory(extract)
        # Broken quarterly chain: falls back to the latest reported annual figure.
        self.assertEqual(history.ttm("revenue", date(2025, 10, 1)), ((date(2024, 12, 31),), 470.0))
        # With no annual figure known either, there is no TTM.
        self.assertIsNone(history.ttm("revenue", date(2025, 2, 14)))

    def test_annual_only_filers_get_ttm_growth_and_currency(self):
        extract = {"ticker": "FPI", "currency": "CAD", "facts": {"revenue": [
            {"start": "2023-01-01", "end": "2023-12-31", "filed": "2024-03-30", "val": 400.0,
             "tag": "ifrs-full:Revenue", "unit": "CAD"},
            {"start": "2024-01-01", "end": "2024-12-31", "filed": "2025-03-30", "val": 500.0,
             "tag": "ifrs-full:Revenue", "unit": "CAD"},
            {"start": "2024-01-01", "end": "2024-12-31", "filed": "2025-03-30", "val": 370.0,
             "tag": "ifrs-full:Revenue", "unit": "USD"},  # minority currency: dropped
        ]}}
        history = FundamentalHistory(extract)
        self.assertEqual(history.currency, "CAD")
        signals = history.signals_as_of(date(2025, 6, 1))
        self.assertEqual(signals["pit_revenue_ttm"], 500.0)
        self.assertAlmostEqual(signals["pit_revenue_growth_ttm"], 0.25, places=6)
        self.assertEqual(signals["pit_reporting_currency"], "CAD")
        self.assertEqual(signals["pit_report_age_days"], 152.0)


class MarketCapReconciliationTests(unittest.TestCase):
    """The filing's share count and the traded price must agree on units.

    An ADS that bundles ordinary shares, or a reverse split between the
    filing and the price, puts the two on different bases and scales every
    valuation ratio by that factor.
    """

    def setUp(self):
        self.history = FundamentalHistory(_extract())
        # 1,100 shares at 2.0 is the self-consistent case.
        self.consistent = 2200.0

    def test_agreeing_market_cap_prefers_the_contemporaneous_derivation(self):
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, market_cap=2400.0,
        )

        self.assertEqual(signals["pit_market_cap"], self.consistent)
        self.assertEqual(signals["pit_market_cap_basis"], "reported_shares")
        self.assertAlmostEqual(signals["pit_share_change_1y"], 0.1, places=6)

    def test_ads_ratio_mismatch_falls_back_to_the_supplied_market_cap(self):
        # The price is per ADS, each bundling 20 ordinary shares, so the
        # derived cap lands 20x low and every yield would be 20x too high.
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, market_cap=44000.0,
        )

        self.assertEqual(signals["pit_market_cap"], 44000.0)
        self.assertEqual(
            signals["pit_market_cap_basis"], "reported_shares_unreconciled",
        )
        self.assertAlmostEqual(signals["pit_sales_yield"], 640 / 44000, places=6)
        self.assertAlmostEqual(
            signals["pit_net_cash_to_market_cap"], 550 / 44000, places=6,
        )
        # The share series spans the same unit break, so its ratio is dropped.
        self.assertIsNone(signals["pit_share_change_1y"])

    def test_reverse_split_mismatch_is_caught_in_the_other_direction(self):
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, market_cap=110.0,
        )

        self.assertEqual(signals["pit_market_cap"], 110.0)
        self.assertEqual(
            signals["pit_market_cap_basis"], "reported_shares_unreconciled",
        )
        self.assertAlmostEqual(signals["pit_sales_yield"], 640 / 110, places=6)

    def test_no_supplied_market_cap_keeps_the_derived_value(self):
        """Historical replay has no contemporaneous provider cap to check."""
        signals = self.history.signals_as_of(date(2026, 3, 1), price=2.0)

        self.assertEqual(signals["pit_market_cap"], self.consistent)
        self.assertEqual(signals["pit_market_cap_basis"], "reported_shares")

    def test_missing_price_yields_no_market_cap_or_ratios(self):
        """A supplied cap is a cross-check, never a substitute for a price."""
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=None, market_cap=5000.0,
        )

        self.assertIsNone(signals["pit_market_cap"])
        self.assertIsNone(signals["pit_market_cap_basis"])
        self.assertIsNone(signals["pit_sales_yield"])
        self.assertIsNone(signals["pit_net_cash_to_market_cap"])
        # Statement signals that need no market cap survive.
        self.assertEqual(signals["pit_revenue_ttm"], 640.0)
        self.assertEqual(signals["pit_net_cash"], 550.0)

    def test_boundary_factor_is_treated_as_a_unit_mismatch(self):
        """A one-for-two reverse split lands exactly on the tolerance."""
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, market_cap=1100.0,
        )

        self.assertEqual(signals["pit_market_cap"], 1100.0)
        self.assertEqual(
            signals["pit_market_cap_basis"], "reported_shares_unreconciled",
        )


class SplitAdjustedSharesTests(unittest.TestCase):
    """Share counts must be restated into the price series' basis.

    The series is adjusted to its download date, so a split that happens
    *after* the as-of date is already reflected in that date's close. The
    share count from a filing before it is not, and the gap is the whole bug:
    a chronic reverse-splitter's historical market cap is overstated by the
    cumulative ratio, pinning its sales yield to zero on a future event.
    """

    def setUp(self):
        self.history = FundamentalHistory(_extract())
        # 1,100 shares at 2.0, filed 2026-02-15 for period end 2025-12-31.
        self.unadjusted = 2200.0

    def test_split_after_the_as_of_date_still_adjusts(self):
        """The case a naive implementation gets wrong."""
        splits = SplitHistory([SplitEvent(date(2026, 8, 14), 0.05)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, splits=splits,
        )

        self.assertAlmostEqual(signals["pit_shares_outstanding"], 55.0, places=6)
        self.assertAlmostEqual(signals["pit_split_factor"], 0.05, places=9)
        self.assertAlmostEqual(signals["pit_market_cap"], 110.0, places=6)
        self.assertAlmostEqual(signals["pit_sales_yield"], 640 / 110, places=6)

    def test_split_before_the_share_count_is_already_reflected(self):
        splits = SplitHistory([SplitEvent(date(2025, 6, 1), 0.05)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, splits=splits,
        )

        self.assertAlmostEqual(signals["pit_shares_outstanding"], 1100.0, places=6)
        self.assertAlmostEqual(signals["pit_split_factor"], 1.0, places=9)
        self.assertAlmostEqual(signals["pit_market_cap"], self.unadjusted, places=6)

    def test_no_splits_leaves_everything_unchanged(self):
        signals = self.history.signals_as_of(date(2026, 3, 1), price=2.0)

        self.assertAlmostEqual(signals["pit_shares_outstanding"], 1100.0, places=6)
        self.assertAlmostEqual(signals["pit_split_factor"], 1.0, places=9)
        self.assertAlmostEqual(signals["pit_market_cap"], self.unadjusted, places=6)
        self.assertFalse(signals["pit_split_ambiguous"])

    def test_forward_split_raises_the_share_count(self):
        splits = SplitHistory([SplitEvent(date(2026, 2, 20), 3.0)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, splits=splits,
        )

        self.assertAlmostEqual(signals["pit_shares_outstanding"], 3300.0, places=6)
        self.assertAlmostEqual(signals["pit_market_cap"], 6600.0, places=6)

    def test_dilution_is_measured_on_one_basis(self):
        """Comparing two counts across a split understates dilution badly.

        The extract reports 1,000 shares a year before 1,100, which reads as
        10% dilution. If a one-for-twenty reverse split fell between them the
        earlier 1,000 became 50, so reporting 1,100 after it is a 22-fold
        increase. v3 excludes its top dilution quintile, and chronic
        reverse-splitters were escaping that filter by this exact arithmetic.
        """
        plain = self.history.signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertAlmostEqual(plain["pit_share_change_1y"], 0.1, places=6)

        splits = SplitHistory([SplitEvent(date(2025, 6, 1), 0.05)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, splits=splits,
        )
        self.assertAlmostEqual(signals["pit_share_change_1y"], 21.0, places=6)

    def test_split_outside_the_comparison_window_leaves_dilution_alone(self):
        """Both counts already share a basis, so nothing should move."""
        splits = SplitHistory([SplitEvent(date(2026, 8, 14), 0.05)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, splits=splits,
        )

        self.assertAlmostEqual(signals["pit_share_change_1y"], 0.1, places=6)

    def test_split_between_period_end_and_filing_is_flagged(self):
        splits = SplitHistory([SplitEvent(date(2026, 1, 15), 0.5)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, splits=splits,
        )

        self.assertTrue(signals["pit_split_ambiguous"])

    def test_adjusted_shares_feed_the_reconciliation_check(self):
        """A split-adjusted cap that now agrees must not be overridden."""
        splits = SplitHistory([SplitEvent(date(2026, 8, 14), 0.05)])
        signals = self.history.signals_as_of(
            date(2026, 3, 1), price=2.0, market_cap=110.0, splits=splits,
        )

        self.assertAlmostEqual(signals["pit_market_cap"], 110.0, places=6)
        self.assertEqual(signals["pit_market_cap_basis"], "reported_shares")


def _multi_class_extract(period_end=None):
    """A filer with no total period-end share count, only weighted averages."""
    extract = _extract()
    facts = extract["facts"]
    facts["shares_outstanding"] = period_end or []
    facts["weighted_average_shares"] = [
        _fact("2024-10-01", "2024-12-31", "2025-02-15", 900),
        _fact("2024-01-01", "2024-12-31", "2025-02-15", 880),   # annual, same end
        _fact("2025-07-01", "2025-09-30", "2025-11-01", 980),
        _fact("2025-10-01", "2025-12-31", "2026-02-15", 1000),
        _fact("2025-01-01", "2025-12-31", "2026-02-15", 960),   # annual, same end
    ]
    return extract


class WeightedShareFallbackTests(unittest.TestCase):
    def test_weighted_average_shares_value_a_multi_class_filer(self):
        signals = FundamentalHistory(_multi_class_extract()).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_shares_basis"], "weighted_average")
        self.assertEqual(signals["pit_shares_outstanding"], 1000.0)  # the quarter, not the year
        self.assertEqual(signals["pit_market_cap"], 2000.0)
        self.assertAlmostEqual(signals["pit_sales_yield"], 640 / 2000, places=6)
        # A year earlier on the same basis: the Q4 2024 quarter average.
        self.assertAlmostEqual(signals["pit_share_change_1y"], 1000 / 900 - 1, places=6)

    def test_a_fresh_period_end_count_is_still_preferred(self):
        signals = FundamentalHistory(_extract()).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_shares_basis"], "period_end")
        self.assertEqual(signals["pit_shares_outstanding"], 1100.0)

    def test_one_class_alone_gives_way_to_the_weighted_total(self):
        partial = [_instant("2025-12-31", "2026-02-15", 300)]  # one class of several
        signals = FundamentalHistory(_multi_class_extract(partial)).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_shares_basis"], "weighted_average")
        self.assertEqual(signals["pit_shares_outstanding"], 1000.0)

    def test_a_class_missing_from_the_period_end_count_is_caught(self):
        # Class A alone is 77% of the weighted total, as at GTEC.
        partial = [_instant("2025-12-31", "2026-02-15", 770)]
        signals = FundamentalHistory(_multi_class_extract(partial)).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_shares_basis"], "weighted_average")
        self.assertEqual(signals["pit_shares_outstanding"], 1000.0)

    def test_a_small_gap_keeps_the_period_end_count(self):
        # 10% under the average, as a buyback during the quarter leaves it.
        bought_back = [_instant("2025-12-31", "2026-02-15", 900)]
        signals = FundamentalHistory(_multi_class_extract(bought_back)).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_shares_basis"], "period_end")
        self.assertEqual(signals["pit_shares_outstanding"], 900.0)

    def test_a_scale_error_in_the_average_is_ignored(self):
        # GLBS: the 20-F tags the average a thousand times too large.
        extract = _multi_class_extract([_instant("2025-12-31", "2026-02-15", 1000)])
        for row in extract["facts"]["weighted_average_shares"]:
            row["val"] *= 1000
        signals = FundamentalHistory(extract).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertEqual(signals["pit_shares_basis"], "period_end")
        self.assertEqual(signals["pit_shares_outstanding"], 1000.0)

    def test_stale_and_zero_period_end_counts_are_ignored(self):
        stale = [_instant("2016-08-05", "2016-08-09", 500)]
        zero = [_instant("2025-12-31", "2026-02-15", 0)]
        for counts in (stale, zero):
            signals = FundamentalHistory(_multi_class_extract(counts)).signals_as_of(date(2026, 3, 1), price=2.0)
            self.assertEqual(signals["pit_shares_basis"], "weighted_average")

    def test_only_averages_known_on_the_date_are_used(self):
        signals = FundamentalHistory(_multi_class_extract()).signals_as_of(date(2026, 1, 15), price=2.0)
        self.assertEqual(signals["pit_shares_outstanding"], 980.0)  # Q4 not filed yet

    def test_no_share_count_at_all_leaves_valuation_empty(self):
        extract = _multi_class_extract()
        extract["facts"]["weighted_average_shares"] = []
        signals = FundamentalHistory(extract).signals_as_of(date(2026, 3, 1), price=2.0)
        self.assertIsNone(signals["pit_shares_basis"])
        self.assertIsNone(signals["pit_market_cap"])
        self.assertIsNone(signals["pit_sales_yield"])


def _annual_filer(old_quarters=False):
    """A June-year-end 20-F filer, optionally with quarters from years ago."""
    facts = {
        "revenue": [
            _fact("2023-07-01", "2024-06-30", "2024-10-25", 800),
            _fact("2024-07-01", "2025-06-30", "2025-10-24", 900),
        ],
        "net_income": [_fact("2024-07-01", "2025-06-30", "2025-10-24", 90)],
        "cash": [_instant("2025-06-30", "2025-10-24", 300)],
        "shares_outstanding": [_instant("2025-06-30", "2025-10-24", 100)],
    }
    if old_quarters:
        facts["revenue"] += [
            _fact(start, end, "2016-11-01", 50)
            for start, end in [("2015-07-01", "2015-09-30"), ("2015-10-01", "2015-12-31"),
                               ("2016-01-01", "2016-03-31"), ("2016-04-01", "2016-06-30")]
        ]
    return {"ticker": "ANNL", "facts": facts}


class AnnualFilerTests(unittest.TestCase):
    def test_an_annual_filer_is_current_until_its_next_report_is_due(self):
        # 477 days after the June year end; the next 20-F is due at the end of October.
        signals = FundamentalHistory(_annual_filer()).signals_as_of(date(2026, 10, 20), price=10.0)
        self.assertEqual(signals["pit_revenue_ttm"], 900.0)
        self.assertEqual(signals["pit_net_income_ttm"], 90.0)
        self.assertEqual(signals["pit_cash"], 300.0)
        self.assertAlmostEqual(signals["pit_sales_yield"], 0.9, places=6)

    def test_an_annual_filer_still_goes_stale(self):
        signals = FundamentalHistory(_annual_filer()).signals_as_of(date(2027, 3, 1), price=10.0)
        self.assertIsNone(signals["pit_revenue_ttm"])

    def test_old_quarters_do_not_hide_a_current_annual_figure(self):
        history = FundamentalHistory(_annual_filer(old_quarters=True))
        self.assertEqual(history.ttm("revenue", date(2026, 10, 20))[1], 900.0)
        signals = history.signals_as_of(date(2026, 10, 20), price=10.0)
        self.assertEqual(signals["pit_revenue_ttm"], 900.0)

    def test_quarterly_filers_keep_the_ordinary_limit(self):
        history = FundamentalHistory(_extract())
        self.assertTrue(history.reports_quarterly(date(2026, 3, 1)))
        self.assertFalse(FundamentalHistory(_annual_filer()).reports_quarterly(date(2026, 10, 20)))


def _half_year_filer():
    """A December-year-end filer reporting half-years on 6-K, as GLBS does."""
    return {"ticker": "HALF", "facts": {
        "revenue": [
            _fact("2024-01-01", "2024-06-30", "2024-09-10", 15),
            _fact("2024-01-01", "2024-12-31", "2025-03-14", 35),
            _fact("2025-01-01", "2025-06-30", "2025-09-19", 18),
            _fact("2025-01-01", "2025-12-31", "2026-03-16", 44),
            _fact("2026-01-01", "2026-06-30", "2026-08-07", 27),
        ],
        "cash": [_instant("2026-06-30", "2026-08-07", 30)],
        "shares_outstanding": [_instant("2026-06-30", "2026-08-07", 20)],
    }}


class HalfYearTests(unittest.TestCase):
    def test_the_trailing_year_adds_the_latest_half(self):
        history = FundamentalHistory(_half_year_filer())
        ends, total = history.ttm("revenue", date(2026, 10, 1))
        self.assertEqual(ends, (date(2026, 6, 30),))
        self.assertEqual(total, 44 + 27 - 18)

    def test_a_year_back_uses_the_halves_known_then(self):
        history = FundamentalHistory(_half_year_filer())
        self.assertEqual(history.ttm("revenue", date(2026, 10, 1), offset=4)[1], 35 + 18 - 15)
        # Before the 2026 half is filed the annual report is the latest.
        self.assertEqual(history.ttm("revenue", date(2026, 8, 1)), ((date(2025, 12, 31),), 44.0))

    def test_quarters_win_when_they_are_as_recent(self):
        extract = _half_year_filer()
        extract["facts"]["revenue"] += [
            _fact(start, end, "2026-08-07", 10)
            for start, end in [("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"),
                               ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
        ]
        self.assertEqual(FundamentalHistory(extract).ttm("revenue", date(2026, 10, 1))[1], 40.0)

    def test_a_half_needs_last_year_s_half(self):
        extract = _half_year_filer()
        extract["facts"]["revenue"] = [
            row for row in extract["facts"]["revenue"] if row["end"] != "2025-06-30"
        ]
        self.assertEqual(
            FundamentalHistory(extract).ttm("revenue", date(2026, 10, 1)),
            ((date(2025, 12, 31),), 44.0),
        )


if __name__ == "__main__":
    unittest.main()
