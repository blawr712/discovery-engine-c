import unittest

from src.config import MAX_MARKET_CAP, MIN_MARKET_CAP
from src.pre_filter import evaluate_stock, filtered_result


class PreFilterTests(unittest.TestCase):
    def test_accepts_market_caps_at_configured_boundaries(self):
        self.assertTrue(evaluate_stock({"market_cap": MIN_MARKET_CAP}).passed)
        self.assertTrue(evaluate_stock({"market_cap": MAX_MARKET_CAP}).passed)

    def test_rejects_missing_and_invalid_market_caps(self):
        self.assertEqual(evaluate_stock({}).reason, "Missing market cap")
        self.assertEqual(
            evaluate_stock({"market_cap": "100000000"}).reason,
            "Invalid market cap",
        )
        self.assertEqual(
            evaluate_stock({"market_cap": True}).reason,
            "Invalid market cap",
        )

    def test_explains_market_caps_outside_the_allowed_range(self):
        below = evaluate_stock({"market_cap": MIN_MARKET_CAP - 1})
        above = evaluate_stock({"market_cap": MAX_MARKET_CAP + 1})

        self.assertEqual(below.reason, "Below minimum market cap")
        self.assertEqual(above.reason, "Above maximum market cap")

    def test_filtered_report_row_preserves_metadata(self):
        row = filtered_result(
            {"ticker": "TEST", "market_cap": None},
            "Missing market cap",
        )

        self.assertEqual(row["ticker"], "TEST")
        self.assertEqual(row["status"], "FILTERED")
        self.assertEqual(row["discovery_score"], 0)
        self.assertEqual(row["reason_flags"], "Missing market cap")

    def test_structural_exclusion_precedes_market_cap_checks(self):
        result = evaluate_stock(
            {
                "company_name": "Example Acquisition Corp.",
                "market_cap": None,
            }
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.asset_type, "acquisition_vehicle")
        self.assertEqual(result.reason, "Acquisition or SPAC vehicle")


if __name__ == "__main__":
    unittest.main()
