import math
import unittest

import numpy as np
import pandas as pd

from src.model_tests import apply_change, decision, ic_series, newey_west_t, paired_test


BASE = {
    "model_version": "test", "output_prefix": "score_v3", "minimum_confidence": 0,
    "sector_neutral": False, "minimum_sector_group": 2, "minimum_price_history_days": 2,
    "exclusions": [], "sector_exclusions": {},
    "signals": {
        "volume_trend": {"weight": 10, "direction": "higher"},
        "pit_sales_yield": {"weight": 1, "direction": "higher"},
    },
}


def _observations(periods=40, names=80, seed=3):
    rng = np.random.default_rng(seed)
    rows = []
    for p in range(periods):
        label = f"{2016 + p // 12}-{p % 12 + 1:02d}" if p < 24 else f"{2022 + (p - 24) // 12}-{(p - 24) % 12 + 1:02d}"
        for n in range(names):
            signal = rng.normal()
            rows.append({
                "period": label, "ticker": f"T{n}", "sector": "Tech",
                "pit_sales_yield": signal, "v2_volume_trend": rng.normal(),
                "excess_1Y": signal * 5 + rng.normal(), "excess_3M": signal * 2 + rng.normal(),
            })
    return pd.DataFrame(rows)


class ChangeTests(unittest.TestCase):
    def test_add_reweight_and_remove(self):
        changed = apply_change(BASE, {"volume_trend": None, "pit_sales_yield": 10, "pit_fcf_yield": 5})
        self.assertNotIn("volume_trend", changed["signals"])
        self.assertEqual(changed["signals"]["pit_sales_yield"], {"weight": 10.0, "direction": "higher"})
        self.assertEqual(changed["signals"]["pit_fcf_yield"]["direction"], "higher")
        self.assertIn("volume_trend", BASE["signals"])  # the original is untouched

    def test_invalid_changes_are_refused(self):
        with self.assertRaises(ValueError):
            apply_change(BASE, {"missing": None})
        with self.assertRaises(ValueError):
            apply_change(BASE, {"volume_trend": None, "pit_sales_yield": None})
        with self.assertRaises(ValueError):
            apply_change(BASE, {"volume_trend": "heavy"})


class StatisticsTests(unittest.TestCase):
    def test_newey_west_shrinks_t_for_overlapping_series(self):
        rng = np.random.default_rng(1)
        shocks = rng.normal(0.01, 0.05, 200)
        overlapping = pd.Series(shocks).rolling(12).mean().dropna() + 0.002
        self.assertLess(abs(newey_west_t(overlapping, 11)), abs(newey_west_t(overlapping, 0)))
        self.assertTrue(math.isnan(newey_west_t([0.1, 0.2], 11)))

    def test_rule(self):
        good = {"mean_difference": 0.01, "newey_west_t": 2.5}
        weak = {"mean_difference": 0.005, "newey_west_t": 1.2}
        bad = {"mean_difference": -0.01, "newey_west_t": -2.5}
        self.assertTrue(decision(weak, good))
        self.assertFalse(decision(weak, weak))
        self.assertFalse(decision(bad, good))
        self.assertFalse(decision(good, {"mean_difference": float("nan"), "newey_west_t": 3.0}))


class PairedTests(unittest.TestCase):
    def test_shifting_weight_to_the_real_signal_is_accepted(self):
        result = paired_test(_observations(), BASE, {"volume_trend": 1, "pit_sales_yield": 10})
        fit = result["horizons"]["1Y"]["fit"]
        self.assertGreater(fit["mean_difference"], 0)
        self.assertEqual(fit["periods"], 24)
        self.assertTrue(result["accepted"])
        self.assertIn("3M", result["horizons"])

    def test_removing_the_real_signal_is_rejected(self):
        result = paired_test(_observations(), BASE, {"pit_sales_yield": None})
        self.assertLess(result["horizons"]["1Y"]["holdout"]["mean_difference"], 0)
        self.assertFalse(result["accepted"])

    def test_ic_series_skips_thin_cross_sections(self):
        frame = _observations(periods=2, names=30)
        self.assertTrue(ic_series(frame, frame["pit_sales_yield"], "1Y").empty)


if __name__ == "__main__":
    unittest.main()
