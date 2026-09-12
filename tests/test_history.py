from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.history import connect_history_read_only, history_summary, index_saved_run
from src.run_state import RunState


class HistoryTests(unittest.TestCase):
    def test_indexes_completed_run_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            database = root / "history.sqlite3"
            clock = lambda: datetime(2026, 8, 9, tzinfo=timezone.utc)
            state = RunState.start_or_resume(runs, "fingerprint", 2, clock=clock)
            rows = [
                {"ticker": "ONE", "status": "OK", "country": "US", "discovery_score": 55},
                {"ticker": "TWO.TO", "status": "FILTERED", "country": "CA"},
            ]
            for position, row in enumerate(rows):
                state.record_result(position, row)
            state.complete(rows, "report.csv")

            first = index_saved_run(database, runs, state.run_id)
            second = index_saved_run(database, runs, state.run_id)
            summary = history_summary(database)
            with closing(sqlite3.connect(database)) as connection:
                statuses = dict(connection.execute(
                    "SELECT status, COUNT(*) FROM results GROUP BY status"
                ).fetchall())

        self.assertEqual(first["indexed_results"], 2)
        self.assertEqual(second["indexed_results"], 2)
        self.assertEqual(summary["run_count"], 1)
        self.assertEqual(summary["result_count"], 2)
        self.assertEqual(summary["latest_run"]["run_id"], state.run_id)
        self.assertEqual(statuses, {"FILTERED": 1, "OK": 1})

    def test_empty_history_summary_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            summary = history_summary(path)

            self.assertEqual(summary["run_count"], 0)
            self.assertFalse(path.exists())

    def test_read_only_connection_rejects_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            database = root / "history.sqlite3"
            clock = lambda: datetime(2026, 8, 9, tzinfo=timezone.utc)
            state = RunState.start_or_resume(runs, "fingerprint", 0, clock=clock)
            state.complete([], "report.csv")
            index_saved_run(database, runs, state.run_id)

            with closing(connect_history_read_only(database)) as connection:
                with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    connection.execute("DELETE FROM runs")


if __name__ == "__main__":
    unittest.main()
