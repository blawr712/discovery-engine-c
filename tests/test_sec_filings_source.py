import gzip
import json
from pathlib import Path
import tempfile
import unittest

from src.data_sources.sec_filings_source import (
    SecFilingsSource,
    compact_index,
    compact_listing_notices,
    compact_submissions,
    quarters,
)

INDEX = """Description:           XBRL Index of EDGAR Dissemination Feed
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
1000045|NICHOLAS FINANCIAL INC|10-Q|2021-02-11|edgar/data/1000045/a.txt
1000184|SAP SE|20-F|2021-03-04|edgar/data/1000184/b.txt
1000209|SOME CO|8-K|2021-03-05|edgar/data/1000209/c.txt
22|FUND TRUST|485BPOS|2021-03-06|edgar/data/22/d.txt
"""

SUBMISSIONS = {
    "cik": "1234", "name": "DEAD CO", "sic": "3674", "sicDescription": "Semiconductors",
    "entityType": "operating", "tickers": [], "exchanges": [None],
    "formerNames": [{"name": "DEAD CORP"}],
    "filings": {"recent": {
        "form": ["8-K", "25-NSE", "10-Q", "8-K", "DEFM14A", "15-12B", "4", "10-K"],
        "filingDate": ["2021-06-30", "2021-07-01", "2021-05-10", "2021-02-01",
                       "2021-04-01", "2021-07-12", "2021-01-05", "2021-03-01"],
        "items": ["2.01,3.01,5.01,9.01", "", "", "7.01,9.01", "", "", "", ""],
    }},
}


class CompactionTests(unittest.TestCase):
    def test_index_keeps_only_periodic_reports(self):
        rows = compact_index(INDEX)
        self.assertEqual([row["form"] for row in rows], ["10-Q", "20-F"])
        self.assertEqual(rows[0]["cik"], "0001000045")
        self.assertEqual(rows[1]["filed"], "2021-03-04")

    def test_submissions_keep_identity_last_report_and_exit_filings(self):
        compact = compact_submissions(SUBMISSIONS)
        self.assertEqual(compact["cik"], "0000001234")
        self.assertEqual(compact["sic"], "3674")
        self.assertEqual(compact["exchanges"], [])
        self.assertEqual(compact["former_names"], ["DEAD CORP"])
        self.assertEqual(compact["last_periodic_filing"], "2021-05-10")
        self.assertEqual(
            [(row["form"], row["filed"]) for row in compact["exit_filings"]],
            [("DEFM14A", "2021-04-01"), ("8-K", "2021-06-30"),
             ("25-NSE", "2021-07-01"), ("15-12B", "2021-07-12")],
        )
        # Only exit-relevant 8-K items survive; an 8-K with none is dropped.
        self.assertEqual(compact["exit_filings"][1]["items"], ["2.01", "3.01", "5.01"])
        self.assertFalse(compact["filings_truncated"])

    def test_listing_notices_are_read_from_the_fixed_width_form_index(self):
        text = "\n".join([
            'Form Type   Company Name                                                      CIK         Date Filed  File Name',
            '-----------------------------------------------------------------',
            '25               22nd Century Group, Inc.                                      1347858     2021-08-13  edgar/data/1347858/a.txt         ',
            '25-NSE           DEAD  &  GONE CORP                                            99          2021-09-01  edgar/data/99/b.txt',
            'SC 14D9          SOME TARGET INC                                               5           2021-09-02  edgar/data/5/c.txt',
            '250              NOT A NOTICE                                                  6           2021-09-03  edgar/data/6/d.txt',
        ])
        rows = compact_listing_notices(text)
        self.assertEqual(
            [(row["form"], row["cik"], row["filed"]) for row in rows],
            [("25", "0001347858", "2021-08-13"), ("25-NSE", "0000000099", "2021-09-01")],
        )
        self.assertEqual(rows[1]["name"], "DEAD  &  GONE CORP")

    def test_quarter_ranges_cross_years(self):
        self.assertEqual(quarters((2025, 3), (2026, 2)), [(2025, 3), (2025, 4), (2026, 1), (2026, 2)])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.calls = []
        self.now = [1_000_000.0]

        def fetch(url, headers):
            self.calls.append(url)
            assert "@" in headers["User-Agent"]
            if url.endswith("xbrl.gz"):
                return gzip.compress(INDEX.encode("latin-1"))
            return json.dumps(SUBMISSIONS).encode("utf-8")

        self.source = SecFilingsSource(
            "Research test@example.com", Path(self.temporary.name),
            request_interval_seconds=0, fetcher=fetch,
            clock=lambda: self.now[0], today=lambda: (2026, 4),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_past_index_quarters_are_fetched_once(self):
        first = self.source.periodic_filings(2021, 1)
        second = self.source.periodic_filings(2021, 1)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("/2021/QTR1/xbrl.gz", self.calls[0])

    def test_submissions_are_cached_and_cik_is_normalised(self):
        self.source.submissions("1234")
        self.source.submissions("0000001234")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("CIK0000001234.json", self.calls[0])
        self.assertEqual(self.source.stats.submissions_hits, 1)

    def test_a_user_agent_without_contact_is_refused(self):
        with self.assertRaises(ValueError):
            SecFilingsSource("anonymous", Path(self.temporary.name))


if __name__ == "__main__":
    unittest.main()
