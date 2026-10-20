"""Restate reported share counts into the basis of a split-adjusted price.

Provider price history is adjusted backwards: a 2018 close in a series
downloaded today is expressed in today's share units. A share count read from
a 2018 filing is expressed in 2018's units. Multiplying the two gives a market
capitalisation wrong by the cumulative split ratio in between.

The error is neither small nor random. 22% of month-end observations in the
ten-year window have at least one split after them, and a company that
reverse-splits seven times over the following five years has its historical
market cap overstated by a factor of a trillion, its sales yield driven to
zero, and its rank pinned to the bottom. Chronic reverse-splitters do go on to
collapse, so the distortion looks exactly like a value signal while being
nothing but a future event leaking backwards. Correcting it is what separates
a sales-yield result from a restatement of which companies later diluted.

Ratios are as the provider reports them: the number of new shares per old
share, so 0.05 for a one-for-twenty reverse split and 2.0 for a two-for-one
forward split. Multiplying an old-basis share count by the product of the
ratios of every split after it restates it into the current basis.

The events are read from the same frame as the prices on purpose. A frame is
adjusted to the date it was downloaded, so a cached frame fetched before a
split neither reflects it in the closes nor lists it in the events, and one
fetched after does both. Reading events from a separate, fresher source would
break that agreement and over-adjust against stale prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math


@dataclass(frozen=True)
class SplitEvent:
    """One split: new shares per old share, effective on a date."""

    on: date
    ratio: float


class SplitHistory:
    """The split events known for one listing, in date order."""

    __slots__ = ("events",)

    def __init__(self, events: list[SplitEvent] | None = None) -> None:
        self.events = sorted(
            (event for event in (events or []) if _usable(event.ratio)),
            key=lambda event: event.on,
        )

    @classmethod
    def from_price_history(cls, frame) -> "SplitHistory":
        """Read split events from a provider price frame.

        The frame is whatever the market-data source returned, so the column
        may be absent entirely; that is an empty history, not an error.
        """
        if frame is None:
            return cls([])
        try:
            if getattr(frame, "empty", False):
                return cls([])
            columns = getattr(frame, "columns", None)
            if columns is None or "Stock Splits" not in columns:
                return cls([])
            if "Date" in columns:
                dates = frame["Date"]
            else:
                # Some callers hand over a frame already indexed by date.
                index = getattr(frame, "index", None)
                if index is None or not hasattr(index, "to_series"):
                    return cls([])
                dates = index.to_series()
            pairs = zip(dates, frame["Stock Splits"])
        except (TypeError, KeyError, ValueError):
            return cls([])
        events = []
        for raw_date, raw_ratio in pairs:
            if not _usable(raw_ratio):
                continue
            moment = _as_date(raw_date)
            if moment is None:
                continue
            events.append(SplitEvent(moment, float(raw_ratio)))
        return cls(events)

    @property
    def has_events(self) -> bool:
        return bool(self.events)

    def factor_after(self, boundary: date | None) -> float:
        """Cumulative ratio of every split strictly after ``boundary``.

        Returns 1.0 when nothing applies, so callers can multiply
        unconditionally.
        """
        if boundary is None or not self.events:
            return 1.0
        factor = 1.0
        for event in self.events:
            if event.on > boundary:
                factor *= event.ratio
        return factor if _usable(factor) else 1.0

    def has_event_between(self, start: date | None, end: date | None) -> bool:
        """True when a split falls strictly after ``start`` and up to ``end``.

        A share count stated for a period that ends before a split but filed
        after it may or may not have been retroactively restated, and nothing
        in the extract says which. Those facts are flagged rather than
        silently adjusted one way.
        """
        if start is None or end is None or end <= start:
            return False
        return any(start < event.on <= end for event in self.events)


def _usable(value: object) -> bool:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0 and number != 1.0


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for reader in (
        lambda item: item.to_pydatetime().date(),  # pandas Timestamp
        lambda item: datetime.fromisoformat(str(item).replace("Z", "+00:00")).date(),
        lambda item: datetime.strptime(str(item)[:10], "%Y-%m-%d").date(),
    ):
        try:
            return reader(value)
        except (AttributeError, TypeError, ValueError):
            continue
    return None
