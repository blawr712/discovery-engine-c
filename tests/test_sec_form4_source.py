from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from src.data_sources.sec_form4_source import SecForm4Source, compact_listing, parse_form4


FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer><issuerCik>0001058811</issuerCik><issuerTradingSymbol>immr</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001571322</rptOwnerCik></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>true</isDirector><isOfficer>false</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000000077</rptOwnerCik></reportingOwnerId>
    <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1,500</value></transactionShares>
        <transactionPricePerShare><value>7.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>9000</value></transactionShares></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-05-02</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><footnoteId id="F2"/></transactionShares></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>100</value></transactionShares></transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>"""

SUBMISSIONS = {
    "cik": "1058811",
    "filings": {"recent": {
        "form": ["4", "8-K", "4/A", "3", "4"],
        "filingDate": ["2026-10-08", "2026-09-01", "2026-05-05", "2026-04-02", "2026-03-15"],
        "accessionNumber": ["0001-26-000005", "0001-26-000004", "0001-26-000003", "0001-26-000002", "0001-26-000001"],
        "primaryDocument": ["xslF345X06/ownership.xml", "doc.htm", "xslF345X05/form4a.xml", "x.xml", "raw.xml"],
    }},
}


class ParseTests(unittest.TestCase):
    def test_rows_match_the_data_set_shape(self):
        rows = parse_form4(FORM4, "0001-26-000003", "2026-05-05")

        self.assertEqual([row["code"] for row in rows], ["P", "S"])
        purchase, sale = rows
        self.assertEqual(purchase["issuer_cik"], "0001058811")
        self.assertEqual(purchase["symbol"], "IMMR")
        self.assertEqual(purchase["filed"], "2026-05-05")
        self.assertEqual(purchase["trans_date"], "2026-05-01")
        self.assertEqual(purchase["shares"], 1500.0)
        self.assertEqual(purchase["value"], 11250.0)
        self.assertTrue(purchase["acquired"])
        self.assertEqual(purchase["owner_ciks"], ["0000000077", "0001571322"])
        self.assertTrue(purchase["officer_or_director"])
        self.assertTrue(purchase["ten_percent_owner"])
        self.assertEqual(purchase["kind"], "purchase")
        # A sale whose price is only a footnote keeps its shares, with no value.
        self.assertEqual(sale["kind"], "sale")
        self.assertIsNone(sale["price"])
        self.assertIsNone(sale["value"])
        self.assertFalse(sale["acquired"])

    def test_documents_without_open_market_rows(self):
        empty = "<ownershipDocument><issuer><issuerCik>1</issuerCik></issuer></ownershipDocument>"
        self.assertEqual(parse_form4(empty, "a", "2026-01-01"), [])

    def test_listing_keeps_ownership_filings_with_raw_document_names(self):
        listing = compact_listing(SUBMISSIONS, date(2026, 10, 26))
        self.assertEqual([f["form"] for f in listing["filings"]], ["4", "4/A", "4"])
        self.assertEqual(listing["filings"][0]["document"], "ownership.xml")
        self.assertEqual(listing["filings"][2]["document"], "raw.xml")
        self.assertEqual(listing["earliest_listed"], "2026-03-15")
        self.assertFalse(listing["truncated"])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.calls = []
        self.now = [1_793_000_000.0]  # late October 2026

        def fetch(url, headers):
            self.calls.append(url)
            if "submissions" in url:
                return json.dumps(SUBMISSIONS).encode("utf-8")
            return FORM4.encode("utf-8")

        self.source = SecForm4Source(
            "Research test@example.com", Path(self.temporary.name),
            request_interval_seconds=0, fetcher=fetch, clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_filings_after_the_cutoff_are_read(self):
        rows, covered, incomplete = self.source.transactions("1058811", filed_after=date(2026, 3, 31))

        documents = [url for url in self.calls if "Archives" in url]
        self.assertEqual(len(documents), 2)          # the March filing is skipped
        self.assertEqual(
            {url.rsplit("/data/", 1)[1] for url in documents},
            {"1058811/000126000005/ownership.xml", "1058811/000126000003/form4a.xml"},
        )
        self.assertEqual(len(rows), 4)               # two filings, two rows each
        self.assertFalse(incomplete)
        self.assertEqual(covered.year, 2026)

    def test_filings_are_cached_for_good_and_listings_for_a_day(self):
        self.source.transactions("1058811", filed_after=date(2026, 3, 31))
        first = len(self.calls)
        self.source.transactions("1058811", filed_after=date(2026, 3, 31))
        self.assertEqual(len(self.calls), first)
        self.now[0] += 2 * 86400
        self.source.transactions("1058811", filed_after=date(2026, 3, 31))
        self.assertEqual(len(self.calls), first + 1)  # only the listing is refetched

    def test_a_truncated_listing_that_misses_the_cutoff_is_flagged(self):
        truncated = {**SUBMISSIONS, "filings": {**SUBMISSIONS["filings"], "files": [{"name": "older.json"}]}}
        source = SecForm4Source(
            "Research test@example.com", Path(self.temporary.name) / "t",
            request_interval_seconds=0,
            fetcher=lambda url, headers: (json.dumps(truncated) if "submissions" in url else FORM4).encode("utf-8"),
        )
        _, _, incomplete = source.transactions("1058811", filed_after=date(2026, 1, 1))
        self.assertTrue(incomplete)
        _, _, complete = source.transactions("1058811", filed_after=date(2026, 3, 20))
        self.assertFalse(complete)


if __name__ == "__main__":
    unittest.main()


class SharedPacerTests(unittest.TestCase):
    def test_two_sources_share_one_schedule(self):
        import threading
        from src.data_sources.sec_pacing import SecPacer

        times = []
        lock = threading.Lock()

        def fetch(url, headers):
            with lock:
                times.append(__import__("time").monotonic())
            return b"{}"

        pacer = SecPacer(0.05)
        with tempfile.TemporaryDirectory() as directory:
            a = SecForm4Source("Research test@example.com", Path(directory) / "a", fetcher=fetch, pacer=pacer)
            b = SecForm4Source("Research test@example.com", Path(directory) / "b", fetcher=fetch, pacer=pacer)
            threads = [threading.Thread(target=source._request, args=("u",)) for source in (a, b) * 3]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        times.sort()
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertEqual(len(times), 6)
        self.assertTrue(all(gap >= 0.045 for gap in gaps), gaps)
