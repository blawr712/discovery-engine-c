"""Configuration-driven classification of investable asset structures."""

from __future__ import annotations

from dataclasses import dataclass
import re

from src.config import ASSET_CLASSIFICATION_CONFIG


@dataclass(frozen=True)
class AssetClassification:
    """Describe an asset structure and whether it is research-eligible."""

    asset_type: str
    eligible: bool
    reason: str
    matched_rule: str | None = None


def classify_asset(stock_data: dict) -> AssetClassification:
    """Classify provider metadata without excluding ambiguous equities."""
    quote_type = _text(stock_data.get("quote_type")).upper()
    company_name = _text(stock_data.get("company_name"))
    industry = _text(stock_data.get("industry"))

    excluded_quote_types = {
        str(value).upper()
        for value in ASSET_CLASSIFICATION_CONFIG.get(
            "excluded_quote_types",
            [],
        )
    }
    if quote_type and quote_type in excluded_quote_types:
        return AssetClassification(
            asset_type="non_common_equity",
            eligible=False,
            reason=f"Excluded quote type: {quote_type}",
            matched_rule=quote_type,
        )

    for excluded_industry in ASSET_CLASSIFICATION_CONFIG.get(
        "excluded_industries",
        [],
    ):
        if str(excluded_industry).lower() in industry.lower():
            return AssetClassification(
                asset_type="shell_company",
                eligible=False,
                reason=f"Excluded industry: {excluded_industry}",
                matched_rule=str(excluded_industry),
            )

    for rule in ASSET_CLASSIFICATION_CONFIG.get("name_rules", []):
        pattern = str(rule["pattern"])
        if re.search(pattern, company_name, flags=re.IGNORECASE):
            return AssetClassification(
                asset_type=str(rule["asset_type"]),
                eligible=False,
                reason=str(rule["reason"]),
                matched_rule=pattern,
            )

    if not company_name and not quote_type and not industry:
        return AssetClassification(
            asset_type="unknown",
            eligible=True,
            reason="Insufficient metadata for structural classification",
        )

    return AssetClassification(
        asset_type="operating_equity",
        eligible=True,
        reason="No excluded asset structure detected",
    )


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
