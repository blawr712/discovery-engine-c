import json
import unittest

import numpy as np
import pandas as pd

from src.scoring import calculate_scores
from src.scoring_v2 import (
    RAW_PREFIX,
    apply_all_models,
    apply_cross_sectional_scores,
    compute_raw_signals,
    raw_column,
    score_frame,
    validated_config,
)


SMALL_CONFIG = {
    "model_version": "test",
    "minimum_price_history_days": 30,
    "sector_neutral": True,
    "minimum_sector_group": 3,
    "lookbacks": {
        "long_window": 40,
        "medium_window": 20,
        "skip_window": 5,
        "reversal_window": 5,
        "volatility_window": 10,
        "volume_short_window": 5,
        "volume_long_window": 40,
        "high_window": 40,
    },
    "signals": {
        "momentum_long": {"weight": 40, "direction": "higher"},
        "volatility": {"weight": 40, "direction": "lower"},
        "volume_trend": {"weight": 20, "direction": "higher"},
    },
}


def _history(closes, volumes=None):
    closes = np.asarray(closes, dtype=float)
    volumes = (
        np.asarray(volumes, dtype=float)
        if volumes is not None
        else np.full(len(closes), 1000.0)
    )
    return pd.DataFrame({"Close": closes, "Volume": volumes})


class RawSignalTests(unittest.TestCase):
    def test_computes_window_returns_skipping_recent_sessions(self):
        closes = np.arange(1, 61, dtype=float)  # 60 sessions, 1..60
        signals = compute_raw_signals(_history(closes), SMALL_CONFIG)

        # momentum_long: close[-6] / close[-41] - 1 = 55/20 - 1
        self.assertAlmostEqual(signals[f"{RAW_PREFIX}momentum_long"], 55 / 20 - 1, places=6)
        self.assertIn(f"{RAW_PREFIX}volatility", signals)
        self.assertGreater(signals[f"{RAW_PREFIX}volatility"], 0)
        self.assertAlmostEqual(signals[f"{RAW_PREFIX}volume_trend"], 1.0, places=6)
        # Signals that are not configured are not emitted.
        self.assertNotIn(f"{RAW_PREFIX}high_proximity", signals)

    def test_full_signal_set_with_defaults(self):
        closes = 10.0 * np.cumprod(1.0 + np.full(300, 0.002))
        volumes = np.concatenate([np.full(279, 1000.0), np.full(21, 3000.0)])
        signals = compute_raw_signals(_history(closes, volumes))

        self.assertAlmostEqual(signals[f"{RAW_PREFIX}high_proximity"], 1.0, places=6)
        self.assertGreater(signals[f"{RAW_PREFIX}momentum_long"], 0)
        self.assertGreater(signals[f"{RAW_PREFIX}momentum_medium"], 0)
        self.assertGreater(signals[f"{RAW_PREFIX}short_term_reversal"], 0)
        self.assertGreater(signals[f"{RAW_PREFIX}volume_trend"], 2.5)

    def test_short_history_yields_unavailable_signals(self):
        signals = compute_raw_signals(_history(np.arange(1, 20)), SMALL_CONFIG)
        self.assertTrue(all(value is None for value in signals.values()))
        self.assertEqual(
            set(signals),
            {f"{RAW_PREFIX}{name}" for name in SMALL_CONFIG["signals"]},
        )

    def test_windows_longer_than_history_are_unavailable_individually(self):
        closes = np.arange(1, 36, dtype=float)  # 35 sessions >= minimum 30
        signals = compute_raw_signals(_history(closes), SMALL_CONFIG)
        self.assertIsNone(signals[f"{RAW_PREFIX}momentum_long"])
        self.assertIsNone(signals[f"{RAW_PREFIX}volume_trend"])
        self.assertIsNotNone(signals[f"{RAW_PREFIX}volatility"])

    def test_non_positive_prices_are_rejected(self):
        closes = np.arange(1, 61, dtype=float)
        closes[-6] = 0.0
        signals = compute_raw_signals(_history(closes), SMALL_CONFIG)
        self.assertIsNone(signals[f"{RAW_PREFIX}momentum_long"])


class CrossSectionTests(unittest.TestCase):
    def _frame(self):
        return pd.DataFrame({
            "sector": ["Tech", "Tech", "Tech", "Energy", "Energy", None],
            f"{RAW_PREFIX}momentum_long": [0.5, 0.2, -0.1, 0.9, 0.0, 0.3],
            f"{RAW_PREFIX}volatility": [0.2, 0.4, 0.6, 0.1, 0.8, None],
            f"{RAW_PREFIX}volume_trend": [None] * 6,
        })

    def test_percentiles_are_direction_aware_and_bounded(self):
        scored = score_frame(self._frame(), {**SMALL_CONFIG, "sector_neutral": False})

        momentum = scored["pct_momentum_long"]
        self.assertEqual(momentum.iloc[3], 100.0)  # highest momentum
        self.assertEqual(momentum.iloc[2], 0.0)
        volatility = scored["pct_volatility"]
        self.assertEqual(volatility.iloc[3], 100.0)  # lowest volatility is best
        self.assertEqual(volatility.iloc[4], 0.0)
        self.assertTrue(pd.isna(volatility.iloc[5]))
        self.assertTrue((scored["score"].dropna() <= 100).all())
        self.assertTrue((scored["score"].dropna() >= 0).all())

    def test_missing_signals_reduce_confidence_not_score_scale(self):
        scored = score_frame(self._frame(), {**SMALL_CONFIG, "sector_neutral": False})
        # volume_trend is missing everywhere: 80 of 100 weight available.
        self.assertTrue((scored["confidence"].iloc[:5] == 80.0).all())
        # Last row also lacks volatility: only momentum's 40 remain.
        self.assertEqual(scored["confidence"].iloc[5], 40.0)
        self.assertAlmostEqual(
            scored["score"].iloc[5], scored["pct_momentum_long"].iloc[5], places=6,
        )

    def test_sector_neutral_ranking_applies_only_to_large_groups(self):
        scored = score_frame(self._frame(), SMALL_CONFIG)

        self.assertEqual(scored["group_momentum_long"].iloc[0], "sector:Tech")
        self.assertEqual(scored["group_momentum_long"].iloc[3], "universe")
        self.assertEqual(scored["group_momentum_long"].iloc[5], "universe")
        # Within Tech the best momentum is 100 even though Energy has 0.9.
        self.assertEqual(scored["pct_momentum_long"].iloc[0], 100.0)
        self.assertEqual(scored["pct_momentum_long"].iloc[2], 0.0)

    def test_single_valid_value_ranks_neutral(self):
        frame = pd.DataFrame({
            "sector": ["Tech"],
            f"{RAW_PREFIX}momentum_long": [0.5],
            f"{RAW_PREFIX}volatility": [None],
            f"{RAW_PREFIX}volume_trend": [None],
        })
        scored = score_frame(frame, SMALL_CONFIG)
        self.assertEqual(scored["pct_momentum_long"].iloc[0], 50.0)


class ApplyScoresTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            {"ticker": "AAA", "status": "OK", "sector": "Tech", "discovery_score": 70,
             f"{RAW_PREFIX}momentum_long": 0.5, f"{RAW_PREFIX}volatility": 0.2,
             f"{RAW_PREFIX}volume_trend": 1.5},
            {"ticker": "BBB", "status": "FILTERED", "discovery_score": 0},
            {"ticker": "CCC", "status": "OK", "sector": "Tech", "discovery_score": 40,
             f"{RAW_PREFIX}momentum_long": -0.2, f"{RAW_PREFIX}volatility": 0.9,
             f"{RAW_PREFIX}volume_trend": 0.8},
            {"ticker": "DDD", "status": "OK", "sector": "Energy", "discovery_score": 55,
             f"{RAW_PREFIX}momentum_long": None, f"{RAW_PREFIX}volatility": None,
             f"{RAW_PREFIX}volume_trend": None},
            {"ticker": "EEE", "status": "FAILED", "discovery_score": 0},
        ]

    def test_scores_only_successful_rows_and_preserves_official_fields(self):
        original = json.dumps(self.results, sort_keys=True)
        updated = apply_cross_sectional_scores(self.results, SMALL_CONFIG)

        self.assertEqual(json.dumps(self.results, sort_keys=True), original)
        self.assertEqual(updated[0]["score_v2"], 100.0)
        self.assertEqual(updated[0]["score_v2_rank"], 1)
        self.assertEqual(updated[2]["score_v2"], 0.0)
        self.assertEqual(updated[2]["score_v2_rank"], 2)
        self.assertIsNone(updated[3]["score_v2"])
        self.assertIsNone(updated[3]["score_v2_rank"])
        self.assertEqual(updated[3]["score_v2_confidence"], 0.0)
        self.assertNotIn("score_v2", updated[1])
        self.assertNotIn("score_v2", updated[4])
        for row in updated:
            self.assertEqual(
                row["discovery_score"],
                next(r for r in self.results if r["ticker"] == row["ticker"])["discovery_score"],
            )

    def test_breakdown_explains_each_signal(self):
        updated = apply_cross_sectional_scores(self.results, SMALL_CONFIG)
        breakdown = json.loads(updated[0]["score_v2_breakdown"])

        self.assertEqual(set(breakdown), set(SMALL_CONFIG["signals"]))
        self.assertEqual(breakdown["momentum_long"]["points"], 40.0)
        self.assertEqual(breakdown["momentum_long"]["max_points"], 40.0)
        self.assertIn("100th percentile", breakdown["momentum_long"]["explanation"])
        self.assertIn("lower is better", breakdown["volatility"]["explanation"])
        self.assertEqual(updated[0]["score_v2_confidence"], 100.0)
        self.assertEqual(updated[0]["score_v2_model_version"], "test")
        missing = json.loads(updated[3]["score_v2_breakdown"])
        self.assertFalse(missing["momentum_long"]["available"])
        self.assertEqual(missing["momentum_long"]["data_quality"], "missing")

    def test_no_successful_rows_is_a_no_op(self):
        rows = [{"ticker": "X", "status": "FILTERED"}]
        self.assertEqual(apply_cross_sectional_scores(rows, SMALL_CONFIG), rows)


class ConfigValidationTests(unittest.TestCase):
    def test_rejects_invalid_configuration(self):
        for override in (
            {"signals": {}},
            {"signals": {"unknown": {"weight": 1}}},
            {"signals": {"momentum_long": {"weight": 0}}},
            {"signals": {"momentum_long": {"weight": 5, "direction": "sideways"}}},
            {"lookbacks": {"skip_window": 0}},
            {"minimum_sector_group": 1},
            {"minimum_price_history_days": 1},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    validated_config({**SMALL_CONFIG, **override})

    def test_defaults_fill_missing_sections(self):
        config = validated_config({})
        self.assertEqual(config["model_version"], "v2.0-shadow")
        self.assertEqual(config["output_prefix"], "score_v2")
        self.assertEqual(len(config["signals"]), 6)
        self.assertEqual(config["lookbacks"]["skip_window"], 21)
        self.assertEqual(config["exclusions"], [])

    def test_rejects_invalid_exclusions_and_confidence(self):
        for override in (
            {"exclusions": [{"signal": "pit_sales_yield"}]},
            {"exclusions": [{"signal": "nope", "exclude_below_percentile": 10}]},
            {"exclusions": [{"signal": "volatility", "exclude_above_percentile": 101}]},
            {"exclusions": "bad"},
            {"minimum_confidence": 101},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    validated_config({**SMALL_CONFIG, **override})


COMPOSITE_CONFIG = {
    "model_version": "test-composite",
    "output_prefix": "score_v3",
    "minimum_price_history_days": 30,
    "sector_neutral": False,
    "minimum_sector_group": 3,
    "minimum_confidence": 50,
    "exclusions": [
        {"signal": "pit_operating_margin_ttm", "exclude_below_percentile": 25,
         "reason": "Weak margin"},
        {"signal": "pit_share_change_1y", "exclude_above_percentile": 75,
         "reason": "Heavy dilution"},
    ],
    "signals": {
        "pit_sales_yield": {"weight": 60, "direction": "higher"},
        "pit_share_change_1y": {"weight": 20, "direction": "lower"},
        "volatility": {"weight": 20, "direction": "lower"},
    },
}


class CompositeModelTests(unittest.TestCase):
    def _rows(self):
        return [
            {"ticker": "A", "status": "OK", "sector": "Tech", "discovery_score": 50,
             "pit_sales_yield": 2.0, "pit_share_change_1y": 0.01,
             "pit_operating_margin_ttm": 0.20, f"{RAW_PREFIX}volatility": 0.3},
            {"ticker": "B", "status": "OK", "sector": "Tech", "discovery_score": 50,
             "pit_sales_yield": 1.0, "pit_share_change_1y": 0.02,
             "pit_operating_margin_ttm": 0.10, f"{RAW_PREFIX}volatility": 0.4},
            {"ticker": "C", "status": "OK", "sector": "Tech", "discovery_score": 50,
             "pit_sales_yield": 0.5, "pit_share_change_1y": 0.03,
             "pit_operating_margin_ttm": 0.05, f"{RAW_PREFIX}volatility": 0.5},
            {"ticker": "D", "status": "OK", "sector": "Tech", "discovery_score": 50,
             "pit_sales_yield": 3.0, "pit_share_change_1y": 0.90,
             "pit_operating_margin_ttm": -0.80, f"{RAW_PREFIX}volatility": 0.9},
            {"ticker": "E", "status": "OK", "sector": "Energy", "discovery_score": 50,
             "pit_sales_yield": None, "pit_share_change_1y": None,
             "pit_operating_margin_ttm": None, f"{RAW_PREFIX}volatility": 0.2},
        ]

    def test_raw_column_mapping(self):
        self.assertEqual(raw_column("pit_sales_yield"), "pit_sales_yield")
        self.assertEqual(raw_column("volatility"), f"{RAW_PREFIX}volatility")

    def test_exclusions_and_confidence_gate(self):
        updated = apply_cross_sectional_scores(self._rows(), COMPOSITE_CONFIG)
        by = {row["ticker"]: row for row in updated}

        # D has the worst margin and the heaviest dilution: excluded, no score.
        self.assertTrue(by["D"]["score_v3_excluded"])
        self.assertIn("Weak margin", by["D"]["score_v3_exclusion_reasons"])
        self.assertIn("Heavy dilution", by["D"]["score_v3_exclusion_reasons"])
        self.assertIsNone(by["D"]["score_v3"])
        self.assertIsNone(by["D"]["score_v3_rank"])
        # E has only volatility (20 of 100 weight): below the confidence gate.
        self.assertFalse(by["E"]["score_v3_excluded"])
        self.assertEqual(by["E"]["score_v3_confidence"], 20.0)
        self.assertIsNone(by["E"]["score_v3"])
        # A, B, C are ranked; A has the best sales yield and lowest dilution.
        self.assertEqual(
            [by[t]["score_v3_rank"] for t in ("A", "B", "C")], [1, 2, 3],
        )
        self.assertEqual(by["A"]["score_v3_model_version"], "test-composite")
        self.assertNotIn("score_v2", by["A"])
        breakdown = json.loads(by["A"]["score_v3_breakdown"])
        self.assertIn("filings available", breakdown["pit_sales_yield"]["explanation"])

    def test_apply_all_models_adds_v2_and_v3_fields(self):
        history = pd.DataFrame({
            "Close": np.linspace(10, 40, 300),
            "Volume": [1_000_000.0] * 300,
        })
        rows = []
        for index in range(25):
            signals = compute_raw_signals(history)
            rows.append({
                "ticker": f"T{index}", "status": "OK", "sector": "Tech",
                **{k: (v * (1 + index / 10) if v is not None else None) for k, v in signals.items()},
                "pit_sales_yield": index / 10, "pit_net_cash_to_market_cap": 0.1,
                "pit_fcf_yield": 0.05, "pit_fcf_margin_ttm": 0.1,
                "pit_operating_margin_ttm": 0.05 + index / 100,
                "pit_share_change_1y": 0.01, "pit_revenue_growth_ttm": index / 100,
            })
        updated = apply_all_models(rows)
        self.assertIn("score_v2", updated[0])
        self.assertIn("score_v3", updated[0])
        scored = [row for row in updated if row["score_v3"] is not None]
        self.assertGreater(len(scored), 10)
        self.assertLess(len(scored), 25)  # top-quintile growth is excluded


class LiveScoringIntegrationTests(unittest.TestCase):
    def test_calculate_scores_emits_raw_v2_signals(self):
        history = pd.DataFrame({
            "Close": np.linspace(10, 40, 260),
            "Volume": [1_000_000.0] * 260,
        })
        result = calculate_scores(
            {"ticker": "AAA", "market_cap": 100_000_000, "sector": "Technology"},
            history,
            history.copy(),
        )
        self.assertEqual(result["status"], "OK")
        self.assertIsNotNone(result[f"{RAW_PREFIX}momentum_long"])
        self.assertIsNotNone(result[f"{RAW_PREFIX}momentum_medium"])
        self.assertAlmostEqual(result[f"{RAW_PREFIX}high_proximity"], 1.0, places=6)
        # Cross-sectional fields are not assigned per company.
        self.assertNotIn("score_v2", result)


if __name__ == "__main__":
    unittest.main()
