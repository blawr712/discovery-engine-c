"""Shared provider pacing and cooldown for concurrent data collection."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

from .base import MarketDataSource
from .retrying_source import is_rate_limit_error


@dataclass
class RateLimitStats:
    """Track deliberate pacing and provider cooldown behavior."""

    pacing_waits: int = 0
    pacing_seconds: float = 0
    cooldown_events: int = 0
    cooldown_waits: int = 0
    cooldown_seconds: float = 0
    circuit_open_events: int = 0
    circuit_rejections: int = 0


class ProviderCircuitOpenError(RuntimeError):
    """Raised when persistent provider throttling stops further live calls."""


class RateLimitedMarketDataSource(MarketDataSource):
    """Pace all threads and globally cool down after provider throttling."""

    def __init__(
        self,
        source: MarketDataSource,
        metadata_interval_seconds: float = 1.0,
        price_interval_seconds: float = 0.2,
        cooldown_seconds: float = 300,
        max_cooldown_events: int = 3,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        for name, value in (
            ("metadata_interval_seconds", metadata_interval_seconds),
            ("price_interval_seconds", price_interval_seconds),
            ("cooldown_seconds", cooldown_seconds),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative.")
        if (
            isinstance(max_cooldown_events, bool)
            or not isinstance(max_cooldown_events, int)
        ):
            raise TypeError("max_cooldown_events must be an integer.")
        if max_cooldown_events < 1:
            raise ValueError("max_cooldown_events must be at least 1.")

        self.source = source
        self.metadata_interval_seconds = metadata_interval_seconds
        self.price_interval_seconds = price_interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_events = max_cooldown_events
        self.enabled = enabled
        self.clock = clock
        self.sleeper = sleeper
        self.stats = RateLimitStats()
        self._lock = threading.Lock()
        self._next_metadata_at = 0.0
        self._next_price_at = 0.0
        self._blocked_until = 0.0
        self._circuit_open = False

    def get_stock_data(self, ticker: str) -> dict:
        """Fetch metadata after reserving a globally paced request slot."""
        if not self.enabled:
            return self.source.get_stock_data(ticker)

        self._wait_for_slot("metadata")
        try:
            return self.source.get_stock_data(ticker)
        except Exception as error:
            self._register_rate_limit(error)
            raise

    def get_price_history(self, ticker: str, period: str = "1y"):
        """Fetch prices after reserving a globally paced request slot."""
        if not self.enabled:
            return self.source.get_price_history(ticker, period)

        self._wait_for_slot("price")
        try:
            return self.source.get_price_history(ticker, period)
        except Exception as error:
            self._register_rate_limit(error)
            raise

    def get_share_history(self, ticker: str, period: str = "18mo"):
        """Fetch share history through the slower metadata pacing lane."""
        if not self.enabled:
            return self.source.get_share_history(ticker, period)

        self._wait_for_slot("metadata")
        try:
            return self.source.get_share_history(ticker, period)
        except Exception as error:
            self._register_rate_limit(error)
            raise

    def _wait_for_slot(self, request_type: str) -> None:
        while True:
            with self._lock:
                if self._circuit_open:
                    self.stats.circuit_rejections += 1
                    raise ProviderCircuitOpenError(
                        "Provider circuit is open after repeated rate limits. "
                        "Resume this run later."
                    )
                now = self.clock()
                next_request_at = (
                    self._next_metadata_at
                    if request_type == "metadata"
                    else self._next_price_at
                )
                ready_at = max(next_request_at, self._blocked_until)
                wait_seconds = max(0.0, ready_at - now)
                in_cooldown = self._blocked_until > now

                if wait_seconds <= 0:
                    interval = (
                        self.metadata_interval_seconds
                        if request_type == "metadata"
                        else self.price_interval_seconds
                    )
                    if request_type == "metadata":
                        self._next_metadata_at = now + interval
                    else:
                        self._next_price_at = now + interval
                    return

                if in_cooldown:
                    self.stats.cooldown_waits += 1
                    self.stats.cooldown_seconds += wait_seconds
                else:
                    self.stats.pacing_waits += 1
                    self.stats.pacing_seconds += wait_seconds

            self.sleeper(wait_seconds)

    def _register_rate_limit(self, error: Exception) -> None:
        if not is_rate_limit_error(error):
            return

        with self._lock:
            self._blocked_until = max(
                self._blocked_until,
                self.clock() + self.cooldown_seconds,
            )
            self.stats.cooldown_events += 1
            if self.stats.cooldown_events >= self.max_cooldown_events:
                self._circuit_open = True
                self.stats.circuit_open_events += 1
