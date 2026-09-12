import unittest

from src.asset_classifier import classify_asset


class AssetClassifierTests(unittest.TestCase):
    def test_accepts_operating_equity(self):
        result = classify_asset(
            {
                "company_name": "Example Software Inc.",
                "quote_type": "EQUITY",
                "industry": "Software - Application",
            }
        )

        self.assertTrue(result.eligible)
        self.assertEqual(result.asset_type, "operating_equity")

    def test_excludes_acquisition_vehicle_by_name(self):
        result = classify_asset(
            {
                "company_name": "Launch One Acquisition Corp.",
                "quote_type": "EQUITY",
            }
        )

        self.assertFalse(result.eligible)
        self.assertEqual(result.asset_type, "acquisition_vehicle")
        self.assertEqual(result.reason, "Acquisition or SPAC vehicle")

    def test_excludes_shell_company_by_industry(self):
        result = classify_asset(
            {
                "company_name": "Example Holdings",
                "industry": "Shell Companies",
            }
        )

        self.assertFalse(result.eligible)
        self.assertEqual(result.asset_type, "shell_company")

    def test_excludes_non_equity_quote_type(self):
        result = classify_asset(
            {"company_name": "Example Index Fund", "quote_type": "ETF"}
        )

        self.assertFalse(result.eligible)
        self.assertEqual(result.asset_type, "non_common_equity")

    def test_retains_ambiguous_metadata_as_unknown(self):
        result = classify_asset({})

        self.assertTrue(result.eligible)
        self.assertEqual(result.asset_type, "unknown")


if __name__ == "__main__":
    unittest.main()
