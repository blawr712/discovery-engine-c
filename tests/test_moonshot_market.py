import hashlib
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.data_sources.base import MarketDataSource
from src.moonshot_market import (
    build_market_risk_snapshot,
    collect_market_risk_evidence,
    export_market_risk_evidence,
    load_market_risk_evidence,
    load_run_compatible_price_evidence,
)


class FakeSource(MarketDataSource):
    def get_stock_data(self, ticker):
        return {"ticker": ticker}

    def get_price_history(self, ticker, period="1y"):
        return MoonshotMarketTests.price_history()

    def get_share_history(self, ticker, period="18mo"):
        if ticker == "SHARE_FAIL":
            raise OSError("share endpoint unavailable")
        return MoonshotMarketTests.share_history()


class MoonshotMarketTests(unittest.TestCase):
    def test_calculates_liquidity_volatility_drawdown_dilution_and_splits(self):
        snapshot = build_market_risk_snapshot(
            "TEST",
            self.price_history(),
            self.share_history(),
            captured_at="2026-08-23T12:00:00+00:00",
        )

        self.assertEqual(snapshot["data_status"], "complete")
        self.assertGreater(snapshot["average_dollar_volume_30d"], 900_000)
        self.assertGreater(snapshot["annualized_volatility_percent"], 0)
        self.assertGreater(snapshot["maximum_drawdown_percent"], 0)
        self.assertEqual(snapshot["share_count_change_percent"], 50)
        self.assertEqual(snapshot["dilution_percent"], 50)
        self.assertEqual(snapshot["reverse_split_count_1y"], 1)

    def test_short_history_does_not_invent_market_metrics(self):
        prices = self.price_history().head(2)

        snapshot = build_market_risk_snapshot("SHORT", prices)

        self.assertIsNone(snapshot["average_dollar_volume_30d"])
        self.assertIsNone(snapshot["annualized_volatility_percent"])
        self.assertIsNone(snapshot["maximum_drawdown_percent"])
        self.assertEqual(snapshot["reverse_split_count_1y"], 0)
        self.assertEqual(snapshot["data_status"], "partial")

    def test_collection_isolates_share_history_failures(self):
        evidence, summary = collect_market_risk_evidence(
            [{"ticker": "GOOD"}, {"ticker": "SHARE_FAIL"}],
            FakeSource(),
            max_workers=2,
        )

        self.assertEqual(evidence["GOOD"]["data_status"], "complete")
        self.assertEqual(evidence["SHARE_FAIL"]["data_status"], "price_only")
        self.assertIn("share_history", evidence["SHARE_FAIL"]["errors"][0])
        self.assertEqual(summary["with_errors"], 1)

    def test_loads_only_price_cache_compatible_with_source_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prices = root / "prices"
            prices.mkdir()
            digest = hashlib.sha256(b"GOOD|1y").hexdigest()
            path = prices / f"{digest}.json"
            self.price_history().to_json(
                path, orient="table", date_format="iso", index=False,
            )

            evidence, stats = load_run_compatible_price_evidence(
                root,
                [{"ticker": "GOOD"}, {"ticker": "MISSING"}],
                "2099-01-01T00:00:00+00:00",
            )

        self.assertEqual(set(evidence), {"GOOD"})
        self.assertEqual(evidence["GOOD"]["source"], "run_compatible_price_cache")
        self.assertEqual(stats["loaded"], 1)
        self.assertEqual(stats["missing"], 1)

    def test_market_evidence_round_trip_checks_run_provenance(self):
        evidence = {"TEST": build_market_risk_snapshot(
            "TEST", self.price_history(), self.share_history(),
        )}
        with tempfile.TemporaryDirectory() as directory:
            export_market_risk_evidence(
                evidence, Path(directory), "run-1", "2026-08-23T12:00:00+00:00",
            )
            loaded = load_market_risk_evidence(Path(directory), "run-1")

        self.assertEqual(loaded["TEST"]["dilution_percent"], 50)

    @staticmethod
    def price_history():
        dates = pd.date_range("2026-01-01", periods=220, freq="B", tz="UTC")
        closes = [10 + index * .03 for index in range(110)]
        closes += [13.3 - (index * .04) for index in range(110)]
        splits = [0.0] * 220
        splits[150] = .1
        return pd.DataFrame({
            "Date": dates,
            "Close": closes,
            "Volume": [100_000] * 220,
            "Stock Splits": splits,
        })

    @staticmethod
    def share_history():
        return pd.DataFrame({
            "Date": pd.to_datetime(
                ["2026-01-01", "2026-10-15"], utc=True,
            ),
            "Shares": [10_000_000, 1_500_000],
        })


if __name__ == "__main__":
    unittest.main()
