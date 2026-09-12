from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.run_state import (
    RunState,
    build_run_fingerprint,
    load_saved_run,
    record_moonshot_analysis,
    record_moonshot_calibration,
    record_recalibration,
    record_research_packets,
    record_research_audit,
    record_research_acceptance,
    summarize_results,
)


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.clock = MutableClock()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_resumes_compatible_incomplete_run(self):
        state = RunState.start_or_resume(
            self.root,
            "same-fingerprint",
            2,
            clock=self.clock,
        )
        state.record_result(
            0,
            {"ticker": "ONE", "status": "OK", "discovery_score": 50},
        )

        resumed = RunState.start_or_resume(
            self.root,
            "same-fingerprint",
            2,
            clock=self.clock,
        )

        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.run_id, state.run_id)
        self.assertEqual(resumed.load_results()[0]["ticker"], "ONE")

    def test_does_not_resume_incompatible_or_completed_run(self):
        first = RunState.start_or_resume(
            self.root,
            "first",
            1,
            clock=self.clock,
        )
        first.complete(
            [{"ticker": "ONE", "status": "OK"}],
            "report.csv",
        )

        second = RunState.start_or_resume(
            self.root,
            "different",
            1,
            clock=self.clock,
        )

        self.assertFalse(second.resumed)
        self.assertNotEqual(second.run_id, first.run_id)

    def test_excludes_error_checkpoints_when_configured_for_retry(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            2,
            clock=self.clock,
        )
        state.record_result(
            0,
            {"ticker": "FAILED", "status": "ERROR"},
        )
        state.record_result(
            1,
            {"ticker": "FILTERED", "status": "FILTERED"},
        )

        self.assertEqual(list(state.load_results()), [1])
        self.assertEqual(len(state.load_results(retry_errors=False)), 2)

    def test_replacing_error_checkpoint_does_not_inflate_progress(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            1,
            clock=self.clock,
        )
        state.record_result(0, {"ticker": "ONE", "status": "ERROR"})
        state.record_result(0, {"ticker": "ONE", "status": "OK"})

        self.assertEqual(state.manifest["completed_count"], 1)
        self.assertEqual(state.load_results()[0]["status"], "OK")

    def test_complete_writes_structured_manifest_summary(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            2,
            clock=self.clock,
        )
        self.clock.value += timedelta(seconds=12.3456)
        results = [
            {
                "ticker": "ONE",
                "status": "OK",
                "country": "CA",
                "exchange": "TSX",
            },
            {
                "ticker": "TWO",
                "status": "ERROR",
                "reason_flags": "Metadata: TimeoutError",
            },
        ]

        state.complete(
            results,
            "final.csv",
            candidate_report_path="candidates.csv",
            analytics_path="analytics.json",
            calibration_csv_path="calibration.csv",
            calibration_json_path="calibration.json",
        )

        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)
        self.assertEqual(manifest["status"], "completed_with_errors")
        self.assertEqual(manifest["duration_seconds"], 12.346)
        self.assertEqual(manifest["report_path"], "final.csv")
        self.assertEqual(manifest["candidate_report_path"], "candidates.csv")
        self.assertEqual(manifest["analytics_path"], "analytics.json")
        self.assertEqual(
            manifest["calibration_csv_path"],
            "calibration.csv",
        )
        self.assertEqual(
            manifest["calibration_json_path"],
            "calibration.json",
        )
        self.assertEqual(manifest["summary"]["statuses"], {"ERROR": 1, "OK": 1})
        self.assertEqual(manifest["summary"]["failure_stages"], {"Metadata": 1})

    def test_resumes_completed_run_with_errors_only(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            2,
            clock=self.clock,
        )
        state.record_result(0, {"ticker": "GOOD", "status": "OK"})
        state.record_result(1, {"ticker": "RETRY", "status": "ERROR"})
        state.complete(
            [
                {"ticker": "GOOD", "status": "OK"},
                {"ticker": "RETRY", "status": "ERROR"},
            ],
            "partial.csv",
        )

        resumed = RunState.start_or_resume(
            self.root,
            "fingerprint",
            2,
            clock=self.clock,
        )

        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.run_id, state.run_id)
        self.assertEqual(resumed.manifest["status"], "in_progress")
        self.assertEqual(resumed.manifest["resume_count"], 1)
        self.assertEqual(list(resumed.load_results()), [0])

    def test_fingerprint_is_stable_and_input_sensitive(self):
        universe = [{"ticker": "ONE", "country": "US"}]
        first = build_run_fingerprint(universe, {"weight": 1}, {"workers": 5})
        reordered = build_run_fingerprint(
            [{"country": "US", "ticker": "ONE"}],
            {"weight": 1},
            {"workers": 5},
        )
        changed = build_run_fingerprint(
            universe,
            {"weight": 2},
            {"workers": 5},
        )

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_summary_counts_unknown_dimensions(self):
        summary = summarize_results([{"ticker": "ONE", "status": "FAILED"}])

        self.assertEqual(summary["countries"], {"UNKNOWN": 1})
        self.assertEqual(summary["exchanges"], {"UNKNOWN": 1})

    def test_loads_complete_saved_run_in_checkpoint_order(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            2,
            clock=self.clock,
        )
        state.record_result(1, {"ticker": "TWO", "status": "OK"})
        state.record_result(0, {"ticker": "ONE", "status": "OK"})
        state.complete(
            [
                {"ticker": "ONE", "status": "OK"},
                {"ticker": "TWO", "status": "OK"},
            ],
            "report.csv",
        )

        manifest, results = load_saved_run(self.root, state.run_id)

        self.assertEqual(manifest["status"], "complete")
        self.assertEqual([row["ticker"] for row in results], ["ONE", "TWO"])

    def test_rejects_unsafe_or_incomplete_saved_run(self):
        with self.assertRaisesRegex(ValueError, "invalid characters"):
            load_saved_run(self.root, "../outside")

        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            1,
            clock=self.clock,
        )
        with self.assertRaisesRegex(ValueError, "not complete"):
            load_saved_run(self.root, state.run_id)

    def test_records_recalibration_provenance(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            1,
            clock=self.clock,
        )
        state.record_result(0, {"ticker": "ONE", "status": "OK"})
        state.complete([{"ticker": "ONE", "status": "OK"}], "report.csv")

        record_recalibration(
            self.root,
            state.run_id,
            {
                "calibration_csv_path": "new.csv",
                "calibration_json_path": "new.json",
                "research_artifacts": {"review_report_path": "review.csv"},
            },
            clock=self.clock,
        )
        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)

        self.assertEqual(manifest["calibration_csv_path"], "new.csv")
        self.assertEqual(
            manifest["recalibration"]["research_artifacts"][
                "review_report_path"
            ],
            "review.csv",
        )

    def test_records_research_packet_artifacts(self):
        state = RunState.start_or_resume(
            self.root,
            "fingerprint",
            1,
            clock=self.clock,
        )
        state.record_result(0, {"ticker": "ONE", "status": "OK"})
        state.complete([{"ticker": "ONE", "status": "OK"}], "report.csv")

        record_research_packets(
            self.root,
            state.run_id,
            {"research_packets_json_path": "packets.json"},
            clock=self.clock,
        )
        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)

        self.assertEqual(
            manifest["research_packet_artifacts"][
                "research_packets_json_path"
            ],
            "packets.json",
        )

    def test_records_moonshot_analysis_provenance(self):
        state = RunState.start_or_resume(
            self.root, "fingerprint", 1, clock=self.clock,
        )
        state.record_result(0, {"ticker": "ONE", "status": "OK"})
        state.complete([{"ticker": "ONE", "status": "OK"}], "report.csv")

        record_moonshot_analysis(
            self.root,
            state.run_id,
            {
                "model_version": "v0.2-market-risk",
                "moonshot_analysis_json_path": "moonshot.json",
                "official_scores_and_ranks_unchanged": True,
            },
            clock=self.clock,
        )
        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)

        self.assertEqual(
            manifest["moonshot_artifacts"]["model_version"], "v0.2-market-risk",
        )
        self.assertTrue(
            manifest["moonshot_artifacts"]["official_scores_and_ranks_unchanged"]
        )

    def test_records_moonshot_calibration_provenance(self):
        state = RunState.start_or_resume(
            self.root, "fingerprint", 1, clock=self.clock,
        )
        state.record_result(0, {"ticker": "ONE", "status": "OK"})
        state.complete([{"ticker": "ONE", "status": "OK"}], "report.csv")

        record_moonshot_calibration(
            self.root,
            state.run_id,
            {
                "automated_status": "pass",
                "baseline_id": "baseline-1",
                "moonshot_calibration_json_path": "calibration.json",
            },
            clock=self.clock,
        )
        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)

        artifacts = manifest["moonshot_calibration_artifacts"]
        self.assertEqual(artifacts["automated_status"], "pass")
        self.assertEqual(artifacts["baseline_id"], "baseline-1")

    def test_records_research_audit_provenance(self):
        state = RunState.start_or_resume(self.root, "fingerprint", 1, clock=self.clock)
        state.record_result(0, {"ticker": "ONE", "status": "OK"})
        state.complete([{"ticker": "ONE", "status": "OK"}], "report.csv")

        record_research_audit(
            self.root,
            state.run_id,
            {"research_audit_json_path": "audit.json", "automated_status": "pass"},
            clock=self.clock,
        )
        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)

        self.assertEqual(
            manifest["research_audit_artifacts"]["research_audit_json_path"],
            "audit.json",
        )

        record_research_acceptance(
            self.root,
            state.run_id,
            {"human_review_decision": "approved"},
            clock=self.clock,
        )
        with state.manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)
        self.assertEqual(
            manifest["research_acceptance"]["human_review_decision"],
            "approved",
        )


if __name__ == "__main__":
    unittest.main()
