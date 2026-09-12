import yfinance as yf
import pandas as pd

from .base import MarketDataSource


class YFinanceSource(MarketDataSource):
    def get_stock_data(self, ticker: str) -> dict:
        stock = yf.Ticker(ticker)
        info = stock.info or {}

        return {
            "ticker": ticker,
            "company_name": info.get("shortName") or info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "quote_type": info.get("quoteType"),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency"),
            "exchange": info.get("exchange"),
            "country": self._infer_country(ticker),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "operating_margin": info.get("operatingMargins"),
            "profit_margin": info.get("profitMargins"),
            "free_cash_flow": info.get("freeCashflow"),
            "operating_cash_flow": info.get("operatingCashflow"),
            "total_cash": info.get("totalCash"),
            "total_debt": info.get("totalDebt"),
            "return_on_equity": info.get("returnOnEquity"),
            "current_ratio": info.get("currentRatio"),
            "enterprise_to_ebitda": info.get("enterpriseToEbitda"),
            "trailing_pe": info.get("trailingPE"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "debt_to_equity": info.get("debtToEquity"),
            "net_income": info.get("netIncomeToCommon"),
            "fundamental_data_timestamp": info.get("mostRecentQuarter"),
        }

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        df = yf.download(
            ticker,
            period=period,
            interval="1d",
            auto_adjust=True,
            actions=True,
            progress=False,
        )

        if df.empty:
            return pd.DataFrame()

        df = df.reset_index()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]

        return df

    def get_share_history(
        self,
        ticker: str,
        period: str = "18mo",
    ) -> pd.DataFrame:
        """Return Yahoo's reported historical shares outstanding."""
        if period != "18mo":
            raise ValueError("Yahoo share history currently supports period='18mo'.")
        history = yf.Ticker(ticker).get_shares_full()
        if history is None or history.empty:
            return pd.DataFrame(columns=["Date", "Shares"])
        return pd.DataFrame({
            "Date": pd.to_datetime(history.index, utc=True),
            "Shares": pd.to_numeric(history.to_numpy(), errors="coerce"),
        }).dropna(subset=["Shares"])

    @staticmethod
    def _infer_country(ticker: str) -> str:
        if ticker.endswith(".TO") or ticker.endswith(".V") or ticker.endswith(".CN"):
            return "CA"
        return "US"
