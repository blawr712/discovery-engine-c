import json
from pathlib import Path
import tempfile
import unittest

from src.ai_review_resolution import ai_review_comparison, load_ai_reviews


def candidate(ticker, excess, defensive=50.0, status="resolved"):
    horizon = {"status": status}
    if status == "resolved":
        horizon["excess_return_percent"] = excess
    return {"ticker": ticker, "scores": {"score_defensive": defensive}, "horizons": {"6M": horizon}}


def name(ticker, verdict, discount="no"):
    return {"ticker": ticker, "status": "valid", "verdict": verdict,
            "review": {"verdict": verdict, "peer_comparison": {"discount_explained": discount}}}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.resolution = {
            "eligible_horizons": ["6M"],
            "candidates": [
                candidate("A", 10.0, 10), candidate("B", 4.0, 20), candidate("C", -6.0, 30),
                candidate("D", -20.0, 40), candidate("E", 2.0, 50), candidate("F", 0.0, 60, status="pending"),
                candidate("G", 99.0, 70),
            ],
        }
        self.reviews = {
            "run_id": "RUN", "dry_run": False, "artifact_sha256": "x",
            "names": [
                name("A", "clear"), name("B", "clear", "yes"), name("C", "caution", "yes"),
                name("D", "avoid"), name("E", "caution"), name("F", "clear"),
                name("Z", "clear"),
                {"ticker": "H", "status": "not_reviewed", "verdict": "not_reviewed"},
            ],
        }

    def test_verdict_groups_and_the_tested_spread(self):
        before = json.dumps(self.resolution, sort_keys=True)
        result = ai_review_comparison(self.resolution, self.reviews)
        self.assertEqual(json.dumps(self.resolution, sort_keys=True), before)
        self.assertEqual(result["joined_to_cohort"], 6)
        self.assertEqual(result["not_in_cohort"], ["H", "Z"])
        horizon = result["horizons"]["6M"]
        self.assertEqual(horizon["resolved"], 5)  # F pending, G not reviewed
        self.assertEqual(horizon["by_verdict"]["clear"]["median_excess_percent"], 7.0)
        self.assertEqual(horizon["by_verdict"]["avoid"]["resolved"], 1)
        # clear median 7.0; caution and avoid together -20, -6, 2 -> median -6.
        self.assertEqual(horizon["clear_minus_flagged_median_percent"], 13.0)
        reported = horizon["reported_only"]
        self.assertEqual(reported["avoid_minus_rest_median_percent"], -23.0)
        self.assertEqual(reported["unexplained_minus_explained_discount_median_percent"], 3.0)
        self.assertEqual([third["names"] for third in reported["clear_minus_flagged_by_defensive_third"]], [1, 2, 2])

    def test_loads_the_real_artifact_before_a_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(load_ai_reviews(root, "RUN"))
            (root / "ai_review_dry_run_RUN.json").write_text(json.dumps({"dry_run": True}), encoding="utf-8")
            self.assertTrue(load_ai_reviews(root, "RUN")["dry_run"])
            (root / "ai_reviews_RUN.json").write_text(json.dumps({"dry_run": False}), encoding="utf-8")
            self.assertFalse(load_ai_reviews(root, "RUN")["dry_run"])


if __name__ == "__main__":
    unittest.main()
