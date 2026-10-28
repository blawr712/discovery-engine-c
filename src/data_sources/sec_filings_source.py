"""SEC filing indexes and per-company filing histories.

The survivorship test needs to know which companies stopped reporting, when,
and why. EDGAR's quarterly XBRL index lists every periodic report with
structured data, about 0.3 MB a quarter compressed, which is enough to see
when each operating company last filed. The submissions API then gives one
company's filing history, including the delisting notices (Form 25) and
deregistrations (Form 15) that date an exit and the 8-K item codes that
separate a completed acquisition from a bankruptcy.

Both are cached compactly. Past quarters' indexes never change and are kept
for good; the current quarter's is refreshed after a day. A company's
submissions are refreshed after a week, though for a company that has
stopped filing they no longer change either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen

INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/xbrl.gz"
FORM_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.gz"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPACT_VERSION = "sec-filings-1"

# Periodic reports that show a company is still reporting.
PERIODIC_FORMS = frozenset({
    "10-K", "10-K/A", "10-KT", "10-Q", "10-Q/A", "10-QT", "20-F", "20-F/A", "40-F", "40-F/A",
})
# Filings that date or explain an exit. 8-Ks are kept only for these items.
EXIT_FORMS = frozenset({
    "25", "25-NSE", "15-12B", "15-12G", "15-15D", "15F-12B", "15F-12G", "15F-15D",
    "DEFM14A", "DEFM14C", "PREM14A", "SC 14D9", "SC TO-T", "SC 13E3",
})
EXIT_ITEMS = frozenset({"1.03", "2.01", "3.01", "5.01"})
# An exchange's notice that it removed a security from listing (25-NSE), or
# the issuer's own voluntary withdrawal (25), indexed under the issuer.
LISTING_NOTICE_FORMS = ("25-NSE", "25")
_NOTICE_LINE = re.compile(
    r"^(25-NSE|25)\s{2,}(.*?)\s{2,}(\d+)\s{2,}(\d{4}-\d{2}-\d{2})\s{2,}\S+"
)
# The public float companies report on the 10-K cover: the market value of
# shares held by non-affiliates as of the second fiscal quarter's end. It is
# the only price-based size measure on record for a company whose own price
# history is gone.
PUBLIC_FLOAT_CONCEPT = {
    "public_float": {"units": ["USD"], "kind": "instant", "tags": ["dei:EntityPublicFloat"]},
}


@dataclass
class FilingsStats:
    requests: int = 0
    index_hits: int = 0
    submissions_hits: int = 0


@dataclass
class SecFilingsSource:
    """Paced, cached access to EDGAR's XBRL index and submissions API."""

    user_agent: str
    cache_directory: Path
    request_interval_seconds: float = 0.11
    submissions_ttl_hours: float = 168.0
    current_index_ttl_hours: float = 24.0
    fetcher: Callable[[str, dict[str, str]], bytes] | None = None
    clock: Callable[[], float] = time.time
    today: Callable[[], tuple[int, int]] | None = None
    stats: FilingsStats = field(default_factory=FilingsStats)
    # Shared with other SEC sources so their combined rate stays in bounds.
    pacer: object | None = None

    def __post_init__(self) -> None:
        if not self.user_agent or "@" not in self.user_agent:
            raise ValueError("SEC_USER_AGENT must identify an organization and contact email.")
        if self.request_interval_seconds < 0:
            raise ValueError("SEC request interval cannot be negative.")
        self.cache_directory = Path(self.cache_directory)
        self.headers = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        self.fetcher = self.fetcher or _fetch
        self.today = self.today or _current_quarter
        self._pace_lock = threading.Lock()
        self._last_request = 0.0

    def periodic_filings(self, year: int, quarter: int) -> list[dict]:
        """Every periodic report in one quarter's XBRL index."""
        path = self.cache_directory / "index" / f"{COMPACT_VERSION}-{year}q{quarter}.json.gz"
        is_current = (year, quarter) >= self.today()
        rows = _read_gzip_json(path)
        if rows is not None and not (
            is_current and _older_than(path, self.current_index_ttl_hours, self.clock)
        ):
            self.stats.index_hits += 1
            return rows
        raw = self._request(INDEX_URL.format(year=year, quarter=quarter))
        rows = compact_index(gzip.decompress(raw).decode("latin-1"))
        _write_gzip_json(path, rows)
        return rows

    def listing_notices(self, year: int, quarter: int) -> list[dict]:
        """Every Form 25 and 25-NSE filed in one quarter, by issuer."""
        path = self.cache_directory / "notices" / f"{COMPACT_VERSION}-{year}q{quarter}.json.gz"
        is_current = (year, quarter) >= self.today()
        rows = _read_gzip_json(path)
        if rows is not None and not (
            is_current and _older_than(path, self.current_index_ttl_hours, self.clock)
        ):
            self.stats.index_hits += 1
            return rows
        raw = self._request(FORM_INDEX_URL.format(year=year, quarter=quarter))
        rows = compact_listing_notices(gzip.decompress(raw).decode("latin-1"))
        _write_gzip_json(path, rows)
        return rows

    def company_facts(self, cik: str | int) -> dict:
        """A compact fact extract by CIK, with public float, for any filer.

        The project's main SEC source looks companies up by today's ticker,
        which a delisted company no longer has.
        """
        from src.data_sources.sec_xbrl_source import CONCEPTS, compact_company_facts

        cik = str(cik).strip().lstrip("0").zfill(10)
        digest = hashlib.sha256(f"{COMPACT_VERSION}|facts|{cik}".encode("utf-8")).hexdigest()
        path = self.cache_directory / "facts" / f"{digest}.json.gz"
        cached = _read_gzip_json(path)
        if cached is not None and not _older_than(path, self.submissions_ttl_hours, self.clock):
            self.stats.submissions_hits += 1
            return cached
        raw = self._request(COMPANY_FACTS_URL.format(cik=cik))
        extract = compact_company_facts(
            json.loads(raw.decode("utf-8")), f"CIK{cik}", {**CONCEPTS, **PUBLIC_FLOAT_CONCEPT},
        )
        _write_gzip_json(path, extract)
        return extract

    def submissions(self, cik: str | int) -> dict:
        """One company's identity and exit-relevant filing history."""
        cik = str(cik).strip().lstrip("0").zfill(10)
        digest = hashlib.sha256(f"{COMPACT_VERSION}|{cik}".encode("utf-8")).hexdigest()
        path = self.cache_directory / "submissions" / f"{digest}.json.gz"
        cached = _read_gzip_json(path)
        if cached is not None and not _older_than(path, self.submissions_ttl_hours, self.clock):
            self.stats.submissions_hits += 1
            return cached
        raw = self._request(SUBMISSIONS_URL.format(cik=cik))
        compact = compact_submissions(json.loads(raw.decode("utf-8")))
        _write_gzip_json(path, compact)
        return compact

    def _request(self, url: str) -> bytes:
        if self.pacer is not None:
            self.pacer.wait()
            with self._pace_lock:
                self.stats.requests += 1
            return self.fetcher(url, self.headers)
        with self._pace_lock:
            wait = self.request_interval_seconds - (self.clock() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = self.clock()
            self.stats.requests += 1
        return self.fetcher(url, self.headers)


def compact_index(text: str) -> list[dict]:
    """Periodic-report rows from an EDGAR ``xbrl.idx`` file."""
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        cik, name, form, filed, _ = parts
        if form.strip() not in PERIODIC_FORMS:
            continue
        rows.append({
            "cik": cik.strip().zfill(10),
            "name": name.strip(),
            "form": form.strip(),
            "filed": filed.strip(),
        })
    return rows


def compact_listing_notices(text: str) -> list[dict]:
    """Form 25 and 25-NSE rows from an EDGAR ``form.idx`` file."""
    rows = []
    for line in text.splitlines():
        if not line.startswith(LISTING_NOTICE_FORMS):
            continue
        match = _NOTICE_LINE.match(line)
        if match is None:
            continue
        form, name, cik, filed = match.groups()
        rows.append({"cik": cik.zfill(10), "name": name.strip(), "form": form, "filed": filed})
    return rows


def compact_submissions(payload: dict) -> dict:
    """Keep a company's identity, last periodic report, and exit filings."""
    recent = ((payload.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or [""] * len(forms)
    filings = []
    last_periodic = None
    for form, filed, item_text in zip(forms, dates, items):
        form = str(form).strip()
        if form in PERIODIC_FORMS:
            if last_periodic is None or filed > last_periodic:
                last_periodic = filed
            continue
        codes = sorted({code.strip() for code in str(item_text or "").split(",") if code.strip()})
        if form in EXIT_FORMS:
            filings.append({"form": form, "filed": filed})
        elif form in {"8-K", "8-K/A"} and EXIT_ITEMS.intersection(codes):
            filings.append({
                "form": form, "filed": filed,
                "items": [code for code in codes if code in EXIT_ITEMS],
            })
    filings.sort(key=lambda row: (row["filed"], row["form"]))
    return {
        "_compact_version": COMPACT_VERSION,
        "cik": str(payload.get("cik", "")).zfill(10),
        "name": payload.get("name"),
        "sic": str(payload.get("sic") or "") or None,
        "sic_description": payload.get("sicDescription"),
        "entity_type": payload.get("entityType"),
        "tickers": list(payload.get("tickers") or []),
        "exchanges": [exchange for exchange in (payload.get("exchanges") or []) if exchange],
        "former_names": [
            entry.get("name") for entry in (payload.get("formerNames") or []) if entry.get("name")
        ],
        "last_periodic_filing": last_periodic,
        "filings_truncated": bool((payload.get("filings") or {}).get("files")),
        "exit_filings": filings,
    }


def quarters(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Inclusive list of (year, quarter) pairs."""
    out = []
    year, quarter = start
    while (year, quarter) <= end:
        out.append((year, quarter))
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return out


def _current_quarter() -> tuple[int, int]:
    now = time.gmtime()
    return now.tm_year, (now.tm_mon - 1) // 3 + 1


def _older_than(path: Path, hours: float, clock: Callable[[], float]) -> bool:
    try:
        return clock() - path.stat().st_mtime > hours * 3600.0
    except OSError:
        return True


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    with urlopen(Request(url, headers=headers), timeout=60) as response:
        return response.read()


def _read_gzip_json(path: Path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def _write_gzip_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    os.close(descriptor)
    try:
        with gzip.open(name, "wt", encoding="utf-8") as file:
            json.dump(value, file, separators=(",", ":"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
