"""v3 read as a screen: quintiles and statuses in the research queue."""

import json
from pathlib import Path
import tempfile
import unittest

from src.research_baseline import build_research_baseline
from src.research_ranking import SCREEN_STATUSES, build_research_queue
from src.scoring_v2 import apply_all_models


def _row(ticker, momentum, dollar_volume=1e6, **extra):
    row = {
        "ticker": ticker, "status": "OK", "country": "US", "sector": "Tech",
        "company_name": f"{ticker} Holdings Company", "discovery_score": 50.0,
        "score_confidence": 100, "average_dollar_volume_30d": dollar_volume,
        "v2_momentum_long": momentum, "v2_high_proximity": momentum, "v2_volatility": 0.3,
        "v2_momentum_medium": momentum, "v2_volume_trend": 1.0, "v2_short_term_reversal": 0.0,
    }
    row.update(extra)
    return row


def _v3_scored(rows):
    """Give rows v3 scores directly so the test controls the ordering."""
    scored = apply_all_models(rows)
    for row in scored:
        if row["ticker"].startswith("S"):
            row["score_v3"] = 100.0 - int(row["ticker"][1:])
            row["score_v3_excluded"] = False
        elif row["ticker"] == "REJECT":
            row["score_v3"] = None
            row["score_v3_excluded"] = True
    return scored


class ScreenTests(unittest.TestCase):
    def setUp(self):
        rows = [_row(f"S{i:02d}", i / 20) for i in range(10)]
        rows += [_row("REJECT", 0.5), _row("V2ONLY", 0.4), _row("THIN", 0.9, dollar_volume=5_000)]
        self.queue, self.summary = build_research_queue(_v3_scored(rows))
        self.by = {row["ticker"]: row for row in self.queue}

    def test_quintiles_run_from_five_at_the_top_to_one(self):
        quintiles = [self.by[f"S{i:02d}"]["v3_quintile"] for i in range(10)]
        self.assertEqual(quintiles, [5, 5, 4, 4, 3, 3, 2, 2, 1, 1])

    def test_each_name_gets_one_screen_status(self):
        statuses = {ticker: row["screen_status"] for ticker, row in self.by.items()}
        self.assertEqual({statuses[f"S{i:02d}"] for i in range(6)}, {"passes"})
        self.assertEqual({statuses[f"S{i:02d}"] for i in range(6, 10)}, {"low_v3_quintile"})
        self.assertEqual(statuses["REJECT"], "rejected_by_filters")
        self.assertEqual(statuses["V2ONLY"], "not_scored_by_v3")
        self.assertEqual(statuses["THIN"], "below_liquidity_floor")
        self.assertTrue(set(statuses.values()) <= set(SCREEN_STATUSES))
        self.assertIsNone(self.by["V2ONLY"]["v3_quintile"])

    def test_summary_counts_the_screen(self):
        counts = self.summary["by_screen"]
        self.assertEqual(counts["passes"], 6)
        self.assertEqual(counts["low_v3_quintile"], 4)
        self.assertEqual(sum(counts.values()), len(self.queue))
        self.assertEqual(self.summary["screen"], {"quantiles": 5, "minimum_quintile": 3})

    def test_queue_quintiles_match_the_frozen_cohort_quintiles(self):
        """The screen shown is the screen the forward test records."""
        baseline = build_research_baseline(self.queue, "RUN", "2026-11-16T00:00:00+00:00", {"top_n": 2})
        for candidate in baseline["candidates"]:
            label = candidate["classification"]
            if label.startswith("v3_quintile_"):
                self.assertEqual(int(label[-1]), self.by[candidate["ticker"]]["v3_quintile"], candidate["ticker"])


class DashboardScreenTests(unittest.TestCase):
    def test_store_filters_by_screen_and_sorts(self):
        from tests.test_dashboard_queue import ROWS, _save_run
        from src.dashboard import DashboardStore
        from src.history import index_saved_run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exports = root / "exports"
            exports.mkdir()
            run_id = _save_run(root / "runs", ROWS)
            index_saved_run(root / "history.sqlite3", root / "runs", run_id)
            queue = [
                {"research_rank": 1, "ticker": "AAA", "company_name": "Zed", "screen_status": "passes",
                 "ins_purchase_count_long": 1, "average_dollar_volume_30d": 5e5},
                {"research_rank": 2, "ticker": "BBB", "company_name": "Alpha", "screen_status": "passes",
                 "ins_purchase_count_long": 9, "average_dollar_volume_30d": 2e6},
                {"research_rank": 3, "ticker": "CCC", "company_name": "Mid", "screen_status": "low_v3_quintile",
                 "ins_purchase_count_long": 4, "average_dollar_volume_30d": 1e6},
            ]
            (exports / f"research_queue_{run_id}.json").write_text(json.dumps({
                "run_id": run_id, "summary": {"by_screen": {"passes": 2, "low_v3_quintile": 1}},
                "queue": queue,
            }), encoding="utf-8")
            store = DashboardStore(root / "history.sqlite3", root / "w.json", exports)

            passing = store.research_queue(run_id, screen="passes")
            self.assertEqual([r["ticker"] for r in passing["rows"]], ["AAA", "BBB"])
            self.assertTrue(passing["screen_available"])
            self.assertEqual([r["ticker"] for r in store.research_queue(run_id, sort="insider")["rows"]],
                             ["BBB", "CCC", "AAA"])
            self.assertEqual([r["ticker"] for r in store.research_queue(run_id, sort="liquidity")["rows"]],
                             ["BBB", "CCC", "AAA"])
            self.assertEqual([r["ticker"] for r in store.research_queue(run_id, sort="name")["rows"]],
                             ["BBB", "CCC", "AAA"])
            self.assertEqual([r["ticker"] for r in store.research_queue(run_id, sort="nonsense")["rows"]],
                             ["AAA", "BBB", "CCC"])

    def test_queue_from_before_the_screen_ignores_the_screen_filter(self):
        from tests.test_dashboard_queue import ROWS, _save_run
        from src.dashboard import DashboardStore
        from src.history import index_saved_run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exports = root / "exports"
            exports.mkdir()
            run_id = _save_run(root / "runs", ROWS)
            index_saved_run(root / "history.sqlite3", root / "runs", run_id)
            (exports / f"research_queue_{run_id}.json").write_text(json.dumps({
                "run_id": run_id, "summary": {"queued": 2},
                "queue": [{"research_rank": 1, "ticker": "AAA"}, {"research_rank": 2, "ticker": "BBB"}],
            }), encoding="utf-8")
            store = DashboardStore(root / "history.sqlite3", root / "w.json", exports)

            old = store.research_queue(run_id, screen="passes")
            self.assertFalse(old["screen_available"])
            self.assertEqual([r["ticker"] for r in old["rows"]], ["AAA", "BBB"])


if __name__ == "__main__":
    unittest.main()
