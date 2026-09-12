import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from src.evidence import (
    CanadianIssuerManifestProvider,
    EvidenceDocument,
    HttpCache,
    ManifestEvidenceProvider,
    SecEdgarProvider,
    attach_evidence,
    collect_evidence,
    validate_document,
)


class FixtureFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"ticker": "TEST", "cik_str": 1234}}).encode()
        if "submissions" in url:
            return json.dumps({
                "filings": {"recent": {
                    "accessionNumber": ["0000001234-26-000001", "0000001234-26-000002"],
                    "filingDate": ["2026-07-01", "2026-06-01"],
                    "form": ["10-Q", "S-8"],
                    "primaryDocument": ["test-10q.htm", "test-s8.htm"],
                    "primaryDocDescription": ["Quarterly report", "Registration"],
                }}
            }).encode()
        return b"<html>primary filing</html>"


class BrokenProvider:
    name = "broken"

    def collect(self, packet):
        raise RuntimeError("source unavailable")


class EvidenceTests(unittest.TestCase):
    def test_sec_provider_collects_primary_filing_and_reuses_raw_cache(self):
        fetcher = FixtureFetcher()
        with tempfile.TemporaryDirectory() as directory:
            cache = HttpCache(Path(directory), fetcher=fetcher)
            provider = SecEdgarProvider(
                "Discovery Engine research@example.com",
                cache=cache,
                max_age_days=1000,
            )
            first = provider.collect({"ticker": "TEST", "country": "US"})
            second = provider.collect({"ticker": "TEST", "country": "US"})

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].source_type, "regulatory_filing")
        self.assertEqual(first[0].excerpt, "primary filing")
        self.assertIn("/Archives/edgar/data/1234/", first[0].url)
        self.assertEqual(first[0].content_hash, hashlib.sha256(b"<html>primary filing</html>").hexdigest())
        self.assertEqual(len(fetcher.calls), 3)
        self.assertEqual(first[0].content_hash, second[0].content_hash)
        self.assertIn("@", fetcher.calls[0][1]["User-Agent"])
        self.assertEqual(cache.stats.misses, 3)
        self.assertEqual(cache.stats.hits, 2)
        self.assertEqual(cache.stats.expired, 0)
        self.assertEqual(cache.stats.read_errors, 0)

    def test_collection_isolates_failure_and_attaches_valid_evidence(self):
        document = EvidenceDocument(
            ticker="TEST",
            url="https://www.sec.gov/example",
            title="Filing",
            publisher="SEC",
            published_at="2026-07-01",
            retrieved_at="2026-07-31T00:00:00+00:00",
            source_type="regulatory_filing",
            content_hash="a" * 64,
            quality_priority=100,
        )

        class GoodProvider:
            name = "good"
            def collect(self, packet):
                return [document, document]

        packets = [{"ticker": "TEST", "source_policy": {"external_sources_attached": False}}]
        result = collect_evidence(packets, [BrokenProvider(), GoodProvider()])
        attach_evidence(packets, result)

        self.assertEqual(result["document_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["cache"], {
            "expired": 0, "hits": 0, "misses": 0, "read_errors": 0,
        })
        self.assertTrue(packets[0]["source_policy"]["external_sources_attached"])
        self.assertEqual(len(packets[0]["evidence_documents"]), 1)

    def test_curated_manifest_requires_allowed_https_source_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(json.dumps({"TEST": [{
                "url": "https://www.sec.gov/filing",
                "title": "Primary filing",
                "publisher": "SEC",
                "published_at": "2026-07-01",
                "source_type": "regulatory_filing",
                "content_hash": "b" * 64,
            }]}), encoding="utf-8")
            documents = ManifestEvidenceProvider(path).collect({"ticker": "TEST"})

        self.assertEqual(validate_document(documents[0]), [])

    def test_sec_user_agent_must_include_contact(self):
        with self.assertRaises(ValueError):
            SecEdgarProvider("Discovery Engine")

    def test_canadian_manifest_fetches_only_curated_allowlisted_urls(self):
        raw = b"<html><body>Canadian issuer results</body></html>"
        calls = []

        def fetcher(url, headers):
            calls.append((url, headers))
            return raw

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "canadian.json"
            path.write_text(json.dumps({
                "version": 1,
                "allowed_domains": ["issuer.example.com"],
                "companies": {"TEST.TO": [{
                    "url": "https://issuer.example.com/results",
                    "title": "Quarterly results",
                    "publisher": "Test Issuer",
                    "published_at": "2026-07-01",
                    "source_type": "earnings_release",
                    "expected_content_hash": hashlib.sha256(raw).hexdigest(),
                }]},
            }), encoding="utf-8")
            provider = CanadianIssuerManifestProvider(
                path,
                "Discovery Engine research@example.com",
                cache=HttpCache(root / "cache", fetcher=fetcher),
            )
            documents = provider.collect({"ticker": "TEST.TO", "country": "CA"})
            us_documents = provider.collect({"ticker": "TEST.TO", "country": "US"})
            evidence = collect_evidence(
                [{"ticker": "TEST.TO", "country": "CA"}],
                [provider],
            )

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].excerpt, "Canadian issuer results")
        self.assertEqual(us_documents, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(evidence["document_count"], 1)
        self.assertEqual(
            evidence["companies"][0]["provider_document_counts"],
            {"canadian_issuer_manifest": 1},
        )

    def test_canadian_manifest_rejects_unapproved_domain_and_hash_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "canadian.json"
            path.write_text(json.dumps({
                "version": 1,
                "allowed_domains": ["issuer.example.com"],
                "companies": {"TEST.TO": [{
                    "url": "https://other.example.com/results",
                    "title": "Results",
                    "publisher": "Issuer",
                    "published_at": "2026-07-01",
                    "source_type": "earnings_release",
                }]},
            }), encoding="utf-8")
            provider = CanadianIssuerManifestProvider(
                path,
                "Discovery Engine research@example.com",
                cache=HttpCache(root / "cache", fetcher=lambda *_: b"changed"),
            )
            with self.assertRaisesRegex(ValueError, "not allowed"):
                provider.collect({"ticker": "TEST.TO", "country": "CA"})
            value = json.loads(path.read_text(encoding="utf-8"))
            value["companies"]["TEST.TO"][0]["url"] = "https://issuer.example.com/results"
            value["companies"]["TEST.TO"][0]["expected_content_hash"] = "a" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            provider = CanadianIssuerManifestProvider(
                path,
                "Discovery Engine research@example.com",
                cache=HttpCache(root / "other-cache", fetcher=lambda *_: b"changed"),
            )
            with self.assertRaisesRegex(ValueError, "hash changed"):
                provider.collect({"ticker": "TEST.TO", "country": "CA"})

    def test_cache_counts_expiry_and_corrupt_entry_recovery(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        current = [now]
        fetcher = FixtureFetcher()
        with tempfile.TemporaryDirectory() as directory:
            cache = HttpCache(
                Path(directory),
                ttl_hours=1,
                clock=lambda: current[0],
                fetcher=fetcher,
            )
            url = "https://www.sec.gov/files/company_tickers.json"
            cache.get(url, {})
            current[0] = now + timedelta(hours=2)
            cache.get(url, {})
            cache_path = next(Path(directory).glob("*.json"))
            cache_path.write_text("not-json", encoding="utf-8")
            cache.get(url, {})

        self.assertEqual(cache.stats.misses, 1)
        self.assertEqual(cache.stats.expired, 1)
        self.assertEqual(cache.stats.read_errors, 1)
        self.assertEqual(cache.stats.hits, 0)


if __name__ == "__main__":
    unittest.main()
