import json
import unittest

from src.factors import (
    FactorResult,
    score_confidence,
    serialize_factor_breakdown,
)


class FactorResultTests(unittest.TestCase):
    def test_serializes_stable_factor_breakdown(self):
        factor = FactorResult(
            name="growth",
            raw_value=0.123456,
            points=7.126,
            max_points=10,
            available=True,
            explanation="Revenue is growing",
        )

        result = json.loads(serialize_factor_breakdown([factor]))

        self.assertEqual(result["growth"]["raw_value"], 0.1235)
        self.assertEqual(result["growth"]["points"], 7.13)

    def test_confidence_uses_available_factor_weight(self):
        factors = [
            FactorResult("one", 1, 5, 10, True, "available"),
            FactorResult("two", None, 0, 30, False, "missing"),
        ]

        self.assertEqual(score_confidence(factors), 25.0)
        self.assertEqual(score_confidence([]), 0.0)

    def test_confidence_discounts_undated_and_rejects_stale_data(self):
        factors = [
            FactorResult("fresh", 1, 5, 10, True, "fresh", "fresh"),
            FactorResult("undated", 1, 5, 10, True, "undated", "undated"),
            FactorResult("stale", 1, 0, 10, False, "stale", "stale"),
        ]

        self.assertEqual(score_confidence(factors), 58.33)

    def test_confidence_excludes_non_applicable_weight(self):
        factors = [
            FactorResult("usable", 1, 5, 10, True, "usable"),
            FactorResult(
                "excluded",
                None,
                0,
                30,
                False,
                "not applicable",
                "not_applicable",
                applicable=False,
            ),
        ]

        self.assertEqual(score_confidence(factors), 100.0)


if __name__ == "__main__":
    unittest.main()
