import json
from pathlib import Path
import tempfile
import unittest

from src.analytics import build_run_analytics, export_run_analytics


def fundamental_breakdown(revenue_available=True, margin_available=True):
    return json.dumps(
        {
            "revenue_growth": {"available": revenue_available},
            "profitability": {"available": margin_available},
        }
    )


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            {
                "ticker": "CA1",
                "status": "OK",
                "country": "CA",
                "sector": "Technology",
                "asset_type": "operating_equity",
                "discovery_score": 60,
                "fundamental_score_normalized": 80,
                "fundamental_confidence": 100,
                "fundamental_data_quality": "fresh",
                "fundamental_breakdown": fundamental_breakdown(),
            },
            {
                "ticker": "US1",
                "status": "OK",
                "country": "US",
                "sector": "Technology",
                "asset_type": "operating_equity",
                "discovery_score": 40,
                "fundamental_score_normalized": 50,
                "fundamental_confidence": 50,
                "fundamental_data_quality": "undated",
                "fundamental_breakdown": fundamental_breakdown(True, False),
            },
            {
                "ticker": "SPAC",
                "status": "FILTERED",
                "asset_type": "acquisition_vehicle",
                "reason_flags": "Acquisition or SPAC vehicle",
            },
        ]

    def test_builds_factor_country_sector_and_exclusion_analytics(self):
        analytics = build_run_analytics(self.results)

        self.assertEqual(analytics["successful_results"], 2)
        self.assertEqual(analytics["structural_exclusions"], 1)
        self.assertEqual(
            analytics["filter_reasons"],
            {"Acquisition or SPAC vehicle": 1},
        )
        self.assertEqual(
            analytics["factor_coverage"]["profitability"]["percentage"],
            50.0,
        )
        self.assertEqual(
            analytics["factor_coverage"]["profitability"]["applicable"],
            2,
        )
        self.assertEqual(
            analytics["coverage_by_country"]["CA"]["companies"],
            1,
        )
        self.assertEqual(
            analytics["coverage_by_sector"]["Technology"][
                "average_fundamental_confidence"
            ],
            75.0,
        )
        self.assertEqual(
            analytics["score_distributions"]["discovery_score"]["median"],
            50.0,
        )
        self.assertEqual(
            analytics["fundamental_data_quality"],
            {"fresh": 1, "undated": 1},
        )

    def test_exports_json_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = export_run_analytics(
                self.results,
                "run-1",
                Path(directory),
            )

            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.assertEqual(data["total_results"], 3)
            self.assertTrue(path.endswith("discovery_analytics_run-1.json"))


if __name__ == "__main__":
    unittest.main()
