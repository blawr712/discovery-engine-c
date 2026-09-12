from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    @abstractmethod
    def get_stock_data(self, ticker: str) -> dict:
        pass

    @abstractmethod
    def get_price_history(self, ticker: str, period: str = "1y"):
        pass

    def get_share_history(self, ticker: str, period: str = "18mo"):
        """Return historical shares outstanding when the provider supports it."""
        raise NotImplementedError("Share-count history is not supported.")
