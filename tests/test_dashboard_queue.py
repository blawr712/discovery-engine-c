from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.request import urlopen

from src.dashboard import DashboardStore, create_dashboard_server
from src.history import index_saved_run
from src.run_state import RunState


def _save_run(runs: Path, rows: list[dict]) -> str:
    clock = lambda: datetime(2026, 9, 18, tzinfo=timezone.utc)  # noqa: E731
    state = RunState.start_or_resume(runs, "fp-queue", len(rows), clock=clock)
    for index, row in enumerate(rows):
        state.record_result(index, row)
    state.complete(rows, "report.csv")
    return state.run_id


ROWS = [
    {"ticker": "AAA", "status": "OK", "country": "US", "sector": "Technology",
     "discovery_score": 60, "score_confidence": 100, "currency": "USD",
     "score_v3": 80.0, "score_v3_rank": 1, "score_v3_confidence": 90.0,
     "score_v3_excluded": False, "score_v3_exclusion_reasons": "",
     "score_v3_model_version": "v3", "score_v3_breakdown": json.dumps({
         "pit_sales_yield": {"points": 20.0, "max_points": 25.0, "available": True,
                             "applicable": True, "explanation": "cheap"}}),
     "score_v2": 55.0, "score_v2_rank": 2, "pit_sales_yield": 1.5,
     "pit_revenue_ttm": 1e8, "fundamentals_status": "collected",
     "pit_data_quality": "filed", "ins_purchase_count_short": 2.0,
     "ins_data_through": "2026-03-31", "insiders_status": "collected"},
    {"ticker": "BBB", "status": "OK", "country": "CA", "sector": "Energy",
     "discovery_score": 70, "score_confidence": 100, "score_v3": None,
     "score_v3_excluded": True,
     "score_v3_exclusion_reasons": "Top-quintile share dilution",
     "score_v2": 65.0, "score_v2_rank": 1},
]


def _queue_document(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "summary": {"queued": 2, "by_basis": {"score_v3": 1, "score_v2": 1},
                    "excluded_by_primary": 1,
                    "exclusion_reasons": {"Top-quintile share dilution": 1}},
        "queue": [
            {"research_rank": 1, "ranking_basis": "score_v3", "ticker": "AAA",
             "company_name": "Aaa Co", "country": "US", "sector": "Technology",
             "score_v3": 80.0, "score_v3_excluded": False, "discovery_rank": 2},
            {"research_rank": 2, "ranking_basis": "score_v2", "ticker": "BBB",
             "company_name": "Bbb Co", "country": "CA", "sector": "Energy",
             "score_v3": None, "score_v3_excluded": True,
             "score_v3_exclusion_reasons": "Top-quintile share dilution",
             "discovery_rank": 1},
        ],
    }


def _backtest_document() -> dict:
    return {
        "model_version": "bt",
        "coverage": {"periods": 3, "first_period": "2026-01", "last_period": "2026-03",
                     "usable_tickers": 2, "observations": 6},
        "config": {"forward_horizons_days": {"1M": 21}, "quantiles": 5, "top_n": [25]},
        "aggregate": {
            "information_coefficient": {
                "score_v3": {"1M": {"mean": 0.1, "t_stat": 3.0, "positive_share_percent": 70, "periods": 3}},
                "pit_sales_yield": {"1M": {"mean": 0.2, "t_stat": 5.0, "positive_share_percent": 90, "periods": 3}},
            },
            "quantiles": {"score_v3": {"1M": {
                "median_excess": [1, 2, 3, 4, 5], "median_spread": 4,
                "trimmed_mean_excess": [1, 2, 3, 4, 5],
                "win_rate_percent": [30, 35, 40, 45, 50],
                "mean_excess": [1, 2, 3, 4, 5], "spread": 4}}},
            "top_n": {"score_v3": {"25": {"1M": {"mean_excess": 1.0, "median_excess": -1.0,
                                                "hit_rate_percent": 40.0, "periods": 3}}}},
            "compounded": {"score_v3": {"25": {"portfolio_return_percent": 10.0,
                                               "benchmark_return_percent": 5.0}}},
            "turnover": {"score_v3": {"25": 20.0}},
        },
        "filter_diagnostics": {"score_v3": {
            "rules": ["Top-quintile share dilution"],
            "horizons": {"1M": {
                "excluded": {"observations": 1, "median_excess": -5, "trimmed_mean_excess": -4,
                             "win_rate_percent": 20},
                "retained": {"observations": 5, "median_excess": 1, "trimmed_mean_excess": 1,
                             "win_rate_percent": 55}}}}},
        "limitations": ["Survivorship."],
    }


class ResearchQueueAndEvidenceTests(unittest.TestCase):
    def _fixture(self, root: Path):
        runs = root / "runs"
        database = root / "history.sqlite3"
        exports = root / "exports"
        exports.mkdir()
        run_id = _save_run(runs, ROWS)
        index_saved_run(database, runs, run_id)
        (exports / f"research_queue_{run_id}.json").write_text(
            json.dumps(_queue_document(run_id)), encoding="utf-8",
        )
        (exports / f"backtest_{run_id}.json").write_text(
            json.dumps(_backtest_document()), encoding="utf-8",
        )
        return database, exports, run_id

    def test_store_serves_queue_evidence_and_shadow_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            database, exports, run_id = self._fixture(Path(directory))
            store = DashboardStore(database, Path(directory) / "w.json", exports)

            queue = store.research_queue(run_id)
            self.assertTrue(queue["available"])
            self.assertEqual([r["ticker"] for r in queue["rows"]], ["AAA", "BBB"])
            self.assertEqual(queue["sectors"], ["Energy", "Technology"])
            self.assertEqual(store.research_queue(run_id, basis="score_v3")["rows"][0]["ticker"], "AAA")
            self.assertEqual(store.research_queue(run_id, search="bbb")["total"], 1)
            self.assertEqual(store.research_queue(run_id, country="CA")["rows"][0]["ticker"], "BBB")
            self.assertEqual(store.research_queue(run_id, hide_excluded=True)["total"], 1)
            self.assertFalse(store.research_queue("nope")["available"])

            evidence = store.evidence(run_id)
            self.assertTrue(evidence["available"])
            self.assertEqual(evidence["composites"], ["score_v3"])
            self.assertIn("pit_sales_yield", evidence["information_coefficient"])
            self.assertEqual(evidence["quantiles"]["score_v3"]["1M"]["median_spread"], 4)
            self.assertEqual(
                evidence["filter_diagnostics"]["score_v3"]["rules"],
                ["Top-quintile share dilution"],
            )
            fallback = store.evidence("missing")
            self.assertTrue(fallback["available"])
            self.assertEqual(fallback["fallback_from_run_id"], run_id)
            self.assertIsNone(evidence["fallback_from_run_id"])

            detail = store.candidate_detail("AAA", run_id)
            models = {m["model"]: m for m in detail["shadow_models"]}
            self.assertEqual(models["score_v3"]["status"], "scored")
            self.assertEqual(models["score_v3"]["signals"][0]["percentile"], 80.0)
            self.assertEqual(detail["model_ranks"]["research_rank"], 1)
            self.assertEqual(detail["model_ranks"]["ranking_basis"], "score_v3")
            self.assertEqual(detail["model_ranks"]["official_rank"], 2)
            self.assertTrue(detail["fundamentals"]["available"])
            self.assertEqual(detail["insiders"]["data_through"], "2026-03-31")
            self.assertIn("score_v3", detail["shadow_glossary"])
            excluded = store.candidate_detail("BBB", run_id)
            statuses = {m["model"]: m["status"] for m in excluded["shadow_models"]}
            self.assertEqual(statuses["score_v3"], "excluded")

    def test_http_routes_for_queue_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            database, exports, run_id = self._fixture(Path(directory))
            server = create_dashboard_server(
                database, port=0, watchlist_path=Path(directory) / "w.json",
                moonshot_directory=exports,
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                queue = json.loads(urlopen(
                    f"{base}/api/research-queue?run_id={run_id}"
                ).read())
                self.assertEqual(queue["total"], 2)
                evidence = json.loads(urlopen(f"{base}/api/evidence?run_id={run_id}").read())
                self.assertTrue(evidence["available"])
                html = urlopen(f"{base}/").read().decode("utf-8")
                for marker in ("id=\"queue\"", "id=\"evidence\"", "loadQueue", "shadowSection"):
                    self.assertIn(marker, html)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
