import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
import zipfile

from src.data_sources.sec_insider_source import (
    SecInsiderTransactionsSource,
    compact_insider_quarter,
    quarter_labels,
)


def _tsv(header, rows):
    lines = ["\t".join(header)] + ["\t".join(str(v) for v in row) for row in rows]
    return "\n".join(lines) + "\n"


def _quarter_zip():
    submission = _tsv(
        ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERTRADINGSYMBOL"],
        [
            ["ACC-1", "05-FEB-2025", "4", "1234", "test"],
            ["ACC-2", "20-MAR-2025", "4", "1234", "TEST"],
            ["ACC-3", "21-MAR-2025", "4", "999", "OTHR"],
        ],
    )
    owners = _tsv(
        ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP"],
        [
            ["ACC-1", "111", "Alice", "Officer"],
            ["ACC-1", "112", "Alice Trust", ""],
            ["ACC-2", "222", "Bob", "TenPercentOwner"],
            ["ACC-3", "333", "Carol", "Director"],
        ],
    )
    trans = _tsv(
        ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
         "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"],
        [
            ["ACC-1", "03-FEB-2025", "P", "1000", "2.50", "A"],
            ["ACC-1", "03-FEB-2025", "A", "500", "", "A"],   # award: ignored
            ["ACC-2", "18-MAR-2025", "S", "2000", "3.00", "D"],
            ["ACC-2", "18-MAR-2025", "P", "0", "3.00", "A"],  # zero shares: ignored
            ["ACC-3", "19-MAR-2025", "P", "10", "", "A"],     # no price: kept, no value
            ["ACC-9", "19-MAR-2025", "P", "10", "1", "A"],    # unknown accession: ignored
        ],
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SUBMISSION.tsv", submission)
        archive.writestr("REPORTINGOWNER.tsv", owners)
        archive.writestr("NONDERIV_TRANS.tsv", trans)
        archive.writestr("FOOTNOTES.tsv", "ACCESSION_NUMBER\tX\n")
    return buffer.getvalue()


class CompactionTests(unittest.TestCase):
    def test_keeps_open_market_transactions_with_owner_context(self):
        rows = compact_insider_quarter(_quarter_zip())

        self.assertEqual([row["accession"] for row in rows], ["ACC-1", "ACC-2", "ACC-3"])
        first = rows[0]
        self.assertEqual(first["issuer_cik"], "0000001234")
        self.assertEqual(first["symbol"], "TEST")
        self.assertEqual(first["filed"], "2025-02-05")
        self.assertEqual(first["trans_date"], "2025-02-03")
        self.assertEqual(first["kind"], "purchase")
        self.assertEqual(first["value"], 2500.0)
        self.assertTrue(first["acquired"])
        self.assertEqual(first["owner_ciks"], ["111", "112"])
        self.assertTrue(first["officer_or_director"])
        self.assertFalse(first["ten_percent_owner"])
        second = rows[1]
        self.assertEqual(second["kind"], "sale")
        self.assertTrue(second["ten_percent_owner"])
        self.assertFalse(second["officer_or_director"])
        self.assertIsNone(rows[2]["value"])

    def test_quarter_labels_are_inclusive_and_ordered(self):
        self.assertEqual(quarter_labels("2024q3", "2025q2"), ["2024q3", "2024q4", "2025q1", "2025q2"])
        self.assertEqual(quarter_labels("2025q1", "2025q1"), ["2025q1"])
        with self.assertRaises(ValueError):
            quarter_labels("2025q5", "2025q1")


class FakeFetcher:
    def __init__(self, available):
        self.available = set(available)
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        quarter = url.rsplit("/", 1)[-1].split("_")[0]
        if quarter in self.available:
            return _quarter_zip()
        raise HTTPError(url, 404, "Not Found", {}, None)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = [1_800_000_000.0]
        self.fetcher = FakeFetcher({"2025q1", "2025q2"})
        self.source = SecInsiderTransactionsSource(
            "Discovery Engine test@example.com",
            Path(self.directory.name),
            request_interval_seconds=0,
            unavailable_ttl_hours=1,
            fetcher=self.fetcher,
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_requires_contact_user_agent(self):
        with self.assertRaises(ValueError):
            SecInsiderTransactionsSource("anon", Path(self.directory.name))

    def test_loads_range_caches_quarters_and_remembers_gaps(self):
        rows, loaded, unavailable = self.source.load_quarters("2025q1", "2025q3")

        self.assertEqual(loaded, ["2025q1", "2025q2"])
        self.assertEqual(unavailable, ["2025q3"])
        self.assertEqual(len(rows), 6)
        self.assertEqual(self.source.stats.quarters_downloaded, 2)
        self.assertEqual(len(self.fetcher.urls), 3)

        rows_again, loaded_again, unavailable_again = self.source.load_quarters("2025q1", "2025q3")
        self.assertEqual((loaded_again, unavailable_again), (loaded, unavailable))
        self.assertEqual(len(rows_again), 6)
        self.assertEqual(len(self.fetcher.urls), 3)  # cached quarters and cached gap

        self.now[0] += 2 * 3600  # gap marker expired: probe again
        self.source.load_quarters("2025q3", "2025q3")
        self.assertEqual(len(self.fetcher.urls), 4)

    def test_cache_files_are_compact_gzip_json(self):
        self.source.load_quarter("2025q1")
        files = list(Path(self.directory.name).glob("*.json.gz"))
        self.assertEqual(len(files), 1)
        with gzip.open(files[0], "rt", encoding="utf-8") as file:
            payload = json.load(file)
        self.assertEqual(len(payload["transactions"]), 3)


if __name__ == "__main__":
    unittest.main()
