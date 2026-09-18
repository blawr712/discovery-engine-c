from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.fundamentals_pit import FundamentalHistory
from src.insider_signals import InsiderHistory
from src.rescore import rescore_results
from src.run_state import RunState, load_saved_run, record_rescore


def _fundamental_extract():
    quarters = [
        ("2025-01-01", "2025-03-31", "2025-05-01"),
        ("2025-04-01", "2025-06-30", "2025-08-01"),
        ("2025-07-01", "2025-09-30", "2025-11-01"),
        ("2025-10-01", "2025-12-31", "2026-02-15"),
    ]
    return {"ticker": "AAA", "facts": {
        "revenue": [{"start": s, "end": e, "filed": f, "val": 250.0, "tag": "us-gaap:Revenues"}
                    for s, e, f in quarters],
        "operating_income": [{"start": s, "end": e, "filed": f, "val": 25.0,
                              "tag": "us-gaap:OperatingIncomeLoss"} for s, e, f in quarters],
        "shares_outstanding": [{"start": None, "end": "2025-12-31", "filed": "2026-02-15",
                                "val": 100.0, "tag": "dei:EntityCommonStockSharesOutstanding"}],
    }}


def _insider_history():
    return InsiderHistory([{
        "issuer_cik": "1", "filed": "2026-06-01", "kind": "purchase", "shares": 10,
        "price": 5.0, "value": 50.0, "owner_ciks": ["X"], "officer_or_director": True,
    }], data_through=date(2026, 6, 30))


class RescoreResultsTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            {"ticker": "AAA", "status": "OK", "country": "US", "sector": "Tech",
             "discovery_score": 70, "score_confidence": 100, "market_cap": 2000.0,
             "v2_momentum_long": 0.5, "v2_high_proximity": 0.9, "v2_volatility": 0.3,
             "v2_momentum_medium": 0.2, "v2_volume_trend": 1.0, "v2_short_term_reversal": 0.0},
            {"ticker": "BBB", "status": "OK", "country": "US", "sector": "Tech",
             "discovery_score": 50, "score_confidence": 100, "market_cap": 3000.0,
             "pit_revenue_ttm": 900.0, "pit_sales_yield": 0.3, "pit_operating_margin_ttm": 0.1,
             "pit_shares_outstanding": 10.0, "fundamentals_status": "collected",
             "v2_momentum_long": 0.1, "v2_high_proximity": 0.7, "v2_volatility": 0.6,
             "v2_momentum_medium": 0.1, "v2_volume_trend": 1.0, "v2_short_term_reversal": 0.0},
            {"ticker": "CCC.TO", "status": "OK", "country": "CA", "discovery_score": 60,
             "score_confidence": 100, "market_cap": 1000.0, "v2_momentum_long": 0.3},
            {"ticker": "DDD", "status": "FILTERED", "discovery_score": 0, "reason_flags": "x"},
        ]

    def test_fills_cached_signals_and_reapplies_models_without_touching_official(self):
        lookups = {"AAA": FundamentalHistory(_fundamental_extract())}
        insiders = {"AAA": _insider_history(), "BBB": _insider_history()}
        rescored, stats = rescore_results(
            self.results, datetime(2026, 8, 23, tzinfo=timezone.utc),
            fundamentals_lookup=lambda t: lookups.get(t),
            insider_lookup=lambda t: insiders.get(t),
        )
        by = {row["ticker"]: row for row in rescored}

        # AAA had no fundamentals: filled from cache, using the row's market cap
        # for valuation ratios because it has no stored close.
        self.assertEqual(by["AAA"]["fundamentals_status"], "rescored")
        self.assertEqual(by["AAA"]["pit_revenue_ttm"], 1000.0)
        self.assertAlmostEqual(by["AAA"]["pit_sales_yield"], 1000.0 / 2000.0, places=6)
        self.assertEqual(by["AAA"]["pit_market_cap"], 2000.0)
        # BBB already had fundamentals: left untouched.
        self.assertEqual(by["BBB"]["fundamentals_status"], "collected")
        self.assertEqual(by["BBB"]["pit_revenue_ttm"], 900.0)
        # Insider signals refreshed for both U.S. rows, Canada untouched.
        self.assertEqual(by["AAA"]["insiders_status"], "rescored")
        self.assertEqual(by["AAA"]["ins_purchase_count_short"], 1.0)
        self.assertEqual(by["BBB"]["ins_net_count_short"], 1.0)
        self.assertNotIn("ins_purchase_count_short", by["CCC.TO"])
        # Shadow models reapplied; official fields identical.
        self.assertIn("score_v2", by["AAA"])
        self.assertIn("score_v3", by["AAA"])
        for row in self.results:
            self.assertEqual(by[row["ticker"]]["discovery_score"], row["discovery_score"])
            self.assertEqual(by[row["ticker"]]["status"], row["status"])
        self.assertNotIn("score_v2", by["DDD"])
        self.assertEqual(stats["fundamentals_filled"], 1)
        self.assertEqual(stats["fundamentals_unavailable"], 0)
        self.assertEqual(stats["insiders_filled"], 2)
        self.assertEqual(stats["not_applicable"], 1)
        self.assertEqual(stats["as_of"], "2026-08-23")
        self.assertIn("score_v3", stats["models"])

    def test_missing_caches_are_recorded_not_fatal(self):
        rescored, stats = rescore_results(
            self.results, date(2026, 8, 23),
            fundamentals_lookup=lambda t: (_ for _ in ()).throw(RuntimeError("boom")),
            insider_lookup=lambda t: None,
        )
        by = {row["ticker"]: row for row in rescored}
        self.assertEqual(by["AAA"]["fundamentals_status"], "unavailable: not cached")
        self.assertIsNone(by["AAA"]["pit_revenue_ttm"])
        self.assertEqual(by["AAA"]["insiders_status"], "no_data")
        self.assertEqual(stats["fundamentals_unavailable"], 1)
        self.assertEqual(stats["insiders_unavailable"], 2)

    def test_without_lookups_only_models_are_reapplied(self):
        rescored, stats = rescore_results(self.results, date(2026, 8, 23))
        self.assertIn("score_v2", rescored[0])
        self.assertNotIn("fundamentals_status", rescored[0])
        self.assertEqual(stats["fundamentals_filled"], 0)


class RunStateRescoreTests(unittest.TestCase):
    def test_open_refreshes_checkpoints_and_records_provenance(self):
        clock = lambda: datetime(2026, 9, 18, tzinfo=timezone.utc)  # noqa: E731
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = RunState.start_or_resume(root, "fp", 2, clock=clock)
            state.record_result(0, {"ticker": "AAA", "status": "OK", "discovery_score": 1})
            state.record_result(1, {"ticker": "BBB", "status": "FILTERED", "discovery_score": 0})
            state.complete([{"status": "OK"}, {"status": "FILTERED"}], "report.csv")
            run_id = state.run_id

            reopened = RunState.open(root, run_id, clock=clock)
            reopened.record_result(0, {"ticker": "AAA", "status": "OK", "discovery_score": 1,
                                       "score_v3": 42.0})
            manifest, results = load_saved_run(root, run_id)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["completed_count"], 2)
            self.assertEqual(results[0]["score_v3"], 42.0)

            path = record_rescore(root, run_id, {
                "models": {"score_v3": "v3"}, "report_path": "new.csv",
                "research_queue_csv_path": "queue.csv",
            }, clock=clock)
            manifest = json.loads(Path(path).read_text("utf-8"))
            self.assertEqual(manifest["rescore_artifacts"]["models"], {"score_v3": "v3"})
            self.assertEqual(manifest["report_path"], "new.csv")
            self.assertEqual(manifest["research_artifacts"]["research_queue_csv_path"], "queue.csv")
            self.assertEqual(manifest["status"], "complete")


if __name__ == "__main__":
    unittest.main()
