"""Filers reporting in any currency: extracted, converted live, never misvalued."""

from datetime import date
import unittest

import pandas as pd

import main
from src.backtest import _same_currency
from src.data_sources.sec_xbrl_source import compact_company_facts
from src.fundamentals_pit import FundamentalHistory


def _payload(unit):
    quarters = [("2025-07-01", "2025-09-30", "2025-11-10"), ("2025-10-01", "2025-12-31", "2026-03-01"),
                ("2026-01-01", "2026-03-31", "2026-05-10"), ("2026-04-01", "2026-06-30", "2026-08-10")]
    return {"cik": 1, "entityName": "Euro Co", "facts": {
        "ifrs-full": {"Revenue": {"units": {unit: [
            {"start": s, "end": e, "filed": f, "form": "6-K", "fy": 2026, "fp": "Q", "val": 100.0}
            for s, e, f in quarters
        ]}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-06-30", "filed": "2026-08-10", "form": "6-K", "fy": 2026, "fp": "Q2", "val": 1000.0},
        ]}}},
        # Not a currency: per-share and ratio units are never money facts.
        "us-gaap": {"Revenues": {"units": {"USD/shares": [
            {"start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-10", "val": 0.1},
        ]}}},
    }}


class ExtractTests(unittest.TestCase):
    def test_money_facts_are_kept_in_any_currency(self):
        extract = compact_company_facts(_payload("EUR"), "EURO")
        self.assertEqual({row["unit"] for row in extract["facts"]["revenue"]}, {"EUR"})
        history = FundamentalHistory(extract)
        self.assertEqual(history.currency, "EUR")
        self.assertEqual(history.ttm("revenue", date(2026, 10, 20))[1], 400.0)

    def test_per_share_units_are_not_money(self):
        extract = compact_company_facts(_payload("EUR"), "EURO")
        self.assertNotIn("USD/shares", {row["unit"] for row in extract["facts"]["revenue"]})


class FxTests(unittest.TestCase):
    def test_cross_rates_go_through_the_dollar(self):
        class Source:
            def get_price_history(self, symbol):
                return pd.DataFrame({"Close": [{"EURUSD=X": 1.10, "CADUSD=X": 0.72}[symbol]]})

        rates = main.load_fx_rates(Source(), pairs=["EURUSD=X", "CADUSD=X"])
        self.assertAlmostEqual(rates[("USD", "EUR")], 1 / 1.10)
        self.assertAlmostEqual(rates[("CAD", "EUR")], 0.72 / 1.10)
        self.assertAlmostEqual(rates[("EUR", "CAD")], 1.10 / 0.72)


class BacktestGuardTests(unittest.TestCase):
    def test_valuation_needs_the_same_currency_as_the_listing(self):
        self.assertTrue(_same_currency("USD", "USD"))
        self.assertTrue(_same_currency("USD", None))   # listings default to USD
        self.assertTrue(_same_currency(None, "USD"))   # nothing to convert
        self.assertFalse(_same_currency("EUR", "USD"))
        self.assertFalse(_same_currency("CNY", None))


if __name__ == "__main__":
    unittest.main()
