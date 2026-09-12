import gzip
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.backtest import (
    build_backtest,
    build_backtest_universe,
    build_backtest_markdown,
    collect_price_histories,
    export_backtest,
)
from src.data_sources.base import MarketDataSource


CONFIG = {
    "forward_horizons_days": {"1M": 21, "3M": 63},
    "compounding_horizon": "1M",
    "top_n": [5, 10],
    "quantiles": 4,
    "minimum_cross_section": 20,
    "minimum_history_days": 200,
    "maximum_stale_trading_days": 5,
}


def _calendar(days: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2020-01-01", periods=days)


def _history(closes: np.ndarray, volume: float, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({
        "Date": calendar[: len(closes)],
        "Close": closes,
        "Volume": [volume] * len(closes),
    })


def _synthetic_universe(ticker_count: int = 40, days: int = 700):
    """Build tickers whose future returns follow their momentum rank."""
    calendar = _calendar(days)
    rng = np.random.default_rng(7)
    benchmark = _history(
        100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.005, days)),
        5_000_000,
        calendar,
    )
    universe = []
    histories = {}
    for index in range(ticker_count):
        ticker = f"T{index:02d}"
        drift = 0.0002 + index * 0.0002
        closes = 10.0 * np.cumprod(
            1.0 + rng.normal(drift, 0.01, days) + drift
        )
        universe.append({
            "ticker": ticker,
            "country": "US",
            "sector": "Technology",
            "market_cap": 100_000_000,
            "source_status": "OK",
        })
        histories[ticker] = _history(closes, 1_000_000, calendar)
    return universe, histories, {"SPY": benchmark}


class FakeSource(MarketDataSource):
    def __init__(self, histories, failing=()):
        self.histories = histories
        self.failing = set(failing)
        self.calls = []

    def get_stock_data(self, ticker: str) -> dict:
        raise AssertionError("Backtest must not request metadata.")

    def get_price_history(self, ticker: str, period: str = "1y"):
        self.calls.append((ticker, period))
        if ticker in self.failing:
            raise RuntimeError("provider unavailable")
        return self.histories[ticker]


class BacktestUniverseTests(unittest.TestCase):
    def test_selects_screened_rows_and_deduplicates(self):
        results = [
            {"ticker": "bbb", "status": "OK", "country": "us", "market_cap": 5e7},
            {"ticker": "AAA", "status": "FAILED", "country": "CA", "sector": "Energy"},
            {"ticker": "AAA", "status": "OK"},
            {"ticker": "CCC", "status": "FILTERED"},
            {"ticker": "DDD", "status": "ERROR"},
            {"ticker": "", "status": "OK"},
        ]

        universe = build_backtest_universe(results, CONFIG)

        self.assertEqual(
            [(row["ticker"], row["country"], row["source_status"]) for row in universe],
            [("AAA", "CA", "FAILED"), ("BBB", "US", "OK")],
        )
        self.assertEqual(universe[1]["market_cap"], 5e7)
        self.assertIsNone(universe[0]["market_cap"])


class CollectionTests(unittest.TestCase):
    def test_collects_benchmarks_then_tickers_with_isolation(self):
        universe, histories, benchmarks = _synthetic_universe(ticker_count=3)
        source = FakeSource({**histories, **benchmarks}, failing={"T01"})
        progress = []

        collected, benchmark_histories, errors = collect_price_histories(
            universe,
            {"US": "SPY"},
            source,
            period="10y",
            max_workers=2,
            progress_callback=lambda *args: progress.append(args),
        )

        self.assertEqual(sorted(collected), ["T00", "T02"])
        self.assertEqual(list(benchmark_histories), ["SPY"])
        self.assertIn("RuntimeError", errors["T01"])
        self.assertEqual(source.calls[0], ("SPY", "10y"))
        self.assertTrue(all(period == "10y" for _, period in source.calls))
        self.assertEqual(progress[0][0], "backtest-benchmarks")
        self.assertEqual(progress[-1][:3], ("backtest-prices", 3, 3))

    def test_rejects_invalid_worker_count(self):
        with self.assertRaises(ValueError):
            collect_price_histories([], {}, FakeSource({}), period="10y", max_workers=0)


class BacktestAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.universe, self.histories, self.benchmarks = _synthetic_universe()
        self.analysis = build_backtest(
            self.universe,
            self.histories,
            self.benchmarks,
            {"US": "SPY"},
            "RUN1",
            config=CONFIG,
        )

    def test_uses_only_point_in_time_history_and_drops_open_month(self):
        coverage = self.analysis["coverage"]
        frame = self.analysis["_observations"]
        calendar = _calendar(700)

        self.assertEqual(coverage["usable_tickers"], 40)
        self.assertEqual(coverage["universe_tickers"], 40)
        self.assertGreater(coverage["periods"], 12)
        self.assertNotEqual(coverage["last_period"], str(calendar[-1].to_period("M")))
        # Every observation date is the last trading day of its month.
        dates = pd.to_datetime(frame["date"])
        self.assertTrue((dates.dt.to_period("M").astype(str) == frame["period"]).all())
        # The earliest period must leave at least 200 sessions of history.
        first_date = dates.min()
        self.assertGreaterEqual(calendar.get_loc(first_date) + 1, 200)

    def test_forward_returns_are_measured_from_the_rebalance_date(self):
        frame = self.analysis["_observations"]
        calendar = _calendar(700)
        row = frame.iloc[0]
        history = self.histories[row["ticker"]]
        start = calendar.get_loc(pd.Timestamp(row["date"]))
        expected = (history["Close"].iloc[start + 21] / history["Close"].iloc[start] - 1) * 100
        self.assertAlmostEqual(row["return_1M"], expected, places=6)
        benchmark = self.benchmarks["SPY"]
        expected_benchmark = (
            benchmark["Close"].iloc[start + 21] / benchmark["Close"].iloc[start] - 1
        ) * 100
        self.assertAlmostEqual(row["benchmark_return_1M"], expected_benchmark, places=6)
        self.assertAlmostEqual(row["excess_1M"], expected - expected_benchmark, places=6)
        # The final periods cannot see 63 sessions ahead and must be null.
        last = frame[frame["period"] == frame["period"].max()]
        self.assertTrue(last["excess_3M"].isna().all())

    def test_momentum_driven_universe_produces_positive_ic_and_spread(self):
        ic = self.analysis["aggregate"]["information_coefficient"]
        self.assertGreater(ic["technical_score"]["1M"]["mean"], 0.2)
        self.assertGreater(ic["relative_strength_6m"]["1M"]["t_stat"], 3)
        spread = self.analysis["aggregate"]["quantiles"]["technical_score"]["1M"]
        self.assertEqual(len(spread["mean_excess"]), 4)
        self.assertGreater(spread["spread"], 0)
        top = self.analysis["aggregate"]["top_n"]["technical_score"]["5"]["1M"]
        self.assertGreater(top["hit_rate_percent"], 50)
        compounded = self.analysis["aggregate"]["compounded"]["technical_score"]["5"]
        self.assertGreater(compounded["difference_percent"], 0)
        turnover = self.analysis["aggregate"]["turnover"]["technical_score"]["5"]
        self.assertGreaterEqual(turnover, 0)
        self.assertLessEqual(turnover, 100)

    def test_shadow_score_v2_is_scored_per_period_and_evaluated(self):
        frame = self.analysis["_observations"]
        self.assertIn("score_v2", frame.columns)
        self.assertIn("v2_momentum_long", frame.columns)
        self.assertTrue(frame["score_v2"].notna().all())
        # Percentile blends stay within 0-100 inside every period.
        self.assertLessEqual(frame["score_v2"].max(), 100.0)
        self.assertGreaterEqual(frame["score_v2"].min(), 0.0)
        ic = self.analysis["aggregate"]["information_coefficient"]
        self.assertGreater(ic["score_v2"]["1M"]["mean"], 0.2)
        self.assertIn("v2_momentum_long", ic)
        self.assertIn("score_v2", self.analysis["aggregate"]["quantiles"])
        self.assertIn("score_v2", self.analysis["aggregate"]["top_n"])

    def test_static_factors_are_excluded_from_technical_score(self):
        frame = self.analysis["_observations"]
        difference = frame["discovery_score_static"] - frame["technical_score"]
        self.assertTrue((difference > 0).all())
        self.assertLessEqual(frame["technical_score"].max(), 75)

    def test_periods_below_minimum_cross_section_are_null(self):
        analysis = build_backtest(
            self.universe[:10],
            self.histories,
            self.benchmarks,
            {"US": "SPY"},
            "RUN1",
            config=CONFIG,
        )
        self.assertTrue(all(entry["1M"] is None for entry in analysis["periods"]))
        self.assertIsNone(
            analysis["aggregate"]["information_coefficient"]["technical_score"]["1M"]
        )

    def test_reports_missing_and_invalid_histories(self):
        universe = self.universe[:3] + [
            {"ticker": "CA1", "country": "CA", "sector": None, "market_cap": None,
             "source_status": "OK"},
        ]
        histories = {
            "T00": self.histories["T00"],
            "T01": pd.DataFrame(),
            "CA1": self.histories["T02"],
        }
        analysis = build_backtest(
            universe, histories, self.benchmarks, {"US": "SPY", "CA": "XIU.TO"},
            "RUN1", config=CONFIG, collection_errors={"T02": "RuntimeError: x"},
        )
        coverage = analysis["coverage"]
        self.assertEqual(coverage["skipped"], {
            "missing_history": 1,
            "missing_benchmark": 1,
            "invalid_history": 1,
        })
        self.assertEqual(coverage["collection_errors"], 1)
        self.assertEqual(coverage["usable_tickers"], 1)

    def test_rejects_invalid_configuration(self):
        for override in (
            {"forward_horizons_days": {}},
            {"forward_horizons_days": {"1M": 0}},
            {"compounding_horizon": "9M"},
            {"quantiles": 1},
            {"top_n": []},
            {"maximum_stale_trading_days": -1},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    build_backtest(
                        self.universe, self.histories, self.benchmarks,
                        {"US": "SPY"}, "RUN1", config={**CONFIG, **override},
                    )


class ExportTests(unittest.TestCase):
    def test_exports_deterministic_artifacts(self):
        universe, histories, benchmarks = _synthetic_universe(ticker_count=25)
        analysis = build_backtest(
            universe, histories, benchmarks, {"US": "SPY"}, "RUN1", config=CONFIG,
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = export_backtest(analysis, Path(directory))
            payload = json.loads(Path(paths["backtest_json_path"]).read_text("utf-8"))
            self.assertNotIn("_observations", payload)
            self.assertTrue(payload["official_scores_and_ranks_unchanged"])
            self.assertEqual(payload["source_run_id"], "RUN1")
            self.assertTrue(payload["limitations"])
            periods = pd.read_csv(paths["backtest_periods_csv_path"])
            self.assertIn("ic_technical_score", periods.columns)
            self.assertIn("top5_excess_technical_score", periods.columns)
            with gzip.open(paths["backtest_observations_csv_path"], "rt") as file:
                observations = pd.read_csv(file)
            self.assertEqual(len(observations), payload["coverage"]["observations"])
            markdown = Path(paths["backtest_summary_markdown_path"]).read_text("utf-8")
            self.assertIn("Spearman IC", markdown)
            self.assertIn("## Limitations", markdown)
            self.assertEqual(markdown, build_backtest_markdown(payload))
            leftovers = [
                name for name in Path(directory).iterdir() if name.name.startswith(".")
            ]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
