import json
from pathlib import Path
import tempfile
import time
import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from src.data_sources.base import MarketDataSource
from src.data_sources.cached_source import CachedMarketDataSource


class FakeMarketDataSource(MarketDataSource):
    def __init__(self):
        self.metadata_calls = 0
        self.history_calls = 0
        self.share_calls = 0

    def get_stock_data(self, ticker: str) -> dict:
        self.metadata_calls += 1
        return {"ticker": ticker, "market_cap": 100_000_000}

    def get_price_history(
        self,
        ticker: str,
        period: str = "1y",
    ) -> pd.DataFrame:
        self.history_calls += 1
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    ["2026-07-20", "2026-07-21"]
                ).astype("datetime64[ns]"),
                "Close": [10.0, 11.0],
                "Volume": [1000, 1200],
            }
        )

    def get_share_history(
        self,
        ticker: str,
        period: str = "18mo",
    ) -> pd.DataFrame:
        self.share_calls += 1
        return pd.DataFrame({
            "Date": pd.to_datetime(["2025-01-01", "2026-01-01"], utc=True),
            "Shares": [10_000_000, 12_000_000],
        })


class CachedMarketDataSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.cache_directory = Path(self.temporary_directory.name)
        self.provider = FakeMarketDataSource()
        self.now = time.time()
        self.source = CachedMarketDataSource(
            self.provider,
            self.cache_directory,
            metadata_ttl_hours=1,
            price_history_ttl_hours=1,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_caches_metadata_across_source_instances(self):
        first = self.source.get_stock_data("TEST")
        next_run = CachedMarketDataSource(
            self.provider,
            self.cache_directory,
            metadata_ttl_hours=1,
            clock=lambda: self.now,
        )
        second = next_run.get_stock_data("TEST")

        self.assertEqual(first, second)
        self.assertEqual(self.provider.metadata_calls, 1)
        self.assertEqual(self.source.stats.misses, 1)
        self.assertEqual(next_run.stats.hits, 1)

    def test_caches_price_history_by_ticker_and_period(self):
        first = self.source.get_price_history("TEST", "1y")
        second = self.source.get_price_history("TEST", "1y")
        self.source.get_price_history("TEST", "6mo")

        assert_frame_equal(first, second)
        self.assertEqual(self.provider.history_calls, 2)
        self.assertEqual(self.source.stats.hits, 1)
        self.assertEqual(self.source.stats.misses, 2)

    def test_caches_share_history_by_ticker_and_period(self):
        first = self.source.get_share_history("TEST")
        second = self.source.get_share_history("TEST")

        assert_frame_equal(first, second, check_dtype=False)
        self.assertEqual(self.provider.share_calls, 1)
        self.assertEqual(self.source.stats.hits, 1)
        self.assertEqual(self.source.stats.misses, 1)

    def test_refreshes_expired_cache_entries(self):
        self.source.get_stock_data("TEST")
        self.now += 3601
        self.source.get_stock_data("TEST")

        self.assertEqual(self.provider.metadata_calls, 2)
        self.assertEqual(self.source.stats.expired, 1)

    def test_recovers_from_corrupt_metadata(self):
        self.source.get_stock_data("TEST")
        cache_file = next((self.cache_directory / "metadata").iterdir())
        cache_file.write_text("not json", encoding="utf-8")

        result = self.source.get_stock_data("TEST")

        self.assertEqual(result["ticker"], "TEST")
        self.assertEqual(self.provider.metadata_calls, 2)
        self.assertEqual(self.source.stats.read_errors, 1)
        with cache_file.open("r", encoding="utf-8") as file:
            self.assertIsInstance(json.load(file), dict)

    def test_recovers_from_corrupt_price_history(self):
        expected = self.source.get_price_history("TEST")
        cache_file = next((self.cache_directory / "prices").iterdir())
        cache_file.write_text("not table json", encoding="utf-8")

        result = self.source.get_price_history("TEST")

        assert_frame_equal(result, expected)
        self.assertEqual(self.provider.history_calls, 2)
        self.assertEqual(self.source.stats.read_errors, 1)

    def test_disabled_cache_bypasses_disk_and_statistics(self):
        source = CachedMarketDataSource(
            self.provider,
            self.cache_directory / "disabled",
            enabled=False,
        )

        source.get_stock_data("TEST")
        source.get_stock_data("TEST")

        self.assertEqual(self.provider.metadata_calls, 2)
        self.assertEqual(source.stats.hits, 0)
        self.assertEqual(source.stats.misses, 0)
        self.assertFalse((self.cache_directory / "disabled").exists())

    def test_rejects_negative_ttl_values(self):
        with self.assertRaises(ValueError):
            CachedMarketDataSource(
                self.provider,
                self.cache_directory,
                metadata_ttl_hours=-1,
            )

    def test_metadata_version_invalidates_old_schema_entries(self):
        first = CachedMarketDataSource(
            self.provider,
            self.cache_directory,
            metadata_version="v1",
        )
        second = CachedMarketDataSource(
            self.provider,
            self.cache_directory,
            metadata_version="v2",
        )

        first.get_stock_data("TEST")
        second.get_stock_data("TEST")

        self.assertEqual(self.provider.metadata_calls, 2)

    def test_rejects_empty_metadata_version(self):
        with self.assertRaises(ValueError):
            CachedMarketDataSource(
                self.provider,
                self.cache_directory,
                metadata_version=" ",
            )


if __name__ == "__main__":
    unittest.main()
