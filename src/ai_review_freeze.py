"""Validate AI reviews and freeze them into one artifact per run.

Every review is checked against the schema in AI_REVIEW_BRIEF.md. Every quote
must appear word for word in the saved filing whose hash it cites, every
cited peer metric must exist in the name's ``peers.json``, and every saved
file must still have the hash recorded when it was prepared. A review that
fails is kept in the artifact as failed; it is never rewritten. The artifact
is never overwritten, the way frozen baselines are not.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable

from src.ai_review import PREPARE_VERSION, normalize_text, safe_name
from src.ai_review_peers import METRICS as PEER_METRICS, PEERS_VERSION

CHECKLIST_VERSION = "review-v1"
SKILL_VERSION = "ai-review-skill-1"
FREEZE_VERSION = "ai-review-freeze-1"

VERDICTS = ("clear", "caution", "avoid", "not_reviewed")
AREAS = (
    "earnings_quality", "balance_sheet", "dilution", "going_concern",
    "pending_event", "business", "governance",
)
SEVERITIES = ("info", "concern", "serious")
DISCOUNT_ANSWERS = ("yes", "partly", "no", "not_cheaper", "insufficient_peers")
REQUIRED_KEYS = (
    "ticker", "verdict", "deciding_reason", "data_check", "findings",
    "peer_comparison", "bear_case", "unanswered_questions", "evidence_through",
)
OPTIONAL_KEYS = ("checklist_version", "skill_version", "model", "written_by")
MINIMUM_QUOTE = 10
# Fields a data check may compare: the packet's figures.
DATA_CHECK_FIELDS = (
    "pit_revenue_ttm", "pit_shares_outstanding", "pit_market_cap", "pit_reporting_currency",
    "pit_net_income_ttm", "pit_cash", "pit_total_debt", "pit_net_cash", "pit_earnings_yield",
    "pit_gross_margin_ttm", "pit_operating_margin_ttm", "pit_fcf_margin_ttm", "net_margin_ttm",
    "pit_share_change_1y", "pit_revenue_growth_ttm", "latest_close", "currency",
)
# What a review may never contain (AI_REVIEW_BRIEF.md, schema).
FORBIDDEN = re.compile(
    r"price target|target price|fair value of \$|expected return|upside of|downside of|"
    r"position siz|% upside|% downside",
    re.IGNORECASE,
)


def validate_review(
    review: object,
    *,
    ticker: str,
    packet: dict,
    peers: dict | None,
    documents: dict[str, str],
    anchor: date,
) -> list[str]:
    """Every way a review breaks the schema or its evidence rules.

    ``documents`` maps each cited-able SHA-256 to the saved text it hashes.
    """
    if not isinstance(review, dict):
        return ["review is not a JSON object"]
    errors: list[str] = []
    missing = [key for key in REQUIRED_KEYS if key not in review]
    if missing:
        errors.append(f"missing keys: {', '.join(missing)}")
    extra = sorted(set(review) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    if extra:
        errors.append(f"keys not in the schema: {', '.join(extra)}")
    if str(review.get("ticker")) != ticker:
        errors.append(f"ticker {review.get('ticker')!r} is not {ticker!r}")
    verdict = review.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    for key in ("deciding_reason", "bear_case"):
        if not isinstance(review.get(key), str):
            errors.append(f"{key} must be text")
    if not isinstance(review.get("deciding_reason"), str) or not review.get("deciding_reason", "").strip():
        errors.append("deciding_reason is empty")
    if verdict in ("clear", "caution", "avoid") and not str(review.get("bear_case") or "").strip():
        errors.append("bear_case is empty")
    questions = review.get("unanswered_questions")
    if not isinstance(questions, list) or not all(isinstance(item, str) for item in questions):
        errors.append("unanswered_questions must be a list of text")
    if review.get("checklist_version") not in (None, CHECKLIST_VERSION):
        errors.append(f"checklist_version {review.get('checklist_version')!r} is not {CHECKLIST_VERSION}")
    errors.extend(_check_date(review.get("evidence_through"), anchor))

    for position, item in enumerate(_list(review, "data_check", errors)):
        errors.extend(_check_data_item(item, position, packet, documents))
    findings = _list(review, "findings", errors)
    for position, item in enumerate(findings):
        errors.extend(_check_finding(item, position, documents))
    errors.extend(_check_peers(review.get("peer_comparison"), peers))

    for key in ("deciding_reason", "bear_case"):
        text = review.get(key)
        if isinstance(text, str) and FORBIDDEN.search(text):
            errors.append(f"{key} contains forbidden content ({FORBIDDEN.search(text).group(0)!r})")
    for item in findings:
        if isinstance(item, dict) and isinstance(item.get("text"), str) and FORBIDDEN.search(item["text"]):
            errors.append(f"a finding contains forbidden content ({FORBIDDEN.search(item['text']).group(0)!r})")

    if verdict == "avoid":
        serious = any(isinstance(item, dict) and item.get("severity") == "serious" for item in findings)
        wrong = any(
            isinstance(item, dict) and item.get("agrees") is False
            for item in (review.get("data_check") or [])
        )
        if not serious and not wrong:
            errors.append("verdict avoid needs a serious finding or a data-check disagreement")
    if verdict == "not_reviewed" and review.get("written_by") != "prepare":
        errors.append("only preparation may mark a name not_reviewed")
    return errors


def quote_found(quote: str, text: str) -> bool:
    """Whether ``quote`` appears word for word in ``text``, ignoring spacing."""
    return _collapse(quote) in _collapse(text)


def _collapse(value: str) -> str:
    return " ".join(normalize_text(value).split())


def _list(review: dict, key: str, errors: list[str]) -> list:
    value = review.get(key)
    if not isinstance(value, list):
        if key in review:
            errors.append(f"{key} must be a list")
        return []
    return value


def _check_date(value: object, anchor: date) -> list[str]:
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return [f"evidence_through {value!r} is not a YYYY-MM-DD date"]
    if day > anchor:
        return [f"evidence_through {day} is after the anchor date {anchor}"]
    return []


def _check_citation(citation: object, where: str, documents: dict[str, str]) -> list[str]:
    if not isinstance(citation, dict):
        return [f"{where}: citation missing"]
    sha = str(citation.get("sha256") or "")
    quote = citation.get("quote")
    if sha not in documents:
        return [f"{where}: cites a hash that is not one of this name's saved filings"]
    if not isinstance(quote, str) or len(_collapse(quote)) < MINIMUM_QUOTE:
        return [f"{where}: quote shorter than {MINIMUM_QUOTE} characters"]
    if not quote_found(quote, documents[sha]):
        return [f"{where}: quote not found word for word in the cited filing: {quote[:80]!r}"]
    return []


def _check_data_item(item: object, position: int, packet: dict, documents: dict[str, str]) -> list[str]:
    where = f"data_check[{position}]"
    if not isinstance(item, dict):
        return [f"{where} is not an object"]
    errors = []
    allowed = {"field", "ours", "filing", "agrees", "citation", "note"}
    extra = sorted(set(item) - allowed)
    if extra:
        errors.append(f"{where}: keys not in the schema: {', '.join(extra)}")
    field = item.get("field")
    if field not in DATA_CHECK_FIELDS:
        errors.append(f"{where}: field {field!r} is not a packet figure a data check may compare")
    elif not _same_value(item.get("ours"), packet.get(field)):
        errors.append(f"{where}: ours {item.get('ours')!r} is not the packet's {field} {packet.get(field)!r}")
    if not isinstance(item.get("agrees"), bool):
        errors.append(f"{where}: agrees must be true or false")
    if item.get("filing") is not None:
        errors.extend(_check_citation(item.get("citation"), where, documents))
    elif item.get("citation") is not None:
        errors.extend(_check_citation(item.get("citation"), where, documents))
    return errors


def _check_finding(item: object, position: int, documents: dict[str, str]) -> list[str]:
    where = f"findings[{position}]"
    if not isinstance(item, dict):
        return [f"{where} is not an object"]
    errors = []
    extra = sorted(set(item) - {"area", "severity", "text", "citation"})
    if extra:
        errors.append(f"{where}: keys not in the schema: {', '.join(extra)}")
    if item.get("area") not in AREAS:
        errors.append(f"{where}: area {item.get('area')!r} is not one of {', '.join(AREAS)}")
    if item.get("severity") not in SEVERITIES:
        errors.append(f"{where}: severity {item.get('severity')!r} is not one of {', '.join(SEVERITIES)}")
    if not isinstance(item.get("text"), str) or not item["text"].strip():
        errors.append(f"{where}: text is empty")
    errors.extend(_check_citation(item.get("citation"), where, documents))
    return errors


def _check_peers(comparison: object, peers: dict | None) -> list[str]:
    if not isinstance(comparison, dict):
        return ["peer_comparison is missing"]
    errors = []
    extra = sorted(set(comparison) - {"discount_explained", "text", "metrics_cited"})
    if extra:
        errors.append(f"peer_comparison: keys not in the schema: {', '.join(extra)}")
    answer = comparison.get("discount_explained")
    if answer not in DISCOUNT_ANSWERS:
        errors.append(f"peer_comparison.discount_explained {answer!r} is not one of {', '.join(DISCOUNT_ANSWERS)}")
    if not isinstance(comparison.get("text"), str):
        errors.append("peer_comparison.text must be text")
    cited = comparison.get("metrics_cited")
    if not isinstance(cited, list):
        errors.append("peer_comparison.metrics_cited must be a list")
        cited = []
    insufficient = peers is None or peers.get("status") != "ok"
    if insufficient and answer != "insufficient_peers":
        errors.append("the peer table has too few peers, so discount_explained must be insufficient_peers")
    available = set(peers.get("metrics") or []) if peers else set()
    summary = (peers or {}).get("summary") or {}
    for name in cited:
        if name not in available or name not in summary:
            errors.append(f"peer_comparison cites {name!r}, which is not a metric in peers.json")
    if answer in ("yes", "partly", "no", "not_cheaper") and not cited:
        errors.append("peer_comparison must cite the peer-table metrics it used")
    return errors


def _same_value(left: object, right: object) -> bool:
    if isinstance(right, (int, float)) and not isinstance(right, bool):
        try:
            value = float(left)
        except (TypeError, ValueError):
            return False
        return abs(value - float(right)) <= max(1e-6, 1e-6 * abs(float(right)))
    return left == right


def load_name(folder: Path) -> dict:
    """A prepared folder's files, with each saved text checked against its hash."""
    packet = _read_json(folder / "packet.json") or {}
    filings = _read_json(folder / "filings.json") or {}
    peers_path = folder / "peers.json"
    peers = _read_json(peers_path)
    documents: dict[str, str] = {}
    integrity: list[str] = []
    for doc in filings.get("documents") or []:
        path = folder / str(doc.get("path"))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            integrity.append(f"filing text missing: {doc.get('path')}")
            continue
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != doc.get("sha256"):
            integrity.append(f"filing text changed since preparation: {doc.get('path')}")
            continue
        documents[str(doc["sha256"])] = text
    review_path = folder / "review.json"
    review, review_error = None, None
    if review_path.is_file():
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            review_error = f"review.json is not valid JSON: {error}"
    return {
        "packet": packet,
        "filings": filings,
        "peers": peers,
        "peers_sha256": _file_sha256(peers_path) if peers_path.is_file() else None,
        "packet_sha256": _file_sha256(folder / "packet.json") if (folder / "packet.json").is_file() else None,
        "documents": documents,
        "integrity": integrity,
        "review": review,
        "review_error": review_error,
        "review_exists": review_path.is_file(),
    }


def check_reviews(run_directory: Path) -> dict:
    """Validate every name in a prepared run; nothing is written."""
    selection = _read_json(Path(run_directory) / "selection.json")
    if selection is None:
        raise FileNotFoundError(f"No selection.json in {run_directory}; run --ai-review-prepare first.")
    anchor = datetime.fromisoformat(str(selection["anchor_datetime"])).date()
    names = []
    for entry in selection.get("names") or []:
        ticker = str(entry["ticker"])
        folder = Path(run_directory) / (entry.get("directory") or safe_name(ticker))
        loaded = load_name(folder)
        result = {
            "ticker": ticker,
            "research_rank": entry.get("research_rank"),
            "prepare_status": entry.get("status"),
            "packet_sha256": loaded["packet_sha256"],
            "peers_sha256": loaded["peers_sha256"],
            "peers_status": (loaded["peers"] or {}).get("status"),
            "filings": [
                {key: doc.get(key) for key in ("form", "filing_date", "accession", "document", "sha256", "url")}
                for doc in (loaded["filings"].get("documents") or [])
            ],
        }
        if not loaded["review_exists"]:
            result.update({"status": "missing", "errors": []})
        elif loaded["review_error"]:
            result.update({"status": "validation_failed", "errors": [loaded["review_error"]]})
        else:
            errors = list(loaded["integrity"]) + validate_review(
                loaded["review"], ticker=ticker, packet=loaded["packet"], peers=loaded["peers"],
                documents=loaded["documents"], anchor=anchor,
            )
            verdict = (loaded["review"] or {}).get("verdict")
            status = "not_reviewed" if verdict == "not_reviewed" and not errors else (
                "valid" if not errors else "validation_failed"
            )
            result.update({"status": status, "errors": errors, "review": loaded["review"]})
        names.append(result)
    counts: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    disagreements = []
    for result in names:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        review = result.get("review") or {}
        if result["status"] in ("valid", "not_reviewed"):
            verdicts[review.get("verdict")] = verdicts.get(review.get("verdict"), 0) + 1
        for item in review.get("data_check") or []:
            if isinstance(item, dict) and item.get("agrees") is False:
                disagreements.append({
                    "ticker": result["ticker"], "field": item.get("field"),
                    "ours": item.get("ours"), "filing": item.get("filing"),
                })
    return {
        "run_id": selection.get("run_id"),
        "anchor_datetime": selection.get("anchor_datetime"),
        "names": names,
        "counts": dict(sorted(counts.items())),
        "verdicts": dict(sorted(verdicts.items())),
        "data_check_disagreements": disagreements,
    }


def freeze_reviews(
    run_directory: Path,
    output_directory: Path,
    *,
    dry_run: bool = False,
    baseline_tickers: set[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[dict, str]:
    """Write the one frozen artifact for a run; refuse to overwrite it.

    Every prepared name must have a review. A dry run writes a separately
    named artifact that is not part of any test.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    checked = check_reviews(run_directory)
    missing = [result["ticker"] for result in checked["names"] if result["status"] == "missing"]
    pending = [
        result["ticker"] for result in checked["names"]
        if result["status"] == "missing" and result["prepare_status"] in ("ready", "no_periodic_report")
    ]
    if pending:
        raise ValueError(f"{len(pending)} prepared names have no review yet: {', '.join(pending)}")
    run_id = str(checked["run_id"])
    name = f"ai_review_dry_run_{run_id}.json" if dry_run else f"ai_reviews_{run_id}.json"
    path = Path(output_directory) / name
    if path.exists():
        raise FileExistsError(f"AI reviews already frozen for {run_id}: {path}")
    models = sorted({
        str((result.get("review") or {}).get("model"))
        for result in checked["names"]
        if result.get("review") and (result["review"].get("written_by") != "prepare")
        and (result["review"]).get("model")
    })
    names = []
    for result in checked["names"]:
        entry = {key: value for key, value in result.items() if key != "review"}
        if result["status"] in ("valid", "not_reviewed"):
            entry["review"] = result["review"]
            entry["verdict"] = result["review"]["verdict"]
        else:
            entry["verdict"] = None
        if baseline_tickers is not None:
            entry["in_research_baseline"] = result["ticker"] in baseline_tickers
        names.append(entry)
    artifact = {
        "run_id": run_id,
        "anchor_datetime": checked["anchor_datetime"],
        "frozen_at": clock().isoformat(),
        "dry_run": bool(dry_run),
        "freeze_version": FREEZE_VERSION,
        "prepare_version": PREPARE_VERSION,
        "peers_version": PEERS_VERSION,
        "checklist_version": CHECKLIST_VERSION,
        "skill_version": SKILL_VERSION,
        "models": models,
        "counts": checked["counts"],
        "verdicts": checked["verdicts"],
        "data_check_disagreements": checked["data_check_disagreements"],
        "missing_without_preparation": [ticker for ticker in missing if ticker not in pending],
        "names": names,
        "note": (
            "Reviews never change scores, ranks, the queue order or the dashboard order. "
            + ("This is a dry run and is not part of the forward test." if dry_run else
               "Judged by the forward-test rule in DECISIONS.md.")
        ),
    }
    artifact["artifact_sha256"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    _atomic_json_exclusive(path, artifact)
    return artifact, str(path)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json_exclusive(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, indent=2, sort_keys=True, default=str)
            file.write("\n")
        if path.exists():
            raise FileExistsError(f"AI reviews already frozen: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
