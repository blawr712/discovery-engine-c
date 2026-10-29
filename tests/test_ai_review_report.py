import hashlib
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from src.ai_review_report import build_report, load_peer_tables, report_names
from src.cli import parse_args
from src.dashboard import DashboardStore, create_dashboard_server
from src.history import index_saved_run
from tests.test_dashboard_queue import ROWS, _queue_document, _save_run

PEERS = {
    "status": "ok", "sic_used": "3841", "widened": False,
    "peers": [{"ticker": "PEER", "group": "closest"}, {"ticker": "BIG", "group": "leader"}],
    "summary": {"pit_earnings_yield": {"company": 0.12, "closest_median": 0.04,
                                       "leader_median": 0.03, "company_percentile_among_closest": 90.0}},
}


def artifact(dry_run=True):
    peers_sha = hashlib.sha256(json.dumps(PEERS).encode("utf-8")).hexdigest()
    return {
        "run_id": "RUN", "dry_run": dry_run, "anchor_datetime": "2026-10-20T00:55:00+00:00",
        "frozen_at": "2026-10-29T00:00:00+00:00", "checklist_version": "review-v1",
        "skill_version": "ai-review-skill-1", "models": ["claude-test"], "artifact_sha256": "abc",
        "data_check_disagreements": [{"ticker": "BBB", "field": "pit_net_cash", "ours": 1e8, "filing": -1.3e9}],
        "names": [
            {"ticker": "BBB", "status": "valid", "verdict": "avoid", "peers_sha256": peers_sha,
             "filings": [{"sha256": "s1", "form": "10-Q", "filing_date": "2026-07-31",
                          "url": "https://www.sec.gov/Archives/edgar/data/1/q.htm"},
                         {"sha256": "s2", "form": "8-K", "filing_date": "2026-09-01",
                          "url": "javascript:alert(1)"}],
             "review": {
                 "verdict": "avoid", "deciding_reason": "Debt is <b>missing</b> from our net cash.",
                 "data_check": [{"field": "pit_net_cash", "ours": 1e8, "filing": -1.3e9, "agrees": False}],
                 "findings": [
                     {"area": "business", "severity": "info", "text": "Sells rentals.",
                      "citation": {"sha256": "s2", "quote": "We rent furniture to customers."}},
                     {"area": "balance_sheet", "severity": "serious", "text": "Large debt.",
                      "citation": {"sha256": "s1", "quote": "Total debt of $1.44 billion"}},
                 ],
                 "peer_comparison": {"discount_explained": "yes", "text": "Debt explains it.",
                                     "metrics_cited": ["pit_earnings_yield"]},
                 "bear_case": "Leverage.", "unanswered_questions": ["When does it mature?"],
                 "evidence_through": "2026-09-01"}},
            {"ticker": "AAA", "status": "validation_failed", "verdict": None,
             "errors": ["quote not found"], "filings": []},
            {"ticker": "DR.TO", "status": "not_reviewed", "verdict": "not_reviewed",
             "review": {"verdict": "not_reviewed", "deciding_reason": "No SEC registrant."}},
        ],
    }


class ReportTests(unittest.TestCase):
    def test_report_keeps_queue_order_escapes_text_and_links_only_to_sec(self):
        html, markdown = build_report(artifact(), {"BBB": PEERS})
        self.assertIn("Dry run: not part of the forward test.", html)
        # Queue order, not verdict order.
        self.assertLess(html.index("1. BBB"), html.index("2. AAA"))
        self.assertLess(html.index("2. AAA"), html.index("3. DR.TO"))
        self.assertIn("Debt is &lt;b&gt;missing&lt;/b&gt;", html)
        self.assertNotIn("<b>missing</b>", html)
        self.assertIn("href='https://www.sec.gov/Archives/edgar/data/1/q.htm'", html)
        self.assertNotIn("javascript:", html)
        # Serious findings come first.
        self.assertLess(html.index("Large debt."), html.index("Sells rentals."))
        self.assertIn("This review failed validation", html)
        self.assertIn("12.0%", html)  # company earnings yield in the peer table
        self.assertIn("No horizon has resolved yet.", html)
        self.assertIn("| BBB | pit_net_cash |", markdown)
        self.assertIn("### 1. BBB: Avoid", markdown)

    def test_outcomes_appear_once_resolved(self):
        comparison = {"horizons": {"6M": {
            "by_verdict": {"clear": {"median_excess_percent": 4.0, "resolved": 10},
                           "caution": {"median_excess_percent": -2.0, "resolved": 5},
                           "avoid": {"median_excess_percent": -9.0, "resolved": 3}},
            "clear_minus_flagged_median_percent": 7.5}}}
        html, _ = build_report(artifact(), {}, comparison)
        self.assertIn("+7.5 pts", html)
        self.assertIn("Peer table not available.", html)

    def test_peer_tables_load_only_when_their_hash_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "BBB"
            folder.mkdir()
            (folder / "peers.json").write_text(json.dumps(PEERS), encoding="utf-8")
            self.assertIn("BBB", load_peer_tables(artifact(), Path(directory)))
            (folder / "peers.json").write_text(json.dumps({**PEERS, "status": "changed"}), encoding="utf-8")
            self.assertEqual(load_peer_tables(artifact(), Path(directory)), {})

    def test_names_and_cli(self):
        self.assertEqual(report_names("RUN", True)[0], "ai_review_dry_run_report_RUN.html")
        self.assertEqual(report_names("RUN", False)[1], "ai_review_report_RUN.md")
        self.assertEqual(parse_args(["--ai-review-report", "RUN"]).ai_review_report, "RUN")


class DashboardTests(unittest.TestCase):
    def test_queue_rows_carry_the_verdict_without_changing_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, database, exports = root / "runs", root / "history.sqlite3", root / "exports"
            exports.mkdir()
            run_id = _save_run(runs, ROWS)
            index_saved_run(database, runs, run_id)
            (exports / f"research_queue_{run_id}.json").write_text(json.dumps(_queue_document(run_id)), encoding="utf-8")
            store = DashboardStore(database, root / "w.json", exports)
            self.assertFalse(store.research_queue(run_id)["ai_review"]["available"])

            frozen = {**artifact(), "run_id": run_id}
            (exports / f"ai_review_dry_run_{run_id}.json").write_text(json.dumps(frozen), encoding="utf-8")
            queue = store.research_queue(run_id)
            self.assertEqual([row["ticker"] for row in queue["rows"]], ["AAA", "BBB"])
            self.assertEqual(queue["rows"][1]["ai_review"]["verdict"], "avoid")
            self.assertIsNone(queue["rows"][0]["ai_review"]["verdict"])
            self.assertTrue(queue["ai_review"]["dry_run"])
            self.assertIsNone(queue["ai_review"]["report_url"])

            html, _ = build_report(frozen)
            (exports / report_names(run_id, True)[0]).write_text(html, encoding="utf-8")
            self.assertEqual(store.research_queue(run_id)["ai_review"]["report_url"], f"/ai-review/{run_id}")

            server = create_dashboard_server(database, port=0, moonshot_directory=exports)
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                page = urlopen(f"http://127.0.0.1:{port}/ai-review/{run_id}").read().decode("utf-8")
                self.assertIn("AI review of the top screen names", page)
                with self.assertRaises(HTTPError):
                    urlopen(f"http://127.0.0.1:{port}/ai-review/..%2Fsecret")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
