from collections import deque
import unittest

import pandas as pd

from src.data_sources.base import MarketDataSource
from src.data_sources.rate_limited_source import (
    ProviderCircuitOpenError,
    RateLimitedMarketDataSource,
)


class YFRateLimitError(Exception):
    pass


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedSource(MarketDataSource):
    def __init__(self, metadata=None, prices=None, shares=None):
        self.metadata = deque(metadata or [])
        self.prices = deque(prices or [])
        self.shares = deque(shares or [])
        self.metadata_calls = 0
        self.price_calls = 0
        self.share_calls = 0

    def get_stock_data(self, ticker: str) -> dict:
        self.metadata_calls += 1
        result = self.metadata.popleft() if self.metadata else {"ticker": ticker}
        if isinstance(result, Exception):
            raise result
        return result

    def get_price_history(self, ticker: str, period: str = "1y"):
        self.price_calls += 1
        result = self.prices.popleft() if self.prices else pd.DataFrame()
        if isinstance(result, Exception):
            raise result
        return result

    def get_share_history(self, ticker: str, period: str = "18mo"):
        self.share_calls += 1
        result = self.shares.popleft() if self.shares else pd.DataFrame()
        if isinstance(result, Exception):
            raise result
        return result


class RateLimitedMarketDataSourceTests(unittest.TestCase):
    def test_paces_repeated_metadata_requests(self):
        clock = FakeClock()
        provider = ScriptedSource()
        source = RateLimitedMarketDataSource(
            provider,
            metadata_interval_seconds=1.5,
            clock=clock,
            sleeper=clock.sleep,
        )

        source.get_stock_data("ONE")
        source.get_stock_data("TWO")

        self.assertEqual(clock.sleeps, [1.5])
        self.assertEqual(source.stats.pacing_waits, 1)
        self.assertEqual(source.stats.pacing_seconds, 1.5)

    def test_rate_limit_creates_shared_cooldown(self):
        clock = FakeClock()
        provider = ScriptedSource(
            metadata=[YFRateLimitError("limited"), {"ticker": "TWO"}]
        )
        source = RateLimitedMarketDataSource(
            provider,
            metadata_interval_seconds=1,
            cooldown_seconds=300,
            clock=clock,
            sleeper=clock.sleep,
        )

        with self.assertRaises(YFRateLimitError):
            source.get_stock_data("ONE")
        result = source.get_stock_data("TWO")

        self.assertEqual(result["ticker"], "TWO")
        self.assertEqual(clock.sleeps, [300])
        self.assertEqual(source.stats.cooldown_events, 1)
        self.assertEqual(source.stats.cooldown_waits, 1)

    def test_uses_separate_metadata_and_price_intervals(self):
        clock = FakeClock()
        source = RateLimitedMarketDataSource(
            ScriptedSource(),
            metadata_interval_seconds=2,
            price_interval_seconds=0.25,
            clock=clock,
            sleeper=clock.sleep,
        )

        source.get_stock_data("ONE")
        source.get_price_history("ONE")
        source.get_price_history("TWO")

        self.assertEqual(clock.sleeps, [0.25])

    def test_share_history_uses_metadata_pacing_lane(self):
        clock = FakeClock()
        provider = ScriptedSource()
        source = RateLimitedMarketDataSource(
            provider,
            metadata_interval_seconds=2,
            clock=clock,
            sleeper=clock.sleep,
        )

        source.get_stock_data("ONE")
        source.get_share_history("ONE")

        self.assertEqual(clock.sleeps, [2])
        self.assertEqual(provider.share_calls, 1)

    def test_disabled_limiter_bypasses_waits_and_cooldowns(self):
        clock = FakeClock()
        provider = ScriptedSource(
            metadata=[YFRateLimitError("limited"), {"ticker": "TWO"}]
        )
        source = RateLimitedMarketDataSource(
            provider,
            enabled=False,
            clock=clock,
            sleeper=clock.sleep,
        )

        with self.assertRaises(YFRateLimitError):
            source.get_stock_data("ONE")
        source.get_stock_data("TWO")

        self.assertEqual(clock.sleeps, [])
        self.assertEqual(source.stats.cooldown_events, 0)

    def test_rejects_negative_configuration(self):
        provider = ScriptedSource()

        with self.assertRaises(ValueError):
            RateLimitedMarketDataSource(
                provider,
                metadata_interval_seconds=-1,
            )
        with self.assertRaises(ValueError):
            RateLimitedMarketDataSource(
                provider,
                price_interval_seconds=-1,
            )
        with self.assertRaises(ValueError):
            RateLimitedMarketDataSource(provider, cooldown_seconds=-1)
        with self.assertRaises(ValueError):
            RateLimitedMarketDataSource(provider, max_cooldown_events=0)
        with self.assertRaises(TypeError):
            RateLimitedMarketDataSource(provider, max_cooldown_events=True)

    def test_opens_circuit_after_repeated_rate_limits(self):
        clock = FakeClock()
        provider = ScriptedSource(
            metadata=[
                YFRateLimitError("first"),
                YFRateLimitError("second"),
            ]
        )
        source = RateLimitedMarketDataSource(
            provider,
            cooldown_seconds=10,
            max_cooldown_events=2,
            clock=clock,
            sleeper=clock.sleep,
        )

        with self.assertRaises(YFRateLimitError):
            source.get_stock_data("ONE")
        with self.assertRaises(YFRateLimitError):
            source.get_stock_data("TWO")
        with self.assertRaises(ProviderCircuitOpenError):
            source.get_stock_data("THREE")

        self.assertEqual(provider.metadata_calls, 2)
        self.assertEqual(source.stats.circuit_open_events, 1)
        self.assertEqual(source.stats.circuit_rejections, 1)


if __name__ == "__main__":
    unittest.main()
