import json
import unittest

import pandas as pd

from src.scoring import calculate_scores


class ScoringFoundationTests(unittest.TestCase):
    def setUp(self):
        self.history = pd.DataFrame(
            {
                "Close": [float(value) for value in range(10, 230)],
                "Volume": [1_000_000.0] * 220,
            }
        )
        self.benchmark = pd.DataFrame(
            {
                "Close": [float(value) for value in range(10, 230)],
                "Volume": [1_000_000.0] * 220,
            }
        )

    def test_adds_explainable_factor_breakdown_without_changing_total(self):
        result = calculate_scores(
            {
                "ticker": "TEST",
                "market_cap": 100_000_000,
                "sector": "Technology",
            },
            self.history,
            self.benchmark,
        )
        breakdown = json.loads(result["factor_breakdown"])

        component_total = sum(
            result[column]
            for column in (
                "volume_score",
                "relative_strength_score",
                "trend_score",
                "market_cap_score",
                "sector_score",
                "liquidity_score",
            )
        )
        self.assertEqual(result["discovery_score"], round(component_total, 2))
        self.assertEqual(result["score_confidence"], 100.0)
        self.assertEqual(result["fundamental_score"], 0.0)
        self.assertEqual(result["fundamental_confidence"], 0.0)
        self.assertEqual(
            set(breakdown),
            {
                "volume_acceleration",
                "relative_strength",
                "trend_strength",
                "market_cap",
                "sector_bonus",
                "liquidity",
            },
        )

    def test_missing_sector_reduces_confidence_not_eligibility(self):
        result = calculate_scores(
            {"ticker": "TEST", "market_cap": 100_000_000, "sector": None},
            self.history,
            self.benchmark,
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["score_confidence"], 90.0)

    def test_exposes_fundamentals_without_changing_discovery_score(self):
        stock_data = {
            "ticker": "TEST",
            "market_cap": 100_000_000,
            "sector": "Technology",
            "revenue_growth": 0.40,
            "earnings_growth": 0.35,
            "operating_margin": 0.25,
            "free_cash_flow": 20_000_000,
            "total_cash": 50_000_000,
            "total_debt": 10_000_000,
        }
        without_fundamentals = calculate_scores(
            {"ticker": "TEST", "market_cap": 100_000_000, "sector": "Technology"},
            self.history,
            self.benchmark,
        )
        with_fundamentals = calculate_scores(
            stock_data,
            self.history,
            self.benchmark,
        )

        self.assertEqual(
            with_fundamentals["discovery_score"],
            without_fundamentals["discovery_score"],
        )
        self.assertEqual(with_fundamentals["fundamental_score"], 40.0)


if __name__ == "__main__":
    unittest.main()
