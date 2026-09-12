"""Retry transient provider failures with bounded exponential backoff."""

from __future__ import annotations

from dataclasses import dataclass
import random
import threading
import time
from typing import Callable, TypeVar

from .base import MarketDataSource


T = TypeVar("T")
RetryPredicate = Callable[[Exception], bool]


@dataclass
class RetryStats:
    """Track retry behavior during one engine run."""

    retries: int = 0
    exhausted: int = 0


def is_transient_provider_error(error: Exception) -> bool:
    """Identify network, timeout, and provider rate-limit failures."""
    return isinstance(error, (ConnectionError, TimeoutError, OSError)) or (
        is_rate_limit_error(error)
    )


def is_rate_limit_error(error: Exception) -> bool:
    """Identify Yahoo's rate-limit exception without coupling to yfinance."""
    return type(error).__name__ == "YFRateLimitError"


class RetryingMarketDataSource(MarketDataSource):
    """Decorate a market-data source with bounded transient retries."""

    def __init__(
        self,
        source: MarketDataSource,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 8,
        jitter_seconds: float = 0.25,
        retry_predicate: RetryPredicate = is_transient_provider_error,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if base_delay_seconds < 0:
            raise ValueError("base_delay_seconds cannot be negative.")
        if max_delay_seconds < 0:
            raise ValueError("max_delay_seconds cannot be negative.")
        if jitter_seconds < 0:
            raise ValueError("jitter_seconds cannot be negative.")

        self.source = source
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter_seconds = jitter_seconds
        self.retry_predicate = retry_predicate
        self.sleeper = sleeper
        self.jitter = jitter
        self.stats = RetryStats()
        self._stats_lock = threading.Lock()

    def get_stock_data(self, ticker: str) -> dict:
        """Fetch metadata, retrying only transient provider failures."""
        return self._execute(lambda: self.source.get_stock_data(ticker))

    def get_price_history(self, ticker: str, period: str = "1y"):
        """Fetch prices, retrying only transient provider failures."""
        return self._execute(
            lambda: self.source.get_price_history(ticker, period)
        )

    def get_share_history(self, ticker: str, period: str = "18mo"):
        """Fetch share history, retrying only transient provider failures."""
        return self._execute(
            lambda: self.source.get_share_history(ticker, period)
        )

    def _execute(self, operation: Callable[[], T]) -> T:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except Exception as error:
                retryable = self.retry_predicate(error)
                final_attempt = attempt == self.max_attempts

                if not retryable:
                    raise

                if final_attempt:
                    self._record_stat("exhausted")
                    raise

                self._record_stat("retries")
                self.sleeper(self._delay_before_retry(attempt))

        raise RuntimeError("Retry loop completed without a result.")

    def _delay_before_retry(self, failed_attempt: int) -> float:
        exponential_delay = self.base_delay_seconds * (
            2 ** (failed_attempt - 1)
        )
        bounded_delay = min(exponential_delay, self.max_delay_seconds)
        return bounded_delay + self.jitter(0, self.jitter_seconds)

    def _record_stat(self, name: str) -> None:
        with self._stats_lock:
            setattr(self.stats, name, getattr(self.stats, name) + 1)
