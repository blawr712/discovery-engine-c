from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from src.baseline_resolution import (
    available_baseline_kinds,
    load_forward_baseline,
    resolve_baseline,
)
from src.research_baseline import (
    build_research_baseline,
    export_research_baseline,
    validated_config,
)


CONFIG = {"top_n": 2, "quantiles": 5, "model_version": "v3-test",
          "horizon_days": {"1M": 30, "1Y": 365}}


def _queue():
    """Twenty v3-scored names plus one excluded, one v2 fallback, one official."""
    rows = [
        {"research_rank": index + 1, "ranking_basis": "score_v3",
         "ticker": f"V{index:02d}", "company_name": f"V{index} Co",
         "country": "US", "sector": "Industrials",
         "score_v3": 90.0 - index, "score_v3_confidence": 90.0,
         "score_v3_excluded": False, "score_v2": 50.0, "discovery_rank": 100 + index}
        for index in range(20)
    ]
    rows.append({"research_rank": 21, "ranking_basis": "score_v2", "ticker": "EXC",
                 "company_name": "Excluded Co", "country": "US", "sector": "Energy",
                 "score_v3": None, "score_v3_excluded": True, "score_v2": 40.0,
                 "score_v3_exclusion_reasons": "Top-quintile share dilution",
                 "discovery_rank": 200})
    rows.append({"research_rank": 22, "ranking_basis": "score_v2", "ticker": "FB1",
                 "company_name": "Fallback Co", "country": "CA", "sector": "Energy",
                 "score_v3": None, "score_v3_excluded": False, "score_v2": 30.0,
                 "discovery_rank": 300})
    rows.append({"research_rank": 23, "ranking_basis": "discovery_score",
                 "ticker": "OLD", "company_name": "Legacy Co", "country": "US",
                 "sector": "Energy", "score_v3": None, "score_v2": None,
                 "discovery_rank": 400})
    return rows


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.baseline = build_research_baseline(
            _queue(), "RUN1", "2026-09-18T15:27:22+00:00", CONFIG,
        )

    def test_every_queue_row_gets_exactly_one_cohort(self):
        counts = self.baseline["cohort_counts"]
        self.assertEqual(sum(counts.values()), self.baseline["candidate_count"])
        self.assertEqual(self.baseline["candidate_count"], 23)
        self.assertEqual(counts["queue_top_n"], 2)
        self.assertEqual(counts["v3_excluded"], 1)
        self.assertEqual(counts["v2_fallback"], 1)
        self.assertEqual(counts["official_fallback"], 1)
        tickers = [row["ticker"] for row in self.baseline["candidates"]]
        self.assertEqual(len(tickers), len(set(tickers)))

    def test_cohorts_follow_v3_score_order(self):
        by_ticker = {c["ticker"]: c["classification"] for c in self.baseline["candidates"]}
        self.assertEqual(by_ticker["V00"], "queue_top_n")   # highest v3
        self.assertEqual(by_ticker["V01"], "queue_top_n")
        self.assertEqual(by_ticker["V19"], "v3_quintile_1")  # lowest v3
        # The top-N carve-out comes out of the best quintile, which keeps
        # its remaining members.
        self.assertEqual(by_ticker["V02"], "v3_quintile_5")
        self.assertEqual(
            self.baseline["selected_classifications"], ["queue_top_n", "v3_quintile_5"],
        )
        self.assertEqual(
            self.baseline["control_classifications"], ["v3_quintile_1", "v3_excluded"],
        )

    def test_horizons_are_fixed_from_the_run_completion_date(self):
        candidate = self.baseline["candidates"][0]
        self.assertEqual(candidate["baseline_as_of"], "2026-09-18T15:27:22+00:00")
        self.assertTrue(candidate["horizons"]["1M"]["eligible_at"].startswith("2026-10-18"))
        self.assertTrue(candidate["horizons"]["1Y"]["eligible_at"].startswith("2027-09-18"))
        self.assertEqual(candidate["tracking_status"], "pending")
        self.assertEqual(candidate["benchmark"], "SPY")
        canadian = next(c for c in self.baseline["candidates"] if c["ticker"] == "FB1")
        self.assertEqual(canadian["benchmark"], "XIU.TO")

    def test_baseline_id_tracks_cohort_membership(self):
        same = build_research_baseline(
            _queue(), "RUN1", "2026-09-18T15:27:22+00:00", CONFIG,
        )
        self.assertEqual(same["baseline_id"], self.baseline["baseline_id"])
        shuffled = list(reversed(_queue()))
        self.assertEqual(
            build_research_baseline(
                shuffled, "RUN1", "2026-09-18T15:27:22+00:00", CONFIG,
            )["baseline_id"],
            self.baseline["baseline_id"],
        )
        different = build_research_baseline(
            _queue(), "RUN1", "2026-09-19T15:27:22+00:00", CONFIG,
        )
        self.assertNotEqual(different["baseline_id"], self.baseline["baseline_id"])

    def test_rejects_invalid_configuration(self):
        for override in ({"top_n": 0}, {"quantiles": 0},
                         {"horizon_days": {}}, {"horizon_days": {"1M": 0}}):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    validated_config({**CONFIG, **override})


class ExportAndResolveTests(unittest.TestCase):
    def test_export_is_immutable_and_resolver_reads_its_cohorts(self):
        baseline = build_research_baseline(
            _queue(), "RUN1", "2026-09-18T15:27:22+00:00", CONFIG,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = export_research_baseline(baseline, root)
            payload = json.loads(
                Path(paths["research_baseline_json_path"]).read_text("utf-8")
            )
            self.assertEqual(payload["kind"], "research")
            self.assertIn("not a backtest", payload["interpretation_warning"])

            # Re-freezing the same run refuses rather than overwriting.
            with self.assertRaises(FileExistsError):
                export_research_baseline(baseline, root)

            self.assertEqual(available_baseline_kinds(root, "RUN1"), ["research"])
            loaded = load_forward_baseline(root, "RUN1", kind="research")
            self.assertEqual(loaded["baseline_id"], baseline["baseline_id"])
            with self.assertRaises(FileNotFoundError):
                load_forward_baseline(root, "RUN1", kind="moonshot")

            # The resolver takes its cohort split from the baseline itself.
            series = {
                "SPY": [{"date": "2026-09-18", "close": 100.0},
                        {"date": "2026-10-18", "close": 100.0}],
                # Returns track v3 order, so cohorts should separate.
                **{
                    f"V{index:02d}": [
                        {"date": "2026-09-18", "close": 10.0},
                        {"date": "2026-10-18", "close": 10.0 * (1 + (20 - index) / 100)},
                    ]
                    for index in range(20)
                },
                "EXC": [{"date": "2026-09-18", "close": 10.0},
                        {"date": "2026-10-18", "close": 5.0}],
            }
            resolution = resolve_baseline(
                loaded, series, as_of=date(2026, 10, 19),
                config={"minimum_cohort_size": 1},
            )
            self.assertEqual(resolution["kind"], "research")
            report = resolution["horizons"]["1M"]
            self.assertEqual(report["selected"]["resolved"], 4)  # top 2 + quintile 5
            self.assertEqual(report["selected"]["win_rate_percent"], 100.0)
            self.assertGreater(
                report["selected"]["median_excess_percent"],
                report["control"]["median_excess_percent"],
            )
            # Control mixes the worst quintile with the excluded name.
            self.assertEqual(report["control"]["candidates"], 5)
            self.assertGreater(report["selected_minus_control"]["median_excess_percent"], 0)
            self.assertEqual(resolution["pending_horizons"], ["1Y"])


if __name__ == "__main__":
    unittest.main()


class BaselineVersionTests(unittest.TestCase):
    """A cohort is labelled with the model that produced its scores."""

    def test_default_version_comes_from_the_scoring_model(self):
        from src.config import SCORING_V3_CONFIG
        from src.research_baseline import validated_config

        self.assertEqual(
            validated_config({})["model_version"], SCORING_V3_CONFIG["model_version"],
        )
        self.assertEqual(SCORING_V3_CONFIG["model_version"], "v3.2-shadow-composite")

    def test_an_explicit_version_is_still_honoured(self):
        from src.research_baseline import validated_config

        self.assertEqual(validated_config({"model_version": "v9-test"})["model_version"], "v9-test")

    def test_the_challenger_is_frozen_beside_v3(self):
        from src.research_baseline import build_research_baseline

        queue = [{
            "ticker": "A", "ranking_basis": "score_v3", "score_v3": 80.0,
            "score_v3_challenger": 75.0, "research_rank": 1,
        }]
        baseline = build_research_baseline(queue, "RUN", "2026-11-16T12:00:00+00:00", {})
        self.assertEqual(baseline["candidates"][0]["score_v3_challenger"], 75.0)
