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


class FundamentalsIntegrationTests(unittest.TestCase):
    def test_collects_us_fundamentals_with_isolation(self):
        class FakeFundamentals:
            def __init__(self):
                self.calls = []

            def get_company_facts(self, ticker):
                self.calls.append(ticker)
                if ticker == "SECFAIL":
                    raise RuntimeError("sec down")
                if ticker == "EMPTY":
                    return {"ticker": ticker, "facts": {}}
                return {"ticker": ticker, "facts": {"revenue": [
                    {"start": "2026-04-01", "end": "2026-06-30",
                     "filed": "2026-08-01", "val": 100.0},
                ]}}

        fundamentals = FakeFundamentals()
        engine = DiscoveryEngine(
            ConcurrentFakeSource(),
            benchmarks={"US": "SPY", "CA": "XIU.TO"},
            max_workers=2,
            fundamentals_source=fundamentals,
        )
        results = engine.run([
            {"ticker": "GOOD", "country": "US"},
            {"ticker": "SECFAIL", "country": "US"},
            {"ticker": "EMPTY", "country": "US"},
            {"ticker": "NORTH.TO", "country": "CA"},
        ])
        by = {row["ticker"]: row for row in results}

        self.assertEqual(sorted(fundamentals.calls), ["EMPTY", "GOOD", "SECFAIL"])
        self.assertEqual(by["GOOD"]["fundamentals_status"], "collected")
        self.assertTrue(by["SECFAIL"]["fundamentals_status"].startswith("unavailable: RuntimeError"))
        self.assertEqual(by["EMPTY"]["fundamentals_status"], "no_data")
        self.assertEqual(by["NORTH.TO"]["fundamentals_status"], "not_applicable")
        # Every row still scored; failures never block the company.
        self.assertTrue(all(by[t]["status"] == "OK" for t in by))
        self.assertIn("pit_revenue_ttm", by["GOOD"])
        self.assertIn("latest_close", by["GOOD"])

    def test_without_fundamentals_source_marks_not_requested(self):
        engine = DiscoveryEngine(ConcurrentFakeSource(), benchmarks={"US": "SPY"})
        results = engine.run([{"ticker": "AAA", "country": "US"}])
        self.assertEqual(results[0]["fundamentals_status"], "not_requested")
        self.assertIsNone(results[0]["pit_revenue_ttm"])


class InsiderIntegrationTests(unittest.TestCase):
    def test_looks_up_insider_history_with_isolation(self):
        from src.insider_signals import InsiderHistory

        history = InsiderHistory([{
            "issuer_cik": "1", "filed": "2025-01-10", "kind": "purchase",
            "shares": 100, "price": 2.0, "value": 200.0, "owner_ciks": ["X"],
            "officer_or_director": True,
        }])

        def lookup(ticker):
            if ticker == "BOOM":
                raise RuntimeError("index broken")
            return history if ticker == "GOOD" else None

        engine = DiscoveryEngine(
            ConcurrentFakeSource(), benchmarks={"US": "SPY", "CA": "XIU.TO"},
            max_workers=2, insider_lookup=lookup,
        )
        results = engine.run([
            {"ticker": "GOOD", "country": "US"},
            {"ticker": "NONE", "country": "US"},
            {"ticker": "BOOM", "country": "US"},
            {"ticker": "NORTH.TO", "country": "CA"},
        ])
        by = {row["ticker"]: row for row in results}
        self.assertEqual(by["GOOD"]["insiders_status"], "collected")
        self.assertIn("ins_purchase_count_long", by["GOOD"])
        self.assertEqual(by["NONE"]["insiders_status"], "no_data")
        self.assertTrue(by["BOOM"]["insiders_status"].startswith("unavailable: RuntimeError"))
        self.assertEqual(by["NORTH.TO"]["insiders_status"], "not_applicable")
        self.assertTrue(all(row["status"] == "OK" for row in results))


class CanadianFundamentalsTests(unittest.TestCase):
    def _statements(self):
        quarters = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
        return {
            "currency": "USD",
            "quarterly": {
                "income": {q: {"Total Revenue": 100.0, "Operating Income": 10.0} for q in quarters},
                "balance": {q: {"Cash And Cash Equivalents": 50.0, "Ordinary Shares Number": 10.0}
                            for q in quarters},
                "cashflow": {q: {"Operating Cash Flow": 12.0, "Capital Expenditure": -2.0}
                             for q in quarters},
            },
            "annual": {},
        }

    def test_statements_fallback_with_fx_conversion_and_resolver(self):
        engine_source = ConcurrentFakeSource()
        statements = self._statements()

        class Statements:
            calls = []

            def get_financial_statements(inner, ticker):
                inner.calls.append(ticker)
                if ticker == "NOSTMT.TO":
                    return {"currency": None, "quarterly": {}, "annual": {}}
                return statements

        class SecFacts:
            def get_company_facts(inner, ticker):
                assert ticker == "BCHT", ticker
                return {"ticker": "BCHT", "facts": {"revenue": [
                    {"start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-01",
                     "val": 5.0, "tag": "us-gaap:Revenues", "unit": "USD"},
                ]}}

        def resolver(stock_data):
            if stock_data["ticker"] == "BCHT.TO":
                return "BCHT", "interlisted:BCHT name match 1.00"
            return None, "not interlisted"

        original = engine_source.get_stock_data

        def with_currency(ticker):
            data = original(ticker)
            data["currency"] = "CAD" if ticker.endswith(".TO") else "USD"
            return data

        engine_source.get_stock_data = with_currency
        engine = DiscoveryEngine(
            engine_source, benchmarks={"US": "SPY", "CA": "XIU.TO"}, max_workers=2,
            fundamentals_source=SecFacts(), statements_source=Statements(),
            sec_ticker_resolver=resolver, fundamentals_countries=("US", "CA"),
            fx_rates={("CAD", "USD"): 0.5, ("USD", "CAD"): 2.0},
        )
        results = engine.run([
            {"ticker": "WELL.TO", "country": "CA", "root_ticker": "WELL",
             "company_name": "WELL Health", "interlisted": None},
            {"ticker": "BCHT.TO", "country": "CA", "root_ticker": "BCHT",
             "company_name": "Birchtech Corp.", "interlisted": "NYSE Mkt"},
            {"ticker": "NOSTMT.TO", "country": "CA"},
        ])
        by = {row["ticker"]: row for row in results}

        # Statements fallback: reports in USD, trades in CAD -> converted.
        self.assertTrue(by["WELL.TO"]["fundamentals_status"].startswith("statements; converted CAD->USD"))
        self.assertEqual(by["WELL.TO"]["pit_reporting_currency"], "USD")
        self.assertEqual(by["WELL.TO"]["pit_data_quality"], "estimated_filing_dates")
        self.assertEqual(by["WELL.TO"]["pit_revenue_ttm"], 400.0)
        # Price 229 CAD * 0.5 = 114.5 USD * 10 shares = market cap 1145 USD.
        self.assertAlmostEqual(by["WELL.TO"]["pit_market_cap"], 1145.0, places=6)
        self.assertEqual(by["WELL.TO"]["universe_interlisted"], None)
        # Interlisted name resolved to SEC facts, currencies match.
        self.assertEqual(by["BCHT.TO"]["fundamentals_status"], "collected via BCHT; converted CAD->USD")
        self.assertEqual(by["BCHT.TO"]["universe_root_ticker"], "BCHT")
        # Empty statements: no data, still scored.
        self.assertEqual(by["NOSTMT.TO"]["fundamentals_status"], "no_data")
        self.assertTrue(all(row["status"] == "OK" for row in results))

    def test_missing_fx_rate_skips_valuation_ratios_only(self):
        engine_source = ConcurrentFakeSource()
        original = engine_source.get_stock_data
        engine_source.get_stock_data = lambda t: {**original(t), "currency": "CAD"}

        class Statements:
            def get_financial_statements(inner, ticker):
                return self._statements()

        engine = DiscoveryEngine(
            engine_source, benchmarks={"CA": "XIU.TO"}, max_workers=1,
            statements_source=Statements(), fundamentals_countries=("CA",),
        )
        row = engine.run([{"ticker": "WELL.TO", "country": "CA"}])[0]
        self.assertIn("rate unavailable", row["fundamentals_status"])
        self.assertEqual(row["pit_revenue_ttm"], 400.0)
        self.assertIsNone(row["pit_market_cap"])
        self.assertIsNone(row["pit_sales_yield"])


class StaleSecFallbackTests(unittest.TestCase):
    def test_stale_sec_facts_fall_back_to_statements(self):
        engine_source = ConcurrentFakeSource()

        class SecFacts:
            def get_company_facts(inner, ticker):
                return {"ticker": ticker, "facts": {"revenue": [
                    {"start": "2019-01-01", "end": "2019-03-31", "filed": "2019-05-01",
                     "val": 5.0, "tag": "us-gaap:Revenues", "unit": "USD"},
                ]}}

        class Statements:
            def get_financial_statements(inner, ticker):
                quarters = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
                return {"currency": "USD", "annual": {}, "quarterly": {
                    "income": {q: {"Total Revenue": 10.0} for q in quarters},
                    "balance": {}, "cashflow": {}}}

        engine = DiscoveryEngine(
            engine_source, benchmarks={"US": "SPY"}, max_workers=1,
            fundamentals_source=SecFacts(), statements_source=Statements(),
        )
        row = engine.run([{"ticker": "OLD", "country": "US"}])[0]
        self.assertEqual(row["fundamentals_status"], "statements (SEC facts stale)")
        self.assertEqual(row["pit_data_quality"], "estimated_filing_dates")
        self.assertEqual(row["pit_revenue_ttm"], 40.0)

    def test_stale_sec_facts_without_statements_are_kept_and_labeled(self):
        class SecFacts:
            def get_company_facts(inner, ticker):
                return {"ticker": ticker, "facts": {"revenue": [
                    {"start": "2019-01-01", "end": "2019-03-31", "filed": "2019-05-01",
                     "val": 5.0, "tag": "us-gaap:Revenues", "unit": "USD"},
                ]}}

        engine = DiscoveryEngine(
            ConcurrentFakeSource(), benchmarks={"US": "SPY"}, max_workers=1,
            fundamentals_source=SecFacts(),
        )
        row = engine.run([{"ticker": "OLD", "country": "US"}])[0]
        self.assertEqual(row["fundamentals_status"], "collected (stale)")
        self.assertIsNone(row["pit_revenue_ttm"])
