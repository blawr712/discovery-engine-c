from datetime import date
import unittest

from src.fundamentals_pit import FundamentalHistory, SIGNAL_NAMES


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


if __name__ == "__main__":
    unittest.main()
