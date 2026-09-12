import json
from pathlib import Path
import tempfile
import unittest

from src.research import (
    ResearchCache,
    ResearchRunner,
    build_research_packets,
    build_research_prompt,
    export_research_packets,
)


def factor(name, points, maximum, quality="fresh"):
    return json.dumps({
        name: {
            "name": name,
            "points": points,
            "max_points": maximum,
            "available": True,
            "applicable": True,
            "data_quality": quality,
            "explanation": f"{name} explanation",
        }
    })


def synthesis(citations=None):
    claims = []
    if citations is not None:
        claims = [{
            "text": "The company operates a software business.",
            "classification": "interpretation" if not citations else "sourced_fact",
            "citations": citations,
        }]
    return {
        "business_overview": claims,
        "growth_drivers": [],
        "risks": [],
        "recent_developments": [],
        "unanswered_questions": ["Can margins expand?"],
    }


class CountingProvider:
    def __init__(self):
        self.calls = 0

    def generate(self, packet, prompt):
        self.calls += 1
        return synthesis()


class FailingProvider:
    def generate(self, packet, prompt):
        if packet["ticker"] == "BAD":
            raise RuntimeError("provider failure")
        return synthesis()


class UnverifiedCitationProvider:
    def generate(self, packet, prompt):
        return synthesis([{"url": "https://example.com", "content_hash": "x" * 64}])


class EvidenceRequiredProvider(CountingProvider):
    requires_evidence = True
    cache_identity = "fixture:model-1"


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            {
                "ticker": "ONE",
                "company_name": "One Co",
                "status": "OK",
                "country": "US",
                "sector": "Technology",
                "industry": "Software",
                "market_cap": 100_000_000,
                "factor_breakdown": factor("trend", 10, 10),
                "fundamental_breakdown": factor("profitability", 8, 8),
            },
            {
                "ticker": "TWO",
                "company_name": "Two Co",
                "status": "OK",
                "country": "CA",
                "sector": "Industrials",
                "factor_breakdown": factor("trend", 0, 10),
                "fundamental_breakdown": factor("profitability", 0, 8),
            },
        ]
        self.calibration = {
            "rows": [
                {
                    "ticker": "ONE",
                    "official_rank": 2,
                    "discovery_score": 60,
                    "technical_percentile": 90,
                    "core_fundamental_score": 100,
                    "core_fundamental_confidence": 100,
                    "peer_fundamental_percentile": 90,
                    "fundamental_peer_group": "US / Technology",
                    "outlier_flags": "",
                    "experimental_selected_rank": 1,
                    "experimental_selected_score": 80,
                },
                {
                    "ticker": "TWO",
                    "official_rank": 1,
                    "discovery_score": 70,
                    "technical_percentile": 100,
                    "core_fundamental_score": 0,
                    "core_fundamental_confidence": 100,
                    "peer_fundamental_percentile": 10,
                    "fundamental_peer_group": "CA / Industrials",
                    "outlier_flags": "",
                    "experimental_selected_rank": 2,
                    "experimental_selected_score": 70,
                },
            ],
            "summary": {
                "research_ranking_config": {
                    "selected_scenario": "selected",
                },
                "scenario_acceptance": {
                    "selected": {"status": "pass", "failures": []},
                },
                "coverage_neutral_model": {
                    "core_factors": ["profitability"],
                },
            },
        }

    def test_builds_ranked_deterministic_packets_without_changing_rank(self):
        packets, metadata = build_research_packets(
            self.results,
            1,
            calibration=self.calibration,
        )

        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["ticker"], "ONE")
        self.assertEqual(packets[0]["selected_rank"], 1)
        self.assertEqual(packets[0]["official_rank"], 2)
        self.assertTrue(
            packets[0]["source_policy"]["synthesis_must_not_change_rank"]
        )
        self.assertEqual(metadata["selected_scenario"], "selected")

    def test_balanced_research_selection_interleaves_ranked_countries(self):
        packets, metadata = build_research_packets(
            self.results,
            2,
            calibration=self.calibration,
            balanced_per_country=1,
        )

        self.assertEqual([row["ticker"] for row in packets], ["TWO", "ONE"])
        self.assertEqual(metadata["balanced_per_country"], 1)
        self.assertEqual([row["selected_rank"] for row in packets], [2, 1])

    def test_prompt_forbids_rank_changes_and_requires_citations(self):
        packets, _ = build_research_packets(
            self.results,
            1,
            calibration=self.calibration,
        )
        prompt = build_research_prompt(packets[0], "v-test")

        self.assertIn("Do not change", prompt)
        self.assertIn("citations", prompt)
        self.assertIn("v-test", prompt)

    def test_runner_reuses_cache_and_isolates_provider_failures(self):
        packets, _ = build_research_packets(
            self.results,
            2,
            calibration=self.calibration,
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = CountingProvider()
            cache = ResearchCache(Path(directory), "v-test")
            runner = ResearchRunner(provider, cache, "v-test")
            first = runner.run(packets)
            second = runner.run(packets)

            self.assertEqual(provider.calls, 2)
            self.assertTrue(all(row["status"] == "complete" for row in first))
            self.assertTrue(all(row["cached"] for row in second))

            failing_packets = [dict(packets[0], ticker="BAD"), packets[1]]
            failed = ResearchRunner(
                FailingProvider(),
                ResearchCache(Path(directory) / "fail", "v-test"),
                "v-test",
            ).run(failing_packets)

        self.assertEqual(failed[0]["status"], "error")
        self.assertEqual(failed[1]["status"], "complete")

    def test_cache_identity_ignores_evidence_retrieval_time(self):
        packets, _ = build_research_packets(
            self.results,
            1,
            calibration=self.calibration,
        )
        packets[0]["evidence_documents"] = [{
            "url": "https://www.sec.gov/filing",
            "content_hash": "a" * 64,
            "retrieved_at": "2026-08-01T00:00:00+00:00",
        }]
        with tempfile.TemporaryDirectory() as directory:
            cache = ResearchCache(Path(directory), "v-test", "fixture:model")
            cache.put(packets[0], {"cached": True})
            refreshed = json.loads(json.dumps(packets[0]))
            refreshed["evidence_documents"][0]["retrieved_at"] = (
                "2026-08-06T00:00:00+00:00"
            )

            self.assertEqual(cache.get(refreshed), {"cached": True})

    def test_runner_rejects_citations_not_present_in_packet_evidence(self):
        packets, _ = build_research_packets(self.results, 1, calibration=self.calibration)
        result = ResearchRunner(UnverifiedCitationProvider()).run(packets)

        self.assertEqual(result[0]["status"], "error")
        self.assertIn("does not match attached evidence", result[0]["error"])

    def test_packet_only_mode_and_exports(self):
        packets, metadata = build_research_packets(
            self.results,
            2,
            calibration=self.calibration,
        )
        outputs = ResearchRunner(provider=None).run(packets)
        with tempfile.TemporaryDirectory() as directory:
            artifacts = export_research_packets(
                packets,
                outputs,
                metadata,
                "run-1",
                Path(directory),
            )
            with open(
                artifacts["research_packets_json_path"],
                "r",
                encoding="utf-8",
            ) as file:
                payload = json.load(file)
            markdown = Path(
                artifacts["research_packets_markdown_path"]
            ).read_text(encoding="utf-8")
            briefs = Path(
                artifacts["research_briefs_markdown_path"]
            ).read_text(encoding="utf-8")

        self.assertEqual(payload["official_scores_and_ranks_unchanged"], True)
        self.assertEqual(artifacts["synthesis_statuses"], {"packet_only": 2})
        self.assertIn("Research questions", markdown)
        self.assertEqual(markdown, briefs)
        self.assertEqual(artifacts["validated_claim_count"], 0)
        self.assertEqual(artifacts["synthesis_usage"]["total_tokens"], 0)

    def test_sourced_claim_requires_attached_citation(self):
        packets, _ = build_research_packets(self.results, 1, calibration=self.calibration)

        with self.assertRaisesRegex(ValueError, "requires a citation"):
            from src.research import validate_synthesis
            invalid = synthesis([])
            invalid["business_overview"][0]["classification"] = "sourced_fact"
            validate_synthesis(packets[0], invalid)

    def test_interpretation_also_requires_attached_citation(self):
        packets, _ = build_research_packets(self.results, 1, calibration=self.calibration)
        from src.research import validate_synthesis

        with self.assertRaisesRegex(ValueError, "Every research claim"):
            validate_synthesis(packets[0], synthesis([]))

    def test_evidence_required_provider_skips_packet_without_sources(self):
        packets, _ = build_research_packets(self.results, 1, calibration=self.calibration)
        provider = EvidenceRequiredProvider()
        result = ResearchRunner(provider).run(packets)

        self.assertEqual(result[0]["status"], "skipped_no_evidence")
        self.assertEqual(provider.calls, 0)

    def test_valid_sourced_claim_reports_validation_metrics(self):
        packets, _ = build_research_packets(self.results, 1, calibration=self.calibration)
        citation = {
            "url": "https://www.sec.gov/filing",
            "content_hash": "a" * 64,
        }
        packets[0]["evidence_documents"] = [citation]

        from src.research import validate_synthesis
        validation = validate_synthesis(packets[0], synthesis([citation]))

        self.assertEqual(validation["sourced_claim_count"], 1)
        self.assertEqual(validation["cited_claim_count"], 1)
        self.assertEqual(validation["citation_count"], 1)
        self.assertEqual(validation["status"], "pass")


if __name__ == "__main__":
    unittest.main()
