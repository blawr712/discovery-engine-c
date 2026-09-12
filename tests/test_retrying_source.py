from collections import deque
import unittest

import pandas as pd

from src.data_sources.base import MarketDataSource
from src.data_sources.retrying_source import (
    RetryingMarketDataSource,
    is_transient_provider_error,
)


class ScriptedSource(MarketDataSource):
    def __init__(self, metadata=None, prices=None, shares=None):
        self.metadata = deque(metadata or [])
        self.prices = deque(prices or [])
        self.shares = deque(shares or [])
        self.metadata_calls = 0
        self.price_calls = []
        self.share_calls = []

    def get_stock_data(self, ticker: str) -> dict:
        self.metadata_calls += 1
        result = self.metadata.popleft()

        if isinstance(result, Exception):
            raise result

        return result

    def get_price_history(self, ticker: str, period: str = "1y"):
        self.price_calls.append((ticker, period))
        result = self.prices.popleft()

        if isinstance(result, Exception):
            raise result

        return result

    def get_share_history(self, ticker: str, period: str = "18mo"):
        self.share_calls.append((ticker, period))
        result = self.shares.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class YFRateLimitError(Exception):
    pass


class RetryingMarketDataSourceTests(unittest.TestCase):
    def test_retries_transient_errors_with_exponential_backoff(self):
        provider = ScriptedSource(
            metadata=[
                TimeoutError("slow"),
                ConnectionError("offline"),
                {"ticker": "TEST"},
            ]
        )
        delays = []
        source = RetryingMarketDataSource(
            provider,
            max_attempts=3,
            base_delay_seconds=0.5,
            max_delay_seconds=10,
            jitter_seconds=0,
            sleeper=delays.append,
        )

        result = source.get_stock_data("TEST")

        self.assertEqual(result["ticker"], "TEST")
        self.assertEqual(provider.metadata_calls, 3)
        self.assertEqual(delays, [0.5, 1.0])
        self.assertEqual(source.stats.retries, 2)
        self.assertEqual(source.stats.exhausted, 0)

    def test_does_not_retry_permanent_errors(self):
        provider = ScriptedSource(metadata=[ValueError("bad ticker")])
        source = RetryingMarketDataSource(
            provider,
            sleeper=lambda _: self.fail("should not sleep"),
        )

        with self.assertRaises(ValueError):
            source.get_stock_data("BAD")

        self.assertEqual(provider.metadata_calls, 1)
        self.assertEqual(source.stats.retries, 0)
        self.assertEqual(source.stats.exhausted, 0)

    def test_marks_exhausted_transient_failures(self):
        provider = ScriptedSource(
            metadata=[TimeoutError("one"), TimeoutError("two")]
        )
        source = RetryingMarketDataSource(
            provider,
            max_attempts=2,
            base_delay_seconds=0,
            jitter_seconds=0,
            sleeper=lambda _: None,
        )

        with self.assertRaises(TimeoutError):
            source.get_stock_data("TEST")

        self.assertEqual(source.stats.retries, 1)
        self.assertEqual(source.stats.exhausted, 1)

    def test_preserves_price_period_across_retries(self):
        expected = pd.DataFrame({"Close": [10.0]})
        provider = ScriptedSource(
            prices=[OSError("network"), expected]
        )
        source = RetryingMarketDataSource(
            provider,
            base_delay_seconds=0,
            jitter_seconds=0,
            sleeper=lambda _: None,
        )

        result = source.get_price_history("TEST", "6mo")

        self.assertIs(result, expected)
        self.assertEqual(
            provider.price_calls,
            [("TEST", "6mo"), ("TEST", "6mo")],
        )

    def test_retries_share_history_and_preserves_period(self):
        expected = pd.DataFrame({"Shares": [10_000_000]})
        provider = ScriptedSource(
            shares=[OSError("network"), expected]
        )
        source = RetryingMarketDataSource(
            provider,
            base_delay_seconds=0,
            jitter_seconds=0,
            sleeper=lambda _: None,
        )

        result = source.get_share_history("TEST", "18mo")

        self.assertIs(result, expected)
        self.assertEqual(
            provider.share_calls,
            [("TEST", "18mo"), ("TEST", "18mo")],
        )

    def test_recognizes_rate_limit_by_provider_exception_name(self):
        self.assertTrue(is_transient_provider_error(YFRateLimitError()))
        self.assertFalse(is_transient_provider_error(ValueError()))

    def test_bounds_delay_and_adds_jitter(self):
        source = RetryingMarketDataSource(
            ScriptedSource(),
            base_delay_seconds=10,
            max_delay_seconds=3,
            jitter_seconds=0.5,
            jitter=lambda lower, upper: upper,
        )

        self.assertEqual(source._delay_before_retry(4), 3.5)

    def test_validates_retry_configuration(self):
        provider = ScriptedSource()

        with self.assertRaises(ValueError):
            RetryingMarketDataSource(provider, max_attempts=0)
        with self.assertRaises(TypeError):
            RetryingMarketDataSource(provider, max_attempts=True)
        with self.assertRaises(ValueError):
            RetryingMarketDataSource(provider, base_delay_seconds=-1)
        with self.assertRaises(ValueError):
            RetryingMarketDataSource(provider, max_delay_seconds=-1)
        with self.assertRaises(ValueError):
            RetryingMarketDataSource(provider, jitter_seconds=-1)


if __name__ == "__main__":
    unittest.main()
