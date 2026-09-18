from datetime import date
import unittest

from src.fundamentals_pit import FundamentalHistory
from src.statements_extract import extract_from_statements


def _statements():
    quarters = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    income = {
        end: {"Total Revenue": 100.0 * (i + 1), "Gross Profit": 50.0 * (i + 1),
              "Operating Income": 10.0 * (i + 1), "Net Income": 5.0 * (i + 1)}
        for i, end in enumerate(quarters)
    }
    income["2025-06-30"] = {"Total Revenue": float("nan")}  # missing value: skipped
    balance = {
        end: {"Cash And Cash Equivalents": 500.0, "Total Debt": 100.0, "Long Term Debt": 80.0,
              "Current Debt": 20.0, "Ordinary Shares Number": 1000.0, "Stockholders Equity": 900.0}
        for end in quarters
    }
    cashflow = {end: {"Operating Cash Flow": 20.0, "Capital Expenditure": -5.0} for end in quarters}
    annual_income = {"2025-12-31": {"Total Revenue": 700.0, "Net Income": 30.0},
                     "2024-12-31": {"Total Revenue": 500.0, "Net Income": 20.0}}
    return {
        "currency": "CAD",
        "quarterly": {"income": income, "balance": balance, "cashflow": cashflow},
        "annual": {"income": annual_income, "cashflow": {}},
    }


class ExtractTests(unittest.TestCase):
    def test_builds_compact_extract_with_estimated_filing_dates(self):
        extract = extract_from_statements("well.to", _statements())

        self.assertEqual(extract["ticker"], "WELL.TO")
        self.assertEqual(extract["currency"], "CAD")
        self.assertEqual(extract["data_quality"], "estimated_filing_dates")
        revenue = extract["facts"]["revenue"]
        quarterly = [row for row in revenue if row["form"] == "statement-quarterly"]
        self.assertEqual([row["end"] for row in quarterly],
                         ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
        first = quarterly[0]
        self.assertEqual(first["start"], "2025-07-01")
        self.assertEqual(first["filed"], "2025-11-29")  # 60-day lag
        self.assertEqual(first["tag"], "statement:Total Revenue")
        self.assertEqual(first["unit"], "CAD")
        annual = [row for row in revenue if row["form"] == "statement-annual"]
        self.assertEqual([row["end"] for row in annual], ["2024-12-31", "2025-12-31"])
        self.assertEqual(annual[0]["start"], "2024-01-01")
        self.assertEqual(annual[0]["filed"], "2025-04-30")  # 120-day lag
        # Capex sign is flipped to a positive outflow; instants have no start.
        self.assertEqual(extract["facts"]["capital_expenditure"][0]["val"], 5.0)
        self.assertIsNone(extract["facts"]["cash"][0]["start"])
        self.assertEqual(extract["facts"]["short_term_debt"][0]["val"], 20.0)
        self.assertEqual(extract["tags_found"]["revenue"], ["statement:Total Revenue"])

    def test_feeds_point_in_time_history_with_currency_and_quality(self):
        history = FundamentalHistory(extract_from_statements("WELL.TO", _statements()))

        self.assertEqual(history.currency, "CAD")
        self.assertEqual(history.data_quality, "estimated_filing_dates")
        # On 2026-08-15 the June quarter (filed 2026-08-29) is not yet known.
        signals = history.signals_as_of(date(2026, 8, 15), price=2.0)
        # Only three quarters are public, so TTM falls back to the FY2025 annual figure.
        self.assertEqual(signals["pit_revenue_ttm"], 700.0)
        self.assertEqual(signals["pit_reporting_currency"], "CAD")
        self.assertEqual(signals["pit_data_quality"], "estimated_filing_dates")
        later = history.signals_as_of(date(2026, 9, 15), price=2.0)
        self.assertEqual(later["pit_revenue_ttm"], 100 + 200 + 300 + 400)
        self.assertAlmostEqual(later["pit_operating_margin_ttm"], 100 / 1000, places=6)
        self.assertEqual(later["pit_shares_outstanding"], 1000.0)
        self.assertEqual(later["pit_total_debt"], 100.0)
        self.assertEqual(later["pit_market_cap"], 2000.0)

    def test_empty_statements_yield_empty_extract(self):
        extract = extract_from_statements("X", {"currency": None, "quarterly": {}, "annual": {}})
        self.assertTrue(all(rows == [] for rows in extract["facts"].values()))
        self.assertFalse(FundamentalHistory(extract).has_data)


if __name__ == "__main__":
    unittest.main()
