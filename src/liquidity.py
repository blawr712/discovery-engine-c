"""The trading-liquidity floor that decides which names are research candidates.

A stock that trades a few thousand dollars a day cannot be bought in any
meaningful size without moving its price, and its closing prices are thin
enough that measured returns overstate what anyone could have captured. In
the Oct 19 run half of v3's top 25 traded below $250,000 a day and the top
name about $5,300. The configured floor was defined in
``universe.minimum_average_dollar_volume`` but never applied.

Average dollar volume is the 30-day mean volume times the 30-day mean close,
the definition the technical liquidity score has always used, in the
listing's trading currency, as the market-cap band is.
"""

from __future__ import annotations

import json
import math

from src.config import STRATEGY

MINIMUM_AVERAGE_DOLLAR_VOLUME = float(
    (STRATEGY.get("universe") or {}).get("minimum_average_dollar_volume", 250_000)
)


def average_dollar_volume(row: dict) -> float | None:
    """A row's 30-day average dollar volume.

    Rows scored before the field existed carry the same number as the raw
    value of the technical liquidity factor, so saved runs can be gated
    without new price downloads.
    """
    value = _number(row.get("average_dollar_volume_30d"))
    if value is not None:
        return value
    breakdown = row.get("factor_breakdown")
    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except (TypeError, ValueError):
            breakdown = None
    if isinstance(breakdown, dict):
        return _number((breakdown.get("liquidity") or {}).get("raw_value"))
    return None


def liquidity_eligible(row: dict, floor: float = MINIMUM_AVERAGE_DOLLAR_VOLUME) -> bool:
    """True when a row trades at least the floor; unknown liquidity is not enough."""
    if floor <= 0:
        return True
    value = average_dollar_volume(row)
    return value is not None and value >= floor


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
