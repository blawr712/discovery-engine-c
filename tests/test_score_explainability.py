import json
import unittest

from src.score_explainability import (
    confidence_descriptor,
    explain_candidate,
    percentile_descriptor,
    score_percentiles,
)


class ScoreExplainabilityTests(unittest.TestCase):
    def test_percentiles_and_descriptors_are_relative_to_successful_rows(self):
        rows = [
            {"ticker": "TOP", "status": "OK", "discovery_score": 90},
            {"ticker": "MID", "status": "OK", "discovery_score": 50},
            {"ticker": "LOW", "status": "OK", "discovery_score": 10},
            {"ticker": "FAILED", "status": "FAILED", "discovery_score": 100},
        ]

        percentiles = score_percentiles(rows, "discovery_score")

        self.assertEqual(percentiles, {"TOP": 100, "MID": 50, "LOW": 0})
        self.assertEqual(percentile_descriptor(percentiles["TOP"]), "Top tier")
        self.assertEqual(percentile_descriptor(percentiles["MID"]), "Middle")
        self.assertEqual(percentile_descriptor(None), "Not ranked")

    def test_explanation_parses_factors_and_separates_coverage_from_scores(self):
        row = {
            "ticker": "TEST", "company_name": "Test Company", "status": "OK",
            "discovery_score": 72, "fundamental_score_normalized": 64,
            "score_confidence": 100, "fundamental_confidence": 45,
            "factor_breakdown": json.dumps({
                "trend_strength": {
                    "name": "trend_strength", "points": 15, "max_points": 15,
                    "available": True, "data_quality": "fresh",
                    "explanation": "Price trend is strong",
                }
            }),
            "fundamental_breakdown": "{}",
        }

        explanation = explain_candidate(
            row, 4, 92, 70, candidate_count=100, discovery_median=55,
        )

        self.assertEqual(explanation["scores"]["discovery"]["descriptor"], "Strong")
        self.assertEqual(explanation["confidence"]["technical"]["descriptor"], "High coverage")
        self.assertEqual(explanation["confidence"]["fundamental"]["descriptor"], "Limited coverage")
        self.assertEqual(explanation["technical_factors"][0]["points"], 15)
        self.assertEqual(explanation["score_story"]["score_vs_median"], 17)
        self.assertIn("Ranks #4 of 100", explanation["score_story"]["why_it_ranks"])
        self.assertEqual(
            explanation["score_story"]["drivers"][0]["label"], "Trend Strength"
        )
        self.assertIn("not recommendations", explanation["interpretation_warning"])

    def test_score_story_identifies_constraints_and_data_gaps(self):
        row = {
            "ticker": "TEST", "status": "OK", "discovery_score": 60,
            "score_confidence": 70, "fundamental_confidence": 20,
            "factor_breakdown": json.dumps({
                "trend": {"points": 10, "max_points": 10, "available": True},
                "liquidity": {"points": 1, "max_points": 10, "available": True},
                "volume": {"points": None, "max_points": 10, "available": False,
                           "data_quality": "missing"},
            }),
            "fundamental_breakdown": "{}",
        }

        story = explain_candidate(row, 10, 50, None, 50, 55)["score_story"]

        self.assertEqual(story["constraints"][0]["label"], "Liquidity")
        self.assertIn("Volume", story["data_gaps"])
        self.assertIn("limited inputs", story["reliability"])

    def test_score_story_handles_unavailable_coverage_and_median_tie(self):
        row = {
            "ticker": "TEST", "status": "OK", "discovery_score": 55,
            "score_confidence": None, "fundamental_confidence": None,
            "factor_breakdown": "{}", "fundamental_breakdown": "{}",
        }

        story = explain_candidate(row, 1, 100, None, 1, 55)["score_story"]

        self.assertIn("at the run median", story["rank_context"])
        self.assertNotIn("unavailable%", story["reliability"])

    def test_confidence_descriptors_use_documented_coverage_bands(self):
        self.assertEqual(confidence_descriptor(75), "High coverage")
        self.assertEqual(confidence_descriptor(50), "Moderate coverage")
        self.assertEqual(confidence_descriptor(25), "Limited coverage")
        self.assertEqual(confidence_descriptor(0), "Very limited coverage")

    def test_tied_top_scores_share_top_percentile(self):
        rows = [
            {"ticker": "ONE", "status": "OK", "discovery_score": 80},
            {"ticker": "TWO", "status": "OK", "discovery_score": 80},
            {"ticker": "THREE", "status": "OK", "discovery_score": 20},
        ]

        percentiles = score_percentiles(rows, "discovery_score")

        self.assertEqual(percentiles["ONE"], 100)
        self.assertEqual(percentiles["TWO"], 100)


if __name__ == "__main__":
    unittest.main()
