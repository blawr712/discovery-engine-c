"""Offline research quality audit and human-review queue exports."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from src.config import OUTPUT_DIR, RESEARCH_REVIEW_CONFIG
from src.research import validate_synthesis


SECTIONS = (
    "business_overview", "growth_drivers", "risks", "recent_developments",
)
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
MATERIAL_TERMS = {
    "adverse", "bankruptcy", "clinical", "covenant", "debt", "dilution",
    "financing", "fraud", "guidance", "investigation", "lawsuit", "loss",
    "merger", "regulatory", "revenue", "trial",
}


def audit_research_payload(payload: dict, gates: dict | None = None) -> dict:
    """Revalidate a saved research payload and evaluate automated gates."""
    gates = gates or RESEARCH_REVIEW_CONFIG
    packets = payload.get("packets", [])
    outputs = payload.get("outputs", [])
    output_lookup = {str(row.get("ticker")): row for row in outputs}
    companies = []
    for packet in packets:
        ticker = str(packet.get("ticker", ""))
        output = output_lookup.get(ticker, {})
        evidence = packet.get("evidence_documents", [])
        synthesis = output.get("synthesis")
        validation_error = None
        validation = None
        if isinstance(synthesis, dict):
            try:
                validation = validate_synthesis(packet, synthesis)
            except (TypeError, ValueError) as error:
                validation_error = str(error)
        section_presence = {
            section: bool(synthesis.get(section)) if isinstance(synthesis, dict) else False
            for section in SECTIONS
        }
        companies.append({
            "ticker": ticker,
            "country": packet.get("country") or "UNKNOWN",
            "selected_rank": packet.get("selected_rank"),
            "official_rank": packet.get("official_rank"),
            "evidence_document_count": len(evidence),
            "evidence_source_types": sorted({
                str(row.get("source_type")) for row in evidence if row.get("source_type")
            }),
            "evidence_publishers": sorted({
                str(row.get("publisher")) for row in evidence if row.get("publisher")
            }),
            "evidence_providers": packet.get("evidence_providers", {}),
            "synthesis_status": output.get("status", "missing"),
            "cached": bool(output.get("cached")),
            "validation_status": (
                "pass" if validation else "fail" if validation_error else "not_evaluated"
            ),
            "validation_error": validation_error,
            "claim_count": validation.get("claim_count", 0) if validation else 0,
            "sourced_claim_count": validation.get("sourced_claim_count", 0) if validation else 0,
            "cited_claim_count": validation.get("cited_claim_count", 0) if validation else 0,
            "citation_count": validation.get("citation_count", 0) if validation else 0,
            "section_presence": section_presence,
            "human_review_status": "pending" if validation else "not_ready",
        })

    count = len(companies)
    evidenced = [row for row in companies if row["evidence_document_count"] > 0]
    completed = [row for row in evidenced if row["validation_status"] == "pass"]
    sourced_claims = sum(row["sourced_claim_count"] for row in completed)
    cited_claims = sum(row["cited_claim_count"] for row in completed)
    claims = sum(row["claim_count"] for row in completed)
    citations = sum(row["citation_count"] for row in completed)
    section_slots = len(completed) * len(SECTIONS)
    sections_present = sum(
        sum(row["section_presence"].values()) for row in completed
    )
    metrics = {
        "candidate_count": count,
        "evidenced_candidate_count": len(evidenced),
        "validated_synthesis_count": len(completed),
        "synthesis_error_count": sum(
            row["synthesis_status"] == "error" or row["validation_status"] == "fail"
            for row in companies
        ),
        "evidence_coverage_percent": _percent(len(evidenced), count),
        "synthesis_completion_percent": _percent(len(completed), len(evidenced)),
        "citation_coverage_percent": _percent(cited_claims, claims),
        "section_coverage_percent": _percent(sections_present, section_slots),
        "claim_count": sum(row["claim_count"] for row in completed),
        "cited_claim_count": cited_claims,
        "sourced_claim_count": sourced_claims,
        "citation_count": citations,
        "average_sourced_claims_per_synthesis": round(
            sourced_claims / len(completed), 2
        ) if completed else 0.0,
        "country_evidence_coverage": _country_coverage(companies),
    }
    gate_results = _gate_results(
        metrics,
        gates,
        synthesis_present=bool(completed),
        ranking_integrity=payload.get("official_scores_and_ranks_unchanged") is True,
    )
    failed = [name for name, row in gate_results.items() if row["status"] == "fail"]
    not_evaluated = [
        name for name, row in gate_results.items() if row["status"] == "not_evaluated"
    ]
    automated_status = "fail" if failed else "not_evaluated" if not_evaluated else "pass"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": payload.get("run_id"),
        "scores_and_ranks_unchanged": payload.get("official_scores_and_ranks_unchanged") is True,
        "metrics": metrics,
        "gates": gate_results,
        "automated_status": automated_status,
        "failed_gates": failed,
        "not_evaluated_gates": not_evaluated,
        "human_signoff_required": True,
        "release_status": "pending_human_review" if automated_status == "pass" else automated_status,
        "companies": companies,
    }


def export_research_audit(
    payload: dict,
    run_id: str,
    output_directory: Path | None = None,
    gates: dict | None = None,
) -> dict:
    """Export audit JSON plus a claim-level human-review CSV."""
    output_directory = Path(output_directory or OUTPUT_DIR)
    output_directory.mkdir(parents=True, exist_ok=True)
    audit = audit_research_payload(payload, gates=gates)
    audit_path = output_directory / f"research_audit_{run_id}.json"
    review_path = output_directory / f"research_human_review_{run_id}.csv"
    candidate_path = output_directory / f"research_candidate_audit_{run_id}.csv"
    all_rows = _review_rows(payload, audit)
    rows, policy = _select_review_rows(all_rows, gates or RESEARCH_REVIEW_CONFIG)
    audit["review_sampling"] = {
        **policy,
        "total_claim_count": len(all_rows),
        "selected_claim_count": len(rows),
        "coverage_percent": _percent(len(rows), len(all_rows)),
        "risk_counts": _counts(all_rows, "review_risk_level"),
        "selected_risk_counts": _counts(rows, "review_risk_level"),
        "selection_counts": _counts(rows, "review_selection_basis"),
        "selected_claim_ids": [row["claim_id"] for row in rows],
    }
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    fields = [
        "claim_id", "ticker", "country", "selected_rank", "section", "classification",
        "review_risk_level", "review_risk_reasons", "review_selection_basis",
        "claim", "citation_urls", "citation_hashes", "automated_validation",
        "accuracy_review", "citation_support_review", "materiality_notes",
        "human_review_status",
    ]
    with review_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    _write_candidate_audit(candidate_path, audit["companies"])
    triage_path = output_directory / f"research_claim_triage_{run_id}.csv"
    with triage_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[*fields[:-4], "selected_for_human_review"],
        )
        writer.writeheader()
        selected_ids = {row["claim_id"] for row in rows}
        selection_basis = {
            row["claim_id"]: row["review_selection_basis"] for row in rows
        }
        for row in all_rows:
            writer.writerow({
                **{field: row.get(field) for field in fields[:-4]},
                "review_selection_basis": selection_basis.get(row["claim_id"], "not_selected"),
                "selected_for_human_review": row["claim_id"] in selected_ids,
            })
    return {
        "research_audit_json_path": str(audit_path),
        "research_human_review_csv_path": str(review_path),
        "research_candidate_audit_csv_path": str(candidate_path),
        "research_claim_triage_csv_path": str(triage_path),
        "automated_status": audit["automated_status"],
        "release_status": audit["release_status"],
        "metrics": audit["metrics"],
        "failed_gates": audit["failed_gates"],
        "not_evaluated_gates": audit["not_evaluated_gates"],
        "review_row_count": len(rows),
        "review_sampling": audit["review_sampling"],
    }


def finalize_research_review(
    audit_path: Path,
    review_path: Path,
    run_id: str,
    output_directory: Path | None = None,
) -> dict:
    """Create a decision record from an automated pass and completed review CSV."""
    audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    if audit.get("run_id") != run_id:
        raise ValueError("Research audit run ID does not match.")
    if audit.get("automated_status") != "pass":
        raise ValueError("Automated research gates must pass before finalization.")
    with Path(review_path).open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError("Human review queue has no claim rows.")
    required = {"accuracy_review", "citation_support_review", "human_review_status"}
    if not required.issubset(rows[0]):
        raise ValueError("Human review queue is missing required decision columns.")
    expected_ids = set((audit.get("review_sampling") or {}).get("selected_claim_ids", []))
    if expected_ids:
        actual_ids = [str(row.get("claim_id", "")) for row in rows]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
            raise ValueError("Human review queue does not match the audited claim sample.")
    pending = []
    rejected = []
    for index, row in enumerate(rows, start=2):
        accuracy = str(row.get("accuracy_review", "")).strip().lower()
        support = str(row.get("citation_support_review", "")).strip().lower()
        status = str(row.get("human_review_status", "")).strip().lower()
        if accuracy not in {"pass", "fail"} or support not in {"pass", "fail"} or status not in {"approved", "rejected"}:
            pending.append(index)
        elif "fail" in {accuracy, support} or status == "rejected":
            rejected.append(index)
    if pending:
        decision = "incomplete"
    elif rejected:
        decision = "rejected"
    else:
        decision = "approved"
    candidate_decisions = _candidate_decisions(audit.get("companies", []), rows)
    candidate_status_counts = _counts(candidate_decisions, "release_status")
    output_directory = Path(output_directory or OUTPUT_DIR)
    release_path = output_directory / f"research_release_{run_id}.csv"
    _write_release_report(release_path, candidate_decisions)
    record = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "automated_status": audit["automated_status"],
        "human_review_decision": decision,
        "research_release_status": decision,
        "review_row_count": len(rows),
        "approved_row_count": len(rows) - len(pending) - len(rejected),
        "pending_csv_rows": pending,
        "rejected_csv_rows": rejected,
        "candidate_status_counts": candidate_status_counts,
        "candidate_decisions": candidate_decisions,
        "review_sampling": audit.get("review_sampling"),
        "scores_and_ranks_unchanged": audit.get("scores_and_ranks_unchanged") is True,
        "source_audit_path": str(Path(audit_path)),
        "source_review_path": str(Path(review_path)),
        "research_release_csv_path": str(release_path),
    }
    decision_path = output_directory / f"research_acceptance_{run_id}.json"
    decision_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return {**record, "research_acceptance_json_path": str(decision_path)}


def _write_candidate_audit(path: Path, companies: list[dict]) -> None:
    fields = [
        "ticker", "country", "selected_rank", "official_rank",
        "evidence_document_count", "evidence_providers", "synthesis_status", "cached",
        "validation_status", "claim_count", "cited_claim_count",
        "citation_count", "sections_present", "human_review_status",
        "validation_error",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for company in companies:
            writer.writerow({
                **{field: company.get(field) for field in fields},
                "evidence_providers": json.dumps(company.get("evidence_providers", {})),
                "sections_present": sum(company.get("section_presence", {}).values()),
            })


def _candidate_decisions(companies: list[dict], rows: list[dict]) -> list[dict]:
    decisions = []
    for company in companies:
        ticker = str(company.get("ticker", ""))
        claim_rows = [row for row in rows if str(row.get("ticker", "")) == ticker]
        row_decisions = [_claim_review_decision(row) for row in claim_rows]
        pending = row_decisions.count("incomplete")
        rejected = row_decisions.count("rejected")
        approved = len(claim_rows) - pending - rejected
        if company.get("validation_status") != "pass" or not claim_rows:
            status = "not_ready"
        elif pending:
            status = "incomplete"
        elif rejected:
            status = "rejected"
        else:
            status = "approved"
        decisions.append({
            "ticker": ticker,
            "country": company.get("country"),
            "selected_rank": company.get("selected_rank"),
            "evidence_document_count": company.get("evidence_document_count", 0),
            "synthesis_status": company.get("synthesis_status"),
            "automated_validation": company.get("validation_status"),
            "claim_count": company.get("claim_count", 0),
            "review_claim_count": len(claim_rows),
            "unreviewed_claim_count": max(
                int(company.get("claim_count", 0)) - len(claim_rows), 0
            ),
            "approved_claim_count": approved,
            "rejected_claim_count": rejected,
            "pending_claim_count": pending,
            "release_status": status,
        })
    return decisions


def _claim_review_decision(row: dict) -> str:
    accuracy = str(row.get("accuracy_review", "")).strip().lower()
    support = str(row.get("citation_support_review", "")).strip().lower()
    status = str(row.get("human_review_status", "")).strip().lower()
    if accuracy not in {"pass", "fail"} or support not in {"pass", "fail"}:
        return "incomplete"
    if status not in {"approved", "rejected"}:
        return "incomplete"
    if "fail" in {accuracy, support} or status == "rejected":
        return "rejected"
    return "approved"


def _write_release_report(path: Path, decisions: list[dict]) -> None:
    fields = [
        "ticker", "country", "selected_rank", "evidence_document_count",
        "synthesis_status", "automated_validation", "claim_count",
        "review_claim_count", "unreviewed_claim_count",
        "approved_claim_count", "rejected_claim_count", "pending_claim_count",
        "release_status",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(decisions)


def _counts(rows: list[dict], field: str) -> dict:
    counts = {}
    for row in rows:
        value = str(row.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _review_rows(payload: dict, audit: dict) -> list[dict]:
    packets = {str(row.get("ticker")): row for row in payload.get("packets", [])}
    companies = {row["ticker"]: row for row in audit["companies"]}
    rows = []
    for output in payload.get("outputs", []):
        ticker = str(output.get("ticker", ""))
        synthesis = output.get("synthesis")
        if not isinstance(synthesis, dict):
            continue
        packet = packets.get(ticker, {})
        company = companies.get(ticker, {})
        for section in SECTIONS:
            for claim in synthesis.get(section, []):
                citations = claim.get("citations", [])
                rows.append({
                    "claim_id": _claim_id(ticker, section, claim),
                    "ticker": ticker,
                    "country": packet.get("country"),
                    "selected_rank": packet.get("selected_rank"),
                    "section": section,
                    "classification": claim.get("classification"),
                    "review_risk_level": _risk_level(section, claim)[0],
                    "review_risk_reasons": json.dumps(_risk_level(section, claim)[1]),
                    "review_selection_basis": "",
                    "claim": claim.get("text"),
                    "citation_urls": json.dumps([row.get("url") for row in citations]),
                    "citation_hashes": json.dumps([row.get("content_hash") for row in citations]),
                    "automated_validation": company.get("validation_status"),
                    "accuracy_review": "",
                    "citation_support_review": "",
                    "materiality_notes": "",
                    "human_review_status": "pending",
                })
    return rows


def _risk_level(section: str, claim: dict) -> tuple[str, list[str]]:
    text = str(claim.get("text", ""))
    lowered = text.lower()
    reasons = []
    if claim.get("classification") == "interpretation":
        reasons.append("interpretation")
    if section == "risks":
        reasons.append("risk_section")
    if re.search(r"(?:\d|%|\$)", text):
        reasons.append("numeric_claim")
    matched_terms = sorted(term for term in MATERIAL_TERMS if term in lowered)
    if matched_terms:
        reasons.append("material_terms:" + ",".join(matched_terms))
    if len(claim.get("citations", [])) > 1:
        reasons.append("multiple_citations")
    material_interpretation = "interpretation" in reasons and (
        "risk_section" in reasons
        or any(reason.startswith("material_terms:") for reason in reasons)
    )
    if material_interpretation:
        return "high", reasons
    if reasons:
        return "medium", reasons
    return "low", ["straightforward_sourced_fact"]


def _claim_id(ticker: str, section: str, claim: dict) -> str:
    payload = json.dumps(
        {"ticker": ticker, "section": section, "claim": claim},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _select_review_rows(rows: list[dict], gates: dict) -> tuple[list[dict], dict]:
    policy = dict(gates.get("review_sampling") or {})
    enabled = bool(policy.get("enabled", False))
    medium_percent = float(policy.get("medium_risk_sample_percent", 50))
    low_percent = float(policy.get("low_risk_sample_percent", 25))
    seed = str(policy.get("seed") or "v0.3-review-1")
    for value in (medium_percent, low_percent):
        if not 0 <= value <= 100:
            raise ValueError("Research review sample percentages must be between 0 and 100.")
    selected = {}
    for row in rows:
        if not enabled or row["review_risk_level"] == "high":
            selected[row["claim_id"]] = "full_review" if not enabled else "mandatory_high_risk"
    if enabled and policy.get("minimum_one_claim_per_section", True):
        groups = sorted({(row["ticker"], row["section"]) for row in rows})
        for ticker, section in groups:
            candidates = [
                row for row in rows
                if row["ticker"] == ticker and row["section"] == section
            ]
            candidates.sort(key=lambda row: (RISK_ORDER[row["review_risk_level"]], row["claim_id"]))
            selected.setdefault(candidates[0]["claim_id"], "section_coverage")
    if enabled:
        for row in rows:
            if row["claim_id"] in selected:
                continue
            percent = medium_percent if row["review_risk_level"] == "medium" else low_percent
            if _sample_value(seed, row["claim_id"]) < percent:
                selected[row["claim_id"]] = "reproducible_sample"
    result = []
    for row in rows:
        if row["claim_id"] in selected:
            result.append({**row, "review_selection_basis": selected[row["claim_id"]]})
    return result, {
        "enabled": enabled,
        "medium_risk_sample_percent": medium_percent,
        "low_risk_sample_percent": low_percent,
        "minimum_one_claim_per_section": bool(
            policy.get("minimum_one_claim_per_section", True)
        ),
        "seed": seed,
    }


def _sample_value(seed: str, claim_id: str) -> float:
    digest = hashlib.sha256(f"{seed}:{claim_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF * 100


def _gate_results(
    metrics: dict,
    gates: dict,
    synthesis_present: bool,
    ranking_integrity: bool,
) -> dict:
    definitions = {
        "evidence_coverage": (
            metrics["evidence_coverage_percent"],
            float(gates.get("minimum_evidence_coverage_percent", 80)),
        ),
        "synthesis_completion": (
            metrics["synthesis_completion_percent"],
            float(gates.get("minimum_synthesis_completion_percent", 100)),
        ),
        "citation_coverage": (
            metrics["citation_coverage_percent"],
            float(gates.get("minimum_citation_coverage_percent", 100)),
        ),
        "section_coverage": (
            metrics["section_coverage_percent"],
            float(gates.get("minimum_section_coverage_percent", 75)),
        ),
    }
    results = {}
    for name, (actual, required) in definitions.items():
        if name != "evidence_coverage" and not synthesis_present:
            status = "not_evaluated"
        else:
            status = "pass" if actual >= required else "fail"
        results[name] = {"status": status, "actual": actual, "minimum": required}
    minimum_sourced = float(gates.get("minimum_sourced_claims_per_synthesis", 1))
    results["sourced_evidence_use"] = {
        "status": (
            "not_evaluated" if not synthesis_present
            else "pass" if metrics["average_sourced_claims_per_synthesis"] >= minimum_sourced
            else "fail"
        ),
        "actual": metrics["average_sourced_claims_per_synthesis"],
        "minimum": minimum_sourced,
    }
    results["ranking_integrity"] = {
        "status": "pass" if ranking_integrity else "fail",
        "actual": ranking_integrity,
        "required": True,
    }
    maximum = int(gates.get("maximum_synthesis_errors", 0))
    results["synthesis_errors"] = {
        "status": (
            "not_evaluated" if not synthesis_present and metrics["synthesis_error_count"] == 0
            else "pass" if metrics["synthesis_error_count"] <= maximum else "fail"
        ),
        "actual": metrics["synthesis_error_count"],
        "maximum": maximum,
    }
    return results


def _country_coverage(companies: list[dict]) -> dict:
    result = {}
    countries = sorted({str(row["country"]) for row in companies})
    for country in countries:
        rows = [row for row in companies if str(row["country"]) == country]
        evidenced = sum(row["evidence_document_count"] > 0 for row in rows)
        result[country] = {
            "candidates": len(rows),
            "evidenced": evidenced,
            "coverage_percent": _percent(evidenced, len(rows)),
        }
    return result


def _percent(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 2) if denominator else 0.0
