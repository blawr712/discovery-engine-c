from unittest.mock import Mock, patch
import unittest

import pandas as pd

from src.data_sources.yfinance_source import YFinanceSource


class YFinanceSourceTests(unittest.TestCase):
    @patch("src.data_sources.yfinance_source.yf.download")
    def test_requests_corporate_actions_with_adjusted_daily_history(self, download):
        download.return_value = pd.DataFrame()

        result = YFinanceSource().get_price_history("TEST")

        self.assertTrue(result.empty)
        download.assert_called_once_with(
            "TEST", period="1y", interval="1d", auto_adjust=True,
            actions=True, progress=False,
        )

    @patch("src.data_sources.yfinance_source.yf.Ticker")
    def test_normalizes_reported_share_history(self, ticker_class):
        ticker = Mock()
        ticker.get_shares_full.return_value = pd.Series(
            [10_000_000, 12_000_000],
            index=pd.to_datetime(["2025-01-01", "2026-01-01"], utc=True),
        )
        ticker_class.return_value = ticker

        result = YFinanceSource().get_share_history("TEST")

        self.assertEqual(list(result.columns), ["Date", "Shares"])
        self.assertEqual(result.iloc[-1]["Shares"], 12_000_000)
        ticker.get_shares_full.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
