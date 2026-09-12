from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.history import index_saved_run
from src.history_reporting import (
    build_ticker_history,
    build_weekly_report,
    export_ticker_history,
    export_weekly_report,
)
from src.run_state import RunState


class HistoryReportingTests(unittest.TestCase):
    def test_builds_chronological_ticker_timeline_and_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            database = root / "history.sqlite3"
            old_id = self._save(runs, "old", datetime(2026, 8, 1, tzinfo=timezone.utc), [
                self._row("AAA", "OK", 80), self._row("BBB", "OK", 70),
            ])
            new_id = self._save(runs, "new", datetime(2026, 8, 8, tzinfo=timezone.utc), [
                self._row("AAA", "OK", 60), self._row("BBB", "OK", 90),
            ])
            index_saved_run(database, runs, new_id)
            index_saved_run(database, runs, old_id)

            history = build_ticker_history(database, "aaa")
            csv_path, json_path = export_ticker_history(history, root / "exports")

            self.assertEqual(history["appearances"], 2)
            self.assertEqual(history["best_rank"], 1)
            self.assertEqual(history["worst_rank"], 2)
            self.assertEqual(history["timeline"][1]["rank_change"], -1)
            self.assertEqual(history["timeline"][1]["score_change"], -20)
            self.assertTrue(csv_path.is_file())
            self.assertEqual(json.loads(json_path.read_text())["ticker"], "AAA")

    def test_weekly_report_selects_latest_matching_complete_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            database = root / "history.sqlite3"
            old_id = self._save(runs, "old", datetime(2026, 8, 1, tzinfo=timezone.utc), [
                self._row("AAA", "FILTERED", 40), self._row("BBB", "OK", 70),
            ])
            new_id = self._save(runs, "new", datetime(2026, 8, 8, tzinfo=timezone.utc), [
                self._row("AAA", "OK", 80), self._row("BBB", "FILTERED", 30),
            ])
            small_id = self._save(runs, "small", datetime(2026, 8, 9, tzinfo=timezone.utc), [
                self._row("AAA", "OK", 85),
            ])
            for run_id in (old_id, new_id, small_id):
                index_saved_run(database, runs, run_id)

            report = build_weekly_report(database)
            markdown, csv_path, json_path = export_weekly_report(
                report, root / "exports"
            )

            self.assertEqual(report["old_run_id"], old_id)
            self.assertEqual(report["new_run_id"], new_id)
            self.assertEqual(report["promotions_to_ok"][0]["ticker"], "AAA")
            self.assertEqual(report["demotions_from_ok"][0]["ticker"], "BBB")
            self.assertEqual(
                report["compatibility"]["classification"],
                "same_universe_configuration_changed",
            )
            self.assertTrue(report["compatibility"]["same_universe_membership"])
            self.assertIn("Compatibility warnings", markdown.read_text())
            self.assertTrue(csv_path.is_file())
            self.assertTrue(json_path.is_file())

    @staticmethod
    def _row(ticker, status, score):
        return {
            "ticker": ticker, "status": status, "country": "US",
            "sector": "Technology", "exchange": "NASDAQ",
            "discovery_score": score, "score_confidence": 80,
            "fundamental_confidence": 60,
            "fundamental_data_quality": "fresh",
        }

    @staticmethod
    def _save(runs, fingerprint, now, rows):
        state = RunState.start_or_resume(
            runs, fingerprint, len(rows), clock=lambda: now,
        )
        for position, row in enumerate(rows):
            state.record_result(position, row)
        state.complete(rows, "report.csv")
        return state.run_id


if __name__ == "__main__":
    unittest.main()
