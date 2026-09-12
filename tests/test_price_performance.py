import unittest
from datetime import date, timedelta

from src.price_performance import build_price_performance


class PricePerformanceTests(unittest.TestCase):
    def test_calculates_period_and_benchmark_relative_returns(self):
        equity = self._snapshot("AAA", [
            ("2025-08-01", 100), ("2026-02-01", 120),
            ("2026-07-01", 130), ("2026-08-01", 150),
        ])
        benchmark = self._snapshot("SPY", [
            ("2025-08-01", 100), ("2026-02-01", 110),
            ("2026-07-01", 120), ("2026-08-01", 125),
        ])

        result = build_price_performance(equity, benchmark)

        self.assertEqual(result["period_returns"]["1Y"]["ticker_return"], 50)
        self.assertEqual(result["period_returns"]["1Y"]["benchmark_return"], 25)
        self.assertEqual(result["period_returns"]["1Y"]["relative_return"], 25)
        self.assertIn("YTD", result["period_returns"])
        self.assertEqual(result["summary"]["latest_close"], 150)
        self.assertEqual(result["summary"]["period_high"], 150)
        self.assertIn("20", result["series"]["moving_averages"])
        self.assertEqual(result["series"]["ticker"][0]["value"], 100)
        self.assertEqual(result["series"]["ticker"][-1]["value"], 150)
        self.assertIn("S&P 500", result["benchmark_definition"])
        self.assertEqual(result["data_quality"], "clean")

    def test_suppresses_returns_that_cross_possible_corporate_action(self):
        equity = self._snapshot("AAA", [
            ("2026-06-01", 0.4), ("2026-07-20", 4.2),
            ("2026-08-01", 4.5),
        ], include_ohlcv=True)

        result = build_price_performance(equity, None)

        self.assertEqual(result["data_quality"], "unresolved_discontinuity")
        self.assertEqual(result["anomalies"][0]["date"], "2026-07-20")
        self.assertIsNone(result["period_returns"]["3M"]["ticker_return"])
        self.assertFalse(result["period_returns"]["3M"]["reliable"])

    def test_missing_equity_snapshot_returns_none(self):
        self.assertIsNone(build_price_performance(None, None))

    def test_builds_standard_moving_average_series(self):
        start = date(2025, 8, 1)
        values = [
            ((start + timedelta(days=index)).isoformat(), 100 + index)
            for index in range(220)
        ]

        result = build_price_performance(self._snapshot("AAA", values), None)

        self.assertEqual(len(result["series"]["moving_averages"]["20"]), 201)
        self.assertEqual(len(result["series"]["moving_averages"]["50"]), 171)
        self.assertEqual(len(result["series"]["moving_averages"]["200"]), 21)
        self.assertEqual(
            len(result["series"]["exponential_moving_averages"]["20"]), 201
        )
        self.assertEqual(len(result["series"]["bollinger_bands"]["20"]), 201)

    def test_exposes_adjusted_ohlcv_for_candlestick_charting(self):
        equity = self._snapshot("AAA", [
            ("2026-08-01", 100), ("2026-08-02", 105),
        ], include_ohlcv=True)

        result = build_price_performance(equity, None)

        self.assertTrue(result["capabilities"]["ohlcv"])
        self.assertEqual(result["capabilities"]["ohlcv_coverage_percent"], 100)
        self.assertEqual(result["series"]["ohlcv"][1]["open"], 103.95)
        self.assertEqual(result["series"]["ohlcv"][1]["high"], 107.1)
        self.assertEqual(result["summary"]["latest_low"], 102.9)
        self.assertEqual(result["summary"]["period_high"], 107.1)
        self.assertEqual(result["summary"]["period_low"], 98)

    def test_legacy_close_only_snapshot_disables_candlesticks(self):
        result = build_price_performance(
            self._snapshot("AAA", [("2026-08-01", 100), ("2026-08-02", 105)]),
            None,
        )

        self.assertFalse(result["capabilities"]["ohlcv"])
        self.assertTrue(result["capabilities"]["legacy_close_only"])
        self.assertEqual(result["series"]["ohlcv"], [])

    def test_verified_reverse_split_adjusts_prior_prices_and_restores_returns(self):
        equity = self._snapshot("AAA", [
            ("2026-06-01", 0.4), ("2026-07-20", 4.2),
            ("2026-08-01", 4.5),
        ], include_ohlcv=True)
        equity["points"][1]["split"] = 0.1

        result = build_price_performance(equity, None)

        self.assertEqual(result["data_quality"], "verified_adjusted")
        self.assertEqual(result["corporate_actions"][0]["status"], "verified_adjusted")
        self.assertEqual(result["series"]["price"][0]["value"], 4)
        self.assertEqual(result["series"]["ohlcv"][0]["open"], 3.96)
        self.assertEqual(result["period_returns"]["3M"]["ticker_return"], 12.5)

    def test_reported_split_does_not_double_adjust_already_adjusted_prices(self):
        equity = self._snapshot("AAA", [
            ("2026-06-01", 4.0), ("2026-07-20", 4.2),
            ("2026-08-01", 4.5),
        ])
        equity["points"][1]["split"] = 0.1

        result = build_price_performance(equity, None)

        self.assertEqual(result["data_quality"], "verified")
        self.assertEqual(
            result["corporate_actions"][0]["status"],
            "reported_already_adjusted",
        )
        self.assertEqual(result["series"]["price"][0]["value"], 4)

    @staticmethod
    def _snapshot(ticker, values, include_ohlcv=False):
        points = [
            {
                "date": day,
                **({
                    "open": round(close * .99, 6),
                    "high": round(close * 1.02, 6),
                    "low": round(close * .98, 6),
                } if include_ohlcv else {}),
                "close": close, "volume": 1000,
            }
            for day, close in values
        ]
        return {
            "ticker": ticker, "start_date": points[0]["date"],
            "end_date": points[-1]["date"], "point_count": len(points),
            "source_mtime": "2026-08-01T00:00:00+00:00", "points": points,
        }


if __name__ == "__main__":
    unittest.main()
