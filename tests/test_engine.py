from collections import Counter
import threading
import time
import unittest

import pandas as pd

from src.data_sources.base import MarketDataSource
from src.engine import DiscoveryEngine


class ConcurrentFakeSource(MarketDataSource):
    def __init__(self):
        self.metadata_calls = Counter()
        self.history_calls = Counter()
        self.active_calls = 0
        self.max_active_calls = 0
        self.lock = threading.Lock()

    def get_stock_data(self, ticker: str) -> dict:
        self._start_call()

        try:
            time.sleep(0.01)
            self.metadata_calls[ticker] += 1

            if ticker == "META_ERROR":
                raise RuntimeError("metadata unavailable")

            market_cap = 2_000_000_000 if ticker == "TOO_BIG" else 100_000_000
            return {
                "ticker": ticker,
                "market_cap": market_cap,
                "country": "CA" if ticker.endswith(".TO") else "US",
            }
        finally:
            self._finish_call()

    def get_price_history(
        self,
        ticker: str,
        period: str = "1y",
    ) -> pd.DataFrame:
        self._start_call()

        try:
            time.sleep(0.01)
            self.history_calls[ticker] += 1

            if ticker == "PRICE_ERROR":
                raise RuntimeError("prices unavailable")

            dates = pd.date_range("2025-01-01", periods=220)
            return pd.DataFrame(
                {
                    "Date": dates,
                    "Close": range(10, 230),
                    "Volume": [1_000_000] * 220,
                }
            )
        finally:
            self._finish_call()

    def _start_call(self) -> None:
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(
                self.max_active_calls,
                self.active_calls,
            )

    def _finish_call(self) -> None:
        with self.lock:
            self.active_calls -= 1


class DiscoveryEngineTests(unittest.TestCase):
    def setUp(self):
        self.source = ConcurrentFakeSource()
        self.progress = []
        self.engine = DiscoveryEngine(
            self.source,
            benchmarks={"CA": "XIU.TO", "US": "SPY"},
            max_workers=3,
            progress_callback=lambda *args: self.progress.append(args),
        )

    def test_runs_concurrently_and_preserves_universe_order(self):
        universe = [
            {"ticker": "FIRST", "country": "US"},
            {"ticker": "SECOND", "country": "US"},
            {"ticker": "THIRD.TO", "country": "CA"},
        ]

        results = self.engine.run(universe)

        self.assertEqual(
            [result["ticker"] for result in results],
            ["FIRST", "SECOND", "THIRD.TO"],
        )
        self.assertGreaterEqual(self.source.max_active_calls, 2)
        self.assertTrue(all(result["status"] == "OK" for result in results))
        self.assertEqual(self.source.history_calls["SPY"], 1)
        self.assertEqual(self.source.history_calls["XIU.TO"], 1)

    def test_filters_before_fetching_company_price_history(self):
        results = self.engine.run(
            [{"ticker": "TOO_BIG", "country": "US"}]
        )

        self.assertEqual(results[0]["status"], "FILTERED")
        self.assertEqual(self.source.history_calls["TOO_BIG"], 0)
        self.assertEqual(self.source.history_calls["SPY"], 0)

    def test_isolates_metadata_and_price_failures(self):
        universe = [
            {"ticker": "GOOD", "country": "US"},
            {"ticker": "META_ERROR", "country": "US"},
            {"ticker": "PRICE_ERROR", "country": "US"},
        ]

        results = self.engine.run(universe)

        self.assertEqual(results[0]["status"], "OK")
        self.assertEqual(results[1]["status"], "ERROR")
        self.assertIn("Metadata: RuntimeError", results[1]["reason_flags"])
        self.assertEqual(results[2]["status"], "ERROR")
        self.assertIn("Price history: RuntimeError", results[2]["reason_flags"])

    def test_reports_progress_for_each_completed_operation(self):
        self.engine.run(
            [
                {"ticker": "ONE", "country": "US"},
                {"ticker": "TWO", "country": "US"},
            ]
        )

        phases = Counter(item[0] for item in self.progress)
        self.assertEqual(phases["metadata"], 2)
        self.assertEqual(phases["benchmarks"], 1)
        self.assertEqual(phases["prices"], 2)

    def test_validates_worker_count(self):
        with self.assertRaises(ValueError):
            DiscoveryEngine(self.source, {}, max_workers=0)

        with self.assertRaises(TypeError):
            DiscoveryEngine(self.source, {}, max_workers=2.5)
        with self.assertRaises(ValueError):
            DiscoveryEngine(self.source, {}, metadata_workers=0)
        with self.assertRaises(TypeError):
            DiscoveryEngine(self.source, {}, price_workers=2.5)

    def test_uses_separate_worker_limits(self):
        engine = DiscoveryEngine(
            self.source,
            {},
            max_workers=5,
            metadata_workers=1,
            price_workers=3,
        )

        self.assertEqual(engine.metadata_workers, 1)
        self.assertEqual(engine.price_workers, 3)

    def test_empty_universe_does_not_call_provider(self):
        self.assertEqual(self.engine.run([]), [])
        self.assertEqual(self.source.active_calls, 0)

    def test_reuses_prior_results_and_only_emits_new_results(self):
        emitted = []
        engine = DiscoveryEngine(
            self.source,
            benchmarks={"US": "SPY"},
            max_workers=2,
            result_callback=lambda index, result: emitted.append(
                (index, result["ticker"])
            ),
        )
        prior = {0: {"ticker": "DONE", "status": "OK"}}

        results = engine.run(
            [
                {"ticker": "DONE", "country": "US"},
                {"ticker": "NEW", "country": "US"},
            ],
            prior_results=prior,
        )

        self.assertEqual([row["ticker"] for row in results], ["DONE", "NEW"])
        self.assertEqual(self.source.metadata_calls["DONE"], 0)
        self.assertEqual(emitted, [(1, "NEW")])


if __name__ == "__main__":
    unittest.main()
