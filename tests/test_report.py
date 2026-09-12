import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.report import (
    export_candidate_report,
    export_experimental_research_reports,
)


def breakdown(name, points, maximum, explanation):
    return json.dumps(
        {
            name: {
                "name": name,
                "points": points,
                "max_points": maximum,
                "available": True,
                "explanation": explanation,
            }
        }
    )


class CandidateReportTests(unittest.TestCase):
    def test_exports_ranked_successful_candidates_only(self):
        results = [
            {
                "ticker": "LOW",
                "status": "OK",
                "discovery_score": 40,
                "fundamental_score_normalized": 90,
                "factor_breakdown": breakdown(
                    "trend", 0, 10, "Weak trend"
                ),
                "fundamental_breakdown": breakdown(
                    "growth", 10, 10, "Strong growth"
                ),
            },
            {
                "ticker": "HIGH",
                "status": "OK",
                "discovery_score": 70,
                "fundamental_score_normalized": 50,
                "factor_breakdown": breakdown(
                    "trend", 10, 10, "Strong trend"
                ),
                "fundamental_breakdown": breakdown(
                    "growth", 0, 10, "Weak growth"
                ),
            },
            {
                "ticker": "FILTERED",
                "status": "FILTERED",
                "discovery_score": 0,
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = export_candidate_report(
                results,
                "run-1",
                Path(directory),
                top_n=10,
            )
            frame = pd.read_csv(path)

        self.assertEqual(frame["ticker"].tolist(), ["HIGH", "LOW"])
        self.assertEqual(frame["rank"].tolist(), [1, 2])
        self.assertEqual(frame["fundamental_rank"].tolist(), [2, 1])
        self.assertIn("Strong trend", frame.iloc[0]["strongest_signals"])
        self.assertIn("Weak growth", frame.iloc[0]["principal_risks"])

    def test_applies_top_candidate_limit(self):
        results = [
            {
                "ticker": str(index),
                "status": "OK",
                "discovery_score": index,
            }
            for index in range(5)
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = export_candidate_report(
                results,
                "run-2",
                Path(directory),
                top_n=2,
            )
            frame = pd.read_csv(path)

        self.assertEqual(frame["ticker"].astype(str).tolist(), ["4", "3"])

    def test_exports_passing_scenario_and_top_review_reports(self):
        results = [
            {
                "ticker": "UP",
                "status": "OK",
                "industry": "Software",
                "reason_flags": "Strong trend",
                "fundamental_breakdown": breakdown(
                    "profitability", 8, 8, "Strong margin"
                ),
            },
            {
                "ticker": "DOWN",
                "status": "OK",
                "industry": "Manufacturing",
                "fundamental_breakdown": breakdown(
                    "profitability", 0, 8, "Weak margin"
                ),
            },
        ]
        calibration = {
            "rows": [
                {
                    "ticker": "UP",
                    "company_name": "Up Co",
                    "country": "US",
                    "sector": "Technology",
                    "official_rank": 2,
                    "discovery_score": 50,
                    "technical_percentile": 50,
                    "core_fundamental_score": 100,
                    "core_fundamental_confidence": 100,
                    "peer_fundamental_percentile": 90,
                    "fundamental_peer_group": "US / Technology",
                    "outlier_flags": "",
                    "experimental_blend_score": 80,
                    "experimental_blend_rank": 1,
                    "experimental_aggressive_score": 70,
                    "experimental_aggressive_rank": 2,
                },
                {
                    "ticker": "DOWN",
                    "company_name": "Down Co",
                    "country": "CA",
                    "sector": "Industrials",
                    "official_rank": 1,
                    "discovery_score": 60,
                    "technical_percentile": 100,
                    "core_fundamental_score": 0,
                    "core_fundamental_confidence": 100,
                    "peer_fundamental_percentile": 10,
                    "fundamental_peer_group": "CA / Industrials",
                    "outlier_flags": "",
                    "experimental_blend_score": 20,
                    "experimental_blend_rank": 2,
                    "experimental_aggressive_score": 75,
                    "experimental_aggressive_rank": 1,
                },
            ],
            "summary": {
                "coverage_neutral_model": {
                    "core_factors": ["profitability"],
                },
                "blend_scenarios": {
                    "blend": {
                        "technical_weight": 0.8,
                        "fundamental_weight": 0.2,
                    },
                    "aggressive": {
                        "technical_weight": 0.7,
                        "fundamental_weight": 0.3,
                    },
                },
                "scenario_acceptance": {
                    "blend": {"status": "pass", "failures": []},
                    "aggressive": {"status": "pass", "failures": []},
                },
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            artifacts = export_experimental_research_reports(
                results,
                calibration,
                "run-3",
                Path(directory),
                review_n=1,
            )
            scenario = pd.read_csv(
                artifacts["scenario_report_paths"]["blend"]
            )
            review = pd.read_csv(artifacts["review_report_path"])
            with open(
                artifacts["scenario_summary_path"],
                "r",
                encoding="utf-8",
            ) as file:
                summary = json.load(file)
            comparison = pd.read_csv(
                artifacts["scenario_comparison_path"]
            )
            selected = pd.read_csv(
                artifacts["selected_research_report_path"]
            )
            with open(
                artifacts["research_decision_path"],
                "r",
                encoding="utf-8",
            ) as file:
                decision = json.load(file)

        self.assertEqual(scenario["ticker"].tolist(), ["UP", "DOWN"])
        self.assertEqual(set(review["ticker"]), {"UP", "DOWN"})
        self.assertIn("Promoted 1 positions", scenario.iloc[0]["movement_explanation"])
        self.assertIn("profitability", scenario.iloc[0]["core_strengths"])
        self.assertEqual(summary["composition"]["blend"]["top_25"]["companies"], 2)
        self.assertEqual(artifacts["selected_research_scenario"], "blend")
        self.assertEqual(selected["ticker"].tolist(), ["UP", "DOWN"])
        self.assertIn("consensus_rank", comparison.columns)
        self.assertTrue(decision["official_discovery_score_unchanged"])
        self.assertEqual(decision["selected_scenario"], "blend")


if __name__ == "__main__":
    unittest.main()
