"""Compact, cached access to SEC XBRL company facts.

The SEC company-facts API returns every reported fact for a filer, which can
run to several megabytes per company. This source keeps only the concepts
Discovery Engine derives signals from, stores that compact extract on disk
with a freshness window, and paces requests below the SEC's published limit.
Every retained fact keeps its ``filed`` date so downstream code can reason
point-in-time about what was knowable on a given day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen


TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
EXTRACT_VERSION = "sec-facts-1"

# Ordered aliases per concept: the first taxonomy tag with usable data wins
# for each period, but every alias is retained so coverage can be measured.
CONCEPTS: dict[str, dict] = {
    "revenue": {
        "unit": "USD",
        "kind": "duration",
        "tags": [
            "us-gaap:Revenues",
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
            "us-gaap:SalesRevenueNet",
            "us-gaap:SalesRevenueGoodsNet",
        ],
    },
    "gross_profit": {
        "unit": "USD", "kind": "duration", "tags": ["us-gaap:GrossProfit"],
    },
    "operating_income": {
        "unit": "USD", "kind": "duration", "tags": ["us-gaap:OperatingIncomeLoss"],
    },
    "net_income": {
        "unit": "USD", "kind": "duration", "tags": ["us-gaap:NetIncomeLoss"],
    },
    "operating_cash_flow": {
        "unit": "USD",
        "kind": "duration",
        "tags": ["us-gaap:NetCashProvidedByUsedInOperatingActivities"],
    },
    "capital_expenditure": {
        "unit": "USD",
        "kind": "duration",
        "tags": [
            "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
            "us-gaap:PaymentsToAcquireProductiveAssets",
        ],
    },
    "cash": {
        "unit": "USD",
        "kind": "instant",
        "tags": [
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ],
    },
    "long_term_debt": {
        "unit": "USD",
        "kind": "instant",
        "tags": ["us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebt"],
    },
    "short_term_debt": {
        "unit": "USD",
        "kind": "instant",
        "tags": [
            "us-gaap:LongTermDebtCurrent",
            "us-gaap:DebtCurrent",
            "us-gaap:ShortTermBorrowings",
        ],
    },
    "stockholders_equity": {
        "unit": "USD", "kind": "instant", "tags": ["us-gaap:StockholdersEquity"],
    },
    "shares_outstanding": {
        "unit": "shares",
        "kind": "instant",
        "tags": [
            "dei:EntityCommonStockSharesOutstanding",
            "us-gaap:CommonStockSharesOutstanding",
        ],
    },
}

FACT_FIELDS = ("start", "end", "filed", "form", "fy", "fp", "val")


@dataclass
class SecCacheStats:
    """Track compact-extract cache behavior during one collection."""

    hits: int = 0
    misses: int = 0
    expired: int = 0
    read_errors: int = 0
    requests: int = 0


@dataclass
class SecXbrlSource:
    """Fetch, compact, and cache SEC company facts for U.S. filers."""

    user_agent: str
    cache_directory: Path
    ttl_hours: float = 168.0
    request_interval_seconds: float = 0.11
    concepts: dict = field(default_factory=lambda: CONCEPTS)
    fetcher: Callable[[str, dict[str, str]], bytes] | None = None
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not self.user_agent or "@" not in self.user_agent:
            raise ValueError(
                "SEC_USER_AGENT must identify an organization and contact email."
            )
        if self.ttl_hours < 0:
            raise ValueError("SEC cache TTL cannot be negative.")
        if self.request_interval_seconds < 0:
            raise ValueError("SEC request interval cannot be negative.")
        self.cache_directory = Path(self.cache_directory)
        self.headers = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        self.fetcher = self.fetcher or _fetch
        self.stats = SecCacheStats()
        self._pace_lock = threading.Lock()
        self._map_lock = threading.Lock()
        self._last_request = 0.0
        self._ticker_map: dict[str, dict] | None = None

    def cik_for(self, ticker: str) -> str | None:
        """Return the zero-padded CIK for a ticker, or None when unmapped."""
        if self._ticker_map is None:
            with self._map_lock:
                if self._ticker_map is None:
                    self._ticker_map = self._load_ticker_map()
        row = self._ticker_map.get(str(ticker).strip().upper())
        return str(row["cik_str"]).zfill(10) if row else None

    def get_company_facts(self, ticker: str) -> dict:
        """Return the compact fact extract for a ticker from cache or the SEC."""
        ticker = str(ticker).strip().upper()
        path = self._cache_path(ticker)
        cached = self._read_cached(path)
        if cached is not None:
            return cached
        cik = self.cik_for(ticker)
        if cik is None:
            raise LookupError(f"No SEC CIK mapping for {ticker}.")
        raw = self._request(COMPANY_FACTS_URL.format(cik=cik))
        payload = json.loads(raw.decode("utf-8"))
        extract = compact_company_facts(payload, ticker, self.concepts)
        extract["_retrieved_at"] = self.clock()
        _atomic_json(path, extract)
        return extract

    def _load_ticker_map(self) -> dict[str, dict]:
        path = self.cache_directory / "company_tickers.json"
        cached = self._read_cached(path)
        if cached is None:
            raw = self._request(TICKER_MAP_URL)
            rows = json.loads(raw.decode("utf-8"))
            cached = {
                str(row.get("ticker", "")).upper(): {
                    "cik_str": row.get("cik_str"),
                    "title": row.get("title"),
                }
                for row in (rows.values() if isinstance(rows, dict) else rows)
                if row.get("ticker")
            }
            cached["_extract_version"] = EXTRACT_VERSION
            cached["_retrieved_at"] = self.clock()
            _atomic_json(path, cached)
        return {key: value for key, value in cached.items() if not key.startswith("_")}

    def _request(self, url: str) -> bytes:
        with self._pace_lock:
            wait = self.request_interval_seconds - (self.clock() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = self.clock()
            self.stats.requests += 1
        return self.fetcher(url, self.headers)

    def _cache_path(self, ticker: str) -> Path:
        digest = hashlib.sha256(f"{EXTRACT_VERSION}|{ticker}".encode("utf-8")).hexdigest()
        return self.cache_directory / f"{digest}.json"

    def _read_cached(self, path: Path) -> dict | None:
        if not path.is_file():
            self.stats.misses += 1
            return None
        try:
            with path.open("r", encoding="utf-8") as file:
                value = json.load(file)
        except (OSError, json.JSONDecodeError, TypeError):
            self.stats.read_errors += 1
            return None
        if not isinstance(value, dict) or value.get("_extract_version") != EXTRACT_VERSION:
            self.stats.read_errors += 1
            return None
        retrieved = value.get("_retrieved_at")
        if not isinstance(retrieved, (int, float)) or (
            self.clock() - float(retrieved) > self.ttl_hours * 3600
        ):
            self.stats.expired += 1
            return None
        self.stats.hits += 1
        return value


def compact_company_facts(
    payload: dict,
    ticker: str,
    concepts: dict | None = None,
) -> dict:
    """Reduce a company-facts response to the configured concepts.

    The result maps each concept to a list of fact dictionaries with
    ``start`` (duration facts only), ``end``, ``filed``, ``form``, ``fy``,
    ``fp``, ``val``, and the ``tag`` they came from. Facts are sorted by
    ``end`` then ``filed`` so downstream lookups are deterministic.
    """
    concepts = concepts or CONCEPTS
    facts_by_taxonomy = payload.get("facts", {}) if isinstance(payload, dict) else {}
    extract: dict[str, list[dict]] = {}
    tags_found: dict[str, list[str]] = {}
    for concept, spec in concepts.items():
        rows: list[dict] = []
        seen: set[tuple] = set()
        found = []
        for tag in spec["tags"]:
            taxonomy, _, name = tag.partition(":")
            units = (
                facts_by_taxonomy.get(taxonomy, {}).get(name, {}).get("units", {})
            )
            entries = units.get(spec["unit"], [])
            if entries:
                found.append(tag)
            for entry in entries:
                row = _fact_row(entry, spec["kind"], tag)
                if row is None:
                    continue
                key = (row.get("start"), row["end"], row["filed"], row["val"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        rows.sort(key=lambda row: (row["end"], row["filed"], row.get("start") or ""))
        extract[concept] = rows
        tags_found[concept] = found
    return {
        "_extract_version": EXTRACT_VERSION,
        "ticker": str(ticker).upper(),
        "cik": str(payload.get("cik", "")) if isinstance(payload, dict) else "",
        "entity_name": payload.get("entityName") if isinstance(payload, dict) else None,
        "tags_found": tags_found,
        "facts": extract,
    }


def _fact_row(entry: dict, kind: str, tag: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    end = entry.get("end")
    filed = entry.get("filed")
    value = entry.get("val")
    if not end or not filed or value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    start = entry.get("start")
    if kind == "duration" and not start:
        return None
    if kind == "instant" and start:
        return None
    return {
        "start": start if kind == "duration" else None,
        "end": str(end),
        "filed": str(filed),
        "form": str(entry.get("form") or ""),
        "fy": entry.get("fy"),
        "fp": entry.get("fp"),
        "val": value,
        "tag": tag,
    }


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=60) as response:
        return response.read()


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".json", dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
