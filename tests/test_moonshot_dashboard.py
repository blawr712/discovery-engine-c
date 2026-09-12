import json
from pathlib import Path
import tempfile
import unittest

from src.moonshot_dashboard import MoonshotDashboardStore


class MoonshotDashboardTests(unittest.TestCase):
    RUN_ID = "20260905T120000Z-test"

    def test_filters_candidates_and_exposes_calibration_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_artifacts(root)
            store = MoonshotDashboardStore(root)

            result = store.candidates(
                self.RUN_ID, classification="selected", country="US",
            )

            self.assertTrue(result["available"])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["ticker"], "AAA")
            self.assertEqual(result["rows"][0]["scenario_rank_range"], 3)
            self.assertEqual(result["calibration"]["gates_passed"], 1)
            self.assertEqual(result["forward_baseline"]["candidate_count"], 2)
            self.assertEqual(
                result["forward_baseline"]["horizons"]["1M"]["status"],
                "pending",
            )

    def test_candidate_exposes_factors_scenarios_and_forward_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_artifacts(root)
            store = MoonshotDashboardStore(root)

            result = store.candidate(self.RUN_ID, "aaa")

            self.assertEqual(result["candidate"]["ticker"], "AAA")
            self.assertEqual(result["scenarios"]["survival_first"]["rank"], 4)
            self.assertEqual(
                result["forward_baseline"]["tracking_status"], "pending"
            )
            self.assertEqual(
                result["explanation"]["strongest_upside_driver"],
                "Revenue growth: 90% of factor capacity",
            )

    def test_missing_analysis_is_available_as_an_empty_dashboard_state(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MoonshotDashboardStore(Path(directory)).candidates(
                self.RUN_ID
            )

        self.assertFalse(result["available"])
        self.assertEqual(result["rows"], [])

    def test_rejects_unsafe_run_ids(self):
        store = MoonshotDashboardStore(Path("unused"))

        with self.assertRaisesRegex(ValueError, "Invalid"):
            store.candidates("../manifest")

    def _write_artifacts(self, root):
        candidates = [
            self._candidate("AAA", "US", "priority_research", 1),
            self._candidate("BBB.V", "CA", "low_upside_signal", 2),
        ]
        analysis = {
            "run_id": self.RUN_ID,
            "model_version": "v0.2-market-risk",
            "analysis_warning": "Not a forecast.",
            "summary": {"candidate_count": 2, "classifications": {
                "priority_research": 1, "low_upside_signal": 1,
            }},
            "market_evidence_summary": {
                "coverage_percent": 100, "cross_section_comparable": True,
            },
            "candidates": candidates,
        }
        calibration = {
            "run_id": self.RUN_ID,
            "model_version": "v0.2-market-risk",
            "automated_status": "pass",
            "interpretation_warning": "Stability only.",
            "validation_gates": [{
                "name": "baseline_reproduction", "passed": True,
                "actual": 0, "required": "0 mismatches", "severity": "fail",
            }],
            "scenario_overlaps": {"survival_first": {
                "top_25": {"overlap_percent": 80},
            }},
            "rank_sensitivity": {
                "median_rank_range": 2, "p90_rank_range": 3,
                "maximum_rank_range": 3,
                "candidates": [{
                    "ticker": "AAA", "baseline_rank": 1,
                    "minimum_rank": 1, "maximum_rank": 4, "rank_range": 3,
                    "scenario_ranks": {"baseline": 1, "survival_first": 4},
                }],
            },
            "cohorts": {"country": {"US": {
                "total": 1, "selected": 1, "selection_rate_percent": 100,
            }}},
            "outliers": {},
            "candidate_explanations": [{
                "ticker": "AAA",
                "strongest_upside_driver": "Revenue growth: 90% of factor capacity",
                "largest_risk_driver": "Liquidity: 40% of factor capacity",
                "risk_warnings": ["extreme_volatility"],
            }],
            "scenarios": {
                "baseline": {"candidates": [self._scenario("AAA", 1)]},
                "survival_first": {"candidates": [self._scenario("AAA", 4)]},
            },
        }
        baseline = {
            "run_id": self.RUN_ID,
            "model_version": "v0.2-market-risk",
            "baseline_id": "baseline-1",
            "baseline_as_of": "2026-09-05T12:00:00+00:00",
            "candidate_count": 2,
            "excluded_candidates": [],
            "interpretation_warning": "Observation only.",
            "candidates": [{
                "ticker": row["ticker"], "tracking_status": "pending",
                "horizons": {"1M": {
                    "eligible_at": "2026-10-05T12:00:00+00:00",
                    "status": "pending",
                }},
            } for row in candidates],
        }
        self._write(root / f"moonshot_analysis_{self.RUN_ID}.json", analysis)
        self._write(
            root / f"moonshot_calibration_{self.RUN_ID}.json", calibration,
        )
        self._write(
            root / (
                f"moonshot_forward_baseline_{self.RUN_ID}_v0.2-market-risk.json"
            ),
            baseline,
        )

    @staticmethod
    def _candidate(ticker, country, classification, rank):
        factor = {
            "name": "revenue_growth", "label": "Revenue growth",
            "points": 9, "max_points": 10, "available": True,
            "applicable": True, "raw_value": .5, "explanation": "Strong growth.",
        }
        return {
            "ticker": ticker, "company_name": f"{ticker} Company",
            "country": country, "sector": "Technology",
            "size_tier": "nano_cap", "classification": classification,
            "moonshot_rank": rank, "upside_score": 80,
            "risk_of_ruin_score": 35, "moonshot_confidence": 90,
            "market_risk_confidence": 100,
            "average_dollar_volume_30d": 500_000,
            "annualized_volatility_percent": 90,
            "maximum_drawdown_percent": 45, "dilution_percent": 5,
            "reverse_split_count_1y": 0, "missing_inputs": [],
            "upside_factors": [factor], "risk_factors": [{
                **factor, "name": "liquidity", "label": "Liquidity",
                "points": 4,
            }],
        }

    @staticmethod
    def _scenario(ticker, rank):
        return {
            "ticker": ticker, "scenario_rank": rank, "rank_change": 1 - rank,
            "classification": "priority_research", "upside_score": 80,
            "risk_of_ruin_score": 35,
        }

    @staticmethod
    def _write(path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
