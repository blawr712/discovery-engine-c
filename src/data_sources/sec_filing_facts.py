"""Facts read from a filing's own XBRL when SEC company facts skipped it.

From 2026 the SEC company-facts API has left out the financial statements of
many filings that do carry XBRL: 141 of 641 20-Fs filed in 2026, 405 of 495
XBRL 6-Ks and about a hundred 10-Qs (Visa's July report among them), against
one or two percent a year before. Often only the cover-page share count
comes through. The filing itself is complete, so this source finds filings
in EDGAR's quarterly XBRL index that left no financial fact in a company's
extract, reads the XBRL instance filed with each one, and returns rows in
the same shape company facts gives.

Only facts without dimensions are read, as company facts does. A filing's
parsed facts never change, so they are cached for good; the current
quarter's index is refreshed after a day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import gzip
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.data_sources.sec_xbrl_source import MONEY_UNITS, _is_currency


INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/xbrl.gz"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/index.json"
FILING_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{name}"
# Bumped when the parsed shape or the concepts read change.
FILING_FACTS_VERSION = "filing-facts-1"
# Original reports only: amendments are often a cover page and Part III.
SUPPLEMENT_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F", "6-K"})
# The cover-page share count is the one fact that does come through from
# a skipped filing, so it does not show the filing was read.
COVER_CONCEPTS = frozenset({"shares_outstanding"})

_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_INSTANCE_SKIP = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_ref.xml")


@dataclass
class FilingFactsStats:
    requests: int = 0
    filings_read: int = 0
    filings_cached: int = 0
    filings_failed: int = 0
    rows_added: int = 0


@dataclass
class SecFilingFactsSource:
    """Paced, cached reading of XBRL instances that company facts skipped."""

    user_agent: str
    cache_directory: Path
    since: date = date(2025, 1, 1)
    index_ttl_hours: float = 24.0
    fetcher: Callable[[str, dict[str, str]], bytes] | None = None
    clock: Callable[[], float] = time.time
    today: Callable[[], date] | None = None
    pacer: object | None = None
    stats: FilingFactsStats = field(default_factory=FilingFactsStats)

    def __post_init__(self) -> None:
        if not self.user_agent or "@" not in self.user_agent:
            raise ValueError("SEC_USER_AGENT must identify an organization and contact email.")
        self.cache_directory = Path(self.cache_directory)
        self.headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip"}
        self.fetcher = self.fetcher or _fetch
        self.today = self.today or date.today
        self._index_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._filings_by_cik: dict[str, list[dict]] | None = None

    def supplement(self, extract: dict, concepts: dict) -> dict:
        """The extract with facts added from every skipped filing.

        A filing counts as skipped when nothing but its cover-page share
        count reached the extract. A filing that cannot be fetched or parsed
        is left out and tried again the next time the extract is built.
        """
        cik = str(extract.get("cik") or "").lstrip("0")
        if not cik:
            return extract
        reported = reported_filing_dates(extract)
        added = []
        for filing in self.filings_for(cik):
            if filing["filed"] in reported:
                continue
            facts = self.filing_facts(filing, concepts)
            if facts is None:
                continue
            count = merge_facts(extract, facts)
            added.append({
                "accession": filing["accession"], "form": filing["form"],
                "filed": filing["filed"], "rows": count,
            })
            with self._stats_lock:
                self.stats.rows_added += count
        extract["filing_supplement"] = added
        return extract

    def filings_for(self, cik: str) -> list[dict]:
        """Original XBRL reports a company filed since ``since``."""
        if self._filings_by_cik is None:
            with self._index_lock:
                if self._filings_by_cik is None:
                    self._filings_by_cik = self._load_index()
        return self._filings_by_cik.get(str(cik).lstrip("0"), [])

    def filing_facts(self, filing: dict, concepts: dict) -> dict[str, list[dict]] | None:
        """One filing's facts by concept, from cache or its XBRL instance."""
        path = self.cache_directory / "filings" / f"{filing['accession']}.json.gz"
        cached = _read_gzip_json(path)
        if isinstance(cached, dict) and cached.get("_version") == FILING_FACTS_VERSION:
            with self._stats_lock:
                self.stats.filings_cached += 1
            return cached["facts"]
        try:
            folder = filing["accession"].replace("-", "")
            listing = json.loads(self._request(
                FILING_INDEX_URL.format(cik=filing["cik"], folder=folder)
            ).decode("utf-8"))
            name = instance_name([
                item.get("name", "") for item in listing.get("directory", {}).get("item", [])
            ])
            if name is None:
                facts: dict[str, list[dict]] = {}
            else:
                document = self._request(
                    FILING_FILE_URL.format(cik=filing["cik"], folder=folder, name=name)
                )
                facts = parse_instance(document, concepts, filing["form"], filing["filed"])
        except Exception:  # noqa: BLE001 - one bad filing must not stop a run
            with self._stats_lock:
                self.stats.filings_failed += 1
            return None
        _write_gzip_json(path, {
            "_version": FILING_FACTS_VERSION, "instance": name, "facts": facts,
        })
        with self._stats_lock:
            self.stats.filings_read += 1
        return facts

    def _load_index(self) -> dict[str, list[dict]]:
        today = self.today()
        current = (today.year, (today.month - 1) // 3 + 1)
        by_cik: dict[str, list[dict]] = {}
        seen: set[str] = set()
        year, quarter = self.since.year, (self.since.month - 1) // 3 + 1
        while (year, quarter) <= current:
            for row in self._index_quarter(year, quarter, (year, quarter) == current):
                if row["filed"] >= self.since.isoformat() and row["accession"] not in seen:
                    seen.add(row["accession"])
                    by_cik.setdefault(row["cik"], []).append(row)
            year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
        for rows in by_cik.values():
            rows.sort(key=lambda row: (row["filed"], row["accession"]))
        return by_cik

    def _index_quarter(self, year: int, quarter: int, is_current: bool) -> list[dict]:
        path = self.cache_directory / "index" / f"{FILING_FACTS_VERSION}-{year}q{quarter}.json.gz"
        rows = _read_gzip_json(path)
        fresh = rows is not None and not (
            is_current and self.clock() - path.stat().st_mtime > self.index_ttl_hours * 3600
        )
        if fresh:
            return rows
        try:
            raw = self._request(INDEX_URL.format(year=year, quarter=quarter))
        except Exception:  # noqa: BLE001 - an old copy is better than none
            return rows or []
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
        rows = compact_xbrl_index(raw.decode("latin-1"))
        _write_gzip_json(path, rows)
        return rows

    def _request(self, url: str) -> bytes:
        if self.pacer is not None:
            self.pacer.wait()
        with self._stats_lock:
            self.stats.requests += 1
        return self.fetcher(url, self.headers)


def compact_xbrl_index(text: str) -> list[dict]:
    """Original reports from an EDGAR ``xbrl.idx`` file, with accessions."""
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        cik, _, form, filed, filename = (part.strip() for part in parts)
        if form not in SUPPLEMENT_FORMS:
            continue
        accession = filename.rsplit("/", 1)[-1].removesuffix(".txt")
        rows.append({"cik": cik.lstrip("0"), "form": form, "filed": filed, "accession": accession})
    return rows


def reported_filing_dates(extract: dict) -> set[str]:
    """Filing dates that left at least one financial fact in the extract."""
    dates = set()
    for concept, rows in (extract.get("facts") or {}).items():
        if concept in COVER_CONCEPTS:
            continue
        for row in rows:
            dates.add(row.get("filed"))
    return dates


def instance_name(names: list[str]) -> str | None:
    """The XBRL instance among a filing's documents, if it has one."""
    for name in names:
        if name.endswith("_htm.xml"):
            return name
    for name in names:
        lower = name.lower()
        if (
            lower.endswith(".xml")
            and lower != "filingsummary.xml"
            and not lower.endswith(_INSTANCE_SKIP)
        ):
            return name
    return None


def parse_instance(document: bytes, concepts: dict, form: str, filed: str) -> dict[str, list[dict]]:
    """Rows by concept from an XBRL instance, shaped like company facts.

    Facts in contexts with dimensions are skipped, as company facts skips
    them, so a segment's revenue never stands in for the company's.
    """
    root = ET.fromstring(document)
    prefixes = _taxonomy_prefixes(document)
    contexts = {}
    for context in root.iter(_XBRLI + "context"):
        if context.find(f"{_XBRLI}entity/{_XBRLI}segment") is not None:
            continue
        if context.find(_XBRLI + "scenario") is not None:
            continue
        period = context.find(_XBRLI + "period")
        if period is None:
            continue
        instant = period.findtext(_XBRLI + "instant")
        start = period.findtext(_XBRLI + "startDate")
        end = period.findtext(_XBRLI + "endDate")
        if instant:
            contexts[context.get("id")] = (None, instant.strip()[:10])
        elif start and end:
            contexts[context.get("id")] = (start.strip()[:10], end.strip()[:10])
    units = {}
    for unit in root.iter(_XBRLI + "unit"):
        measures = list(unit.iter(_XBRLI + "measure"))
        if len(measures) == 1 and measures[0].text:
            units[unit.get("id")] = measures[0].text.strip().rsplit(":", 1)[-1]
    wanted: dict[str, list[tuple[str, dict]]] = {}
    for concept, spec in concepts.items():
        for tag in spec["tags"]:
            wanted.setdefault(tag, []).append((concept, spec))
    facts: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    for element in root:
        if not isinstance(element.tag, str) or "}" not in element.tag:
            continue
        namespace, local = element.tag[1:].split("}", 1)
        prefix = prefixes.get(namespace)
        if prefix is None:
            continue
        tag = f"{prefix}:{local}"
        if tag not in wanted or element.get(_XSI_NIL) == "true":
            continue
        period = contexts.get(element.get("contextRef"))
        unit = units.get(element.get("unitRef"))
        if period is None or unit is None:
            continue
        try:
            value = float((element.text or "").strip())
        except ValueError:
            continue
        start, end = period
        for concept, spec in wanted[tag]:
            if (spec["kind"] == "duration") != (start is not None):
                continue
            if not _unit_matches(unit, spec):
                continue
            key = (concept, tag, start, end, value, unit)
            if key in seen:
                continue
            seen.add(key)
            facts.setdefault(concept, []).append({
                "start": start, "end": end, "filed": filed, "form": form,
                "fy": None, "fp": None, "val": value, "tag": tag, "unit": unit,
            })
    return facts


def merge_facts(extract: dict, facts: dict[str, list[dict]]) -> int:
    """Add rows to the extract's facts, skipping exact duplicates."""
    added = 0
    store = extract.setdefault("facts", {})
    found = extract.setdefault("tags_found", {})
    for concept, rows in facts.items():
        existing = store.setdefault(concept, [])
        keys = {
            (row.get("start"), row["end"], row["filed"], row["val"], row.get("unit"), row["tag"])
            for row in existing
        }
        for row in rows:
            key = (row.get("start"), row["end"], row["filed"], row["val"], row.get("unit"), row["tag"])
            if key in keys:
                continue
            keys.add(key)
            existing.append(dict(row))
            added += 1
            tags = found.setdefault(concept, [])
            if row["tag"] not in tags:
                tags.append(row["tag"])
        existing.sort(key=lambda row: (row["end"], row["filed"], row.get("start") or ""))
    return added


def _taxonomy_prefixes(document: bytes) -> dict[str, str]:
    """Namespace URI to the prefix company facts uses, for the three taxonomies read."""
    prefixes = {}
    for _, (_, uri) in ET.iterparse(io.BytesIO(document), events=("start-ns",)):
        if "fasb.org/us-gaap/" in uri:
            prefixes[uri] = "us-gaap"
        elif "xbrl.ifrs.org/taxonomy/" in uri and uri.rstrip("/").endswith("ifrs-full"):
            prefixes[uri] = "ifrs-full"
        elif "xbrl.sec.gov/dei/" in uri:
            prefixes[uri] = "dei"
    return prefixes


def _unit_matches(unit: str, spec: dict) -> bool:
    wanted = spec.get("units", [spec.get("unit")])
    if wanted == MONEY_UNITS:
        return _is_currency(unit)
    return unit in wanted


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    with urlopen(Request(url, headers=headers), timeout=120) as response:
        body = response.read()
        if str(response.headers.get("Content-Encoding", "")).lower() == "gzip":
            body = gzip.decompress(body)
        return body


def _read_gzip_json(path: Path):
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def _write_gzip_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as file:
            json.dump(value, file)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
