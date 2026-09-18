import json
from pathlib import Path
import tempfile
import unittest

from src.data_sources.sec_xbrl_source import (
    COMPANY_FACTS_URL,
    TICKER_MAP_URL,
    SecXbrlSource,
    compact_company_facts,
)


def _payload():
    return {
        "cik": 1234,
        "entityName": "Test Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01",
                     "form": "10-Q", "fy": 2025, "fp": "Q1", "val": 100},
                    {"start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01",
                     "form": "10-Q", "fy": 2025, "fp": "Q1", "val": 100},
                    {"end": "2025-03-31", "filed": "2025-05-01", "val": 5},
                    {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01",
                     "form": "10-Q", "fy": 2025, "fp": "Q2", "val": "bad"},
                ]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                    {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01",
                     "form": "10-Q", "fy": 2025, "fp": "Q2", "val": 110},
                ]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                    {"end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "val": 900},
                    {"start": "2025-01-01", "end": "2025-06-30", "filed": "2025-08-01", "val": 1},
                ]}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                    {"end": "2025-07-31", "filed": "2025-08-01", "form": "10-Q", "val": 5000},
                ]}},
            },
        },
    }


class CompactionTests(unittest.TestCase):
    def test_keeps_configured_concepts_with_alias_provenance(self):
        extract = compact_company_facts(_payload(), "test")

        self.assertEqual(extract["ticker"], "TEST")
        self.assertEqual(extract["cik"], "1234")
        revenue = extract["facts"]["revenue"]
        self.assertEqual([row["val"] for row in revenue], [100.0, 110.0])
        self.assertEqual(revenue[0]["tag"], "us-gaap:Revenues")
        self.assertEqual(
            revenue[1]["tag"],
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        )
        self.assertEqual(extract["tags_found"]["revenue"], [
            "us-gaap:Revenues",
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        ])
        # Instant facts reject duration rows and vice versa.
        self.assertEqual([row["val"] for row in extract["facts"]["cash"]], [900.0])
        self.assertIsNone(extract["facts"]["cash"][0]["start"])
        self.assertEqual(extract["facts"]["shares_outstanding"][0]["val"], 5000.0)
        self.assertEqual(extract["facts"]["net_income"], [])

    def test_handles_malformed_payloads(self):
        self.assertEqual(compact_company_facts({}, "X")["facts"]["revenue"], [])
        self.assertEqual(compact_company_facts([], "X")["cik"], "")


class FakeFetcher:
    def __init__(self):
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append((url, headers["User-Agent"]))
        if url == TICKER_MAP_URL:
            return json.dumps({
                "0": {"cik_str": 1234, "ticker": "TEST", "title": "Test Corp"},
                "1": {"cik_str": 99, "ticker": "OTHER", "title": "Other"},
            }).encode("utf-8")
        if url == COMPANY_FACTS_URL.format(cik="0000001234"):
            return json.dumps(_payload()).encode("utf-8")
        raise AssertionError(f"unexpected url {url}")


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fetcher = FakeFetcher()
        self.now = [1_000_000.0]
        self.source = SecXbrlSource(
            user_agent="Discovery Engine test@example.com",
            cache_directory=Path(self.directory.name),
            ttl_hours=1,
            request_interval_seconds=0,
            fetcher=self.fetcher,
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_requires_contact_user_agent(self):
        with self.assertRaises(ValueError):
            SecXbrlSource(user_agent="anonymous", cache_directory=Path(self.directory.name))

    def test_maps_tickers_and_caches_compact_extracts(self):
        extract = self.source.get_company_facts("test")

        self.assertEqual(extract["ticker"], "TEST")
        self.assertEqual([row["val"] for row in extract["facts"]["revenue"]], [100.0, 110.0])
        self.assertEqual(len(self.fetcher.urls), 2)
        self.assertTrue(all(agent.endswith("test@example.com") for _, agent in self.fetcher.urls))
        self.assertEqual(self.source.stats.requests, 2)

        again = self.source.get_company_facts("TEST")
        self.assertEqual(again["facts"], extract["facts"])
        self.assertEqual(len(self.fetcher.urls), 2)
        self.assertEqual(self.source.stats.hits, 1)
        # Cached files hold the compact extract, not the raw response.
        cached_files = [p for p in Path(self.directory.name).iterdir() if p.suffix == ".json"]
        self.assertEqual(len(cached_files), 2)
        self.assertTrue(all(p.stat().st_size < 4000 for p in cached_files))

    def test_expired_cache_refetches(self):
        self.source.get_company_facts("TEST")
        self.now[0] += 2 * 3600
        self.source.get_company_facts("TEST")
        # The ticker map stays in memory; only the extract expires on disk.
        self.assertEqual(self.source.stats.expired, 1)
        self.assertEqual(len(self.fetcher.urls), 3)

    def test_unmapped_ticker_raises_lookup_error(self):
        self.assertIsNone(self.source.cik_for("NOPE"))
        with self.assertRaises(LookupError):
            self.source.get_company_facts("NOPE")


if __name__ == "__main__":
    unittest.main()
