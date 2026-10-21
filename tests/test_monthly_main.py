"""Wiring of the monthly cycle in main.py, with every provider call patched out."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import main


RUN_ID = "20261120T120000Z-aaaaaaaa"


class MonthlyWiringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.runs = root / "runs"
        self.exports = root / "exports"
        self.base = root / "base"
        universe = self.base / "data" / "raw" / "universe"
        universe.mkdir(parents=True)
        (universe / "universe_ca_auto.csv").write_text(
            "ticker,source_date\nA.TO,2026-10-31\n", encoding="utf-8",
        )
        self.calls = []
        self.results = [{"status": "OK"}] * 300 + [{"status": "FILTERED"}] * 300

        def record(name):
            return lambda *args, **kwargs: self.calls.append((name, args, kwargs))

        patches = [
            mock.patch.object(main, "RUN_DIR", self.runs),
            mock.patch.object(main, "OUTPUT_DIR", self.exports),
            mock.patch.object(main, "BASE_DIR", self.base),
            mock.patch.object(main, "run_discovery", side_effect=self._discovery),
            mock.patch.object(main, "load_saved_run", side_effect=lambda *_: (
                {"status": "completed_with_errors"}, self.results)),
            mock.patch.object(main, "index_history_run", side_effect=record("index")),
            mock.patch.object(main, "analyze_moonshot_run", side_effect=record("moonshot")),
            mock.patch.object(main, "calibrate_saved_moonshot", side_effect=record("calibrate")),
            mock.patch.object(main, "freeze_saved_research_baseline", side_effect=record("freeze")),
            mock.patch.object(main, "resolve_saved_baseline", side_effect=record("resolve")),
            mock.patch.object(main, "build_market_data_source", return_value=object()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def _discovery(self, cycle=None, **_):
        self.calls.append(("discovery", (), {"cycle": cycle}))
        return RUN_ID

    def test_full_cycle_runs_every_step_and_writes_a_report(self):
        main.run_monthly(cycle="2026-11", skip_universe=True)

        names = [name for name, _, _ in self.calls]
        self.assertEqual(names, ["discovery", "index", "moonshot", "calibrate", "freeze"])
        # The cycle label reaches the run, so its fingerprint is this cycle's.
        self.assertEqual(self.calls[0][2]["cycle"], "2026-11")
        self.assertEqual(self.calls[1][1], (RUN_ID,))
        report = (self.exports / "monthly_cycle_2026-11.md").read_text("utf-8")
        self.assertIn("Status: **completed**", report)
        self.assertIn("rebuild skipped (--skip-universe); TMX data as of 2026-10-31", report)
        self.assertTrue((self.runs / "monthly" / "2026-11.json").is_file())

    def test_incomplete_run_is_never_frozen(self):
        self.results = [{"status": "OK"}] * 500 + [{"status": "ERROR"}] * 100

        with self.assertRaises(SystemExit) as raised:
            main.run_monthly(cycle="2026-11", skip_universe=True)

        names = [name for name, _, _ in self.calls]
        self.assertNotIn("freeze", names)
        self.assertNotIn("moonshot", names)
        self.assertIn("finished with failures", str(raised.exception))
        report = (self.exports / "monthly_cycle_2026-11.md").read_text("utf-8")
        self.assertIn("| freeze | blocked |", report)

    def test_existing_research_baseline_is_not_refrozen(self):
        self.exports.mkdir(parents=True)
        (self.exports / f"research_forward_baseline_{RUN_ID}_v3.0-shadow-composite.json").write_text(
            '{"candidates": [{"ticker": "A"}]}', encoding="utf-8",
        )

        main.run_monthly(cycle="2026-11", skip_universe=True)

        self.assertNotIn("freeze", [name for name, _, _ in self.calls])
        state = (self.runs / "monthly" / "2026-11.json").read_text("utf-8")
        self.assertIn("research baseline already frozen", state)

    def test_stale_tmx_workbook_is_reported(self):
        (self.base / "data" / "raw" / "universe" / "universe_ca_auto.csv").write_text(
            "ticker,source_date\nA.TO,2026-07-31\n", encoding="utf-8",
        )

        main.run_monthly(cycle="2026-11", skip_universe=True)

        report = (self.exports / "monthly_cycle_2026-11.md").read_text("utf-8")
        self.assertIn("TMX workbook data is from 2026-07-31", report)

    def test_bad_cycle_label_is_rejected_before_anything_runs(self):
        with self.assertRaises(SystemExit):
            main.run_monthly(cycle="november")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
