from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.baseline_resolution import (
    export_baseline_resolution,
    load_forward_baseline,
    load_price_series,
    resolve_baseline,
    validated_config,
)


def _series(start: str, days: int, start_price: float, daily: float) -> list[dict]:
    first = date.fromisoformat(start)
    return [
        {"date": (first + timedelta(days=index)).isoformat(),
         "close": start_price * (1.0 + daily) ** index}
        for index in range(days)
    ]


def _candidate(ticker, classification, rank=1, benchmark="SPY"):
    return {
        "ticker": ticker, "company_name": f"{ticker} Co", "country": "US",
        "sector": "Industrials", "size_tier": "nano_cap",
        "classification": classification, "moonshot_rank": rank,
        "upside_score": 80.0, "risk_of_ruin_score": 20.0,
        "moonshot_confidence": 85.0, "market_risk_confidence": 100.0,
        "benchmark": benchmark, "baseline_as_of": "2026-01-01T00:00:00+00:00",
        "tracking_status": "pending",
        "horizons": {
            "1M": {"eligible_at": "2026-01-31T00:00:00+00:00", "status": "pending"},
            "3M": {"eligible_at": "2026-04-02T00:00:00+00:00", "status": "pending"},
        },
    }


def _baseline(candidates):
    return {
        "run_id": "RUN1", "baseline_id": "abc", "model_version": "v0.2",
        "baseline_as_of": "2026-01-01T00:00:00+00:00",
        "candidate_count": len(candidates), "candidates": candidates,
        "schema_version": 1,
    }


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        # Benchmark rises 10% over the month; winners beat it, losers trail.
        self.series = {
            "SPY": _series("2025-12-01", 200, 100.0, 0.0032),
            "WIN": _series("2025-12-01", 200, 10.0, 0.010),
            "LOSE": _series("2025-12-01", 200, 10.0, -0.004),
            "FLAT": _series("2025-12-01", 200, 10.0, 0.0032),
        }
        self.baseline = _baseline([
            _candidate("WIN", "priority_research", 1),
            _candidate("FLAT", "asymmetric_watch", 2),
            _candidate("LOSE", "low_upside_signal", 3),
        ])

    def test_resolves_eligible_horizons_and_leaves_later_ones_pending(self):
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10),
            config={"minimum_cohort_size": 1},
        )

        self.assertEqual(resolution["eligible_horizons"], ["1M"])
        self.assertEqual(resolution["pending_horizons"], ["3M"])
        win = next(c for c in resolution["candidates"] if c["ticker"] == "WIN")
        horizon = win["horizons"]["1M"]
        self.assertEqual(horizon["status"], "resolved")
        self.assertEqual(horizon["anchor_date"], "2026-01-01")
        self.assertEqual(horizon["final_date"], "2026-01-31")
        self.assertGreater(horizon["excess_return_percent"], 0)
        # Benchmark-relative: FLAT tracks SPY, so its excess is about zero.
        flat = next(c for c in resolution["candidates"] if c["ticker"] == "FLAT")
        self.assertAlmostEqual(flat["horizons"]["1M"]["excess_return_percent"], 0, places=4)
        self.assertEqual(win["horizons"]["3M"]["status"], "pending")
        self.assertNotIn("ticker_return_percent", win["horizons"]["3M"])

    def test_cohort_report_compares_selected_with_control(self):
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10),
            config={"minimum_cohort_size": 1},
        )
        report = resolution["horizons"]["1M"]

        self.assertTrue(report["comparable"])
        self.assertEqual(report["selected"]["resolved"], 2)
        self.assertEqual(report["control"]["resolved"], 1)
        self.assertEqual(report["selected"]["win_rate_percent"], 50.0)
        self.assertGreater(report["selected_minus_control"]["median_excess_percent"], 0)
        self.assertIn("priority_research", report["by_classification"])
        self.assertEqual(report["by_classification"]["low_upside_signal"]["resolved"], 1)

    def test_small_cohorts_are_marked_not_comparable(self):
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10),
            config={"minimum_cohort_size": 5},
        )
        self.assertFalse(resolution["horizons"]["1M"]["comparable"])

    def test_delisted_names_are_unresolved_not_dropped(self):
        series = dict(self.series)
        # Trades past the baseline date, then stops before the 1M horizon.
        series["LOSE"] = _series("2025-12-01", 45, 10.0, -0.01)  # stops 2026-01-14
        resolution = resolve_baseline(
            self.baseline, series, as_of=date(2026, 2, 10),
            config={"minimum_cohort_size": 1},
        )
        lose = next(c for c in resolution["candidates"] if c["ticker"] == "LOSE")
        horizon = lose["horizons"]["1M"]
        self.assertEqual(horizon["status"], "unresolved_stale")
        self.assertEqual(horizon["last_price_date"], "2026-01-14")
        self.assertIn("delisted", horizon["reason"])
        control = resolution["horizons"]["1M"]["control"]
        self.assertEqual(control["candidates"], 1)
        self.assertEqual(control["resolved"], 0)
        self.assertEqual(control["unresolved_stale"], 1)
        self.assertEqual(control["resolution_rate_percent"], 0.0)

    def test_missing_series_and_missing_benchmark_are_explained(self):
        resolution = resolve_baseline(
            _baseline([_candidate("GONE", "priority_research"),
                       _candidate("NOBM", "priority_research", benchmark="XIU.TO")]),
            {"NOBM": _series("2025-12-01", 200, 5.0, 0.002)},
            as_of=date(2026, 2, 10), config={"minimum_cohort_size": 1},
        )
        gone = next(c for c in resolution["candidates"] if c["ticker"] == "GONE")
        self.assertEqual(gone["horizons"]["1M"]["status"], "unresolved")
        self.assertIn("no cached price series", gone["horizons"]["1M"]["reason"])
        nobm = next(c for c in resolution["candidates"] if c["ticker"] == "NOBM")
        horizon = nobm["horizons"]["1M"]
        # The ticker return still resolves; only the comparison is withheld,
        # and the status says so rather than reading as a plain failure.
        self.assertEqual(horizon["status"], "resolved_without_benchmark")
        self.assertIsNotNone(horizon["ticker_return_percent"])
        self.assertNotIn("excess_return_percent", horizon)
        self.assertIn("--collect-prices", horizon["benchmark_note"])
        self.assertEqual(resolution["horizons"]["1M"]["selected"]["resolved"], 0)

    def test_returns_use_one_series_so_prices_stay_consistent(self):
        # Anchor and final both come from the same list; a later-refetched
        # series would be a separate resolution, never a mixed one.
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10),
            config={"minimum_cohort_size": 1},
        )
        win = next(c for c in resolution["candidates"] if c["ticker"] == "WIN")
        horizon = win["horizons"]["1M"]
        anchor = next(r for r in self.series["WIN"] if r["date"] == "2026-01-01")
        final = next(r for r in self.series["WIN"] if r["date"] == "2026-01-31")
        expected = (final["close"] / anchor["close"] - 1.0) * 100.0
        self.assertAlmostEqual(horizon["ticker_return_percent"], expected, places=4)

    def test_rejects_invalid_configuration(self):
        for override in (
            {"maximum_price_staleness_days": 0},
            {"minimum_cohort_size": 0},
            {"selected_classifications": []},
            {"selected_classifications": ["a"], "control_classifications": ["a"]},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    validated_config(override)


class IoTests(unittest.TestCase):
    def test_loads_baseline_and_price_series_from_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = _baseline([_candidate("AAA", "priority_research")])
            (root / "moonshot_forward_baseline_RUN1_v0.2-market-risk.json").write_text(
                json.dumps(baseline), encoding="utf-8",
            )
            self.assertEqual(load_forward_baseline(root, "RUN1")["run_id"], "RUN1")
            with self.assertRaises(FileNotFoundError):
                load_forward_baseline(root, "MISSING")

            cache = root / "cache"
            (cache / "prices").mkdir(parents=True)
            import hashlib
            digest = hashlib.sha256(b"AAA|1y").hexdigest()
            pd.DataFrame({
                "Date": pd.to_datetime(["2026-01-01", "2026-01-02"]),
                "Close": [10.0, 11.0],
            }).to_json(
                cache / "prices" / f"{digest}.json", orient="table",
                date_format="iso", index=False,
            )
            series, stats = load_price_series(cache, ["AAA", "NOPE"])
            self.assertEqual([row["date"] for row in series["AAA"]],
                             ["2026-01-01", "2026-01-02"])
            self.assertEqual(stats, {"loaded": 1, "missing": 1, "read_errors": 0})

    def test_exports_json_and_flat_csv(self):
        resolution = resolve_baseline(
            _baseline([_candidate("WIN", "priority_research")]),
            {"WIN": _series("2025-12-01", 200, 10.0, 0.01),
             "SPY": _series("2025-12-01", 200, 100.0, 0.003)},
            as_of=date(2026, 2, 10), config={"minimum_cohort_size": 1},
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = export_baseline_resolution(resolution, Path(directory))
            payload = json.loads(
                Path(paths["baseline_resolution_json_path"]).read_text("utf-8")
            )
            self.assertEqual(payload["run_id"], "RUN1")
            self.assertTrue(payload["limitations"])
            self.assertTrue(payload["official_scores_and_ranks_unchanged"])
            rows = list(csv_rows(paths["baseline_resolution_csv_path"]))
            self.assertEqual(len(rows), 1)  # only the eligible 1M horizon
            self.assertEqual(rows[0]["horizon"], "1M")
            self.assertEqual(rows[0]["ticker"], "WIN")
            self.assertEqual(rows[0]["status"], "resolved")
            leftovers = [p.name for p in Path(directory).iterdir()
                         if p.name.startswith(".")]
            self.assertEqual(leftovers, [])


def csv_rows(path):
    import csv as _csv
    with open(path, encoding="utf-8") as file:
        yield from _csv.DictReader(file)


if __name__ == "__main__":
    unittest.main()


class NeedsPricesTests(unittest.TestCase):
    def test_lists_only_series_that_fall_short_of_an_eligible_horizon(self):
        from src.baseline_resolution import tickers_needing_prices

        baseline = _baseline([
            _candidate("FRESH", "priority_research"),
            _candidate("SHORT", "low_upside_signal"),
            _candidate("GONE", "low_upside_signal"),
        ])
        series = {
            "FRESH": _series("2025-12-01", 200, 10.0, 0.001),   # reaches past 1M
            "SHORT": _series("2025-12-01", 30, 10.0, 0.001),    # stops 2025-12-30
            "SPY": _series("2025-12-01", 30, 100.0, 0.001),     # benchmark too short
        }
        needed = tickers_needing_prices(baseline, series, as_of=date(2026, 2, 10))

        self.assertEqual(sorted(needed), ["GONE", "SHORT", "SPY"])
        self.assertNotIn("FRESH", needed)
        # With every series current, nothing needs fetching.
        current = {**series, "SHORT": _series("2025-12-01", 200, 10.0, 0.001),
                   "GONE": _series("2025-12-01", 200, 10.0, 0.001),
                   "SPY": _series("2025-12-01", 200, 100.0, 0.001)}
        self.assertEqual(tickers_needing_prices(baseline, current, as_of=date(2026, 2, 10)), [])
        # Before any horizon is eligible there is nothing to fetch for.
        self.assertEqual(tickers_needing_prices(baseline, {}, as_of=date(2026, 1, 5)), [])

    def test_missing_benchmark_is_reported_as_its_own_status(self):
        resolution = resolve_baseline(
            _baseline([_candidate("WIN", "priority_research")]),
            {"WIN": _series("2025-12-01", 200, 10.0, 0.01)},
            as_of=date(2026, 2, 10), config={"minimum_cohort_size": 1},
        )
        horizon = resolution["candidates"][0]["horizons"]["1M"]
        self.assertEqual(horizon["status"], "resolved_without_benchmark")
        self.assertIsNotNone(horizon["ticker_return_percent"])
        selected = resolution["horizons"]["1M"]["selected"]
        self.assertEqual(selected["resolved"], 0)
        self.assertEqual(selected["resolved_without_benchmark"], 1)


class ModelComparisonTests(unittest.TestCase):
    """Frozen scores are compared on outcomes neither could have seen."""

    def setUp(self):
        self.series = {"SPY": _series("2025-12-01", 200, 100.0, 0.0)}
        candidates = []
        # Sixty names whose daily drift rises with their index, so a score
        # equal to the index ranks the outcomes perfectly.
        for index in range(60):
            ticker = f"T{index:02d}"
            self.series[ticker] = _series("2025-12-01", 200, 10.0, (index - 30) / 10000)
            row = _candidate(ticker, "v3_quintile_3", index + 1)
            row["score_v3"] = float(index)        # perfect ranking
            row["score_v3_1"] = float(-index)     # perfectly backwards
            row["score_v3_confidence"] = 90.0     # metadata, never compared
            candidates.append(row)
        # A name only one model scored must not enter the common comparison.
        lone = _candidate("LONE", "v3_quintile_3", 61)
        lone["score_v3"] = 100.0
        self.series["LONE"] = _series("2025-12-01", 200, 10.0, 0.01)
        candidates.append(lone)
        self.baseline = _baseline(candidates)
        self.config = {
            "minimum_cohort_size": 1,
            "selected_classifications": ["v3_quintile_5"],
            "control_classifications": ["v3_quintile_1"],
        }

    def _comparison(self, **overrides):
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10),
            config={**self.config, **overrides},
        )
        return resolution["horizons"]["1M"]["model_comparison"]

    def test_each_model_gets_its_own_ic_and_quintile_ladder(self):
        comparison = self._comparison()

        self.assertEqual(comparison["score_v3"]["resolved"], 61)
        self.assertEqual(comparison["score_v3_1"]["resolved"], 60)
        self.assertGreater(comparison["score_v3"]["information_coefficient"], 0.99)
        self.assertEqual(comparison["score_v3_1"]["information_coefficient"], -1.0)
        ladder = comparison["score_v3"]["quintiles"]
        self.assertEqual([q["quintile"] for q in ladder], [1, 2, 3, 4, 5])
        medians = [q["median_excess_percent"] for q in ladder]
        self.assertEqual(medians, sorted(medians))

    def test_common_comparison_uses_only_names_every_model_scored(self):
        common = self._comparison()["common"]

        self.assertEqual(common["resolved"], 60)
        self.assertEqual(common["information_coefficient"]["score_v3"], 1.0)
        self.assertEqual(common["information_coefficient"]["score_v3_1"], -1.0)

    def test_metadata_fields_are_not_mistaken_for_scores(self):
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10), config=self.config,
        )
        scores = resolution["candidates"][0]["scores"]
        self.assertEqual(set(scores), {"score_v3", "score_v3_1"})

    def test_too_few_names_reports_counts_without_a_correlation(self):
        comparison = self._comparison(minimum_comparison_size=100)

        self.assertEqual(comparison["score_v3"]["resolved"], 61)
        self.assertIsNone(comparison["score_v3"]["information_coefficient"])
        self.assertEqual(comparison["score_v3"]["quintiles"], [])

    def test_baselines_frozen_before_a_model_existed_compare_what_they_have(self):
        for row in self.baseline["candidates"]:
            row.pop("score_v3_1", None)
        comparison = self._comparison()

        self.assertIn("score_v3", comparison)
        self.assertNotIn("score_v3_1", comparison)
        self.assertNotIn("common", comparison)

    def test_moonshot_baselines_without_compared_scores_omit_the_section(self):
        for row in self.baseline["candidates"]:
            row.pop("score_v3", None)
            row.pop("score_v3_1", None)
        resolution = resolve_baseline(
            self.baseline, self.series, as_of=date(2026, 2, 10), config=self.config,
        )
        self.assertNotIn("model_comparison", resolution["horizons"]["1M"])
