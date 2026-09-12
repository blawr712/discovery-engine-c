from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.watchlists import WatchlistStore


class WatchlistStoreTests(unittest.TestCase):
    def test_create_add_remove_and_delete_are_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlists.json"
            now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
            store = WatchlistStore(path, clock=lambda: now)

            watchlist = store.create("Core research")
            item = store.add_item(watchlist["id"], " banl ")
            duplicate = store.add_item(watchlist["id"], "BANL")

            self.assertEqual(item, duplicate)
            self.assertEqual(
                WatchlistStore(path).list_all()[0]["items"][0]["ticker"], "BANL"
            )
            store.remove_item(watchlist["id"], "banl")
            self.assertEqual(store.list_all()[0]["items"], [])
            store.delete(watchlist["id"])
            self.assertEqual(store.list_all(), [])

    def test_rejects_invalid_names_symbols_and_corrupt_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlists.json"
            store = WatchlistStore(path)

            with self.assertRaisesRegex(ValueError, "name"):
                store.create("")
            watchlist = store.create("Ideas")
            with self.assertRaisesRegex(ValueError, "already exists"):
                store.create("ideas")
            with self.assertRaisesRegex(ValueError, "Ticker"):
                store.add_item(watchlist["id"], "bad ticker")
            path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                store.list_all()


if __name__ == "__main__":
    unittest.main()
