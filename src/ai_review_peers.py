"""Peer tables for the AI review: why is a name cheap?

Code picks every peer and computes every figure; the review only interprets
the table (AI_REVIEW_BRIEF.md, "Peer comparison"). Industry is the SEC's
four-digit SIC code, widened to three digits when fewer than three usable
peers share it. Two groups:

- closest peers: up to eight names from the same run with the same code,
  nearest to the company by market cap, whatever their screen status;
- industry leaders: the five largest SEC filers with the code by market cap
  on the anchor date, inside the universe or not.

Figures are the ``fundamentals_pit`` signals v3 reads, as of the anchor date:
from the run row for names in the run, or computed with the same functions
for leaders outside it. A peer that cannot be used is listed with the reason,
never guessed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable

PEERS_VERSION = "ai-review-peers-1"
MAX_CLOSEST_PEERS = 8
MAX_LEADERS = 5
MINIMUM_PEERS = 3
# Leaders are shortlisted by SEC public float, then ranked by market cap on
# the anchor date; only the shortlist is priced.
LEADER_SHORTLIST = 10

# The metrics a review may cite, in table order. Market cap and revenue are
# in each filer's reporting currency, so the table compares them in dollars.
# Net margin is the one figure fundamentals_pit does not produce; it is
# derived from two that it does.
METRICS = (
    "market_cap_usd",
    "revenue_ttm_usd",
    "pit_revenue_growth_ttm",
    "pit_gross_margin_ttm",
    "pit_operating_margin_ttm",
    "net_margin_ttm",
    "pit_earnings_yield",
    "pit_sales_yield",
    "pit_net_cash_to_market_cap",
    "pit_share_change_1y",
)
DERIVED = {
    "market_cap_usd": "pit_market_cap in U.S. dollars at fundamentals.fx_pairs rates",
    "revenue_ttm_usd": "pit_revenue_ttm in U.S. dollars at fundamentals.fx_pairs rates",
    "net_margin_ttm": "pit_net_income_ttm / pit_revenue_ttm",
}


def to_usd(value: float | None, currency: object, fx_rates: dict) -> float | None:
    """A reporting-currency amount in dollars, or None without a rate."""
    if value is None:
        return None
    currency = str(currency or "USD").upper()
    if currency == "USD":
        return value
    rate = fx_rates.get((currency, "USD"))
    return round(value * float(rate), 2) if rate else None


def peer_metrics(row: dict, fx_rates: dict | None = None) -> dict:
    """The table's metrics from a run row or a computed signal set."""
    fx_rates = fx_rates or {}
    currency = row.get("pit_reporting_currency")
    values = {name: _number(row.get(name)) for name in METRICS if name not in DERIVED}
    revenue = _number(row.get("pit_revenue_ttm"))
    net_income = _number(row.get("pit_net_income_ttm"))
    values["market_cap_usd"] = to_usd(_number(row.get("pit_market_cap")), currency, fx_rates)
    values["revenue_ttm_usd"] = to_usd(revenue, currency, fx_rates)
    values["net_margin_ttm"] = (
        round(net_income / revenue, 6)
        if revenue is not None and revenue > 0 and net_income is not None else None
    )
    return {name: values[name] for name in METRICS}


def unusable_reason(row: dict, fx_rates: dict | None = None) -> str | None:
    """Why a peer row cannot be compared, or None when it can."""
    if row.get("pit_revenue_ttm") is None and row.get("pit_market_cap") is None:
        return "no SEC fundamentals"
    if row.get("pit_market_cap") is None:
        return "no market cap (no price, share count or exchange rate)"
    if row.get("pit_revenue_ttm") is None:
        return "no revenue"
    currency = str(row.get("pit_reporting_currency") or "USD").upper()
    if to_usd(1.0, currency, fx_rates or {}) is None:
        return f"no {currency}->USD rate"
    return None


def _usd_size(row: dict, fx_rates: dict) -> float | None:
    return to_usd(_number(row.get("pit_market_cap")), row.get("pit_reporting_currency"), fx_rates)


def closest_peers(
    company: dict,
    candidates: list[dict],
    sic_prefix: str,
    limit: int = MAX_CLOSEST_PEERS,
    fx_rates: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Usable run peers nearest by market cap, and the unusable ones.

    ``candidates`` are run rows carrying ``sic``. The company itself and
    duplicate listings are left out.
    """
    fx_rates = fx_rates or {}
    ticker = str(company.get("ticker"))
    size = _usd_size(company, fx_rates)
    usable, unusable = [], []
    for row in candidates:
        if str(row.get("ticker")) == ticker or row.get("duplicate_of"):
            continue
        if not str(row.get("sic") or "").startswith(sic_prefix):
            continue
        reason = unusable_reason(row, fx_rates)
        if reason is None and (_usd_size(row, fx_rates) or 0) <= 0:
            reason = "market cap not positive"
        if reason is None:
            usable.append(row)
        else:
            unusable.append({"ticker": row.get("ticker"), "group": "closest", "reason": reason})
    if size is not None and size > 0:
        usable.sort(key=lambda row: (abs(math.log(_usd_size(row, fx_rates) / size)), str(row.get("ticker"))))
    else:
        usable.sort(key=lambda row: (-_usd_size(row, fx_rates), str(row.get("ticker"))))
    return usable[:limit], sorted(unusable, key=lambda item: str(item["ticker"]))


def choose_sic_prefix(
    company_sic: str | None, candidates: list[dict], ticker: str, fx_rates: dict | None = None,
) -> tuple[str | None, bool]:
    """The four-digit code, or three digits when fewer than three usable peers share four."""
    sic = str(company_sic or "").strip()
    if len(sic) < 3:
        return None, False

    def count(prefix: str) -> int:
        return sum(
            1 for row in candidates
            if str(row.get("ticker")) != ticker and not row.get("duplicate_of")
            and str(row.get("sic") or "").startswith(prefix)
            and unusable_reason(row, fx_rates) is None
        )

    if len(sic) < 4 or count(sic) >= MINIMUM_PEERS:
        return sic, False
    return sic[:3], True


def shortlist_leaders(
    sic_prefix: str,
    sic_index: dict[str, dict],
    public_floats: dict[str, float],
    exclude_ciks: set[str],
    limit: int = LEADER_SHORTLIST,
) -> list[dict]:
    """SEC filers with the code, largest public float first."""
    rows = []
    for cik, entry in sic_index.items():
        if cik in exclude_ciks or not str(entry.get("sic") or "").startswith(sic_prefix):
            continue
        public_float = public_floats.get(cik)
        if public_float is None or not entry.get("tickers"):
            continue
        rows.append({
            "cik": cik,
            "ticker": entry["tickers"][0],
            "company_name": entry.get("name"),
            "sic": entry.get("sic"),
            "public_float": public_float,
        })
    rows.sort(key=lambda row: (-row["public_float"], row["cik"]))
    return rows[:limit]


def build_peer_table(
    company: dict,
    *,
    company_sic: str | None,
    company_sic_description: str | None,
    company_cik: str | None,
    run_rows: list[dict],
    sic_index: dict[str, dict],
    public_floats: dict[str, float],
    measure_leader: Callable[[dict], tuple[dict | None, str | None]],
    anchor: datetime,
    run_id: str,
    fx_rates: dict | None = None,
) -> dict:
    """One name's ``peers.json``.

    ``run_rows`` are the run's OK rows, each carrying ``sic`` and ``cik``
    where known. ``measure_leader`` returns signals for a shortlisted filer
    as of the anchor date, or a reason it could not.
    """
    fx_rates = fx_rates or {}
    ticker = str(company.get("ticker"))
    table = {
        "ticker": ticker,
        "run_id": run_id,
        "anchor_date": anchor.date().isoformat(),
        "peers_version": PEERS_VERSION,
        "sic": company_sic,
        "sic_description": company_sic_description,
        "provider_industry": company.get("industry"),
        "metrics": list(METRICS),
        "derived_metrics": DERIVED,
        "method": {
            "closest": (
                f"up to {MAX_CLOSEST_PEERS} names in the same run with the same SIC code, "
                "nearest by market cap, any screen status"
            ),
            "leaders": (
                f"the {MAX_LEADERS} largest SEC filers with the same SIC code by market cap on the "
                f"anchor date, from a shortlist of the {LEADER_SHORTLIST} largest by SEC public float "
                "(dei:EntityPublicFloat); filers that report no public float, often foreign filers, "
                "cannot be shortlisted"
            ),
            "figures": "fundamentals_pit signals as of the anchor date, the functions Score v3 uses",
        },
    }
    prefix, widened = choose_sic_prefix(company_sic, run_rows, ticker, fx_rates)
    table["sic_used"] = prefix
    table["widened"] = widened
    company_values = peer_metrics(company, fx_rates)
    if prefix is None:
        table.update({
            "status": "insufficient_peers",
            "status_reason": "no SIC code for the company",
            "company": {"ticker": ticker, "company_name": company.get("company_name"), **company_values},
            "peers": [], "unusable_peers": [], "summary": {},
        })
        return table

    closest, unusable = closest_peers(company, run_rows, prefix, fx_rates=fx_rates)
    peers = [_peer_row(row, "closest", fx_rates) for row in closest]

    run_by_cik = {
        str(row.get("cik")): row for row in run_rows if row.get("cik") and not row.get("duplicate_of")
    }
    exclude = {str(company_cik)} if company_cik else set()
    leaders = []
    for candidate in shortlist_leaders(prefix, sic_index, public_floats, exclude):
        in_run = run_by_cik.get(candidate["cik"])
        if in_run is not None:
            # A leader in the run keeps the run's own figures, the same ones
            # its closest-peer row would show.
            signals, reason, source = in_run, unusable_reason(in_run, fx_rates), "run"
        else:
            signals, reason = measure_leader(candidate)
            source = "computed"
            if signals is not None and reason is None:
                reason = unusable_reason(signals, fx_rates)
        if signals is None or reason is not None:
            unusable.append({
                "ticker": candidate["ticker"], "group": "leader",
                "reason": reason or "no SEC fundamentals",
            })
            continue
        row = {**signals, "ticker": candidate["ticker"],
               "company_name": candidate.get("company_name"), "sic": candidate.get("sic")}
        leaders.append({
            **_peer_row(row, "leader", fx_rates),
            "public_float": candidate["public_float"],
            "figures_from": source,
        })
    leaders.sort(key=lambda row: (-(row["market_cap_usd"] or 0.0), str(row["ticker"])))
    peers.extend(leaders[:MAX_LEADERS])

    distinct = {row["ticker"] for row in peers}
    enough = len(distinct) >= MINIMUM_PEERS
    table.update({
        "status": "ok" if enough else "insufficient_peers",
        "status_reason": None if enough else f"{len(distinct)} usable peers, fewer than {MINIMUM_PEERS}",
        "company": {"ticker": ticker, "company_name": company.get("company_name"), **company_values},
        "peers": peers,
        "unusable_peers": unusable,
        "summary": _summary(company_values, peers),
    })
    return table


def _peer_row(row: dict, group: str, fx_rates: dict) -> dict:
    return {
        "ticker": row.get("ticker"),
        "company_name": row.get("company_name"),
        "group": group,
        "sic": row.get("sic"),
        "screen_status": row.get("screen_status"),
        "pit_reporting_currency": row.get("pit_reporting_currency"),
        **peer_metrics(row, fx_rates),
    }


def _summary(company: dict, peers: list[dict]) -> dict:
    summary = {}
    for name in METRICS:
        closest = [row[name] for row in peers if row["group"] == "closest" and row[name] is not None]
        leaders = [row[name] for row in peers if row["group"] == "leader" and row[name] is not None]
        value = company.get(name)
        summary[name] = {
            "company": value,
            "closest_median": _median(closest),
            "closest_count": len(closest),
            "leader_median": _median(leaders),
            "leader_count": len(leaders),
            "company_percentile_among_closest": _percentile(value, closest),
        }
    return summary


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    value = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return round(value, 6)


def _percentile(value: float | None, values: list[float]) -> float | None:
    """Share of peers below the company, ties counted half, 0-100."""
    if value is None or not values:
        return None
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return round(100.0 * (below + 0.5 * equal) / len(values), 1)


def measure_filer(
    ticker: str,
    anchor: date,
    *,
    facts_lookup: Callable[[str], dict | None],
    price_frame_lookup: Callable[[str], object],
    fx_rates: dict,
) -> tuple[dict | None, str | None]:
    """A filer's signals as of ``anchor``, built the way a live run builds them.

    SEC tickers are U.S. listings, so the close is in dollars and is
    converted into the reporting currency before valuation ratios, as live
    runs do; a filer with no rate cannot be compared.
    """
    from src.data_sources.price_bars import drop_incomplete_bars
    from src.fundamentals_pit import FundamentalHistory
    from src.split_adjustment import SplitHistory

    try:
        extract = facts_lookup(ticker)
    except Exception as error:  # noqa: BLE001 - one filer's failure is a reason, not a crash
        return None, f"no SEC fundamentals ({type(error).__name__})"
    history = FundamentalHistory(extract) if extract else None
    if history is None or not history.has_data:
        return None, "no SEC fundamentals"
    try:
        frame = drop_incomplete_bars(price_frame_lookup(ticker))
    except Exception as error:  # noqa: BLE001
        return None, f"no price ({type(error).__name__})"
    close = close_on_or_before(frame, anchor)
    if close is None:
        return None, "no price on or before the anchor date"
    reporting = str(history.currency or "USD").upper()
    multiplier = 1.0 if reporting == "USD" else fx_rates.get(("USD", reporting))
    if multiplier is None:
        return None, f"no USD->{reporting} rate"
    signals = history.signals_as_of(
        anchor, price=close * float(multiplier), splits=SplitHistory.from_price_history(frame),
    )
    signals["latest_close"] = close
    return signals, None


def close_on_or_before(frame, anchor: date) -> float | None:
    """The last positive close on or before ``anchor`` in a provider frame."""
    import pandas as pd

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty or "Close" not in frame:
        return None
    raw_dates = frame["Date"] if "Date" in frame.columns else pd.Series(frame.index, index=frame.index)
    dates = pd.to_datetime(raw_dates, errors="coerce", utc=True)
    closes = pd.to_numeric(frame["Close"], errors="coerce")
    best = None
    for day, close in zip(dates, closes):
        if pd.isna(day) or pd.isna(close) or close <= 0:
            continue
        if day.date() <= anchor and (best is None or day >= best[0]):
            best = (day, float(close))
    return best[1] if best else None


def latest_public_floats(frames: list[dict]) -> dict[str, float]:
    """Each filer's latest public float across EDGAR ``frames`` payloads."""
    latest: dict[str, tuple[str, float]] = {}
    for frame in frames:
        for item in frame.get("data") or []:
            cik = str(item.get("cik") or "").zfill(10)
            value = _number(item.get("val"))
            end = str(item.get("end") or "")
            if not cik.strip("0") or value is None or value <= 0:
                continue
            if cik not in latest or end > latest[cik][0]:
                latest[cik] = (end, value)
    return {cik: value for cik, (end, value) in latest.items()}


def float_frame_periods(anchor: date, count: int = 4) -> list[str]:
    """The ``CY####Q#I`` frames for the calendar quarters ending before ``anchor``.

    Public float is measured at the end of a filer's second fiscal quarter
    and reported on the next annual report's cover, so four quarters reach
    every fiscal year-end. Floats are used only to shortlist; the ranking
    that picks leaders is by market cap on the anchor date.
    """
    year, quarter = anchor.year, (anchor.month - 1) // 3 + 1
    periods = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
        periods.append(f"CY{year}Q{quarter}I")
    return periods


class SicIndex:
    """SIC codes for every SEC filer with a ticker, kept on disk.

    Built from per-company submissions, read through the paced, cached
    ``SecFilingsSource``. The first fill reads every filer once; later runs
    read only filers not yet indexed and entries older than ``max_age_days``,
    since SIC codes rarely change.
    """

    def __init__(
        self,
        path: Path,
        submissions: Callable[[str], dict],
        *,
        max_age_days: int = 365,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.submissions = submissions
        self.max_age_days = max_age_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.entries: dict[str, dict] = {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = value.get("entries", {}) if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.entries = {}

    def update(
        self,
        ciks: list[str],
        progress: Callable[[int, int], None] | None = None,
        save_every: int = 250,
    ) -> dict[str, int]:
        """Index the given filers; failures are counted and retried next time."""
        now = self.clock()
        wanted = []
        for cik in sorted({str(cik).zfill(10) for cik in ciks if cik}):
            entry = self.entries.get(cik)
            if entry is None or _age_days(entry.get("indexed_at"), now) > self.max_age_days:
                wanted.append(cik)
        counts = {"requested": len(wanted), "indexed": 0, "failed": 0}
        for position, cik in enumerate(wanted, start=1):
            try:
                compact = self.submissions(cik)
            except Exception:  # noqa: BLE001 - one filer's failure must not stop the index
                counts["failed"] += 1
            else:
                self.entries[cik] = {
                    "sic": compact.get("sic"),
                    "sic_description": compact.get("sic_description"),
                    "name": compact.get("name"),
                    "tickers": list(compact.get("tickers") or []),
                    "exchanges": list(compact.get("exchanges") or []),
                    "entity_type": compact.get("entity_type"),
                    "indexed_at": now.isoformat(),
                }
                counts["indexed"] += 1
            if position % save_every == 0:
                self.save()
            if progress:
                progress(position, len(wanted))
        self.save()
        counts["known"] = len(self.entries)
        return counts

    def save(self) -> None:
        _atomic_json(self.path, {"version": PEERS_VERSION, "entries": self.entries})


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _age_days(value: object, now: datetime) -> float:
    try:
        then = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 86400.0


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _atomic_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
