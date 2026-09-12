from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.history import connect_history_read_only, index_saved_run
from src.price_snapshots import load_price_snapshot
from src.run_state import RunState


class PriceSnapshotTests(unittest.TestCase):
    def test_loader_treats_pre_migration_database_as_no_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE runs (run_id TEXT)")
                connection.commit()
                self.assertIsNone(load_price_snapshot(connection, "run", "AAA"))

    def test_indexes_compressed_provenance_compatible_cache_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, cache = root / "runs", root / "cache"
            completed = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
            state = RunState.start_or_resume(
                runs, "fingerprint", 1, clock=lambda: completed,
            )
            row = {"ticker": "AAA", "status": "OK", "country": "US"}
            state.record_result(0, row)
            state.complete([row], "report.csv")
            self._cache(cache, "AAA", completed.timestamp() - 60)

            result = index_saved_run(
                root / "history.sqlite3", runs, state.run_id,
                price_cache_directory=cache, benchmarks={},
            )
            with closing(connect_history_read_only(root / "history.sqlite3")) as connection:
                snapshot = load_price_snapshot(connection, state.run_id, "AAA")

            self.assertEqual(result["price_snapshots"]["indexed"], 1)
            self.assertEqual(snapshot["point_count"], 2)
            self.assertEqual(snapshot["points"][1]["open"], 105)
            self.assertEqual(snapshot["points"][1]["high"], 112)
            self.assertEqual(snapshot["points"][1]["low"], 103)
            self.assertEqual(snapshot["points"][1]["close"], 110)
            self.assertEqual(snapshot["points"][1]["split"], 0.1)

            cache_path = next((cache / "prices").iterdir())
            os.utime(cache_path, (completed.timestamp() + 60,) * 2)
            second = index_saved_run(
                root / "history.sqlite3", runs, state.run_id,
                price_cache_directory=cache, benchmarks={},
            )
            with closing(connect_history_read_only(root / "history.sqlite3")) as connection:
                preserved = load_price_snapshot(connection, state.run_id, "AAA")

            self.assertEqual(second["price_snapshots"]["skipped_newer_than_run"], 1)
            self.assertEqual(preserved["points"][1]["close"], 110)

    def test_rejects_cache_file_newer_than_completed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, cache = root / "runs", root / "cache"
            completed = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
            state = RunState.start_or_resume(
                runs, "fingerprint", 1, clock=lambda: completed,
            )
            row = {"ticker": "AAA", "status": "OK", "country": "US"}
            state.record_result(0, row)
            state.complete([row], "report.csv")
            self._cache(cache, "AAA", completed.timestamp() + 60)

            result = index_saved_run(
                root / "history.sqlite3", runs, state.run_id,
                price_cache_directory=cache, benchmarks={},
            )

            self.assertEqual(result["price_snapshots"]["indexed"], 0)
            self.assertEqual(result["price_snapshots"]["skipped_newer_than_run"], 1)

    @staticmethod
    def _cache(cache, ticker, mtime):
        directory = cache / "prices"
        directory.mkdir(parents=True)
        digest = hashlib.sha256(f"{ticker}|1y".encode()).hexdigest()
        path = directory / f"{digest}.json"
        path.write_text(json.dumps({"data": [
            {"Date": "2026-08-01T00:00:00.000", "Open": 98, "High": 102,
             "Low": 97, "Close": 100, "Volume": 10},
            {"Date": "2026-08-08T00:00:00.000", "Open": 105, "High": 112,
             "Low": 103, "Close": 110, "Volume": 20,
             "Stock Splits": 0.1, "Dividends": 0},
        ]}), encoding="utf-8")
        os.utime(path, (mtime, mtime))


if __name__ == "__main__":
    unittest.main()
