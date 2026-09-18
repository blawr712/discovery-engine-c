"""SEC insider-transaction data sets (Forms 3, 4, and 5) as a compact source.

The SEC publishes one zip per calendar quarter containing every insider
filing as relational TSV tables. This source downloads a quarter once,
keeps only open-market purchases and sales with the fields Discovery Engine
needs (issuer CIK, filing date, transaction date, code, shares, price, and
the reporting owners' relationships), and caches that compact extract.
Completed quarters never change, so extracts are kept indefinitely; the
absence of a not-yet-published quarter is remembered for a short while so
runs do not probe the SEC repeatedly.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
import gzip
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile


DEFAULT_BASE_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{quarter}_form345.zip"
)
EXTRACT_VERSION = "sec-insiders-1"
OPEN_MARKET_CODES = {"P": "purchase", "S": "sale"}
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class InsiderSourceStats:
    """Track quarter cache behavior during one collection."""

    quarters_loaded: int = 0
    quarters_downloaded: int = 0
    quarters_unavailable: int = 0
    transactions: int = 0


class SecInsiderTransactionsSource:
    """Download, compact, and cache SEC insider-transaction quarters."""

    def __init__(
        self,
        user_agent: str,
        cache_directory: Path,
        *,
        base_url: str = DEFAULT_BASE_URL,
        request_interval_seconds: float = 0.11,
        unavailable_ttl_hours: float = 168.0,
        fetcher: Callable[[str, dict[str, str]], bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC_USER_AGENT must identify an organization and contact email."
            )
        if request_interval_seconds < 0 or unavailable_ttl_hours < 0:
            raise ValueError("SEC insider pacing and TTL values cannot be negative.")
        self.user_agent = user_agent
        self.cache_directory = Path(cache_directory)
        self.base_url = base_url
        self.request_interval_seconds = request_interval_seconds
        self.unavailable_ttl_seconds = unavailable_ttl_hours * 3600.0
        self.fetcher = fetcher or _fetch
        self.clock = clock
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
        self.stats = InsiderSourceStats()
        self._lock = threading.Lock()
        self._last_request = 0.0

    def load_quarters(
        self,
        start: str,
        end: str | None = None,
    ) -> tuple[list[dict], list[str], list[str]]:
        """Load every available quarter from ``start`` (e.g. ``2015q1``).

        Returns the concatenated transactions, the quarters loaded, and the
        quarters that were unavailable at the SEC.
        """
        transactions: list[dict] = []
        loaded: list[str] = []
        unavailable: list[str] = []
        for quarter in quarter_labels(start, end or current_quarter(self.clock)):
            rows = self.load_quarter(quarter)
            if rows is None:
                unavailable.append(quarter)
                continue
            loaded.append(quarter)
            transactions.extend(rows)
        self.stats.transactions = len(transactions)
        return transactions, loaded, unavailable

    def load_quarter(self, quarter: str) -> list[dict] | None:
        """Return one quarter's compact transactions, or None if unpublished."""
        quarter = _validated_quarter(quarter)
        path = self.cache_directory / f"{EXTRACT_VERSION}-{quarter}.json.gz"
        cached = _read_gzip_json(path)
        if cached is not None:
            self.stats.quarters_loaded += 1
            return cached
        marker = self.cache_directory / f"{EXTRACT_VERSION}-{quarter}.unavailable"
        if self._recently_unavailable(marker):
            self.stats.quarters_unavailable += 1
            return None
        url = self.base_url.format(quarter=quarter)
        try:
            raw = self._request(url)
        except HTTPError as error:
            if error.code == 404:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(repr(float(self.clock())), encoding="utf-8")
                self.stats.quarters_unavailable += 1
                return None
            raise
        rows = compact_insider_quarter(raw)
        _write_gzip_json(path, rows)
        marker.unlink(missing_ok=True)
        self.stats.quarters_downloaded += 1
        self.stats.quarters_loaded += 1
        return rows

    def _recently_unavailable(self, marker: Path) -> bool:
        if not marker.is_file():
            return False
        try:
            recorded = float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        return self.clock() - recorded <= self.unavailable_ttl_seconds

    def _request(self, url: str) -> bytes:
        with self._lock:
            wait = self.request_interval_seconds - (self.clock() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = self.clock()
        return self.fetcher(url, self.headers)


def compact_insider_quarter(raw_zip: bytes) -> list[dict]:
    """Reduce one quarterly zip to open-market transactions with owners."""
    archive = zipfile.ZipFile(io.BytesIO(raw_zip))
    submissions = {
        row["ACCESSION_NUMBER"]: row
        for row in _read_table(archive, "SUBMISSION.tsv")
    }
    owners: dict[str, list[dict]] = {}
    for row in _read_table(archive, "REPORTINGOWNER.tsv"):
        owners.setdefault(row["ACCESSION_NUMBER"], []).append({
            "cik": row.get("RPTOWNERCIK", "").strip(),
            "relationship": row.get("RPTOWNER_RELATIONSHIP", "").strip(),
        })
    transactions = []
    for row in _read_table(archive, "NONDERIV_TRANS.tsv"):
        code = row.get("TRANS_CODE", "").strip().upper()
        if code not in OPEN_MARKET_CODES:
            continue
        accession = row["ACCESSION_NUMBER"]
        submission = submissions.get(accession)
        if submission is None:
            continue
        filed = _parse_sec_date(submission.get("FILING_DATE"))
        trans_date = _parse_sec_date(row.get("TRANS_DATE"))
        shares = _number(row.get("TRANS_SHARES"))
        price = _number(row.get("TRANS_PRICEPERSHARE"))
        if filed is None or shares is None or shares <= 0:
            continue
        owner_rows = owners.get(accession, [])
        relationships = {
            item["relationship"].lower() for item in owner_rows if item["relationship"]
        }
        transactions.append({
            "accession": accession,
            "issuer_cik": submission.get("ISSUERCIK", "").strip().zfill(10),
            "symbol": submission.get("ISSUERTRADINGSYMBOL", "").strip().upper(),
            "filed": filed.isoformat(),
            "trans_date": trans_date.isoformat() if trans_date else None,
            "code": code,
            "kind": OPEN_MARKET_CODES[code],
            "shares": shares,
            "price": price,
            "value": round(shares * price, 2) if price is not None else None,
            "acquired": (
                row.get("TRANS_ACQUIRED_DISP_CD", "").strip().upper() == "A"
            ),
            "owner_ciks": sorted({item["cik"] for item in owner_rows if item["cik"]}),
            "officer_or_director": any(
                token in relationships for token in ("officer", "director")
            ),
            "ten_percent_owner": "tenpercentowner" in relationships,
        })
    transactions.sort(key=lambda item: (item["filed"], item["accession"]))
    return transactions


def quarter_labels(start: str, end: str) -> list[str]:
    """Return inclusive quarter labels from ``start`` to ``end``."""
    start_year, start_quarter = _split_quarter(_validated_quarter(start))
    end_year, end_quarter = _split_quarter(_validated_quarter(end))
    labels = []
    year, quarter = start_year, start_quarter
    while (year, quarter) <= (end_year, end_quarter):
        labels.append(f"{year}q{quarter}")
        quarter += 1
        if quarter > 4:
            quarter = 1
            year += 1
    return labels


def current_quarter(clock: Callable[[], float] = time.time) -> str:
    today = datetime.fromtimestamp(clock()).date()
    return f"{today.year}q{(today.month - 1) // 3 + 1}"


def _validated_quarter(value: str) -> str:
    text = str(value).strip().lower()
    year, quarter = _split_quarter(text)
    if not 2000 <= year <= 2100 or not 1 <= quarter <= 4:
        raise ValueError(f"Invalid quarter label: {value!r}")
    return f"{year}q{quarter}"


def _split_quarter(text: str) -> tuple[int, int]:
    try:
        year_text, quarter_text = text.lower().split("q")
        return int(year_text), int(quarter_text)
    except ValueError as error:
        raise ValueError(f"Invalid quarter label: {text!r}") from error


def _read_table(archive: zipfile.ZipFile, name: str) -> list[dict]:
    try:
        text = archive.read(name).decode("utf-8", errors="replace")
    except KeyError:
        return []
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [row for row in reader if row.get("ACCESSION_NUMBER")]


def _parse_sec_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parts = text.split("-")
    if len(parts) == 3 and parts[1].upper() in MONTHS:
        try:
            return date(int(parts[2]), MONTHS[parts[1].upper()], int(parts[0]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if number == number else None


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=300) as response:
        return response.read()


def _read_gzip_json(path: Path) -> list[dict] | None:
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError, EOFError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != EXTRACT_VERSION:
        return None
    rows = payload.get("transactions")
    return rows if isinstance(rows, list) else None


def _write_gzip_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as file:
            json.dump({"version": EXTRACT_VERSION, "transactions": rows}, file)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
