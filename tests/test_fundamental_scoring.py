import json
import unittest
from datetime import datetime, timezone

from src.fundamental_scoring import calculate_fundamental_scores


class FundamentalScoringTests(unittest.TestCase):
    AS_OF = datetime(2026, 7, 27, tzinfo=timezone.utc)
    RECENT_QUARTER = datetime(2026, 6, 30, tzinfo=timezone.utc).timestamp()

    def test_scores_complete_strong_fundamentals(self):
        result = calculate_fundamental_scores(
            {
                "market_cap": 100_000_000,
                "revenue_growth": 0.40,
                "earnings_growth": 0.35,
                "operating_margin": 0.25,
                "free_cash_flow": 20_000_000,
                "total_cash": 50_000_000,
                "total_debt": 10_000_000,
                "trailing_pe": 8,
                "price_to_sales": 0.8,
                "enterprise_to_ebitda": 7,
                "current_ratio": 2.5,
                "debt_to_equity": 20,
                "operating_cash_flow": 30_000_000,
                "net_income": 20_000_000,
                "fundamental_data_timestamp": self.RECENT_QUARTER,
            },
            as_of=self.AS_OF,
        )

        self.assertEqual(result["fundamental_score"], 65.0)
        self.assertEqual(result["fundamental_score_max"], 65)
        self.assertEqual(result["fundamental_score_normalized"], 100.0)
        self.assertEqual(result["fundamental_confidence"], 100.0)
        self.assertEqual(result["fundamental_data_quality"], "fresh")

    def test_missing_data_does_not_create_false_penalty(self):
        result = calculate_fundamental_scores(
            {"market_cap": 100_000_000, "revenue_growth": 0.40}
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        self.assertEqual(result["fundamental_score"], 10.0)
        self.assertEqual(result["fundamental_score_normalized"], 100.0)
        self.assertEqual(result["fundamental_confidence"], 11.54)
        self.assertFalse(breakdown["earnings_growth"]["available"])

    def test_available_weak_fundamentals_score_zero_with_full_confidence(self):
        result = calculate_fundamental_scores(
            {
                "market_cap": 100_000_000,
                "revenue_growth": -0.10,
                "earnings_growth": -0.10,
                "operating_margin": -0.10,
                "free_cash_flow": -10_000_000,
                "total_cash": 0,
                "total_debt": 50_000_000,
            }
        )

        self.assertEqual(result["fundamental_score"], 0.0)
        self.assertEqual(result["fundamental_score_normalized"], 0.0)
        self.assertEqual(result["fundamental_confidence"], 46.15)

    def test_uses_profit_margin_when_operating_margin_is_missing(self):
        result = calculate_fundamental_scores(
            {"market_cap": 100_000_000, "profit_margin": 0.12}
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        self.assertEqual(breakdown["profitability"]["points"], 6.0)
        self.assertIn(
            "Profit margin",
            breakdown["profitability"]["explanation"],
        )

    def test_rejects_non_numeric_and_boolean_values_as_missing(self):
        result = calculate_fundamental_scores(
            {
                "market_cap": 100_000_000,
                "revenue_growth": "20%",
                "earnings_growth": True,
            }
        )

        self.assertEqual(result["fundamental_confidence"], 0.0)

    def test_scores_valuation_and_risk_factors_in_shadow_lane(self):
        result = calculate_fundamental_scores(
            {
                "trailing_pe": 10,
                "price_to_sales": 2,
                "enterprise_to_ebitda": 12,
                "current_ratio": 1.5,
                "debt_to_equity": 50,
                "operating_cash_flow": 12_000_000,
                "net_income": 10_000_000,
                "fundamental_data_timestamp": self.RECENT_QUARTER,
            },
            as_of=self.AS_OF,
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        self.assertEqual(breakdown["earnings_yield"]["points"], 5.0)
        self.assertEqual(breakdown["sales_yield"]["points"], 3.0)
        self.assertEqual(breakdown["enterprise_value_ebitda"]["points"], 2.8)
        self.assertEqual(breakdown["liquidity"]["points"], 2.8)
        self.assertEqual(breakdown["leverage"]["points"], 2.8)
        self.assertEqual(breakdown["earnings_quality"]["points"], 3.0)

    def test_stale_data_is_visible_and_cannot_score(self):
        result = calculate_fundamental_scores(
            {
                "revenue_growth": 0.50,
                "trailing_pe": 5,
                "fundamental_data_timestamp": datetime(
                    2024, 1, 1, tzinfo=timezone.utc
                ).timestamp(),
            },
            as_of=self.AS_OF,
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        self.assertEqual(result["fundamental_data_quality"], "stale")
        self.assertEqual(result["fundamental_score"], 0)
        self.assertEqual(result["fundamental_confidence"], 0)
        self.assertFalse(breakdown["revenue_growth"]["available"])
        self.assertEqual(breakdown["revenue_growth"]["data_quality"], "stale")

    def test_invalid_ratios_are_unavailable_not_zero_scoring_evidence(self):
        result = calculate_fundamental_scores(
            {
                "trailing_pe": -4,
                "current_ratio": -1,
                "debt_to_equity": -20,
                "fundamental_data_timestamp": self.RECENT_QUARTER,
            },
            as_of=self.AS_OF,
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        self.assertFalse(breakdown["earnings_yield"]["available"])
        self.assertEqual(breakdown["earnings_yield"]["data_quality"], "invalid")
        self.assertEqual(breakdown["liquidity"]["data_quality"], "invalid")
        self.assertEqual(breakdown["leverage"]["data_quality"], "invalid")

    def test_extreme_inputs_follow_configured_invalid_and_cap_policies(self):
        result = calculate_fundamental_scores(
            {
                "revenue_growth": 18,
                "current_ratio": 106,
                "fundamental_data_timestamp": self.RECENT_QUARTER,
            },
            as_of=self.AS_OF,
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        self.assertFalse(breakdown["revenue_growth"]["available"])
        self.assertEqual(
            breakdown["revenue_growth"]["data_quality"],
            "invalid",
        )
        self.assertEqual(breakdown["revenue_growth"]["source_value"], 18)
        self.assertTrue(breakdown["liquidity"]["available"])
        self.assertEqual(breakdown["liquidity"]["raw_value"], 50)
        self.assertEqual(breakdown["liquidity"]["source_value"], 106)
        self.assertEqual(breakdown["liquidity"]["data_quality"], "capped")

    def test_sector_exclusions_do_not_reduce_confidence(self):
        result = calculate_fundamental_scores(
            {
                "sector": "Financial Services",
                "revenue_growth": 0.4,
                "operating_margin": 0.2,
                "free_cash_flow": 10_000_000,
                "market_cap": 100_000_000,
                "enterprise_to_ebitda": 5,
                "current_ratio": 2,
                "fundamental_data_timestamp": self.RECENT_QUARTER,
            },
            as_of=self.AS_OF,
        )
        breakdown = json.loads(result["fundamental_breakdown"])

        for name in ("free_cash_flow", "enterprise_value_ebitda", "liquidity"):
            self.assertFalse(breakdown[name]["applicable"])
            self.assertEqual(
                breakdown[name]["data_quality"],
                "not_applicable",
            )
        self.assertGreater(result["fundamental_confidence"], 0)


if __name__ == "__main__":
    unittest.main()
