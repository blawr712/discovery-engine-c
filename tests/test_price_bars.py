"""Bars without a close are dropped, and rescores rebuild what they cost."""

from datetime import date
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np
import pandas as pd

from src.data_sources.cached_source import CachedMarketDataSource
from src.data_sources.price_bars import drop_incomplete_bars
from src.rescore import rescore_results
from src.split_adjustment import SplitHistory
from tests.test_cached_source import FakeMarketDataSource


def _history(days=260, last_close=None, split_on_last=0.0):
    dates = pd.bdate_range("2025-09-01", periods=days)
    close = np.linspace(10.0, 20.0, days)
    frame = pd.DataFrame({
        "Date": dates, "Open": close, "High": close, "Low": close, "Close": close,
        "Volume": np.full(days, 100_000.0), "Dividends": 0.0, "Stock Splits": 0.0,
    })
    frame.loc[days - 1, ["Open", "High", "Low", "Close"]] = last_close
    frame.loc[days - 1, "Stock Splits"] = split_on_last
    return frame


class DropTests(unittest.TestCase):
    def test_a_trailing_bar_without_a_close_is_dropped(self):
        cleaned = drop_incomplete_bars(_history(last_close=np.nan))
        self.assertEqual(len(cleaned), 259)
        self.assertTrue(np.isfinite(cleaned["Close"].iloc[-1]))

    def test_complete_histories_come_back_unchanged(self):
        frame = _history(last_close=21.0)
        self.assertIs(drop_incomplete_bars(frame), frame)

    def test_a_split_on_a_dropped_bar_is_kept(self):
        frame = _history(last_close=np.nan, split_on_last=0.1)
        cleaned = drop_incomplete_bars(frame)
        self.assertEqual(cleaned["Stock Splits"].iloc[-1], 0.1)
        self.assertEqual(len(SplitHistory.from_price_history(cleaned).events), 1)

    def test_interior_bars_without_a_close_go_too(self):
        frame = _history(last_close=21.0)
        frame.loc[100, "Close"] = np.nan
        frame.loc[101, "Close"] = 0.0
        self.assertEqual(len(drop_incomplete_bars(frame)), 258)

    def test_empty_or_closeless_frames_pass_through(self):
        self.assertIsNone(drop_incomplete_bars(None))
        empty = pd.DataFrame()
        self.assertIs(drop_incomplete_bars(empty), empty)


class CacheTests(unittest.TestCase):
    def test_cached_histories_are_cleaned_and_readable_at_any_age(self):
        provider = FakeMarketDataSource()
        provider.get_price_history = lambda ticker, period="1y": _history(last_close=np.nan)
        with tempfile.TemporaryDirectory() as directory:
            now = [time.time()]
            source = CachedMarketDataSource(provider, Path(directory), price_history_ttl_hours=1,
                                            clock=lambda: now[0])
            source.get_price_history("TEST")  # written to cache as the provider sent it
            self.assertEqual(len(source.get_cached_price_history("TEST")), 259)
            now[0] += 48 * 3600
            self.assertIsNone(source.get_cached_price_history("TEST"))
            self.assertEqual(len(source.get_cached_price_history("TEST", any_age=True)), 259)


class RescoreTests(unittest.TestCase):
    def _row(self, **extra):
        return {"ticker": "AAA", "status": "OK", "country": "US", "sector": "Industrials",
                "discovery_score": 25.95, "score_confidence": 100, "reason_flags": "",
                "latest_close": None, "v2_high_proximity": None, "v2_volatility": None, **extra}

    def test_price_signals_are_rebuilt_from_the_cached_history(self):
        history = drop_incomplete_bars(_history(last_close=np.nan))
        rescored, stats = rescore_results(
            [self._row()], date(2026, 10, 20), price_history_lookup=lambda ticker: history,
        )
        row = rescored[0]
        self.assertEqual(stats["prices_repaired"], 1)
        self.assertAlmostEqual(row["latest_close"], float(history["Close"].iloc[-1]))
        self.assertIsNotNone(row["v2_high_proximity"])
        self.assertIsNotNone(row["v2_volatility"])
        self.assertEqual(row["discovery_score"], 25.95)  # official score untouched

    def test_rows_with_a_close_are_left_alone(self):
        rescored, stats = rescore_results(
            [self._row(latest_close=12.0, v2_high_proximity=0.5)], date(2026, 10, 20),
            price_history_lookup=lambda ticker: self.fail("should not be read"),
        )
        self.assertEqual(stats["prices_repaired"], 0)
        self.assertEqual(rescored[0]["v2_high_proximity"], 0.5)


if __name__ == "__main__":
    unittest.main()
