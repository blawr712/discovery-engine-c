"""Recent insider transactions read from each company's own Form 4 filings.

The SEC's quarterly insider data sets are the backtest's source and stay the
history of record, but they are published late: in late October 2026 the
newest available quarter was January to March. Live insider signals were
therefore about seven months stale, and Score v3.2's twelve-month purchase
window was missing its most recent seven months.

This source fills the gap from EDGAR directly. A company's submissions list
names every ownership filing made about it; each Form 4 is a small XML
document holding the same fields the data sets do. Rows are produced in
exactly the data sets' shape -- open-market purchases and sales (codes P and
S) from the non-derivative table, dated by filing date, with owners and their
relationship to the company -- so a signal means the same thing whichever
source its rows came from.

A filing never changes once accepted, so each parsed filing is cached for
good; a company's list of filings is refreshed after a day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ElementTree

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
OWNERSHIP_FORMS = frozenset({"4", "4/A", "5", "5/A"})
OPEN_MARKET_CODES = {"P": "purchase", "S": "sale"}
CACHE_VERSION = "form4-1"


@dataclass
class Form4Stats:
    requests: int = 0
    listings_cached: int = 0
    filings_cached: int = 0
    filings_parsed: int = 0
    filings_unreadable: int = 0


@dataclass
class SecForm4Source:
    """Paced, cached access to companies' recent ownership filings."""

    user_agent: str
    cache_directory: Path
    request_interval_seconds: float = 0.11
    listing_ttl_hours: float = 24.0
    fetcher: Callable[[str, dict[str, str]], bytes] | None = None
    clock: Callable[[], float] = time.time
    stats: Form4Stats = field(default_factory=Form4Stats)
    # Shared with other SEC sources so their combined rate stays in bounds.
    pacer: object | None = None

    def __post_init__(self) -> None:
        if not self.user_agent or "@" not in self.user_agent:
            raise ValueError("SEC_USER_AGENT must identify an organization and contact email.")
        self.cache_directory = Path(self.cache_directory)
        self.headers = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        self.fetcher = self.fetcher or _fetch
        self._pace_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._last_request = 0.0

    def ownership_filings(self, cik: str | int) -> dict:
        """The ownership filings EDGAR lists for a company, newest first."""
        cik = _cik(cik)
        path = self._path("listings", cik)
        cached = _read(path)
        if cached is not None and not _older_than(path, self.listing_ttl_hours, self.clock):
            self._count("listings_cached")
            return cached
        payload = json.loads(self._request(SUBMISSIONS_URL.format(cik=cik)).decode("utf-8"))
        listing = compact_listing(payload, retrieved=_today(self.clock))
        _write(path, listing)
        return listing

    def transactions(
        self,
        cik: str | int,
        filed_after: date | None,
    ) -> tuple[list[dict], date, bool]:
        """Open-market transactions filed after a date, the date the list is
        current to, and whether the list could be incomplete."""
        cik = _cik(cik)
        listing = self.ownership_filings(cik)
        rows = []
        for filing in listing["filings"]:
            filed = _date(filing["filed"])
            if filed is None or (filed_after is not None and filed <= filed_after):
                continue
            rows.extend(self._filing_rows(cik, filing))
        rows.sort(key=lambda row: (row["filed"], row["accession"]))
        covered = _date(listing.get("retrieved")) or _today(self.clock)
        earliest = _date(listing.get("earliest_listed"))
        # EDGAR's recent list holds about a thousand filings. If it was cut
        # short and does not reach back to the cutoff, older filings in the
        # window are missing; the caller is told rather than left guessing.
        incomplete = bool(listing.get("truncated")) and (
            filed_after is None or earliest is None or earliest > filed_after
        )
        return rows, covered, incomplete

    def _filing_rows(self, cik: str, filing: dict) -> list[dict]:
        path = self._path("filings", filing["accession"])
        cached = _read(path)
        if cached is not None:
            self._count("filings_cached")
            return cached
        url = DOCUMENT_URL.format(
            cik=int(cik), accession=filing["accession"].replace("-", ""),
            document=filing["document"],
        )
        try:
            rows = parse_form4(self._request(url), filing["accession"], filing["filed"])
            self._count("filings_parsed")
        except ElementTree.ParseError:
            rows = []
            self._count("filings_unreadable")
        _write(path, rows)
        return rows

    def _request(self, url: str) -> bytes:
        if self.pacer is not None:
            self.pacer.wait()
        else:
            with self._pace_lock:
                wait = self.request_interval_seconds - (self.clock() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                self._last_request = self.clock()
        self._count("requests")
        return self.fetcher(url, self.headers)

    def _path(self, kind: str, key: str) -> Path:
        digest = hashlib.sha256(f"{CACHE_VERSION}|{kind}|{key}".encode("utf-8")).hexdigest()
        return self.cache_directory / kind / f"{digest}.json.gz"

    def _count(self, name: str) -> None:
        with self._stats_lock:
            setattr(self.stats, name, getattr(self.stats, name) + 1)


def compact_listing(payload: dict, retrieved: date) -> dict:
    """Ownership filings from a submissions response, with their raw XML name."""
    recent = ((payload.get("filings") or {}).get("recent")) or {}
    filings = []
    dates = recent.get("filingDate") or []
    for form, filed, accession, document in zip(
        recent.get("form") or [], dates,
        recent.get("accessionNumber") or [], recent.get("primaryDocument") or [],
    ):
        if str(form).strip() not in OWNERSHIP_FORMS or not document:
            continue
        # The listed document is EDGAR's rendered view under an xsl folder;
        # the raw XML sits beside it under the same name.
        raw = document.split("/", 1)[1] if document.startswith("xsl") and "/" in document else document
        filings.append({"form": str(form).strip(), "filed": filed, "accession": accession, "document": raw})
    return {
        "cik": str(payload.get("cik", "")).zfill(10),
        "retrieved": retrieved.isoformat(),
        "earliest_listed": min(dates) if dates else None,
        "truncated": bool((payload.get("filings") or {}).get("files")),
        "filings": filings,
    }


def parse_form4(document: bytes | str, accession: str, filed: str) -> list[dict]:
    """Open-market transactions from one ownership document, in data-set shape."""
    root = ElementTree.fromstring(document)
    issuer_cik = (_text(root, "issuer/issuerCik") or "").zfill(10)
    symbol = (_text(root, "issuer/issuerTradingSymbol") or "").upper()
    owner_ciks = sorted({
        cik for cik in (
            _text(owner, "reportingOwnerId/rptOwnerCik") for owner in root.findall("reportingOwner")
        ) if cik
    })
    relationships = [owner.find("reportingOwnerRelationship") for owner in root.findall("reportingOwner")]
    officer_or_director = any(
        _flag(rel, "isOfficer") or _flag(rel, "isDirector") for rel in relationships if rel is not None
    )
    ten_percent = any(_flag(rel, "isTenPercentOwner") for rel in relationships if rel is not None)
    rows = []
    for transaction in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = (_text(transaction, "transactionCoding/transactionCode") or "").upper()
        if code not in OPEN_MARKET_CODES:
            continue
        shares = _number(_text(transaction, "transactionAmounts/transactionShares/value"))
        if shares is None or shares <= 0:
            continue
        price = _number(_text(transaction, "transactionAmounts/transactionPricePerShare/value"))
        trans_date = _text(transaction, "transactionDate/value")
        rows.append({
            "accession": accession,
            "issuer_cik": issuer_cik,
            "symbol": symbol,
            "filed": filed,
            "trans_date": trans_date[:10] if trans_date else None,
            "code": code,
            "kind": OPEN_MARKET_CODES[code],
            "shares": shares,
            "price": price,
            "value": round(shares * price, 2) if price is not None else None,
            "acquired": (
                _text(transaction, "transactionAmounts/transactionAcquiredDisposedCode/value") or ""
            ).upper() == "A",
            "owner_ciks": owner_ciks,
            "officer_or_director": officer_or_director,
            "ten_percent_owner": ten_percent,
        })
    return rows


def _text(node, path: str) -> str | None:
    found = node.find(path) if node is not None else None
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _flag(node, name: str) -> bool:
    return (_text(node, name) or "").lower() in {"1", "true"}


def _number(value: str | None) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value is not None else None
    except ValueError:
        return None


def _cik(value: str | int) -> str:
    return str(value).strip().lstrip("0").zfill(10)


def _date(value: object) -> date | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _today(clock: Callable[[], float]) -> date:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).date()


def _older_than(path: Path, hours: float, clock: Callable[[], float]) -> bool:
    try:
        return clock() - path.stat().st_mtime > hours * 3600.0
    except OSError:
        return True


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    with urlopen(Request(url, headers=headers), timeout=60) as response:
        return response.read()


def _read(path: Path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    os.close(descriptor)
    try:
        with gzip.open(name, "wt", encoding="utf-8") as file:
            json.dump(value, file, separators=(",", ":"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
