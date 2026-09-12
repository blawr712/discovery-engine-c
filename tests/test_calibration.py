import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.calibration import build_calibration, export_calibration


def factors(**values):
    return json.dumps(
        {
            name: {
                "raw_value": value,
                "points": 1,
                "max_points": 1,
                "available": True,
                "applicable": True,
            }
            for name, value in values.items()
        }
    )


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            {
                "ticker": "TECH",
                "company_name": "Technical",
                "status": "OK",
                "country": "CA",
                "sector": "Technology",
                "discovery_score": 90,
                "fundamental_score_normalized": 20,
                "fundamental_confidence": 40,
                "fundamental_breakdown": factors(
                    revenue_growth=0.2,
                    leverage=1200,
                ),
            },
            {
                "ticker": "BAL",
                "company_name": "Balanced",
                "status": "OK",
                "country": "CA",
                "sector": "Industrials",
                "discovery_score": 70,
                "fundamental_score_normalized": 70,
                "fundamental_confidence": 90,
                "fundamental_breakdown": factors(
                    revenue_growth=0.1,
                    leverage=50,
                ),
            },
            {
                "ticker": "FUND",
                "company_name": "Fundamental",
                "status": "OK",
                "country": "US",
                "sector": "Technology",
                "discovery_score": 40,
                "fundamental_score_normalized": 95,
                "fundamental_confidence": 100,
                "fundamental_breakdown": factors(
                    revenue_growth=0.4,
                    leverage=10,
                ),
            },
            {"ticker": "NO", "status": "FILTERED"},
        ]
        self.config = {
            "top_n": 2,
            "low_confidence_threshold": 50,
            "coverage_neutral_blend": {
                "candidate_pool_size": 2,
                "rerank_band_size": 2,
                "minimum_peer_group_size": 2,
                "minimum_country_factor_coverage": 0,
                "neutral_percentile": 50,
                "acceptance_gates": {
                    "maximum_country_eligibility_gap": 100,
                    "maximum_rank_movement": 2,
                    "minimum_top_retention": {"2": 100},
                },
            },
            "blend_scenarios": {
                "baseline": {
                    "technical_weight": 1,
                    "fundamental_weight": 0,
                },
                "blend": {
                    "technical_weight": 0.3,
                    "fundamental_weight": 0.7,
                },
            },
            "outlier_bounds": {
                "leverage": {"minimum": 0, "maximum": 1000},
            },
        }

    def test_builds_percentiles_overlap_disagreements_and_flags(self):
        result = build_calibration(self.results, self.config)
        rows = result["rows"]
        summary = result["summary"]

        self.assertEqual([row["ticker"] for row in rows], ["TECH", "BAL", "FUND"])
        self.assertEqual(rows[0]["official_rank"], 1)
        self.assertEqual(rows[0]["technical_percentile"], 100)
        self.assertEqual(rows[0]["fundamental_rank"], 3)
        self.assertEqual(rows[0]["rank_disagreement"], 2)
        self.assertTrue(rows[0]["low_fundamental_confidence"])
        self.assertTrue(rows[0]["experimental_blend_eligible"])
        self.assertIsNone(
            rows[0]["experimental_blend_ineligibility_reason"]
        )
        self.assertTrue(rows[0]["experimental_candidate_pool"])
        self.assertFalse(rows[2]["experimental_candidate_pool"])
        self.assertIn(
            "Outside top-2",
            rows[2]["experimental_candidate_pool_reason"],
        )
        self.assertIn("leverage above 1000", rows[0]["outlier_flags"])
        self.assertEqual(rows[2]["country_technical_percentile"], 100)
        self.assertTrue(summary["official_order_authoritative"])
        self.assertFalse(
            summary["experimental_scores_control_official_order"]
        )
        self.assertEqual(summary["top_overlap"]["count"], 1)
        self.assertEqual(summary["outlier_candidates"], 1)
        self.assertEqual(
            summary["experimental_blend_eligible_candidates"],
            3,
        )
        self.assertEqual(
            summary["top_overlaps"]["20"]["count"],
            3,
        )
        self.assertEqual(
            summary["factor_readiness"]["revenue_growth"]["status"],
            "ready",
        )
        self.assertEqual(
            summary["coverage_neutral_model"]["core_factors"],
            ["leverage", "revenue_growth"],
        )
        self.assertEqual(
            summary["scenario_acceptance"]["blend"]["status"],
            "pass",
        )
        self.assertLessEqual(
            summary["scenario_acceptance"]["blend"][
                "maximum_rank_movement"
            ],
            1,
        )
        self.assertEqual(
            summary["factor_distributions"]["revenue_growth"]["raw_values"][
                "count"
            ],
            3,
        )

    def test_experimental_blend_never_reorders_export_rows(self):
        result = build_calibration(self.results, self.config)
        rows = result["rows"]

        self.assertEqual(rows[0]["ticker"], "TECH")
        self.assertEqual(rows[0]["experimental_baseline_rank"], 1)
        self.assertIsNone(rows[2]["experimental_blend_rank"])
        self.assertEqual(
            result["summary"]["scenario_movements"]["blend"][
                "ranked_candidates"
            ],
            2,
        )

    def test_rejects_invalid_blend_weights(self):
        config = {
            "blend_scenarios": {
                "bad": {
                    "technical_weight": 0.8,
                    "fundamental_weight": 0.3,
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "must total 1"):
            build_calibration(self.results, config)

    def test_exports_csv_and_json_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path, json_path = export_calibration(
                self.results,
                "run-1",
                Path(directory),
                self.config,
            )
            frame = pd.read_csv(csv_path)
            with open(json_path, "r", encoding="utf-8") as file:
                analysis = json.load(file)

        self.assertEqual(frame["ticker"].tolist(), ["TECH", "BAL", "FUND"])
        self.assertTrue(analysis["official_order_authoritative"])


if __name__ == "__main__":
    unittest.main()
