"""The defensive ordering: scored live as in the backtest, frozen, and resolved."""

from datetime import date
import unittest

import numpy as np
import pandas as pd

from src.baseline_resolution import resolve_baseline
from src.defensive_order import (
    DEFENSIVE_SIGNALS, DEFENSIVE_VERSION, apply_defensive_order, defensive_scores,
)
from src.monthly import STEP_ORDER, build_cycle_report
from src.research_baseline import build_research_baseline
from src.research_ranking import build_research_queue
from src.screen_research import tiebreaker
from tests.test_baseline_resolution import _baseline, _candidate, _series
from tests.test_screen import _row, _v3_scored


class ScoreTests(unittest.TestCase):
    def test_live_score_matches_the_backtest_tiebreaker(self):
        rng = np.random.default_rng(3)
        frame = pd.DataFrame({name: rng.normal(size=40) for name, _ in DEFENSIVE_SIGNALS})
        frame.iloc[::5, 0] = np.nan                    # gaps
        frame.iloc[1:4, 2] = 1.0                       # ties
        frame.iloc[7, :] = np.nan                      # nothing at all
        frame["period"] = "2026-11"
        expected = tiebreaker(frame, dict(DEFENSIVE_SIGNALS)) * 100
        rows = [
            {name: (None if pd.isna(value) else float(value)) for name, value in record.items()}
            for record in frame.drop(columns="period").to_dict("records")
        ]
        np.testing.assert_allclose(defensive_scores(rows), expected.to_numpy(), atol=1e-3)
        self.assertEqual(defensive_scores(rows)[7], 50.0)

    def test_directions(self):
        calm = {"v2_high_proximity": 0.95, "v2_volatility": 0.2, "pit_earnings_yield": 0.1,
                "pit_net_cash_to_market_cap": -0.5, "pit_market_cap": 9e8}
        wild = {"v2_high_proximity": 0.30, "v2_volatility": 1.5, "pit_earnings_yield": -0.4,
                "pit_net_cash_to_market_cap": 0.9, "pit_market_cap": 2e7}
        self.assertGreater(*defensive_scores([calm, wild]))

    def test_only_names_passing_the_screen_are_scored(self):
        queue = [
            {"screen_status": "passes", "v2_high_proximity": 0.9},
            {"screen_status": "passes", "v2_high_proximity": 0.1},
            {"screen_status": "low_v3_quintile", "v2_high_proximity": 1.0},
        ]
        apply_defensive_order(queue)
        self.assertGreater(queue[0]["score_defensive"], queue[1]["score_defensive"])
        self.assertIsNone(queue[2]["score_defensive"])


class QueueAndCohortTests(unittest.TestCase):
    def setUp(self):
        rows = [_row(f"S{i:02d}", i / 20, pit_market_cap=1e8 * (i + 1)) for i in range(10)]
        rows.append(_row("THIN", 0.9, dollar_volume=5_000))
        self.queue, self.summary = build_research_queue(_v3_scored(rows))

    def test_queue_records_the_definition_and_scores_passing_names(self):
        self.assertEqual(self.summary["defensive_order"]["version"], DEFENSIVE_VERSION)
        for row in self.queue:
            if row["screen_status"] == "passes":
                self.assertIsNotNone(row["score_defensive"])
            else:
                self.assertIsNone(row["score_defensive"])

    def test_v3_order_is_untouched(self):
        ranks = [row["ticker"] for row in self.queue if row["ranking_basis"] == "score_v3"]
        self.assertEqual(ranks, [f"S{i:02d}" for i in range(10)])

    def test_cohort_freezes_screen_and_defensive_score(self):
        baseline = build_research_baseline(self.queue, "RUN", "2026-11-16T00:00:00+00:00", {"top_n": 2})
        self.assertEqual(baseline["defensive_order"]["version"], DEFENSIVE_VERSION)
        by = {row["ticker"]: row for row in baseline["candidates"]}
        self.assertEqual(by["S00"]["screen_status"], "passes")
        self.assertEqual(by["S00"]["v3_quintile"], 5)
        self.assertIsNotNone(by["S00"]["score_defensive"])
        self.assertIsNone(by["S09"]["score_defensive"])

    def test_cohort_without_screen_statuses_records_no_definition(self):
        old = [{key: value for key, value in row.items() if key != "score_defensive"} for row in self.queue]
        baseline = build_research_baseline(old, "RUN", "2026-11-16T00:00:00+00:00", {"top_n": 2})
        self.assertIsNone(baseline["defensive_order"])


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.series = {"SPY": _series("2025-12-01", 200, 100.0, 0.0)}
        candidates = []
        for index in range(60):
            ticker = f"P{index:02d}"
            self.series[ticker] = _series("2025-12-01", 200, 10.0, (index - 30) / 10000)
            row = _candidate(ticker, "v3_quintile_4", index + 1)
            row.update(screen_status="passes", score_v3=float(-index),
                       score_v3_challenger=float(index), score_defensive=float(index))
            candidates.append(row)
        # Names outside the screen, with outcomes that would flatter v3, stay out.
        for index in range(20):
            ticker = f"L{index:02d}"
            self.series[ticker] = _series("2025-12-01", 200, 10.0, 0.01)
            row = _candidate(ticker, "v3_quintile_1", 100 + index)
            row.update(screen_status="low_v3_quintile", score_v3=-1000.0 + index,
                       score_v3_challenger=0.0, score_defensive=None)
            candidates.append(row)
        resolution = resolve_baseline(
            _baseline(candidates), self.series, as_of=date(2026, 2, 10),
            config={"minimum_cohort_size": 1, "selected_classifications": ["v3_quintile_4"],
                    "control_classifications": ["v3_quintile_1"]},
        )
        self.report = resolution["horizons"]["1M"]

    def test_inside_screen_comparison_uses_passing_names_only(self):
        screen = self.report["screen_comparison"]
        self.assertEqual(screen["resolved"], 60)
        self.assertAlmostEqual(screen["information_coefficient"]["score_defensive"], 1.0)
        self.assertAlmostEqual(screen["information_coefficient"]["score_v3"], -1.0)
        self.assertAlmostEqual(screen["defensive_minus_v3_information_coefficient"], 2.0)
        terciles = screen["defensive_terciles"]
        self.assertEqual([t["resolved"] for t in terciles], [20, 20, 20])
        self.assertLess(terciles[0]["median_excess_percent"], terciles[2]["median_excess_percent"])
        self.assertGreater(screen["most_minus_least_defensive_median_percent"], 0)

    def test_the_v3_head_to_head_is_unchanged(self):
        comparison = self.report["model_comparison"]
        self.assertNotIn("score_defensive", comparison)
        self.assertEqual(comparison["common"]["resolved"], 80)

    def test_cohorts_frozen_before_the_test_have_no_section(self):
        candidates = [_candidate("A", "v3_quintile_4")]
        candidates[0]["score_v3"] = 1.0
        resolution = resolve_baseline(
            _baseline(candidates), {"SPY": self.series["SPY"], "A": self.series["P00"]},
            as_of=date(2026, 2, 10), config={"minimum_cohort_size": 1},
        )
        self.assertNotIn("screen_comparison", resolution["horizons"]["1M"])


class ReportTests(unittest.TestCase):
    def test_cycle_report_shows_the_inside_screen_result(self):
        state = {
            "cycle": "2026-12", "status": "completed", "attempts": 1, "run_id": "RUN",
            "warnings": [], "steps": {name: {"status": "done"} for name in STEP_ORDER},
        }
        report = build_cycle_report(state, [{
            "run_id": "NOV", "kind": "research", "horizons": ["1M"],
            "summary": {"1M": {
                "selected": {}, "control": {},
                "inside_screen": {"resolved": 420, "score_defensive": 0.061, "score_v3": -0.012,
                                  "most_minus_least_defensive_median_percent": 4.5},
            }},
        }])
        self.assertIn("## Inside the screen: defensive ordering against v3", report)
        self.assertIn("420 names: defensive IC 0.061, v3 IC -0.012", report)


if __name__ == "__main__":
    unittest.main()
