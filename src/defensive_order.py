"""A defensive ordering inside the Score v3 screen, frozen for a forward test.

The within-screen scan of 2026-10-26 (``src/screen_research.py``) found no
signal that ranks screened names in both backtest windows. Five signals that
agree with each other were significant in 2022-26 and held their sign in 7 or
8 of 9 years, but all reversed in 2020. They were picked after seeing the
holdout, so the backtest cannot validate them. Only months nobody has seen
can, which is what this module is for.

The definition below is the hypothesis being tested. It must not be tuned:
a changed definition is a new hypothesis with a new version and starts its
forward record from zero. The score never orders the research queue; it is
computed for names that pass the screen, frozen into each monthly cohort,
and compared with v3's own order when the cohort resolves.
"""

from __future__ import annotations

import math

DEFENSIVE_VERSION = "defensive-v1"

# (input column, direction): +1 means higher is more defensive.
DEFENSIVE_SIGNALS: tuple[tuple[str, int], ...] = (
    ("v2_high_proximity", 1),           # close to the 52-week high
    ("v2_volatility", -1),              # lower volatility
    ("pit_earnings_yield", 1),          # cheaper on earnings
    ("pit_net_cash_to_market_cap", -1), # less of the value held as net cash
    ("pit_market_cap", 1),              # larger
)
DEFENSIVE_INPUTS = tuple(name for name, _ in DEFENSIVE_SIGNALS)
SCORE_FIELD = "score_defensive"


def definition() -> dict:
    """What a cohort records about the hypothesis it froze."""
    return {
        "version": DEFENSIVE_VERSION,
        "signals": [{"input": name, "direction": direction} for name, direction in DEFENSIVE_SIGNALS],
        "method": (
            "equal-weight mean of signed percentile ranks among names passing "
            "the screen; names missing a signal are scored on those they have; "
            "names missing all sit at 50"
        ),
        "population": "screen_status == 'passes'",
    }


def defensive_scores(rows: list[dict]) -> list[float]:
    """0-100 defensive score for each row, ranked among the rows given.

    Matches ``screen_research.tiebreaker`` (pandas ``rank(pct=True)``, average
    ties) times 100, so the live score is the one the backtest measured.
    """
    parts: list[list[float]] = [[] for _ in rows]
    for name, direction in DEFENSIVE_SIGNALS:
        values = [(index, _number(row.get(name))) for index, row in enumerate(rows)]
        present = [(index, value * direction) for index, value in values if value is not None]
        for index, percentile in _percentile_ranks(present).items():
            parts[index].append(percentile)
    return [
        round(100.0 * (sum(values) / len(values) if values else 0.5), 4)
        for values in parts
    ]


def apply_defensive_order(queue: list[dict]) -> None:
    """Give names passing the screen their defensive score; others get None."""
    passing = [row for row in queue if row.get("screen_status") == "passes"]
    for row, score in zip(passing, defensive_scores(passing)):
        row[SCORE_FIELD] = score
    for row in queue:
        row.setdefault(SCORE_FIELD, None)


def _percentile_ranks(pairs: list[tuple[int, float]]) -> dict[int, float]:
    """Average rank over count, ties sharing their mean rank."""
    ordered = sorted(pairs, key=lambda pair: pair[1])
    count = len(ordered)
    ranks: dict[int, float] = {}
    position = 0
    while position < count:
        end = position
        while end + 1 < count and ordered[end + 1][1] == ordered[position][1]:
            end += 1
        rank = (position + end) / 2.0 + 1.0
        for index, _ in ordered[position:end + 1]:
            ranks[index] = rank / count
        position = end + 1
    return ranks


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
