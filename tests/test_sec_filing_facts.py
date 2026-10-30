from datetime import date
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from src.data_sources.sec_filing_facts import (
    FILING_INDEX_URL,
    INDEX_URL,
    SecFilingFactsSource,
    compact_xbrl_index,
    instance_name,
    merge_facts,
    parse_instance,
    reported_filing_dates,
)
from src.data_sources.sec_xbrl_source import CONCEPTS, COMPANY_FACTS_URL, SecXbrlSource


INSTANCE = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
  xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full"
  xmlns:dei="http://xbrl.sec.gov/dei/2025"
  xmlns:abc="http://abc.example/2026">
  <xbrli:context id="fy"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="py"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2024-07-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="ye"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="seg"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="ifrs-full:SegmentsAxis">abc:Cloud</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
  <xbrli:unit id="eps"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
  <ifrs-full:Revenue contextRef="fy" unitRef="usd" decimals="-3">200065000</ifrs-full:Revenue>
  <ifrs-full:Revenue contextRef="fy" unitRef="usd" decimals="-3">200065000</ifrs-full:Revenue>
  <ifrs-full:Revenue contextRef="py" unitRef="usd" decimals="-3">236692000</ifrs-full:Revenue>
  <ifrs-full:Revenue contextRef="seg" unitRef="usd" decimals="-3">90000000</ifrs-full:Revenue>
  <ifrs-full:Revenue contextRef="ye" unitRef="usd" decimals="-3">1</ifrs-full:Revenue>
  <ifrs-full:ProfitLoss contextRef="fy" unitRef="usd" decimals="-3">-81094000</ifrs-full:ProfitLoss>
  <ifrs-full:CashAndCashEquivalents contextRef="ye" unitRef="usd" decimals="-3">10431000</ifrs-full:CashAndCashEquivalents>
  <ifrs-full:Borrowings contextRef="ye" unitRef="usd" xsi:nil="true"/>
  <ifrs-full:WeightedAverageShares contextRef="fy" unitRef="shares" decimals="0">33244861</ifrs-full:WeightedAverageShares>
  <ifrs-full:BasicEarningsLossPerShare contextRef="fy" unitRef="eps" decimals="2">-2.44</ifrs-full:BasicEarningsLossPerShare>
  <dei:EntityCommonStockSharesOutstanding contextRef="ye" unitRef="shares" decimals="0">33338932</dei:EntityCommonStockSharesOutstanding>
  <abc:Revenue contextRef="fy" unitRef="usd" decimals="-3">5</abc:Revenue>
</xbrli:xbrl>
"""

INDEX_TEXT = """Description: XBRL INDEX
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
1753368|Sangoma Technologies Corp|40-F|2026-09-28|edgar/data/1753368/0001753368-26-000011.txt
1753368|Sangoma Technologies Corp|40-F|2025-09-17|edgar/data/1753368/0001753368-25-000020.txt
1753368|Sangoma Technologies Corp|40-F/A|2026-10-01|edgar/data/1753368/0001753368-26-000012.txt
1753368|Sangoma Technologies Corp|S-8|2026-10-02|edgar/data/1753368/0001753368-26-000013.txt
99|Other Corp|10-Q|2026-08-01|edgar/data/99/0000000099-26-000001.txt
"""


def _extract():
    """A company-facts extract that holds the 2025 40-F but only the 2026 cover."""
    return {
        "cik": "1753368",
        "ticker": "SANG",
        "tags_found": {"revenue": ["ifrs-full:Revenue"]},
        "facts": {
            "revenue": [{
                "start": "2024-07-01", "end": "2025-06-30", "filed": "2025-09-17",
                "form": "40-F", "fy": 2025, "fp": "FY", "val": 236692000.0,
                "tag": "ifrs-full:Revenue", "unit": "USD",
            }],
            "shares_outstanding": [{
                "start": None, "end": "2026-06-30", "filed": "2026-09-28", "form": "40-F",
                "fy": 2026, "fp": "FY", "val": 33338932.0,
                "tag": "dei:EntityCommonStockSharesOutstanding", "unit": "shares",
            }],
        },
    }


class FakeFetcher:
    def __init__(self, broken=False):
        self.urls = []
        self.broken = broken

    def __call__(self, url, headers):
        self.urls.append(url)
        if url.endswith("xbrl.gz"):
            return gzip.compress(INDEX_TEXT.encode("latin-1"))
        if url.endswith("index.json"):
            if self.broken:
                raise OSError("network down")
            return json.dumps({"directory": {"item": [
                {"name": "FilingSummary.xml"}, {"name": "sang-20260630_cal.xml"},
                {"name": "sang-20260630.htm"}, {"name": "sang-20260630_htm.xml"},
            ]}}).encode("utf-8")
        if url.endswith("sang-20260630_htm.xml"):
            return INSTANCE
        raise AssertionError(f"unexpected request {url}")


def _source(directory, fetcher):
    return SecFilingFactsSource(
        user_agent="Test research@example.com",
        cache_directory=Path(directory),
        since=date(2026, 7, 1),
        fetcher=fetcher,
        today=lambda: date(2026, 10, 29),
    )


class ParseInstanceTests(unittest.TestCase):
    def test_reads_undimensioned_facts_in_company_facts_shape(self):
        facts = parse_instance(INSTANCE, CONCEPTS, "40-F", "2026-09-28")
        revenue = facts["revenue"]
        self.assertEqual(
            [(row["start"], row["end"], row["val"]) for row in revenue],
            [("2025-07-01", "2026-06-30", 200065000.0), ("2024-07-01", "2025-06-30", 236692000.0)],
        )
        self.assertEqual(revenue[0]["filed"], "2026-09-28")
        self.assertEqual(revenue[0]["form"], "40-F")
        self.assertEqual(revenue[0]["tag"], "ifrs-full:Revenue")
        self.assertEqual(revenue[0]["unit"], "USD")
        self.assertEqual(facts["net_income"][0]["val"], -81094000.0)
        self.assertEqual(facts["cash"][0]["start"], None)
        self.assertEqual(facts["weighted_average_shares"][0]["unit"], "shares")
        self.assertEqual(facts["shares_outstanding"][0]["tag"], "dei:EntityCommonStockSharesOutstanding")

    def test_skips_segments_nil_values_wrong_kinds_and_other_taxonomies(self):
        facts = parse_instance(INSTANCE, CONCEPTS, "40-F", "2026-09-28")
        self.assertNotIn(90000000.0, [row["val"] for row in facts["revenue"]])  # segment
        self.assertNotIn(1.0, [row["val"] for row in facts["revenue"]])  # instant revenue
        self.assertNotIn(5.0, [row["val"] for row in facts["revenue"]])  # company's own tag
        self.assertNotIn("debt_broad", facts)  # nil

    def test_finds_the_instance_among_a_filing_s_documents(self):
        self.assertEqual(
            instance_name(["FilingSummary.xml", "a_cal.xml", "a.htm", "a_htm.xml"]), "a_htm.xml",
        )
        self.assertEqual(
            instance_name(["FilingSummary.xml", "a_lab.xml", "a_pre.xml", "a.xml"]), "a.xml",
        )
        self.assertIsNone(instance_name(["FilingSummary.xml", "a.htm", "a.xsd"]))


class IndexAndMergeTests(unittest.TestCase):
    def test_index_keeps_original_reports_with_accessions(self):
        rows = compact_xbrl_index(INDEX_TEXT)
        self.assertEqual([(row["cik"], row["form"], row["accession"]) for row in rows], [
            ("1753368", "40-F", "0001753368-26-000011"),
            ("1753368", "40-F", "0001753368-25-000020"),
            ("99", "10-Q", "0000000099-26-000001"),
        ])

    def test_a_cover_count_alone_does_not_mark_a_filing_as_read(self):
        dates = reported_filing_dates(_extract())
        self.assertIn("2025-09-17", dates)
        self.assertNotIn("2026-09-28", dates)

    def test_merging_adds_new_rows_once_in_order(self):
        extract = _extract()
        facts = parse_instance(INSTANCE, CONCEPTS, "40-F", "2026-09-28")
        added = merge_facts(extract, facts)
        self.assertGreater(added, 0)
        self.assertEqual(merge_facts(extract, facts), 0)
        ends = [row["end"] for row in extract["facts"]["revenue"]]
        self.assertEqual(ends, sorted(ends))
        self.assertIn("ifrs-full:WeightedAverageShares", extract["tags_found"]["weighted_average_shares"])


class SupplementTests(unittest.TestCase):
    def test_reads_only_skipped_filings_and_caches_them(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = FakeFetcher()
            source = _source(directory, fetcher)
            extract = source.supplement(_extract(), CONCEPTS)
            self.assertEqual(
                [(row["form"], row["filed"]) for row in extract["filing_supplement"]],
                [("40-F", "2026-09-28")],
            )
            values = [row["val"] for row in extract["facts"]["revenue"]]
            self.assertIn(200065000.0, values)
            self.assertEqual(len(fetcher.urls), 4)  # 2026 Q3 and Q4 indexes, listing, instance
            self.assertIn(INDEX_URL.format(year=2026, quarter=3), fetcher.urls)
            self.assertIn(
                FILING_INDEX_URL.format(cik="1753368", folder="000175336826000011"), fetcher.urls,
            )

            again = _source(directory, FakeFetcher())
            again.supplement(_extract(), CONCEPTS)
            self.assertEqual(again.stats.filings_cached, 1)
            self.assertEqual(again.stats.filings_read, 0)

    def test_a_failed_fetch_is_left_out_and_tried_again(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _source(directory, FakeFetcher(broken=True))
            extract = source.supplement(_extract(), CONCEPTS)
            self.assertEqual(extract["filing_supplement"], [])
            self.assertEqual(source.stats.filings_failed, 1)
            retry = _source(directory, FakeFetcher())
            self.assertEqual(len(retry.supplement(_extract(), CONCEPTS)["filing_supplement"]), 1)

    def test_company_facts_source_applies_the_supplement(self):
        payload = {"cik": 1753368, "entityName": "Sangoma", "facts": {}}

        def fetch(url, headers):
            if "company_tickers" in url:
                return json.dumps({"0": {"ticker": "SANG", "cik_str": 1753368, "title": "Sangoma"}}).encode()
            if url == COMPANY_FACTS_URL.format(cik="0001753368"):
                return json.dumps(payload).encode()
            return FakeFetcher()(url, headers)

        with tempfile.TemporaryDirectory() as directory:
            supplement = _source(Path(directory) / "filings", fetch)
            source = SecXbrlSource(
                user_agent="Test research@example.com",
                cache_directory=Path(directory) / "facts",
                request_interval_seconds=0,
                fetcher=fetch,
                filing_supplement=supplement,
            )
            extract = source.get_company_facts("SANG")
            # The 2025 40-F was skipped too but predates the supplement's start.
            self.assertEqual(len(extract["filing_supplement"]), 1)
            self.assertIn(200065000.0, [row["val"] for row in extract["facts"]["revenue"]])
            cached = source.get_cached_company_facts("SANG")
            self.assertEqual(len(cached["filing_supplement"]), 1)


if __name__ == "__main__":
    unittest.main()
