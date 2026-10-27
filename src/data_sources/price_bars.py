"""Remove price bars that carry no closing price.

On Oct 19, 2026 the provider returned that day's bar for 102 tickers with
volume but no open, high, low, or close: an incomplete bar for a day still
trading. Every price signal reads the last close, so each of those names lost
its momentum, volatility, and trailing-high signals and its market cap, and
v3 could not score them. A bar without a close is not a price; it is dropped
wherever histories are read. The backtest already did this.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SPLIT_COLUMN = "Stock Splits"


def drop_incomplete_bars(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """The frame without bars lacking a positive close.

    A split recorded on a dropped bar moves to the next complete bar (or the
    last one, when the dropped bar was last), so share counts can still be
    restated into the price series' basis.
    """
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty or "Close" not in frame:
        return frame
    close = pd.to_numeric(frame["Close"], errors="coerce")
    valid = close.notna() & np.isfinite(close) & (close > 0)
    if bool(valid.all()):
        return frame
    cleaned = frame.copy()
    if SPLIT_COLUMN in cleaned:
        splits = np.array(pd.to_numeric(cleaned[SPLIT_COLUMN], errors="coerce").fillna(0.0), dtype=float)
        keep = valid.to_numpy()
        kept_positions = np.flatnonzero(keep)
        for position in np.flatnonzero(~keep & (splits != 0)):
            if not len(kept_positions):
                break
            later = kept_positions[kept_positions > position]
            target = later[0] if len(later) else kept_positions[-1]
            splits[target] = splits[position] if splits[target] == 0 else splits[target] * splits[position]
        cleaned[SPLIT_COLUMN] = splits
    return cleaned[valid.to_numpy()].reset_index(drop=True) if "Date" in cleaned else cleaned[valid.to_numpy()]
