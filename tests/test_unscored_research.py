"""The never-scored study on synthetic observations with known answers."""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.unscored_research import export_unscored_research, label_groups, unscored_research


def _observations(seed=11):
    rng = np.random.default_rng(seed)
    rows = []
    for period in pd.period_range("2019-01", "2024-12", freq="M").astype(str):
        for i in range(100):  # v3-ranked: 60 pass the screen, 40 do not
            rows.append({"period": period, "ticker": f"V{i:03d}", "sector": "Industrials",
                         "score_v3": float(100 - i), "score_v3_excluded": False,
                         "pit_revenue_ttm": 1.0, "excess_1Y": 0.0, "score_v2": rng.normal()})
        for i in range(20):  # rejected by v3's filters
            rows.append({"period": period, "ticker": f"R{i:02d}", "sector": "Industrials",
                         "score_v3": np.nan, "score_v3_excluded": True,
                         "pit_revenue_ttm": 1.0, "excess_1Y": -30.0, "score_v2": rng.normal()})
        signal = rng.normal(size=60)
        for i in range(60):  # never scored: banks, then names without fundamentals
            bank = i < 30
            rows.append({
                "period": period, "ticker": f"N{i:02d}",
                "sector": "Financial Services" if bank else "Technology",
                "score_v3": np.nan, "score_v3_excluded": False,
                "pit_revenue_ttm": 1.0 if bank else np.nan,
                "excess_1Y": (5.0 if bank else -10.0) + 10 * signal[i] + rng.normal(),
                "score_v2": signal[i],
            })
    frame = pd.DataFrame(rows)
    frame["excess_6M"] = frame["excess_1Y"]
    frame["excess_3M"] = frame["excess_1Y"]
    return frame


class UnscoredTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observations = _observations()
        cls.result = unscored_research(cls.observations)

    def test_every_name_lands_in_one_group(self):
        frame = label_groups(self.observations)
        counts = frame[frame["period"] == "2019-01"].groupby("group").size().to_dict()
        self.assertEqual(counts, {"screen": 60, "low_v3_quintile": 40,
                                  "rejected_by_filters": 20, "never_scored": 60})
        parts = frame[frame["period"] == "2019-01"].groupby("never_scored_part").size().to_dict()
        self.assertEqual(parts, {"financials": 30, "no_fundamentals": 30})

    def test_groups_are_compared_with_the_screen_month_by_month(self):
        groups = self.result["groups"]
        self.assertAlmostEqual(groups["screen"]["windows"]["fit"]["median_excess"], 0.0)
        rejected = groups["rejected_by_filters"]["against_screen"]["holdout"]
        self.assertAlmostEqual(rejected["median_minus_screen"], -30.0)
        self.assertEqual(rejected["periods_median_above_screen"], 0)
        banks = groups["never_scored:financials"]["against_screen"]["fit"]
        self.assertGreater(banks["median_minus_screen"], 0)
        missing = groups["never_scored:no_fundamentals"]["against_screen"]["fit"]
        self.assertLess(missing["median_minus_screen"], 0)

    def test_a_signal_that_sorts_the_group_is_found(self):
        signals = self.result["never_scored_signals"]
        self.assertEqual(signals["score_v2"]["verdict"], "separates")
        self.assertEqual(signals["score_v2"]["direction"], "higher is better")

    def test_export_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = export_unscored_research(self.result, "RUN", Path(directory))
            json.loads(json_path.read_text("utf-8"),
                       parse_constant=lambda name: self.fail(f"{name} in JSON"))
            self.assertIn("never_scored:financials", markdown_path.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
