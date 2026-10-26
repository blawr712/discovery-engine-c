"""Offline rescoring of completed runs with the current shadow models.

A completed run's checkpoints hold every per-company raw signal that was
available when it ran. Rescoring fills in signals that can be derived from
locally cached data as of the run's completion date (SEC fundamentals and
Form 4 insider activity for U.S. filers), then reapplies the configured
shadow models so weight changes never require a new provider run. Official
Discovery Scores, statuses, and ranks are never modified.

Refreshing goes further and recomputes signals a run already has. A fix to
how a signal is derived is worth no less than a fix to its weight, and both
read the same cached extracts, so neither should need fresh provider calls.
Rows whose source no longer resolves are cleared rather than kept, because a
value that cannot be reproduced from any cached source cannot be checked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from src.fundamentals_pit import FundamentalHistory, SIGNAL_NAMES as FUNDAMENTAL_SIGNAL_NAMES
from src.insider_signals import InsiderHistory, SIGNAL_NAMES as INSIDER_SIGNAL_NAMES
from src.scoring_v2 import apply_all_models, model_configs
from src.liquidity import liquidity_eligible
from src.split_adjustment import SplitHistory


OFFICIAL_FIELDS = ("discovery_score", "status", "reason_flags", "score_confidence")


def rescore_results(
    results: list[dict],
    as_of: date | datetime,
    *,
    fundamentals_lookup: Callable[[str], FundamentalHistory | None] | None = None,
    insider_lookup: Callable[[str], InsiderHistory | None] | None = None,
    splits_lookup: Callable[[str], SplitHistory | None] | None = None,
    countries: tuple[str, ...] = ("US",),
    fx_rates: dict[tuple[str, str], float] | None = None,
    refresh_fundamentals: bool = False,
) -> tuple[list[dict], dict]:
    """Fill cached point-in-time signals and reapply shadow models.

    Fundamentals are only filled where a row has none, unless
    ``refresh_fundamentals`` recomputes them everywhere a cached extract
    exists. ``splits_lookup`` supplies each listing's split events so share
    counts land in the same basis as the cached closes. Insider signals are always refreshed because they were absent
    from earlier runs and their data set grows each quarter. Rows are copied
    and official fields are asserted unchanged.
    """
    as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
    countries = tuple(str(country).upper() for country in countries)
    fx_rates = dict(fx_rates or {})
    updated = [dict(row) for row in results]
    stats = {
        "successful_rows": 0,
        "fundamentals_filled": 0,
        "fundamentals_cleared": 0,
        "fundamentals_unavailable": 0,
        "insiders_filled": 0,
        "insiders_unavailable": 0,
        "not_applicable": 0,
    }

    for row in updated:
        if row.get("status") != "OK":
            continue
        stats["successful_rows"] += 1
        ticker = str(row.get("ticker", ""))
        if str(row.get("country") or "").upper() not in countries:
            stats["not_applicable"] += 1
            continue

        if fundamentals_lookup is not None and (
            refresh_fundamentals or not _has_fundamentals(row)
        ):
            history = _safe(fundamentals_lookup, ticker)
            if history is not None and history.has_data:
                multiplier, note = _fx_multiplier(row, history, fx_rates)
                price = _number(row.get("latest_close"))
                market_cap = _number(row.get("market_cap"))
                row.update(history.signals_as_of(
                    as_of_date,
                    price=price * multiplier if price is not None and multiplier is not None else None,
                    market_cap=(
                        market_cap * multiplier
                        if market_cap is not None and multiplier is not None else None
                    ),
                    # Without this the share count stays in the filing's basis
                    # while the cached closes are split-adjusted, so a rescore
                    # would quietly undo the correction a live run applied.
                    splits=(
                        _safe(splits_lookup, ticker)
                        if splits_lookup is not None else None
                    ),
                ))
                row["fundamentals_status"] = "rescored" + note
                stats["fundamentals_filled"] += 1
            elif refresh_fundamentals and _has_fundamentals(row):
                # These values came from a source that no longer resolves to
                # this issuer, or is no longer cached. Either way they cannot
                # be reproduced or checked, so keeping them would publish a
                # number with no source. A later run can re-collect them.
                for name in FUNDAMENTAL_SIGNAL_NAMES:
                    row[name] = None
                row["fundamentals_status"] = "cleared: source no longer resolves"
                stats["fundamentals_cleared"] += 1
            else:
                for name in FUNDAMENTAL_SIGNAL_NAMES:
                    row.setdefault(name, None)
                row.setdefault("fundamentals_status", "unavailable: not cached")
                stats["fundamentals_unavailable"] += 1

        if insider_lookup is not None:
            # Recent filings are read only for tradeable names; others keep
            # the data-set history, as in a live run.
            lookup = insider_lookup
            if hasattr(insider_lookup, "bulk_only") and not liquidity_eligible(row):
                lookup = insider_lookup.bulk_only
            history = _safe(lookup, ticker)
            if history is not None and history.has_data:
                row.update(history.signals_as_of(
                    as_of_date, market_cap=_number(row.get("market_cap")),
                ))
                row["insiders_status"] = "rescored"
                stats["insiders_filled"] += 1
            else:
                for name in INSIDER_SIGNAL_NAMES:
                    row.setdefault(name, None)
                row["insiders_status"] = "no_data"
                stats["insiders_unavailable"] += 1

    rescored = apply_all_models(updated)
    for before, after in zip(results, rescored):
        for field in OFFICIAL_FIELDS:
            if before.get(field) != after.get(field):
                raise RuntimeError(
                    f"Rescoring changed official field {field!r} for "
                    f"{before.get('ticker')}; refusing to continue."
                )
    stats["models"] = {
        model["output_prefix"]: model["model_version"] for model in model_configs()
    }
    stats["as_of"] = as_of_date.isoformat()
    stats["refresh_fundamentals"] = bool(refresh_fundamentals)
    return rescored, stats


def _fx_multiplier(row: dict, history, fx_rates: dict) -> tuple[float | None, str]:
    trading = str(row.get("currency") or "").upper()
    reporting = str(getattr(history, "currency", "") or "").upper()
    if not trading or not reporting or trading == reporting:
        return 1.0, ""
    rate = fx_rates.get((trading, reporting))
    if rate is None:
        return None, f"; {trading}->{reporting} rate unavailable, valuation ratios skipped"
    return float(rate), f"; converted {trading}->{reporting}"


def _has_fundamentals(row: dict) -> bool:
    return any(
        row.get(name) is not None
        for name in ("pit_revenue_ttm", "pit_cash", "pit_shares_outstanding")
    )


def _safe(lookup: Callable[[str], object], ticker: str):
    try:
        return lookup(ticker)
    except Exception:  # noqa: BLE001 - isolate per-company lookup failures
        return None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
