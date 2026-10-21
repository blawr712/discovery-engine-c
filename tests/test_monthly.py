from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.monthly import (
    STEP_ORDER,
    CycleLocked,
    build_cycle_report,
    default_cycle,
    discovery_quality,
    due_baselines,
    run_cycle,
    universe_source_age,
    validate_cycle,
)


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def _steps(calls, fail=(), run_id="20261120T120000Z-aaaaaaaa"):
    def make(name):
        def step(context):
            calls.append((name, context.run_id))
            if name in fail:
                raise SystemExit(f"{name} broke")
            if name == "discovery":
                return {"run_id": run_id, "detail": "3000 passed"}
            if name == "universe":
                context.warnings.append("TMX workbook is 50 days old")
            return {"detail": f"{name} ok"}
        return step
    return {name: make(name) for name in STEP_ORDER}


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_runs_every_step_in_order_and_completes(self):
        calls = []
        state = run_cycle("2026-11", _steps(calls), self.root, clock=Clock(), log=lambda _: None)

        self.assertEqual([name for name, _ in calls], list(STEP_ORDER))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["run_id"], "20261120T120000Z-aaaaaaaa")
        # Steps after discovery see its run.
        self.assertEqual(dict(calls)["freeze"], "20261120T120000Z-aaaaaaaa")
        self.assertEqual(state["warnings"], ["universe: TMX workbook is 50 days old"])
        self.assertFalse((self.root / "2026-11.lock").exists())

    def test_failed_discovery_blocks_dependents_but_not_resolution(self):
        calls = []
        state = run_cycle(
            "2026-11", _steps(calls, fail=("discovery",)), self.root,
            clock=Clock(), log=lambda _: None,
        )

        ran = [name for name, _ in calls]
        self.assertEqual(ran, ["universe", "discovery", "resolve"])
        self.assertEqual(state["status"], "completed_with_failures")
        for name in ("index", "moonshot", "freeze", "calibrate"):
            self.assertEqual(state["steps"][name]["status"], "blocked")
        self.assertEqual(state["steps"]["discovery"]["detail"], "discovery broke")

    def test_independent_failure_does_not_stop_later_steps(self):
        calls = []
        state = run_cycle(
            "2026-11", _steps(calls, fail=("moonshot",)), self.root,
            clock=Clock(), log=lambda _: None,
        )

        self.assertEqual(state["steps"]["moonshot"]["status"], "failed")
        self.assertEqual(state["steps"]["calibrate"]["status"], "blocked")
        self.assertEqual(state["steps"]["freeze"]["status"], "done")
        self.assertEqual(state["steps"]["resolve"]["status"], "done")

    def test_rerun_continues_from_where_it_stopped(self):
        first = []
        run_cycle("2026-11", _steps(first, fail=("moonshot",)), self.root,
                  clock=Clock(), log=lambda _: None)
        second = []
        state = run_cycle("2026-11", _steps(second), self.root,
                          clock=Clock(NOW + timedelta(hours=3)), log=lambda _: None)

        # Only the failed step and the one it blocked run again.
        self.assertEqual([name for name, _ in second], ["moonshot", "calibrate"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["run_id"], "20261120T120000Z-aaaaaaaa")

    def test_partial_outputs_survive_a_failure_and_accumulate(self):
        """Baselines resolved before a failure must still reach the report."""
        attempt = {"n": 0}

        def resolve(context):
            attempt["n"] += 1
            context.outputs["resolved"] = [f"baseline-{attempt['n']}"]
            if attempt["n"] == 1:
                raise RuntimeError("second baseline failed")
            return {"detail": "ok"}

        steps = _steps([])
        steps["resolve"] = resolve
        first = run_cycle("2026-11", steps, self.root, clock=Clock(), log=lambda _: None)
        self.assertEqual(first["steps"]["resolve"]["outputs"]["resolved"], ["baseline-1"])

        second = run_cycle("2026-11", steps, self.root, clock=Clock(), log=lambda _: None)
        self.assertEqual(
            second["steps"]["resolve"]["outputs"]["resolved"], ["baseline-1", "baseline-2"],
        )
        self.assertEqual(second["status"], "completed")

    def test_completed_cycle_is_not_repeated(self):
        run_cycle("2026-11", _steps([]), self.root, clock=Clock(), log=lambda _: None)
        calls = []
        state = run_cycle("2026-11", _steps(calls), self.root, clock=Clock(), log=lambda _: None)

        self.assertEqual(calls, [])
        self.assertEqual(state["status"], "completed")

    def test_unexpected_exceptions_are_isolated_like_system_exit(self):
        steps = _steps([])
        steps["index"] = lambda context: 1 / 0
        state = run_cycle("2026-11", steps, self.root, clock=Clock(), log=lambda _: None)

        self.assertEqual(state["steps"]["index"]["status"], "failed")
        self.assertIn("division", state["steps"]["index"]["detail"])
        self.assertEqual(state["steps"]["freeze"]["status"], "done")

    def test_interrupted_step_is_retried(self):
        steps = _steps([])

        def interrupted(context):
            raise KeyboardInterrupt

        steps["moonshot"] = interrupted
        with self.assertRaises(KeyboardInterrupt):
            run_cycle("2026-11", steps, self.root, clock=Clock(), log=lambda _: None)
        saved = json.loads((self.root / "2026-11.json").read_text("utf-8"))
        self.assertEqual(saved["steps"]["moonshot"]["status"], "running")
        self.assertFalse((self.root / "2026-11.lock").exists())

        calls = []
        state = run_cycle("2026-11", _steps(calls), self.root, clock=Clock(), log=lambda _: None)
        self.assertEqual([name for name, _ in calls], ["moonshot", "calibrate", "freeze", "resolve"])
        self.assertEqual(state["status"], "completed")

    def test_live_lock_refuses_a_second_runner(self):
        (self.root / "2026-11.lock").write_text(
            json.dumps({"pid": 1, "created_at": (NOW - timedelta(hours=2)).isoformat()}),
            encoding="utf-8",
        )
        with self.assertRaises(CycleLocked):
            run_cycle("2026-11", _steps([]), self.root, clock=Clock(), log=lambda _: None)

    def test_stale_lock_from_a_dead_process_is_replaced(self):
        (self.root / "2026-11.lock").write_text(
            json.dumps({"pid": 1, "created_at": (NOW - timedelta(days=3)).isoformat()}),
            encoding="utf-8",
        )
        state = run_cycle("2026-11", _steps([]), self.root, clock=Clock(), log=lambda _: None)
        self.assertEqual(state["status"], "completed")

    def test_missing_steps_are_rejected(self):
        steps = _steps([])
        steps.pop("resolve")
        with self.assertRaises(ValueError):
            run_cycle("2026-11", steps, self.root, clock=Clock(), log=lambda _: None)


class CycleLabelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_defaults_to_the_current_month(self):
        self.assertEqual(default_cycle(self.root, NOW), "2026-11")

    def test_recent_unfinished_cycle_is_continued_across_a_month_end(self):
        """A cycle started on the 30th must not become two cycles on the 1st."""
        (self.root / "2026-10.json").write_text(json.dumps({
            "cycle": "2026-10", "status": "in_progress",
            "updated_at": datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc).isoformat(),
        }), encoding="utf-8")

        self.assertEqual(
            default_cycle(self.root, datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)),
            "2026-10",
        )

    def test_old_or_completed_cycles_do_not_capture_the_default(self):
        (self.root / "2026-09.json").write_text(json.dumps({
            "cycle": "2026-09", "status": "completed_with_failures",
            "updated_at": (NOW - timedelta(days=40)).isoformat(),
        }), encoding="utf-8")
        (self.root / "2026-10.json").write_text(json.dumps({
            "cycle": "2026-10", "status": "completed",
            "updated_at": (NOW - timedelta(days=1)).isoformat(),
        }), encoding="utf-8")

        self.assertEqual(default_cycle(self.root, NOW), "2026-11")

    def test_labels_are_validated(self):
        self.assertEqual(validate_cycle("2026-11"), "2026-11")
        self.assertEqual(validate_cycle("2026-11-rerun"), "2026-11-rerun")
        for bad in ("2026-13", "26-11", "2026/11", "../2026-11", ""):
            with self.assertRaises(ValueError):
                validate_cycle(bad)


class DueBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _baseline(self, prefix, run_id, anchor):
        horizons = {
            label: {"eligible_at": (anchor + timedelta(days=days)).isoformat(), "status": "pending"}
            for label, days in (("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365))
        }
        (self.root / f"{prefix}_{run_id}_v1.json").write_text(json.dumps({
            "run_id": run_id, "candidates": [{"ticker": "A", "horizons": horizons}],
        }), encoding="utf-8")

    def test_newly_matured_horizons_are_due_exactly_once(self):
        run = "20260918T144343Z-8ab9f4fc"
        self._baseline("research_forward_baseline", run, datetime(2026, 9, 18, tzinfo=timezone.utc))

        self.assertEqual(due_baselines(self.root, NOW), [
            {"run_id": run, "kind": "research", "horizons": ["1M"]},
        ])

        (self.root / f"baseline_resolution_research_{run}.json").write_text(
            json.dumps({"eligible_horizons": ["1M"]}), encoding="utf-8")
        self.assertEqual(due_baselines(self.root, NOW), [])

        later = datetime(2026, 12, 19, tzinfo=timezone.utc)
        self.assertEqual(due_baselines(self.root, later)[0]["horizons"], ["3M"])

    def test_nothing_matured_is_not_due(self):
        self._baseline("moonshot_forward_baseline", "20261120T120000Z-bbbbbbbb", NOW)
        self.assertEqual(due_baselines(self.root, NOW), [])

    def test_legacy_moonshot_resolution_name_is_recognised(self):
        run = "20260823T133945Z-4e30d9d9"
        self._baseline("moonshot_forward_baseline", run, datetime(2026, 9, 5, tzinfo=timezone.utc))
        (self.root / f"baseline_resolution_{run}.json").write_text(
            json.dumps({"eligible_horizons": ["1M"]}), encoding="utf-8")

        self.assertEqual(due_baselines(self.root, NOW), [])

    def test_unreadable_files_are_skipped(self):
        (self.root / "research_forward_baseline_20261019T223433Z-f13f71e5_v1.json").write_text(
            "{not json", encoding="utf-8")
        self.assertEqual(due_baselines(self.root, NOW), [])


class DiscoveryQualityTests(unittest.TestCase):
    def test_a_handful_of_errors_passes(self):
        rows = [{"status": "OK"}] * 3000 + [{"status": "FILTERED"}] * 3400 + [{"status": "ERROR"}]
        ok, detail = discovery_quality(rows)
        self.assertTrue(ok)
        self.assertEqual(detail, "3000 passed, 1 provider errors of 6401")

    def test_a_rate_limit_episode_blocks_freezing(self):
        rows = [{"status": "OK"}] * 900 + [{"status": "ERROR"}] * 100
        ok, detail = discovery_quality(rows)
        self.assertFalse(ok)
        self.assertIn("10.0% errors", detail)
        self.assertIn("rerun --monthly", detail)

    def test_an_empty_run_never_passes(self):
        self.assertFalse(discovery_quality([])[0])


class UniverseAndReportTests(unittest.TestCase):
    def test_tmx_source_age(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe_ca_auto.csv"
            path.write_text("ticker,source_date\nA.TO,2026-09-30\nB.TO,2026-09-30\n", encoding="utf-8")
            date, age = universe_source_age(path, NOW)
            self.assertEqual(date, "2026-09-30")
            self.assertEqual(age.days, 51)
            self.assertEqual(universe_source_age(Path(directory) / "missing.csv", NOW), (None, None))

    def test_report_lists_steps_warnings_and_matured_horizons(self):
        state = {
            "cycle": "2026-11", "status": "completed", "attempts": 1,
            "run_id": "RUN", "warnings": ["universe: TMX workbook is 50 days old"],
            "steps": {name: {"status": "done", "detail": f"{name} ok"} for name in STEP_ORDER},
        }
        report = build_cycle_report(state, [{
            "run_id": "OLD", "kind": "research", "horizons": ["1M"],
            "summary": {"1M": {
                "selected": {"median_excess_percent": -2.0, "win_rate_percent": 40.8},
                "control": {"median_excess_percent": -19.5, "win_rate_percent": 12.0},
            }},
        }])

        self.assertIn("# Monthly cycle 2026-11", report)
        self.assertIn("| freeze | done | freeze ok |", report)
        self.assertIn("TMX workbook is 50 days old", report)
        self.assertIn("| research `OLD` | 1M | -2.00% | -19.50% | 40.80% | 12.00% |", report)
        self.assertNotIn("Model head to head", report)

    def test_report_includes_the_model_head_to_head_when_measured(self):
        state = {
            "cycle": "2026-12", "status": "completed", "attempts": 1, "run_id": "RUN",
            "warnings": [],
            "steps": {name: {"status": "done"} for name in STEP_ORDER},
        }
        report = build_cycle_report(state, [{
            "run_id": "NOV", "kind": "research", "horizons": ["1M"],
            "summary": {"1M": {
                "selected": {}, "control": {},
                "head_to_head": {"resolved": 1180, "score_v3": 0.081, "score_v3_1": 0.094},
            }},
        }])

        self.assertIn("## Model head to head", report)
        self.assertIn("research `NOV` 1M, 1180 names: score_v3 0.081, score_v3_1 0.094", report)
        self.assertIn("not a forecast", report)


if __name__ == "__main__":
    unittest.main()
