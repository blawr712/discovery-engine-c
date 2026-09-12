"""Source-backed evidence collection for deterministic research packets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Callable, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.config import (
    EVIDENCE_ALLOWED_DOMAINS,
    EVIDENCE_CACHE_DIR,
    EVIDENCE_MAX_AGE_DAYS,
    EVIDENCE_MAX_DOCUMENTS,
    EVIDENCE_MAX_EXCERPT_CHARS,
    EVIDENCE_SEC_FORMS,
    EVIDENCE_TTL_HOURS,
)


QUALITY_PRIORITY = {
    "regulatory_filing": 100,
    "company_ir": 80,
    "earnings_release": 80,
    "reputable_news": 60,
}


@dataclass
class EvidenceCacheStats:
    """Operational counters for evidence-cache decisions."""

    hits: int = 0
    misses: int = 0
    expired: int = 0
    read_errors: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceDocument:
    """Auditable metadata for one source document."""

    ticker: str
    url: str
    title: str
    publisher: str
    published_at: str | None
    retrieved_at: str
    source_type: str
    content_hash: str
    quality_priority: int
    excerpt: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class EvidenceProvider(Protocol):
    name: str

    def collect(self, packet: dict) -> list[EvidenceDocument]: ...


class HttpCache:
    """Persistent raw-response cache with a configurable freshness window."""

    def __init__(
        self,
        directory: Path = EVIDENCE_CACHE_DIR,
        ttl_hours: float = EVIDENCE_TTL_HOURS,
        clock: Callable[[], datetime] | None = None,
        fetcher: Callable[[str, dict[str, str]], bytes] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.ttl = timedelta(hours=ttl_hours)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fetcher = fetcher or _fetch
        self.stats = EvidenceCacheStats()

    def get(self, url: str, headers: dict[str, str]) -> tuple[bytes, bool]:
        path = self.directory / f"{hashlib.sha256(url.encode()).hexdigest()}.json"
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                stored = _parse_time(value["retrieved_at"])
                if self.clock() - stored <= self.ttl:
                    self.stats.hits += 1
                    return bytes.fromhex(value["content_hex"]), True
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.stats.read_errors += 1
            else:
                self.stats.expired += 1
        else:
            self.stats.misses += 1
        content = self.fetcher(url, headers)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, {
            "url": url,
            "retrieved_at": self.clock().isoformat(),
            "content_hex": content.hex(),
        })
        return content, False


class SecEdgarProvider:
    """Collect recent primary filings from the public SEC submissions API."""

    name = "sec_edgar"
    ticker_map_url = "https://www.sec.gov/files/company_tickers.json"

    def __init__(
        self,
        user_agent: str,
        cache: HttpCache | None = None,
        forms: tuple[str, ...] = EVIDENCE_SEC_FORMS,
        max_documents: int = EVIDENCE_MAX_DOCUMENTS,
        max_age_days: int = EVIDENCE_MAX_AGE_DAYS,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must identify an organization and contact email.")
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
        self.cache = cache or HttpCache()
        self.forms = set(forms)
        self.max_documents = max_documents
        self.max_age_days = max_age_days
        self._ticker_mapping: dict | None = None

    def collect(self, packet: dict) -> list[EvidenceDocument]:
        if str(packet.get("country", "")).upper() != "US":
            return []
        ticker = str(packet.get("ticker", "")).upper()
        if self._ticker_mapping is None:
            self._ticker_mapping = self._json(self.ticker_map_url)
        mapping = self._ticker_mapping
        match = next(
            (row for row in mapping.values() if str(row.get("ticker", "")).upper() == ticker),
            None,
        )
        if not match:
            raise LookupError(f"No SEC CIK mapping for {ticker}.")
        cik = str(match["cik_str"]).zfill(10)
        submission = self._json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        recent = submission.get("filings", {}).get("recent", {})
        keys = ("accessionNumber", "filingDate", "form", "primaryDocument", "primaryDocDescription")
        rows = [dict(zip(keys, values)) for values in zip(*(recent.get(key, []) for key in keys))]
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=self.max_age_days)
        documents = []
        for row in rows:
            if row["form"] not in self.forms or _parse_date(row["filingDate"]) < cutoff:
                continue
            accession = row["accessionNumber"].replace("-", "")
            url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{accession}/{row['primaryDocument']}"
            )
            raw, _ = self.cache.get(url, self.headers)
            retrieved = datetime.now(timezone.utc).isoformat()
            documents.append(EvidenceDocument(
                ticker=ticker,
                url=url,
                title=row["primaryDocDescription"] or f"Form {row['form']}",
                publisher="U.S. Securities and Exchange Commission",
                published_at=row["filingDate"],
                retrieved_at=retrieved,
                source_type="regulatory_filing",
                content_hash=hashlib.sha256(raw).hexdigest(),
                quality_priority=QUALITY_PRIORITY["regulatory_filing"],
                excerpt=_html_excerpt(raw, EVIDENCE_MAX_EXCERPT_CHARS),
            ))
            if len(documents) >= self.max_documents:
                break
        return documents

    def _json(self, url: str) -> dict:
        raw, _ = self.cache.get(url, self.headers)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object from {url}.")
        return value


class ManifestEvidenceProvider:
    """Load curated evidence metadata from a local, reviewable JSON manifest."""

    name = "curated_manifest"

    def __init__(self, path: Path) -> None:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Source manifest must be an object keyed by ticker.")
        self.sources = value

    def collect(self, packet: dict) -> list[EvidenceDocument]:
        ticker = str(packet.get("ticker", "")).upper()
        documents = []
        for row in self.sources.get(ticker, []):
            source_type = str(row.get("source_type", ""))
            url = str(row.get("url", ""))
            validate_source_url(url)
            if source_type not in QUALITY_PRIORITY:
                raise ValueError(f"Unsupported source_type for {ticker}: {source_type}")
            content_hash = str(row.get("content_hash") or "")
            if len(content_hash) != 64:
                raise ValueError(f"A SHA-256 content_hash is required for {ticker}.")
            documents.append(EvidenceDocument(
                ticker=ticker,
                url=url,
                title=str(row.get("title") or ""),
                publisher=str(row.get("publisher") or ""),
                published_at=row.get("published_at"),
                retrieved_at=str(row.get("retrieved_at") or datetime.now(timezone.utc).isoformat()),
                source_type=source_type,
                content_hash=content_hash,
                quality_priority=QUALITY_PRIORITY[source_type],
                excerpt=str(row.get("excerpt") or "")[:EVIDENCE_MAX_EXCERPT_CHARS],
            ))
        return documents


class CanadianIssuerManifestProvider:
    """Fetch explicitly curated Canadian issuer-primary source URLs."""

    name = "canadian_issuer_manifest"

    def __init__(
        self,
        path: Path,
        user_agent: str,
        cache: HttpCache | None = None,
        max_documents: int = EVIDENCE_MAX_DOCUMENTS,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SOURCE_USER_AGENT must include a contact email.")
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("Canadian source manifest must use version 1.")
        companies = value.get("companies")
        domains = value.get("allowed_domains")
        if not isinstance(companies, dict) or not isinstance(domains, list) or not domains:
            raise ValueError("Canadian source manifest requires companies and allowed_domains.")
        self.sources = companies
        self.allowed_domains = tuple(_validated_domain(value) for value in domains)
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
        self.cache = cache or HttpCache()
        self.max_documents = max_documents

    def collect(self, packet: dict) -> list[EvidenceDocument]:
        if str(packet.get("country", "")).upper() != "CA":
            return []
        ticker = str(packet.get("ticker", "")).upper()
        rows = self.sources.get(ticker, [])
        if not isinstance(rows, list):
            raise ValueError(f"Canadian manifest entry must be a list: {ticker}")
        documents = []
        for row in rows[:self.max_documents]:
            if not isinstance(row, dict):
                raise ValueError(f"Canadian manifest source must be an object: {ticker}")
            url = str(row.get("url", ""))
            validate_source_url(url, self.allowed_domains)
            source_type = str(row.get("source_type", ""))
            if source_type not in {"company_ir", "earnings_release"}:
                raise ValueError(
                    f"Canadian issuer source_type must be company_ir or earnings_release: {ticker}"
                )
            if str(row.get("format") or "html").lower() != "html":
                raise ValueError(f"Canadian issuer source format must be html: {ticker}")
            raw, _ = self.cache.get(url, self.headers)
            content_hash = hashlib.sha256(raw).hexdigest()
            expected_hash = str(row.get("expected_content_hash") or "")
            if expected_hash and expected_hash != content_hash:
                raise ValueError(f"Canadian source hash changed for {ticker}: {url}")
            documents.append(EvidenceDocument(
                ticker=ticker,
                url=url,
                title=str(row.get("title") or ""),
                publisher=str(row.get("publisher") or ""),
                published_at=row.get("published_at"),
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                source_type=source_type,
                content_hash=content_hash,
                quality_priority=QUALITY_PRIORITY[source_type],
                excerpt=_html_excerpt(raw, EVIDENCE_MAX_EXCERPT_CHARS),
            ))
        return documents


def collect_evidence(packets: list[dict], providers: list[EvidenceProvider]) -> dict:
    """Collect, validate, deduplicate, and isolate failures by company/provider."""
    companies = []
    for packet in packets:
        ticker = str(packet.get("ticker", ""))
        documents: list[
            tuple[EvidenceDocument, tuple[str, ...] | None, str]
        ] = []
        failures = []
        for provider in providers:
            try:
                documents.extend(
                    (
                        document,
                        getattr(provider, "allowed_domains", None),
                        provider.name,
                    )
                    for document in provider.collect(packet)
                )
            except Exception as error:
                failures.append({"provider": provider.name, "error": f"{type(error).__name__}: {error}"})
        unique = {}
        invalid = []
        for document, allowed_domains, provider_name in documents:
            errors = validate_document(document, allowed_domains=allowed_domains)
            if errors:
                invalid.append({"url": document.url, "errors": errors})
                continue
            key = (document.url.rstrip("/"), document.content_hash)
            unique[key] = (document, provider_name)
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                item[0].quality_priority,
                item[0].published_at or "",
            ),
            reverse=True,
        )
        provider_counts = {}
        for _, provider_name in ordered:
            provider_counts[provider_name] = provider_counts.get(provider_name, 0) + 1
        companies.append({
            "ticker": ticker,
            "status": "complete" if ordered else "no_evidence",
            "documents": [item.as_dict() for item, _ in ordered],
            "provider_document_counts": dict(sorted(provider_counts.items())),
            "failures": failures,
            "invalid_documents": invalid,
        })
    cache_stats = _aggregate_cache_stats(providers)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_categories": QUALITY_PRIORITY,
        "companies": companies,
        "document_count": sum(len(row["documents"]) for row in companies),
        "failure_count": sum(len(row["failures"]) for row in companies),
        "cache": cache_stats,
    }


def _aggregate_cache_stats(providers: list[EvidenceProvider]) -> dict:
    totals = EvidenceCacheStats()
    seen = set()
    for provider in providers:
        cache = getattr(provider, "cache", None)
        if cache is None or id(cache) in seen:
            continue
        seen.add(id(cache))
        stats = getattr(cache, "stats", None)
        if stats is None:
            continue
        totals.hits += stats.hits
        totals.misses += stats.misses
        totals.expired += stats.expired
        totals.read_errors += stats.read_errors
    return totals.as_dict()


def attach_evidence(packets: list[dict], evidence: dict) -> None:
    lookup = {row["ticker"]: row for row in evidence["companies"]}
    for packet in packets:
        company = lookup.get(str(packet.get("ticker")), {})
        packet["evidence_documents"] = company.get("documents", [])
        packet["evidence_failures"] = company.get("failures", [])
        packet["evidence_providers"] = company.get("provider_document_counts", {})
        packet["source_policy"]["external_sources_attached"] = bool(company.get("documents"))
        claim_classes = packet.setdefault("claim_classes", {})
        claim_classes["sourced"] = [
            {"url": row["url"], "content_hash": row["content_hash"]}
            for row in company.get("documents", [])
        ]


def export_evidence(evidence: dict, run_id: str, output_directory: Path) -> str:
    path = Path(output_directory) / f"research_evidence_{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, {"run_id": run_id, **evidence})
    return str(path)


def validate_source_url(
    url: str,
    allowed_domains: tuple[str, ...] | None = None,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Evidence URLs must use HTTPS.")
    host = parsed.hostname.lower()
    domains = allowed_domains or EVIDENCE_ALLOWED_DOMAINS
    if not any(host == domain or host.endswith(f".{domain}") for domain in domains):
        raise ValueError(f"Evidence domain is not allowed: {host}")


def validate_document(
    document: EvidenceDocument,
    allowed_domains: tuple[str, ...] | None = None,
) -> list[str]:
    errors = []
    try:
        validate_source_url(document.url, allowed_domains)
    except ValueError as error:
        errors.append(str(error))
    if not document.title.strip():
        errors.append("title is required")
    if not document.publisher.strip():
        errors.append("publisher is required")
    if document.source_type not in QUALITY_PRIORITY:
        errors.append("source_type is unsupported")
    if len(document.content_hash) != 64:
        errors.append("content_hash must be SHA-256")
    if not document.published_at:
        errors.append("published_at is required")
    else:
        try:
            age = datetime.now(timezone.utc).date() - _parse_date(document.published_at)
            if age.days > EVIDENCE_MAX_AGE_DAYS:
                errors.append("source is stale")
        except ValueError:
            errors.append("published_at must be YYYY-MM-DD")
    try:
        _parse_time(document.retrieved_at)
    except ValueError:
        errors.append("retrieved_at must be ISO-8601")
    return errors


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        content = response.read()
    time.sleep(0.12)
    return content


def _validated_domain(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid allowed evidence domain: {value}")
    domain = str(value).strip().lower().rstrip(".")
    if not domain or ":" in domain or "/" in domain or domain == "localhost":
        raise ValueError(f"Invalid allowed evidence domain: {value}")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError(f"IP addresses cannot be evidence domains: {value}")
    return domain


def _html_excerpt(raw: bytes, maximum: int) -> str:
    parser = _TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    text = " ".join(" ".join(parser.parts).split())
    return text[:maximum]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self.suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.suppressed:
            self.suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _atomic_json(path: Path, data: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
