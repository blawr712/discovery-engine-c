from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.history import index_saved_run
from src.history_comparison import compare_indexed_runs, export_run_comparison
from src.run_state import RunState


class HistoryComparisonTests(unittest.TestCase):
    def test_classifies_presence_status_rank_score_and_quality_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            database = root / "history.sqlite3"
            old_id = self._saved_run(runs, datetime(2026, 8, 1, tzinfo=timezone.utc), [
                self._row("A", "OK", 90, "fresh"),
                self._row("B", "OK", 70, "fresh"),
                self._row("C", "FILTERED", 20, "missing"),
                self._row("E", "FILTERED", 10, "missing"),
            ])
            new_id = self._saved_run(runs, datetime(2026, 8, 8, tzinfo=timezone.utc), [
                self._row("A", "OK", 60, "stale"),
                self._row("B", "OK", 80, "fresh"),
                self._row("C", "OK", 75, "fresh"),
                self._row("D", "FILTERED", 10, "missing"),
            ])
            index_saved_run(database, runs, old_id)
            index_saved_run(database, runs, new_id)

            comparison = compare_indexed_runs(database, old_id, new_id)
            rows = {row["ticker"]: row for row in comparison["rows"]}
            csv_path, json_path = export_run_comparison(comparison, root / "exports")

            self.assertEqual(comparison["summary"]["entrants"], 1)
            self.assertEqual(comparison["summary"]["exits"], 1)
            self.assertEqual(comparison["summary"]["status_transitions"], 1)
            self.assertEqual(rows["A"]["rank_change"], -2)
            self.assertEqual(rows["B"]["rank_change"], 1)
            self.assertIn("data_quality_change", rows["A"]["change_types"])
            self.assertIn("status_transition", rows["C"]["change_types"])
            self.assertEqual(rows["D"]["presence"], "entrant")
            self.assertEqual(rows["E"]["presence"], "exit")
            self.assertEqual(
                comparison["composition_changes"]["country"],
                [{"country": "US", "old_count": 4, "new_count": 4, "change": 0}],
            )
            transitions = comparison["status_transition_matrix"]
            self.assertIn(
                {"old_status": "FILTERED", "new_status": "OK", "count": 1},
                transitions,
            )
            self.assertTrue(csv_path.is_file())
            self.assertEqual(json.loads(json_path.read_text())["old_run_id"], old_id)

    def test_requires_distinct_indexed_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing.sqlite3"
            with self.assertRaisesRegex(ValueError, "different run IDs"):
                compare_indexed_runs(database, "same", "same")

    @staticmethod
    def _row(ticker, status, score, quality):
        return {
            "ticker": ticker, "status": status, "country": "US",
            "exchange": "NYSE", "discovery_score": score,
            "score_confidence": score / 2,
            "fundamental_confidence": score / 4,
            "fundamental_data_quality": quality,
        }

    @staticmethod
    def _saved_run(runs, now, rows):
        state = RunState.start_or_resume(
            runs, f"fingerprint-{now.date()}", len(rows), clock=lambda: now,
        )
        for position, row in enumerate(rows):
            state.record_result(position, row)
        state.complete(rows, "report.csv")
        return state.run_id


if __name__ == "__main__":
    unittest.main()
