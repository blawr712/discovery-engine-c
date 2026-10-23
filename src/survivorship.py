"""Bound what survivorship costs the backtest, using SEC records of exits.

The backtest replays companies that are still listed today. Every company
that was delisted between 2016 and now is missing, along with all of its
monthly observations, and the missing ones are not a random sample: many went
bankrupt or were delisted for failing listing rules, and some were bought out
at a premium. Their prices are not available for free, but the SEC keeps
their filings, and that is enough to bound the damage.

1. Exchange delisting notices (Form 25 and 25-NSE) for companies not in
   today's universe give the population and the exit date.
2. Each company's filings around the exit give the exit type: a bankruptcy
   8-K (item 1.03), a completed acquisition (a merger proxy, a tender-offer
   response, or an 8-K item 5.01 change in control), a delisting for failing
   listing rules (8-K item 3.01), or none of these.
3. Its fundamentals at each month-end, from the same SEC extracts the
   backtest uses, say whether v3's filters would have rejected it, because the
   filters need no price. Its reported public float gives a size estimate for
   the universe's market-cap band.
4. Its returns are unknown, so scenarios assign a total return over its
   in-universe life by exit type, spread evenly across its months, and the
   backtest's compounded group means are recomputed with it included.

The result is a range, not a measurement: the size estimate is rough, a
reverse split before death understates dilution because there is no price
series to read splits from, and the return scenarios are assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from src.fundamentals_pit import FundamentalHistory

EXIT_TYPES = ("bankruptcy", "acquired", "delisted_noncompliance", "other")
MERGER_FORMS = frozenset({"DEFM14A", "DEFM14C", "PREM14A", "SC 14D9", "SC TO-T", "SC 13E3"})
BLANK_CHECK_SIC = "6770"

# Total return over a dead company's in-universe life, by how it exited.
SCENARIOS = {
    "mild": {"bankruptcy": -0.80, "delisted_noncompliance": -0.50, "other": -0.30, "acquired": 0.20},
    "central": {"bankruptcy": -0.95, "delisted_noncompliance": -0.75, "other": -0.50, "acquired": 0.10},
    "severe": {"bankruptcy": -0.999, "delisted_noncompliance": -0.90, "other": -0.70, "acquired": 0.0},
}


@dataclass(frozen=True)
class Exit:
    cik: str
    name: str
    exit_date: date
    exit_type: str
    sic: str | None
    sector: str
    evidence: tuple[str, ...]

    @property
    def blank_check(self) -> bool:
        return self.sic == BLANK_CHECK_SIC


def delisted_candidates(
    notices: Iterable[dict],
    current_ciks: set[str],
    start: date,
    end: date,
) -> dict[str, dict]:
    """Issuers with a delisting notice in the window that are not listed now."""
    found: dict[str, dict] = {}
    for row in notices:
        filed = _date(row["filed"])
        cik = str(row["cik"]).zfill(10)
        if filed is None or not (start <= filed <= end) or cik in current_ciks:
            continue
        entry = found.setdefault(cik, {"name": row.get("name"), "notices": []})
        entry["notices"].append(filed)
    for entry in found.values():
        entry["notices"].sort()
    return found


def classify_exit(cik: str, name: str, notice_dates: list[date], submissions: dict) -> Exit:
    """Date and type a company's exit from its delisting notices and filings.

    The exit is the last delisting notice, since earlier notices are often for
    warrants or notes. Bankruptcy wins over everything, because equity is
    normally wiped out even when assets are sold; a completed acquisition
    comes next, then a delisting for failing listing rules.
    """
    exit_date = max(notice_dates)
    filings = submissions.get("exit_filings") or []
    evidence = []

    def within(row, before_days, after_days):
        filed = _date(row.get("filed"))
        return filed is not None and (
            exit_date - timedelta(days=before_days) <= filed <= exit_date + timedelta(days=after_days)
        )

    bankrupt = [r for r in filings if "1.03" in (r.get("items") or []) and within(r, 730, 60)]
    merger_documents = [r for r in filings if r.get("form") in MERGER_FORMS and within(r, 365, 30)]
    control_change = [r for r in filings if "5.01" in (r.get("items") or []) and within(r, 60, 30)]
    noncompliance = [r for r in filings if "3.01" in (r.get("items") or []) and within(r, 365, 0)]
    if bankrupt:
        exit_type = "bankruptcy"
        evidence = [f"8-K 1.03 {r['filed']}" for r in bankrupt]
    elif merger_documents or control_change:
        exit_type = "acquired"
        evidence = [f"{r['form']} {r['filed']}" for r in merger_documents] + [
            f"8-K 5.01 {r['filed']}" for r in control_change
        ]
    elif noncompliance:
        exit_type = "delisted_noncompliance"
        evidence = [f"8-K 3.01 {r['filed']}" for r in noncompliance]
    else:
        exit_type = "other"
    sic = submissions.get("sic")
    return Exit(
        cik=str(cik).zfill(10),
        name=str(submissions.get("name") or name or ""),
        exit_date=exit_date,
        exit_type=exit_type,
        sic=sic,
        sector=sector_for_sic(sic),
        evidence=tuple(evidence),
    )


def sector_for_sic(sic: object) -> str:
    """The provider-style sector that matters to v3: financials or not.

    v3 treats Financial Services differently, so that is the distinction
    worth recovering from an SIC code; everything else ranks as Unknown.
    """
    try:
        code = int(str(sic))
    except (TypeError, ValueError):
        return "Unknown"
    if code in (6798,) or 6500 <= code <= 6553:
        return "Real Estate"
    if 6000 <= code <= 6799:
        return "Financial Services"
    return "Unknown"


def public_float_as_of(extract: dict, as_of: date, max_age_days: int = 550) -> float | None:
    """The latest public float filed by a date, if recent enough to use."""
    best = None
    for row in (extract.get("facts") or {}).get("public_float", []):
        end, filed = _date(row.get("end")), _date(row.get("filed"))
        if end is None or filed is None or filed > as_of or (as_of - end).days > max_age_days:
            continue
        if best is None or (end, filed) > (best[0], best[1]):
            best = (end, filed, float(row["val"]))
    return best[2] if best else None


def estimated_market_cap(public_float: float | None, insider_share: float = 0.25) -> float | None:
    """Market cap implied by a public float, given insiders' assumed share."""
    if public_float is None or public_float <= 0:
        return None
    return public_float / (1.0 - insider_share)


def dead_observations(
    exits: list[Exit],
    extracts: dict[str, dict],
    periods: list[tuple[str, date]],
    minimum_cap: float,
    maximum_cap: float,
    insider_share: float = 0.25,
    include_unknown_size: bool = False,
) -> pd.DataFrame:
    """One row per dead company per month-end it was plausibly in the universe."""
    rows = []
    for exit in exits:
        if exit.blank_check:
            continue
        extract = extracts.get(exit.cik)
        if not extract:
            continue
        history = FundamentalHistory(extract)
        for label, moment in periods:
            if moment >= exit.exit_date:
                continue
            cap = estimated_market_cap(public_float_as_of(extract, moment), insider_share)
            if cap is None:
                if not include_unknown_size:
                    continue
                size = "unknown"
            elif minimum_cap <= cap <= maximum_cap:
                size = "in_band"
            else:
                continue
            signals = history.signals_as_of(moment)
            if signals.get("pit_revenue_ttm") is None and signals.get("pit_cash") is None:
                continue  # nothing filed recently enough: not a reporting company then
            rows.append({
                "period": label,
                "ticker": f"DEAD:{exit.cik}",
                "sector": exit.sector,
                "exit_type": exit.exit_type,
                "exit_date": exit.exit_date.isoformat(),
                "estimated_market_cap": cap,
                "size_basis": size,
                **signals,
            })
    return pd.DataFrame(rows)


def assign_groups(period_frame: pd.DataFrame, model: dict, score_frame) -> pd.Series:
    """What v3 would have done with each row: rejected, scored, or neither.

    Dead rows have no prices, so their v3 score cannot be computed; whether
    the filters reject them can, because the filters use fundamentals only.
    A dead row the filters keep counts as scored when it had the revenue and
    cash figures v3's value signals need, and as never scored otherwise.
    """
    scored = score_frame(period_frame, model)
    excluded = scored["excluded"].astype(bool)
    has_score = scored["score"].notna()
    dead = period_frame["ticker"].astype(str).str.startswith("DEAD:")
    would_score = (
        period_frame.get("pit_revenue_ttm", pd.Series(index=period_frame.index)).notna()
        & period_frame.get("pit_cash", pd.Series(index=period_frame.index)).notna()
    )
    groups = pd.Series("never_scored", index=period_frame.index)
    groups[has_score] = "scored"
    groups[dead & ~excluded & would_score] = "scored"
    groups[excluded] = "filter_excluded"
    return groups


def monthly_rates(dead: pd.DataFrame, scenario: dict[str, float]) -> pd.Series:
    """Spread each dead company's assumed lifetime return evenly over its months."""
    if dead.empty:
        return pd.Series(dtype=float)
    months = dead.groupby("ticker")["period"].transform("count")
    lifetime = dead["exit_type"].map(scenario).astype(float)
    return ((1.0 + lifetime) ** (1.0 / months) - 1.0) * 100.0


def compounded(values: pd.Series) -> float:
    values = values.dropna()
    return float((np.prod(1.0 + values / 100.0) - 1.0) * 100.0) if len(values) else float("nan")


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
