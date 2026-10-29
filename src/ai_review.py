"""Prepare the inputs for an AI review of the names that pass the v3 screen.

The review itself runs as a Claude Code skill (`.claude/skills/ai-review`).
This module only gathers what that skill may read, so every review is
reproducible from files on disk:

- a packet per name: the research packet plus the figures a data check needs,
  joined from the run row (``pit_*``) and the queue row (screen fields);
- the name's SEC filings as plain text, chosen by form and dated on or before
  the run's completion time, each with its SHA-256;
- later, a peer table (``peers.json``).

No AI runs here and nothing is written to the run. Reviews never change
scores, ranks, the queue order or the dashboard order (AI_REVIEW_BRIEF.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Protocol


PREPARE_VERSION = "ai-review-prepare-1"
DEFAULT_TOP = 50
# Annual reports, the latest quarterly report, and current reports since it.
ANNUAL_FORMS = ("10-K", "10-KT", "20-F", "40-F")
QUARTERLY_FORMS = ("10-Q",)
CURRENT_FORMS = ("8-K",)
# Foreign private issuers file no 10-Q or 8-K; their interim results and
# material events arrive on 6-K, so for 20-F and 40-F filers 6-K takes 8-K's
# place.
FOREIGN_CURRENT_FORMS = ("6-K",)
FOREIGN_ANNUAL_FORMS = ("20-F", "40-F")
MAX_CURRENT_REPORTS = 10
# Exhibits worth keeping: an annual report incorporated by reference (EX-13)
# and the press release or results an 8-K or 6-K carries (EX-99).
ANNUAL_EXHIBITS = ("EX-13",)
CURRENT_EXHIBITS = ("EX-99",)
TEXT_SUFFIXES = (".htm", ".html", ".txt", ".xml")

# Figures a data check compares with the filings. All of them are on the run
# row; the screen fields are on the queue row.
RUN_ROW_FIELDS = (
    "currency",
    "latest_close",
    "market_cap",
    "pit_revenue_ttm",
    "pit_revenue_growth_ttm",
    "pit_shares_outstanding",
    "pit_shares_basis",
    "pit_share_change_1y",
    "pit_market_cap",
    "pit_market_cap_basis",
    "pit_split_factor",
    "pit_split_ambiguous",
    "pit_earnings_yield",
    "pit_sales_yield",
    "pit_fcf_yield",
    "pit_gross_margin_ttm",
    "pit_operating_margin_ttm",
    "pit_fcf_margin_ttm",
    "pit_net_income_ttm",
    "pit_cash",
    "pit_total_debt",
    "pit_debt_coverage",
    "pit_net_cash",
    "pit_net_cash_to_market_cap",
    "pit_report_age_days",
    "pit_reporting_currency",
    "pit_data_quality",
    "fundamentals_status",
)
QUEUE_ROW_FIELDS = (
    "research_rank",
    "screen_status",
    "v3_quintile",
    "score_defensive",
)


class FilingArchive(Protocol):
    """What preparation needs from EDGAR."""

    def submissions(self, cik: str) -> dict: ...

    def filing_documents(self, cik: str, filing: dict) -> list[dict]: ...

    def document(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class SecRegistrant:
    sec_ticker: str | None
    cik: str | None
    reason: str


def select_review_names(queue: list[dict], top: int) -> list[dict]:
    """The first ``top`` queue rows that pass the v3 screen, in queue order.

    The bank lane is not mixed in; it can become its own group later.
    """
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise ValueError("AI review --top must be a positive integer.")
    return [row for row in queue if row.get("screen_status") == "passes"][:top]


def review_packet(
    base_packet: dict,
    run_row: dict,
    queue_row: dict,
    *,
    run_id: str,
    anchor: datetime,
    registrant: SecRegistrant,
    sic: str | None = None,
    sic_description: str | None = None,
) -> dict:
    """The research packet plus the fields a review's data check needs."""
    packet = dict(base_packet)
    packet.update({name: run_row.get(name) for name in RUN_ROW_FIELDS})
    packet.update({name: queue_row.get(name) for name in QUEUE_ROW_FIELDS})
    revenue = _number(run_row.get("pit_revenue_ttm"))
    net_income = _number(run_row.get("pit_net_income_ttm"))
    # fundamentals_pit has no net margin; it is derived from two of its
    # outputs here, and labelled as derived.
    packet["net_margin_ttm"] = (
        round(net_income / revenue, 6)
        if revenue is not None and revenue > 0 and net_income is not None else None
    )
    packet["derived_fields"] = {"net_margin_ttm": "pit_net_income_ttm / pit_revenue_ttm"}
    packet.update({
        "run_id": run_id,
        "anchor_datetime": anchor.isoformat(),
        "anchor_date": anchor.date().isoformat(),
        "sec_ticker": registrant.sec_ticker,
        "sec_cik": registrant.cik,
        "sec_mapping": registrant.reason,
        "sic": sic,
        "sic_description": sic_description,
        "prepare_version": PREPARE_VERSION,
    })
    return packet


def submission_rows(submissions: dict) -> list[dict]:
    """The recent-filings table of an EDGAR submissions payload as rows."""
    recent = ((submissions.get("filings") or {}).get("recent")) or {}
    keys = ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "primaryDocument")
    columns = [recent.get(key) or [] for key in keys]
    length = len(columns[0])
    rows = []
    for index in range(length):
        row = {key: (column[index] if index < len(column) else None) for key, column in zip(keys, columns)}
        rows.append(row)
    return rows


def choose_filings(
    rows: list[dict],
    cutoff: datetime,
    max_current_reports: int = MAX_CURRENT_REPORTS,
) -> list[dict]:
    """Pick the filings a review reads, all made on or before ``cutoff``.

    The latest annual report, the latest 10-Q, and the current reports filed
    since the later of the two (most recent first, capped). A filing counts
    as made when EDGAR accepted it; a row without an acceptance time falls
    back to its filing date.
    """
    known = [row for row in rows if _filed_by(row, cutoff)]
    known.sort(key=lambda row: (_accepted(row), row.get("accessionNumber") or ""), reverse=True)
    annual = next((row for row in known if row.get("form") in ANNUAL_FORMS), None)
    quarterly = next((row for row in known if row.get("form") in QUARTERLY_FORMS), None)
    foreign = annual is not None and annual.get("form") in FOREIGN_ANNUAL_FORMS and quarterly is None
    current_forms = FOREIGN_CURRENT_FORMS if foreign else CURRENT_FORMS
    periodic = [row for row in (annual, quarterly) if row is not None]
    since = max((_accepted(row) for row in periodic), default=None)
    current = [
        row for row in known
        if row.get("form") in current_forms and (since is None or _accepted(row) > since)
    ][:max_current_reports]
    chosen = []
    for role, row in [("annual", annual), ("quarterly", quarterly)] + [("current", row) for row in current]:
        if row is not None:
            chosen.append({**row, "role": role})
    return chosen


def filing_text(raw: bytes, document_name: str = "") -> str:
    """Plain text of a filing document, with block structure kept as lines.

    Inline-XBRL headers (hidden in the rendered filing) are dropped, and
    non-breaking spaces become spaces, so a quote copied from the text is the
    text a reader of the filing sees.
    """
    decoded = raw.decode("utf-8", errors="replace")
    if document_name.lower().endswith(".txt") and "<html" not in decoded[:2000].lower():
        text = decoded
    else:
        parser = _BlockTextExtractor()
        parser.feed(decoded)
        parser.close()
        text = "".join(parser.parts)
    return normalize_text(text)


def normalize_text(text: str) -> str:
    """Whitespace as a reader sees it: one space between words, short gaps."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ").replace("​", "").replace("\t", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    output: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            output.append(line)
        else:
            blank += 1
            if blank == 1 and output:
                output.append("")
    return "\n".join(output).strip() + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare_ai_review(
    *,
    run_id: str,
    manifest: dict,
    results: list[dict],
    queue: list[dict],
    base_packets: dict[str, dict],
    resolver: Callable[[dict], SecRegistrant],
    archive: FilingArchive | None,
    output_root: Path,
    top: int = DEFAULT_TOP,
    max_current_reports: int = MAX_CURRENT_REPORTS,
    clock: Callable[[], datetime] | None = None,
    progress: Callable[[str], None] | None = None,
    peer_table: Callable[[dict], dict] | None = None,
) -> dict:
    """Write packets and filings for the top passing names of a run.

    ``peer_table`` builds a name's ``peers.json`` from its packet; it is
    written once, for names with SEC filings, and kept thereafter.

    A name whose folder already holds a complete ``filings.json`` is left as
    it is, so an interrupted preparation carries on and a folder that a
    review already cites is never rewritten. Returns the selection summary,
    which is also written as ``selection.json``.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    anchor = _parse_time(manifest["completed_at"])
    rows_by_ticker = {str(row.get("ticker")): row for row in results if row.get("status") == "OK"}
    selected = select_review_names(queue, top)
    run_directory = Path(output_root) / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    names = []
    for queue_row in selected:
        ticker = str(queue_row.get("ticker"))
        directory = run_directory / safe_name(ticker)
        entry = {"ticker": ticker, "directory": directory.name, "research_rank": queue_row.get("research_rank")}
        existing = _read_json(directory / "filings.json")
        if existing is not None and existing.get("complete"):
            entry.update({"status": existing.get("status"), "kept": True})
            _write_peers(directory, peer_table, entry)
            names.append(entry)
            continue
        if progress:
            progress(ticker)
        run_row = rows_by_ticker.get(ticker, {})
        registrant = resolver(run_row or queue_row)
        directory.mkdir(parents=True, exist_ok=True)
        if registrant.cik is None or archive is None:
            reason = (
                registrant.reason if registrant.cik is None
                else "SEC access not configured (set SEC_USER_AGENT in .env)"
            )
            packet = review_packet(
                base_packets.get(ticker, {"ticker": ticker}), run_row, queue_row,
                run_id=run_id, anchor=anchor, registrant=registrant,
            )
            _atomic_json(directory / "packet.json", packet)
            if registrant.cik is None:
                # No automatic source exists for this issuer (SEDAR+ may not
                # be scraped), so it is recorded as not reviewed, by code.
                _atomic_json(directory / "review.json", not_reviewed_review(
                    ticker, f"No SEC registrant: {reason}.", anchor,
                ))
                _atomic_json(directory / "filings.json", {
                    "ticker": ticker, "status": "not_reviewed", "reason": reason,
                    "documents": [], "complete": True, "prepared_at": clock().isoformat(),
                })
                entry["status"] = "not_reviewed"
            else:
                entry.update({"status": "error", "error": reason})
            names.append(entry)
            continue
        try:
            submissions = archive.submissions(registrant.cik)
            filings = choose_filings(submission_rows(submissions), anchor, max_current_reports)
            documents = _save_documents(archive, registrant.cik, filings, directory)
        except Exception as error:  # noqa: BLE001 - one name's failure must not stop the rest
            entry.update({"status": "error", "error": f"{type(error).__name__}: {error}"})
            names.append(entry)
            continue
        packet = review_packet(
            base_packets.get(ticker, {"ticker": ticker}), run_row, queue_row,
            run_id=run_id, anchor=anchor, registrant=registrant,
            sic=str(submissions.get("sic") or "") or None,
            sic_description=submissions.get("sicDescription"),
        )
        _atomic_json(directory / "packet.json", packet)
        status = "ready" if any(doc["role"] in ("annual", "quarterly") for doc in documents) else "no_periodic_report"
        _atomic_json(directory / "filings.json", {
            "ticker": ticker,
            "status": status,
            "cik": registrant.cik,
            "anchor_datetime": anchor.isoformat(),
            "documents": documents,
            "complete": True,
            "prepared_at": clock().isoformat(),
        })
        entry.update({"status": status, "documents": len(documents)})
        _write_peers(directory, peer_table, entry)
        names.append(entry)

    summary = {
        "run_id": run_id,
        "prepare_version": PREPARE_VERSION,
        "anchor_datetime": anchor.isoformat(),
        "top": top,
        "screen_status": "passes",
        "max_current_reports": max_current_reports,
        "names": names,
        "counts": _status_counts(names),
        "updated_at": clock().isoformat(),
        "note": "Reviews never change scores, ranks, the queue order or the dashboard order.",
    }
    _atomic_json(run_directory / "selection.json", summary)
    return summary


def _write_peers(directory: Path, peer_table: Callable[[dict], dict] | None, entry: dict) -> None:
    """Write ``peers.json`` once for a name with filings; never rewrite it."""
    path = directory / "peers.json"
    if path.is_file():
        entry["peers"] = (_read_json(path) or {}).get("status")
        return
    if peer_table is None or entry.get("status") not in ("ready", "no_periodic_report"):
        return
    packet = _read_json(directory / "packet.json")
    if packet is None:
        return
    try:
        table = peer_table(packet)
    except Exception as error:  # noqa: BLE001 - a missing table is reported, not fatal
        entry["peers"] = f"error: {type(error).__name__}: {error}"
        return
    _atomic_json(path, table)
    entry["peers"] = table.get("status")


def not_reviewed_review(ticker: str, reason: str, anchor: datetime) -> dict:
    """The record for a name no automatic source covers; written by code."""
    return {
        "ticker": ticker,
        "verdict": "not_reviewed",
        "deciding_reason": reason,
        "data_check": [],
        "findings": [],
        "peer_comparison": {
            "discount_explained": "insufficient_peers",
            "text": "Not reviewed.",
            "metrics_cited": [],
        },
        "bear_case": "",
        "unanswered_questions": [],
        "evidence_through": anchor.date().isoformat(),
        "written_by": "prepare",
    }


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "unnamed"


def _save_documents(archive: FilingArchive, cik: str, filings: list[dict], directory: Path) -> list[dict]:
    filings_directory = directory / "filings"
    filings_directory.mkdir(parents=True, exist_ok=True)
    documents = []
    for filing in filings:
        for item in archive.filing_documents(cik, filing):
            name = str(item["document"])
            if not name.lower().endswith(TEXT_SUFFIXES):
                continue
            raw = archive.document(item["url"])
            text = filing_text(raw, name)
            stem = safe_name(
                f"{filing['filingDate']}_{filing['form']}_{filing['accessionNumber']}_{Path(name).stem}"
            )
            path = filings_directory / f"{stem}.txt"
            _atomic_text(path, text)
            documents.append({
                "role": filing["role"],
                "form": filing["form"],
                "filing_date": filing["filingDate"],
                "accepted": filing.get("acceptanceDateTime"),
                "accession": filing["accessionNumber"],
                "document": name,
                "document_type": item.get("type") or "primary",
                "url": item["url"],
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "sha256": sha256_text(text),
                "path": f"filings/{path.name}",
                "characters": len(text),
            })
    return documents


def _filed_by(row: dict, cutoff: datetime) -> bool:
    accepted = _accepted(row)
    if accepted is None:
        return False
    return accepted <= cutoff


def _accepted(row: dict) -> datetime | None:
    value = row.get("acceptanceDateTime")
    if value:
        try:
            return _parse_time(str(value))
        except ValueError:
            pass
    filed = row.get("filingDate")
    if not filed:
        return None
    try:
        return datetime.strptime(str(filed), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _status_counts(names: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in names:
        counts[str(entry.get("status"))] = counts.get(str(entry.get("status")), 0) + 1
    return dict(sorted(counts.items()))


class _BlockTextExtractor(HTMLParser):
    BLOCKS = {
        "p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "ul", "ol", "dt", "dd", "hr", "title",
    }
    CELLS = {"td", "th"}
    HIDDEN = {"script", "style", "noscript", "ix:header", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.HIDDEN:
            self.hidden += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in self.CELLS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in self.CELLS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class SecFilingArchive:
    """Paced, cached access to EDGAR submissions and filing documents.

    Every request waits on the process's shared SEC pacer. Submissions and
    filing indexes are kept in an ``HttpCache``. Documents are not cached:
    their text is saved in the name's folder, which preparation never
    rewrites, and caching the raw HTML as well would double the disk used.
    """

    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"
    archive_url = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"

    def __init__(self, user_agent: str, cache, pacer=None) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must identify an organization and contact email.")
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
        self.cache = cache
        self.pacer = pacer
        inner = cache.fetcher

        def paced(url: str, headers: dict[str, str]) -> bytes:
            if self.pacer is not None:
                self.pacer.wait()
            return inner(url, headers)

        cache.fetcher = paced
        self._fetch = paced

    def submissions(self, cik: str) -> dict:
        raw, _ = self.cache.get(self.submissions_url.format(cik=str(cik).zfill(10)), self.headers)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object from EDGAR submissions.")
        return value

    def filing_documents(self, cik: str, filing: dict) -> list[dict]:
        """The primary document plus the exhibits worth keeping for its form."""
        accession = str(filing["accessionNumber"])
        base = self.archive_url.format(cik=int(cik), accession=accession.replace("-", ""))
        documents = [{"document": filing["primaryDocument"], "url": base + filing["primaryDocument"], "type": None}]
        wanted = ANNUAL_EXHIBITS if filing.get("role") == "annual" else (
            CURRENT_EXHIBITS if filing.get("role") == "current" else ()
        )
        if not wanted:
            return documents
        raw, _ = self.cache.get(f"{base}{accession}-index.htm", self.headers)
        for row in parse_filing_index(raw.decode("utf-8", errors="replace")):
            if row["document"] == filing["primaryDocument"]:
                continue
            if any(str(row["type"]).upper().startswith(prefix) for prefix in wanted):
                documents.append({"document": row["document"], "url": base + row["document"], "type": row["type"]})
        return documents

    def document(self, url: str) -> bytes:
        return self._fetch(url, self.headers)

    def public_float_frame(self, period: str) -> dict:
        """EDGAR's frame of every filer's ``dei:EntityPublicFloat`` for a period."""
        url = f"https://data.sec.gov/api/xbrl/frames/dei/EntityPublicFloat/USD/{period}.json"
        try:
            raw, _ = self.cache.get(url, self.headers)
        except Exception:  # noqa: BLE001 - a quarter not yet published has no frame
            return {"data": []}
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {"data": []}


def parse_filing_index(html: str) -> list[dict]:
    """Document names and types from an EDGAR ``-index.htm`` page."""
    parser = _IndexParser()
    parser.feed(html)
    parser.close()
    return parser.rows


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._cells: list[str] | None = None
        self._href: str | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._cells, self._href = [], None
        elif tag == "td" and self._cells is not None:
            self._cell = []
        elif tag == "a" and self._cell is not None:
            href = dict(attrs).get("href") or ""
            if "/Archives/edgar/data/" in href:
                self._href = href.split("?")[0].rsplit("/", 1)[-1]

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cells is not None and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._cells is not None:
            # Seq, Description, Document, Type, Size
            if len(self._cells) >= 4 and self._href:
                self.rows.append({"document": self._href, "type": self._cells[3]})
            self._cells = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _parse_time(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    # EDGAR writes acceptance times with milliseconds and a Z.
    result = datetime.fromisoformat(text)
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, data: dict) -> None:
    _atomic_text(path, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
