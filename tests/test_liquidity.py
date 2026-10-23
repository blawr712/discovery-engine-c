import json
import unittest

from src.liquidity import MINIMUM_AVERAGE_DOLLAR_VOLUME, average_dollar_volume, liquidity_eligible
from src.research_baseline import build_research_baseline
from src.research_ranking import build_research_queue
from src.scoring_v2 import apply_all_models


def _row(ticker, dollar_volume, discovery_score=50.0, momentum=0.1):
    return {
        "ticker": ticker, "status": "OK", "sector": "Tech", "country": "US",
        "discovery_score": discovery_score, "score_confidence": 100,
        "average_dollar_volume_30d": dollar_volume,
        "v2_momentum_long": momentum, "v2_high_proximity": momentum,
        "v2_volatility": 0.3, "v2_momentum_medium": momentum,
        "v2_volume_trend": 1.0, "v2_short_term_reversal": 0.0,
    }


class EligibilityTests(unittest.TestCase):
    def test_the_floor_is_the_configured_one(self):
        self.assertEqual(MINIMUM_AVERAGE_DOLLAR_VOLUME, 250_000)

    def test_rows_at_or_above_the_floor_are_eligible(self):
        self.assertTrue(liquidity_eligible({"average_dollar_volume_30d": 250_000}))
        self.assertFalse(liquidity_eligible({"average_dollar_volume_30d": 249_999}))

    def test_unknown_liquidity_is_not_enough(self):
        self.assertFalse(liquidity_eligible({}))
        self.assertFalse(liquidity_eligible({"average_dollar_volume_30d": float("nan")}))
        self.assertTrue(liquidity_eligible({}, floor=0))

    def test_saved_rows_fall_back_to_the_liquidity_factor(self):
        """Runs scored before the field existed can still be gated."""
        row = {"factor_breakdown": json.dumps({"liquidity": {"raw_value": 5345.0}})}
        self.assertEqual(average_dollar_volume(row), 5345.0)
        self.assertFalse(liquidity_eligible(row))
        self.assertIsNone(average_dollar_volume({"factor_breakdown": "not json"}))


class ShadowScoringTests(unittest.TestCase):
    def setUp(self):
        self.rows = [_row(f"L{index}", 1e6, momentum=index / 10) for index in range(6)]
        self.rows.append(_row("THIN", 5_000, momentum=0.9))

    def test_illiquid_names_get_no_shadow_score_and_keep_official_fields(self):
        scored = {row["ticker"]: row for row in apply_all_models(self.rows)}

        self.assertIsNone(scored["THIN"]["score_v2"])
        self.assertFalse(scored["THIN"]["liquidity_eligible"])
        self.assertFalse(scored["THIN"]["score_v2_excluded"])  # not a filter rejection
        self.assertEqual(scored["THIN"]["discovery_score"], 50.0)
        self.assertTrue(all(scored[f"L{i}"]["score_v2"] is not None for i in range(6)))

    def test_liquid_names_are_ranked_only_against_each_other(self):
        with_thin = {row["ticker"]: row["score_v2"] for row in apply_all_models(self.rows)}
        without = {row["ticker"]: row["score_v2"] for row in apply_all_models(self.rows[:-1])}
        for ticker, score in without.items():
            self.assertAlmostEqual(with_thin[ticker], score)


class QueueTests(unittest.TestCase):
    def test_illiquid_names_form_a_last_labelled_tier(self):
        rows = apply_all_models(
            [_row(f"L{i}", 1e6, discovery_score=10 + i, momentum=i / 10) for i in range(5)]
            + [_row("THIN", 5_000, discovery_score=99), _row("THINNER", 1_000, discovery_score=95)]
        )
        queue, _ = build_research_queue(rows)

        self.assertEqual([row["ticker"] for row in queue[-2:]], ["THIN", "THINNER"])
        self.assertEqual({row["ranking_basis"] for row in queue[-2:]}, {"below_liquidity_floor"})
        self.assertEqual({row["ranking_tier"] for row in queue[-2:]}, {"illiquid"})
        self.assertEqual(queue[-2]["ranking_score"], 99)
        self.assertTrue(all(row["liquidity_eligible"] for row in queue[:-2]))
        self.assertEqual(queue[-1]["average_dollar_volume_30d"], 1_000)

    def test_baselines_keep_illiquid_names_in_their_own_cohort(self):
        rows = apply_all_models(
            [_row(f"L{i}", 1e6, momentum=i / 10) for i in range(5)] + [_row("THIN", 5_000)]
        )
        queue, _ = build_research_queue(rows)
        baseline = build_research_baseline(queue, "RUN", "2026-11-16T00:00:00+00:00", {})
        by = {row["ticker"]: row for row in baseline["candidates"]}

        self.assertEqual(by["THIN"]["classification"], "below_liquidity_floor")
        self.assertEqual(baseline["minimum_average_dollar_volume"], 250_000)
        self.assertEqual(by["THIN"]["average_dollar_volume_30d"], 5_000)


if __name__ == "__main__":
    unittest.main()
