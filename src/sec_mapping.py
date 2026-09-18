"""Resolve which SEC registrant, if any, a universe listing corresponds to.

U.S. tickers map directly through the SEC ticker file. Canadian listings can
only be matched when the issuer is interlisted in the United States, and a
bare root symbol is not enough: ``ACT.TO`` (Aduro Clean Technologies) shares
its root with ``ACT`` (Enact Holdings). Every Canadian candidate must
therefore also pass a registrant-name check before its SEC facts are used.
"""

from __future__ import annotations

import re
from typing import Callable


STOPWORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "holdings", "holding", "group", "the", "of", "and",
    "class", "common", "shares", "stock", "trust", "lp", "llc", "sa", "nv",
}


def resolve_sec_ticker(
    stock_data: dict,
    cik_lookup: Callable[[str], str | None],
    title_lookup: Callable[[str], str | None],
    *,
    countries_with_direct_mapping: tuple[str, ...] = ("US",),
    minimum_name_similarity: float = 0.5,
) -> tuple[str | None, str]:
    """Return ``(sec_ticker, reason)`` for a screened company row.

    ``reason`` explains a ``None`` result so run rows can record it.
    """
    ticker = str(stock_data.get("ticker") or "").strip().upper()
    country = str(stock_data.get("country") or "").upper()
    if not ticker:
        return None, "missing ticker"
    if country in countries_with_direct_mapping:
        if cik_lookup(ticker) is None:
            return None, "no SEC CIK mapping"
        return ticker, "direct"

    interlisted = str(stock_data.get("universe_interlisted") or "").strip()
    if not interlisted:
        return None, "not interlisted"
    company_name = str(
        stock_data.get("universe_company_name") or stock_data.get("company_name") or ""
    )
    root = str(stock_data.get("universe_root_ticker") or ticker.split(".")[0]).strip().upper()
    for candidate in _candidates(root):
        if cik_lookup(candidate) is None:
            continue
        title = title_lookup(candidate) or ""
        similarity = name_similarity(company_name, title)
        if similarity >= minimum_name_similarity:
            return candidate, f"interlisted:{candidate} name match {similarity:.2f}"
        return None, (
            f"interlisted root {candidate} maps to {title!r}, "
            f"name similarity {similarity:.2f} below {minimum_name_similarity}"
        )
    return None, "interlisted but no SEC CIK mapping for root symbol"


def name_similarity(left: str, right: str) -> float:
    """Jaccard similarity of meaningful name tokens, 0-1."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(name).lower())
    return {word for word in words if word not in STOPWORDS}


def _candidates(root: str) -> list[str]:
    candidates = [root]
    if "." in root:
        candidates.append(root.replace(".", "-"))
    return [candidate for candidate in candidates if candidate]
