import json
from pathlib import Path
import tempfile
import unittest

from src.research_ranking import (
    build_research_queue,
    export_research_queue,
    validated_config,
)


def _breakdown(**factors):
    return json.dumps({
        name: {"points": points, "max_points": 10.0, "available": True,
               "explanation": f"{name} note"}
        for name, points in factors.items()
    })


def _results():
    return [
        {"ticker": "V3A", "status": "OK", "country": "US", "discovery_score": 40,
         "score_v3": 80.0, "score_v3_rank": 1, "score_v2": 90.0, "score_v2_rank": 1,
         "score_v3_breakdown": _breakdown(pit_sales_yield=9, volatility=2, momentum_long=5),
         "fundamentals_status": "collected"},
        {"ticker": "V3B", "status": "OK", "country": "US", "discovery_score": 90,
         "score_v3": 60.0, "score_v3_rank": 2, "score_v2": 10.0, "score_v2_rank": 4},
        {"ticker": "V2ONLY", "status": "OK", "country": "CA", "discovery_score": 70,
         "score_v3": None, "score_v3_confidence": 25.0, "score_v2": 55.0, "score_v2_rank": 2,
         "fundamentals_status": "not_applicable"},
        {"ticker": "EXCL", "status": "OK", "country": "US", "discovery_score": 85,
         "score_v3": None, "score_v3_excluded": True,
         "score_v3_exclusion_reasons": "Top-quintile share dilution; Top-quintile revenue growth",
         "score_v2": 70.0, "score_v2_rank": 3},
        {"ticker": "LEGACY", "status": "OK", "country": "US", "discovery_score": 60},
        {"ticker": "FILTERED", "status": "FILTERED", "discovery_score": 0, "score_v3": 99},
    ]


class QueueTests(unittest.TestCase):
    def test_orders_primary_then_fallback_then_official(self):
        queue, summary = build_research_queue(_results())

        self.assertEqual(
            [(row["ticker"], row["ranking_basis"]) for row in queue],
            [
                ("V3A", "score_v3"), ("V3B", "score_v3"),
                ("EXCL", "score_v2"), ("V2ONLY", "score_v2"),
                ("LEGACY", "discovery_score"),
            ],
        )
        self.assertEqual([row["research_rank"] for row in queue], [1, 2, 3, 4, 5])
        self.assertEqual(queue[0]["ranking_tier"], "primary")
        self.assertEqual(queue[2]["ranking_tier"], "fallback")
        self.assertEqual(queue[4]["ranking_tier"], "final")
        # Official provenance rank is preserved alongside the research rank.
        self.assertEqual(queue[0]["discovery_rank"], 5)
        self.assertEqual(queue[1]["discovery_rank"], 1)
        self.assertEqual(summary["by_basis"], {"discovery_score": 1, "score_v2": 2, "score_v3": 2})
        self.assertEqual(summary["excluded_by_primary"], 1)
        self.assertEqual(summary["exclusion_reasons"], {
            "Top-quintile revenue growth": 1, "Top-quintile share dilution": 1,
        })
        self.assertNotIn("FILTERED", [row["ticker"] for row in queue])

    def test_reports_drivers_from_the_ranking_basis_breakdown(self):
        queue, _ = build_research_queue(_results())
        first = queue[0]
        self.assertTrue(first["strongest_drivers"].startswith("pit_sales_yield: pit_sales_yield note"))
        self.assertTrue(first["weakest_drivers"].startswith("volatility: volatility note"))
        self.assertEqual(queue[1]["strongest_drivers"], "")

    def test_exports_top_n_csv_and_full_json(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = export_research_queue(
                _results(), "RUN1", Path(directory), {"top_stocks": 2},
            )
            csv_text = Path(paths["research_queue_csv_path"]).read_text("utf-8")
            self.assertEqual(csv_text.count("\n"), 3)  # header + 2 rows
            self.assertIn("research_rank,ranking_basis", csv_text)
            payload = json.loads(Path(paths["research_queue_json_path"]).read_text("utf-8"))
            self.assertEqual(payload["run_id"], "RUN1")
            self.assertEqual(len(payload["queue"]), 5)
            self.assertIn("not a return forecast", payload["summary"]["note"])
            self.assertEqual(
                [p.name for p in Path(directory).iterdir() if p.name.startswith(".")], [],
            )

    def test_rejects_invalid_configuration(self):
        for override in (
            {"primary_model": ""},
            {"top_stocks": 0},
            {"packet_source": "magic"},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    validated_config(override)

    def test_configured_defaults(self):
        config = validated_config({})
        self.assertEqual(config["primary_model"], "score_v3")
        self.assertEqual(config["fallback_model"], "score_v2")
        self.assertEqual(config["packet_source"], "research_queue")


if __name__ == "__main__":
    unittest.main()
