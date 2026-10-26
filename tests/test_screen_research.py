"""Within-screen signal research on synthetic observations with known answers."""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.screen_research import (
    classify, export_screen_research, scan_screen, screen_frame, tiebreaker,
)


def _observations(seed=7, names=200):
    rng = np.random.default_rng(seed)
    periods = pd.period_range("2018-01", "2025-12", freq="M").astype(str)
    rows = []
    for period in periods:
        good = rng.normal(size=names)
        noise = rng.normal(size=(3, names))
        sign = 1.0 if period < "2022-01" else -1.0
        for i in range(names):
            excess = 0.6 * good[i] + noise[0, i]
            rows.append({
                "period": period, "ticker": f"T{i:03d}",
                "score_v3": rng.uniform(0, 100), "score_v3_excluded": False,
                "v2_high_proximity": good[i],                 # predicts in both windows
                "pit_sales_yield": noise[1, i],              # unrelated
                "v2_volatility": sign * good[i],             # flips at the split
                "universe_market_cap": noise[2, i],
                "excess_1Y": excess, "excess_6M": excess, "excess_3M": excess,
            })
    frame = pd.DataFrame(rows)
    rejected = frame.sample(frac=0.1, random_state=seed).index
    frame.loc[rejected, ["score_v3", "score_v3_excluded"]] = [np.nan, True]
    return frame


class ScreenFrameTests(unittest.TestCase):
    def test_keeps_the_top_three_fifths_of_v3_ranked_names(self):
        rows = [{"period": "2020-01", "ticker": f"S{i}", "score_v3": 100 - i,
                 "score_v3_excluded": False} for i in range(10)]
        rows.append({"period": "2020-01", "ticker": "X", "score_v3": np.nan, "score_v3_excluded": True})
        screened = screen_frame(pd.DataFrame(rows))
        self.assertEqual(sorted(screened["ticker"]), [f"S{i}" for i in range(6)])
        self.assertEqual(list(screened.sort_values("score_v3", ascending=False)["v3_quintile"]),
                         [5, 5, 4, 4, 3, 3])


class ClassifyTests(unittest.TestCase):
    def _windows(self, fit_ic, fit_t, holdout_ic, holdout_t):
        return {"fit": {"mean_ic": fit_ic, "newey_west_t": fit_t},
                "holdout": {"mean_ic": holdout_ic, "newey_west_t": holdout_t}}

    def test_rule(self):
        self.assertEqual(classify(self._windows(0.05, 2.5, 0.04, 2.1)), "separates")
        self.assertEqual(classify(self._windows(-0.05, -2.5, -0.04, -2.1)), "separates")
        self.assertEqual(classify(self._windows(0.05, 2.5, 0.01, 0.4)), "suggestive")
        self.assertEqual(classify(self._windows(0.05, 2.5, -0.04, -2.1)), "no")
        self.assertEqual(classify(self._windows(0.01, 0.5, 0.01, 0.6)), "no")
        self.assertEqual(classify(self._windows(float("nan"), 2.5, 0.04, 2.1)), "no")


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = scan_screen(_observations())

    def test_planted_signal_separates_and_noise_does_not(self):
        signals = self.result["signals"]
        self.assertEqual(signals["v2_high_proximity"]["verdict"], "separates")
        self.assertEqual(signals["v2_high_proximity"]["direction"], "higher is better")
        self.assertEqual(signals["pit_sales_yield"]["verdict"], "no")
        self.assertEqual(signals["v2_volatility"]["verdict"], "no")

    def test_v3_itself_carries_no_order_inside_a_random_screen(self):
        self.assertEqual(self.result["signals"]["score_v3"]["verdict"], "no")

    def test_population_is_the_screen(self):
        population = self.result["population"]
        self.assertEqual(population["periods"], 96)
        # About 180 v3-ranked names a month (10% rejected at random), of
        # which three fifths pass.
        self.assertAlmostEqual(population["median_names_per_period"], 108, delta=3)

    def test_tiebreaker_is_chosen_on_the_fit_window_only(self):
        tie = self.result["tiebreaker"]
        # The flipping signal also looks good before the split, so it is
        # chosen there and then hurts the holdout: that is the point of
        # choosing on one window and judging on the other.
        self.assertIn("v2_high_proximity", tie["chosen_on_fit_window"])
        self.assertIn("v2_volatility", tie["chosen_on_fit_window"])
        self.assertNotIn("score_v3", tie["chosen_on_fit_window"])

    def test_yearly_agreement_is_counted(self):
        years = self.result["signals"]["v2_high_proximity"]["years"]
        self.assertEqual(years["years"], 8)  # 2018-2025
        self.assertEqual(years["agreeing"], years["years"])

    def test_export_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = export_screen_research(self.result, "RUN", Path(directory))
            document = json.loads(json_path.read_text("utf-8"),
                                  parse_constant=lambda name: self.fail(f"{name} in JSON"))
            self.assertEqual(document["run_id"], "RUN")
            self.assertIn("v2_high_proximity | separates", markdown_path.read_text("utf-8"))


class TiebreakerTests(unittest.TestCase):
    def test_missing_signals_sit_in_the_middle(self):
        frame = pd.DataFrame({"period": ["A"] * 3, "x": [1.0, 2.0, np.nan]})
        scores = tiebreaker(frame, {"x": -1.0})
        self.assertGreater(scores.iloc[0], scores.iloc[1])
        self.assertEqual(scores.iloc[2], 0.5)


if __name__ == "__main__":
    unittest.main()
