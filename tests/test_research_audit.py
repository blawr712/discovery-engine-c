import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.research_audit import (
    audit_research_payload,
    export_research_audit,
    finalize_research_review,
)


def payload_with_synthesis():
    citation = {"url": "https://www.sec.gov/filing", "content_hash": "a" * 64}
    claim = {
        "text": "A supported fact.",
        "classification": "sourced_fact",
        "citations": [citation],
    }
    return {
        "run_id": "run-1",
        "official_scores_and_ranks_unchanged": True,
        "packets": [
            {
                "ticker": "USCO", "country": "US", "selected_rank": 1,
                "official_rank": 2,
                "evidence_documents": [{**citation, "source_type": "regulatory_filing", "publisher": "SEC"}],
            },
            {
                "ticker": "CACO", "country": "CA", "selected_rank": 2,
                "official_rank": 1, "evidence_documents": [],
            },
        ],
        "outputs": [
            {
                "ticker": "USCO", "status": "complete", "cached": False,
                "synthesis": {
                    "business_overview": [claim],
                    "growth_drivers": [claim],
                    "risks": [claim],
                    "recent_developments": [claim],
                    "unanswered_questions": ["What remains unknown?"],
                },
            },
            {"ticker": "CACO", "status": "skipped_no_evidence", "synthesis": None},
        ],
    }


class ResearchAuditTests(unittest.TestCase):
    def setUp(self):
        self.gates = {
            "minimum_evidence_coverage_percent": 50,
            "minimum_synthesis_completion_percent": 100,
            "minimum_citation_coverage_percent": 100,
            "minimum_section_coverage_percent": 75,
            "minimum_sourced_claims_per_synthesis": 1,
            "maximum_synthesis_errors": 0,
        }

    def test_passes_automated_gates_but_requires_human_signoff(self):
        audit = audit_research_payload(payload_with_synthesis(), self.gates)

        self.assertEqual(audit["automated_status"], "pass")
        self.assertEqual(audit["release_status"], "pending_human_review")
        self.assertTrue(audit["human_signoff_required"])
        self.assertEqual(audit["metrics"]["country_evidence_coverage"]["CA"]["coverage_percent"], 0)
        self.assertEqual(audit["metrics"]["country_evidence_coverage"]["US"]["coverage_percent"], 100)
        self.assertEqual(audit["metrics"]["citation_coverage_percent"], 100)

    def test_packet_only_payload_marks_synthesis_gates_not_evaluated(self):
        payload = payload_with_synthesis()
        payload["outputs"][0] = {"ticker": "USCO", "status": "packet_only", "synthesis": None}
        audit = audit_research_payload(payload, self.gates)

        self.assertEqual(audit["automated_status"], "not_evaluated")
        self.assertIn("synthesis_completion", audit["not_evaluated_gates"])

    def test_ranking_integrity_failure_blocks_audit(self):
        payload = payload_with_synthesis()
        payload["official_scores_and_ranks_unchanged"] = False
        audit = audit_research_payload(payload, self.gates)

        self.assertEqual(audit["automated_status"], "fail")
        self.assertIn("ranking_integrity", audit["failed_gates"])

    def test_exports_claim_level_human_review_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = export_research_audit(
                payload_with_synthesis(), "run-1", Path(directory), self.gates,
            )
            audit = json.loads(Path(artifacts["research_audit_json_path"]).read_text(encoding="utf-8"))
            with open(artifacts["research_human_review_csv_path"], encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            with open(artifacts["research_candidate_audit_csv_path"], encoding="utf-8", newline="") as file:
                candidates = list(csv.DictReader(file))

        self.assertEqual(audit["automated_status"], "pass")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["human_review_status"], "pending")
        self.assertEqual(rows[0]["accuracy_review"], "")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["sections_present"], "4")

    def test_finalizes_only_completed_human_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = export_research_audit(
                payload_with_synthesis(), "run-1", root, self.gates,
            )
            review_path = Path(artifacts["research_human_review_csv_path"])
            with review_path.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
                fields = rows[0].keys()
            for row in rows:
                row["accuracy_review"] = "pass"
                row["citation_support_review"] = "pass"
                row["human_review_status"] = "approved"
            with review_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

            decision = finalize_research_review(
                Path(artifacts["research_audit_json_path"]),
                review_path,
                "run-1",
                root,
            )
            with open(decision["research_release_csv_path"], encoding="utf-8", newline="") as file:
                releases = list(csv.DictReader(file))

        self.assertEqual(decision["human_review_decision"], "approved")
        self.assertEqual(decision["approved_row_count"], 4)
        self.assertEqual(decision["candidate_status_counts"], {"approved": 1, "not_ready": 1})
        self.assertEqual(releases[0]["release_status"], "approved")
        self.assertEqual(releases[0]["review_claim_count"], "4")
        self.assertEqual(releases[0]["unreviewed_claim_count"], "0")
        self.assertEqual(releases[1]["release_status"], "not_ready")

    def test_incomplete_review_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = export_research_audit(
                payload_with_synthesis(), "run-1", root, self.gates,
            )
            decision = finalize_research_review(
                Path(artifacts["research_audit_json_path"]),
                Path(artifacts["research_human_review_csv_path"]),
                "run-1",
                root,
            )

        self.assertEqual(decision["human_review_decision"], "incomplete")
        self.assertTrue(decision["pending_csv_rows"])

    def test_risk_sampling_is_reproducible_and_preserves_section_coverage(self):
        payload = payload_with_synthesis()
        citation = {"url": "https://www.sec.gov/filing", "content_hash": "a" * 64}
        extra = {
            "text": "Another straightforward supported fact.",
            "classification": "sourced_fact",
            "citations": [citation],
        }
        for section in ("business_overview", "growth_drivers", "risks", "recent_developments"):
            payload["outputs"][0]["synthesis"][section].append(extra)
        payload["outputs"][0]["synthesis"]["business_overview"][1] = {
            **extra,
            "text": "The evidence suggests a cautious revenue interpretation.",
            "classification": "interpretation",
        }
        gates = {
            **self.gates,
            "review_sampling": {
                "enabled": True,
                "medium_risk_sample_percent": 0,
                "low_risk_sample_percent": 0,
                "minimum_one_claim_per_section": True,
                "seed": "stable-test",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            artifacts = export_research_audit(payload, "run-1", Path(directory), gates)
            with open(artifacts["research_human_review_csv_path"], encoding="utf-8", newline="") as file:
                selected = list(csv.DictReader(file))
            with open(artifacts["research_claim_triage_csv_path"], encoding="utf-8", newline="") as file:
                triage = list(csv.DictReader(file))

        self.assertEqual(len(triage), 8)
        self.assertEqual(len(selected), 4)
        self.assertEqual(
            sum(row["review_selection_basis"] == "not_selected" for row in triage),
            4,
        )
        self.assertEqual({row["section"] for row in selected}, {
            "business_overview", "growth_drivers", "risks", "recent_developments",
        })
        interpretation = next(row for row in selected if row["classification"] == "interpretation")
        self.assertEqual(interpretation["review_risk_level"], "high")
        self.assertEqual(interpretation["review_selection_basis"], "mandatory_high_risk")
        self.assertEqual(artifacts["review_sampling"]["selected_claim_count"], 4)

    def test_finalization_rejects_a_tampered_sample_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = export_research_audit(
                payload_with_synthesis(), "run-1", root, self.gates,
            )
            review_path = Path(artifacts["research_human_review_csv_path"])
            with review_path.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
                fields = rows[0].keys()
            with review_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows[:-1])

            with self.assertRaisesRegex(ValueError, "does not match"):
                finalize_research_review(
                    Path(artifacts["research_audit_json_path"]),
                    review_path,
                    "run-1",
                    root,
                )


if __name__ == "__main__":
    unittest.main()
