"""One request pace shared by every SEC source in a process.

The SEC asks for no more than ten requests a second per client and enforces
it by blocking the address for a while. Each SEC source used to pace itself,
which was fine while only one of them was active at a time; a discovery run
now fetches company facts and Form 4 filings concurrently, and two sources
each pacing at nine a second would together exceed the limit. Sources given
the same pacer share one schedule.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class SecPacer:
    """Space requests at least ``interval_seconds`` apart, across threads."""

    def __init__(self, interval_seconds: float = 0.11, clock: Callable[[], float] = time.monotonic) -> None:
        if interval_seconds < 0:
            raise ValueError("SEC request interval cannot be negative.")
        self.interval_seconds = float(interval_seconds)
        self.clock = clock
        self._lock = threading.Lock()
        self._last = float("-inf")
        self.waits = 0

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            delay = self._last + self.interval_seconds - now
            if delay > 0:
                self.waits += 1
                time.sleep(delay)
                now = self.clock()
            self._last = now
