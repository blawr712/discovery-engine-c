"""Fast, metadata-only screening before expensive price-history downloads."""

from dataclasses import dataclass
from numbers import Real

from src.config import MAX_MARKET_CAP, MIN_MARKET_CAP
from src.asset_classifier import classify_asset


@dataclass(frozen=True)
class PreFilterResult:
    """Explain whether a company meets the baseline research constraints."""

    passed: bool
    reason: str | None = None
    asset_type: str = "unknown"


def evaluate_stock(stock_data: dict) -> PreFilterResult:
    """Apply checks that only require a provider's company metadata."""
    classification = classify_asset(stock_data)

    if not classification.eligible:
        return PreFilterResult(
            False,
            classification.reason,
            classification.asset_type,
        )

    market_cap = stock_data.get("market_cap")

    if market_cap is None:
        return PreFilterResult(
            False,
            "Missing market cap",
            classification.asset_type,
        )

    if isinstance(market_cap, bool) or not isinstance(market_cap, Real):
        return PreFilterResult(
            False,
            "Invalid market cap",
            classification.asset_type,
        )

    if market_cap < MIN_MARKET_CAP:
        return PreFilterResult(
            False,
            "Below minimum market cap",
            classification.asset_type,
        )

    if market_cap > MAX_MARKET_CAP:
        return PreFilterResult(
            False,
            "Above maximum market cap",
            classification.asset_type,
        )

    return PreFilterResult(True, asset_type=classification.asset_type)


def filtered_result(stock_data: dict, reason: str) -> dict:
    """Create an explainable report row for a company removed early."""
    return {
        **stock_data,
        "discovery_score": 0,
        "reason_flags": reason,
        "status": "FILTERED",
    }
