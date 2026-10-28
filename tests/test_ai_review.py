import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from src.ai_review import (
    SecFilingArchive,
    SecRegistrant,
    choose_filings,
    filing_text,
    parse_filing_index,
    prepare_ai_review,
    review_packet,
    select_review_names,
    sha256_text,
    submission_rows,
)
from src.cli import parse_args

ANCHOR = datetime(2026, 10, 20, 0, 55, tzinfo=timezone.utc)


def filing(form, filed, accepted=None, accession=None, document="doc.htm"):
    return {
        "form": form,
        "filingDate": filed,
        "acceptanceDateTime": accepted or f"{filed}T16:00:00.000Z",
        "accessionNumber": accession or f"0000000000-{filed.replace('-', '')}-{form}",
        "primaryDocument": document,
    }


class SelectionTests(unittest.TestCase):
    def test_only_passing_names_in_queue_order(self):
        queue = [
            {"ticker": "A", "screen_status": "passes"},
            {"ticker": "B", "screen_status": "passes_bank_lane"},
            {"ticker": "C", "screen_status": "low_v3_quintile"},
            {"ticker": "D", "screen_status": "passes"},
            {"ticker": "E", "screen_status": "passes"},
        ]
        self.assertEqual([row["ticker"] for row in select_review_names(queue, 2)], ["A", "D"])

    def test_top_must_be_positive(self):
        with self.assertRaises(ValueError):
            select_review_names([], 0)


class FilingChoiceTests(unittest.TestCase):
    def test_latest_annual_quarterly_and_8ks_since_the_10q(self):
        rows = [
            filing("10-K", "2026-03-01"),
            filing("10-K", "2025-03-01"),
            filing("10-Q", "2026-08-05"),
            filing("10-Q", "2026-05-05"),
            filing("8-K", "2026-08-01"),      # before the 10-Q: dropped
            filing("8-K", "2026-09-10"),
            filing("8-K", "2026-10-15"),
            filing("4", "2026-10-16"),
        ]
        chosen = choose_filings(rows, ANCHOR)
        self.assertEqual(
            [(row["role"], row["form"], row["filingDate"]) for row in chosen],
            [("annual", "10-K", "2026-03-01"), ("quarterly", "10-Q", "2026-08-05"),
             ("current", "8-K", "2026-10-15"), ("current", "8-K", "2026-09-10")],
        )

    def test_nothing_after_the_anchor(self):
        rows = [
            filing("10-K", "2026-03-01"),
            filing("10-Q", "2026-08-05"),
            # Same calendar day as the anchor but accepted after it.
            filing("8-K", "2026-10-20", accepted="2026-10-20T12:00:00.000Z"),
            filing("10-Q", "2026-11-05"),
            filing("8-K", "2026-10-19", accepted="2026-10-19T20:00:00.000Z"),
        ]
        chosen = choose_filings(rows, ANCHOR)
        self.assertEqual(
            [(row["form"], row["filingDate"]) for row in chosen],
            [("10-K", "2026-03-01"), ("10-Q", "2026-08-05"), ("8-K", "2026-10-19")],
        )

    def test_current_reports_are_capped_most_recent_first(self):
        rows = [filing("10-Q", "2026-08-05")] + [
            filing("8-K", f"2026-09-{day:02d}") for day in range(1, 21)
        ]
        chosen = choose_filings(rows, ANCHOR, max_current_reports=3)
        self.assertEqual(
            [row["filingDate"] for row in chosen if row["role"] == "current"],
            ["2026-09-20", "2026-09-19", "2026-09-18"],
        )

    def test_foreign_filers_read_6ks_since_the_annual_report(self):
        rows = [
            filing("20-F", "2026-04-20"),
            filing("6-K", "2026-04-01"),
            filing("6-K", "2026-08-14"),
            filing("8-K", "2026-09-01"),
        ]
        chosen = choose_filings(rows, ANCHOR)
        self.assertEqual(
            [(row["role"], row["form"]) for row in chosen],
            [("annual", "20-F"), ("current", "6-K")],
        )

    def test_submission_rows_zip_the_recent_table(self):
        payload = {"filings": {"recent": {
            "accessionNumber": ["1", "2"], "filingDate": ["2026-01-01", "2026-02-01"],
            "acceptanceDateTime": ["2026-01-01T10:00:00.000Z", "2026-02-01T10:00:00.000Z"],
            "form": ["10-K", "8-K"], "primaryDocument": ["a.htm", "b.htm"],
        }}}
        rows = submission_rows(payload)
        self.assertEqual(rows[1]["form"], "8-K")
        self.assertEqual(rows[0]["primaryDocument"], "a.htm")


class TextTests(unittest.TestCase):
    def test_blocks_become_lines_and_hidden_xbrl_is_dropped(self):
        html = (
            b"<html><head><title>x</title></head><body>"
            b"<div style='display:none'><ix:header>dei:Hidden 123</ix:header></div>"
            b"<p>Item&nbsp;1A.   Risk   Factors</p><table><tr><td>Revenue</td><td>$1,234</td></tr></table>"
            b"<p>We may need to raise<br/>additional capital.</p><script>var x=1;</script>"
            b"</body></html>"
        )
        text = filing_text(html, "doc.htm")
        self.assertNotIn("Hidden", text)
        self.assertNotIn("var x", text)
        self.assertIn("Item 1A. Risk Factors", text)
        self.assertIn("Revenue $1,234", text)
        self.assertIn("We may need to raise\nadditional capital.", text)
        self.assertTrue(text.endswith("\n"))

    def test_plain_text_documents_are_kept(self):
        self.assertEqual(filing_text(b"Line one\r\n\r\n\r\nLine  two", "x.txt"), "Line one\n\nLine two\n")

    def test_filing_index_lists_documents_and_types(self):
        html = """<table class="tableFile">
        <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
        <tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/1/000/a8k.htm">a8k.htm</a></td><td>8-K</td><td>10</td></tr>
        <tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1/000/ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>20</td></tr>
        </table>"""
        self.assertEqual(parse_filing_index(html), [
            {"document": "a8k.htm", "type": "8-K"},
            {"document": "ex991.htm", "type": "EX-99.1"},
        ])


class FakeArchive:
    def __init__(self, submissions, documents, fail=()):
        self._submissions = submissions
        self._documents = documents
        self.fail = set(fail)
        self.requests = []

    def submissions(self, cik):
        if cik in self.fail:
            raise ConnectionError("blocked")
        self.requests.append(("submissions", cik))
        return self._submissions[cik]

    def filing_documents(self, cik, filing):
        names = [filing["primaryDocument"]]
        if filing["role"] == "current":
            names.append("ex991.htm")
        return [{"document": name, "url": f"https://sec/{cik}/{filing['accessionNumber']}/{name}",
                 "type": None if name == filing["primaryDocument"] else "EX-99.1"} for name in names]

    def document(self, url):
        self.requests.append(("document", url))
        return self._documents.get(url.rsplit("/", 1)[-1], b"<p>Body of " + url.encode() + b"</p>")


def submissions(sic="3841"):
    return {"sic": sic, "sicDescription": "Surgical instruments", "filings": {"recent": {
        "accessionNumber": ["acc-k", "acc-q", "acc-8", "acc-late"],
        "filingDate": ["2026-03-01", "2026-08-05", "2026-09-10", "2026-11-01"],
        "acceptanceDateTime": [
            "2026-03-01T16:00:00.000Z", "2026-08-05T16:00:00.000Z",
            "2026-09-10T16:00:00.000Z", "2026-11-01T16:00:00.000Z",
        ],
        "form": ["10-K", "10-Q", "8-K", "8-K"],
        "primaryDocument": ["k.htm", "q.htm", "e.htm", "late.htm"],
    }}}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.queue = [
            {"ticker": "UTMD", "screen_status": "passes", "research_rank": 1, "v3_quintile": 5,
             "score_defensive": 61.0},
            {"ticker": "BANK", "screen_status": "passes_bank_lane", "research_rank": 2},
            {"ticker": "DR.TO", "screen_status": "passes", "research_rank": 3, "v3_quintile": 5},
            {"ticker": "FAIL", "screen_status": "passes", "research_rank": 4, "v3_quintile": 4},
        ]
        self.results = [
            {"ticker": "UTMD", "status": "OK", "country": "US", "pit_revenue_ttm": 40e6,
             "pit_net_income_ttm": 10e6, "pit_shares_outstanding": 3.2e6, "pit_market_cap": 2e8,
             "pit_reporting_currency": "USD", "score_v3": 90.0},
            {"ticker": "DR.TO", "status": "OK", "country": "CA"},
            {"ticker": "FAIL", "status": "OK", "country": "US"},
        ]
        self.registrants = {
            "UTMD": SecRegistrant("UTMD", "0000706698", "direct"),
            "DR.TO": SecRegistrant(None, None, "not interlisted"),
            "FAIL": SecRegistrant("FAIL", "0000000002", "direct"),
        }
        self.archive = FakeArchive(
            {"0000706698": submissions(), "0000000002": submissions()},
            {"k.htm": b"<p>Total revenue was $40.1 million.</p>"},
            fail={"0000000002"},
        )

    def tearDown(self):
        self.directory.cleanup()

    def prepare(self, archive=None):
        return prepare_ai_review(
            run_id="RUN",
            manifest={"completed_at": ANCHOR.isoformat()},
            results=self.results,
            queue=self.queue,
            base_packets={"UTMD": {"ticker": "UTMD", "company_name": "Utah Medical"}},
            resolver=lambda row: self.registrants[row["ticker"]],
            archive=archive or self.archive,
            output_root=self.root,
            top=50,
            clock=lambda: ANCHOR,
        )

    def test_writes_packet_filings_and_hashes(self):
        summary = self.prepare()
        self.assertEqual(summary["counts"], {"error": 1, "not_reviewed": 1, "ready": 1})
        self.assertEqual([entry["ticker"] for entry in summary["names"]], ["UTMD", "DR.TO", "FAIL"])
        folder = self.root / "RUN" / "UTMD"
        packet = json.loads((folder / "packet.json").read_text(encoding="utf-8"))
        self.assertEqual(packet["company_name"], "Utah Medical")
        self.assertEqual(packet["pit_revenue_ttm"], 40e6)
        self.assertEqual(packet["screen_status"], "passes")
        self.assertEqual(packet["v3_quintile"], 5)
        self.assertEqual(packet["score_defensive"], 61.0)
        self.assertEqual(packet["net_margin_ttm"], 0.25)
        self.assertEqual(packet["sic"], "3841")
        self.assertEqual(packet["anchor_date"], "2026-10-20")
        manifest = json.loads((folder / "filings.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "ready")
        documents = manifest["documents"]
        # 10-K, 10-Q, and the 8-K with its press release; the November 8-K is after the anchor.
        self.assertEqual(
            [(doc["form"], doc["document"]) for doc in documents],
            [("10-K", "k.htm"), ("10-Q", "q.htm"), ("8-K", "e.htm"), ("8-K", "ex991.htm")],
        )
        for doc in documents:
            text = (folder / doc["path"]).read_text(encoding="utf-8")
            self.assertEqual(doc["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
            self.assertEqual(doc["sha256"], sha256_text(text))
        self.assertIn("Total revenue was $40.1 million.", (folder / documents[0]["path"]).read_text(encoding="utf-8"))
        self.assertTrue(all("late.htm" not in url for _, url in self.archive.requests))

    def test_names_without_an_sec_registrant_are_not_reviewed(self):
        self.prepare()
        review = json.loads((self.root / "RUN" / "DR.TO" / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["verdict"], "not_reviewed")
        self.assertIn("not interlisted", review["deciding_reason"])
        self.assertEqual(review["written_by"], "prepare")

    def test_a_failure_is_isolated_and_retried_while_finished_names_are_kept(self):
        first = self.prepare()
        self.assertEqual(next(e for e in first["names"] if e["ticker"] == "FAIL")["status"], "error")
        self.assertFalse((self.root / "RUN" / "FAIL" / "filings.json").exists())
        before = len(self.archive.requests)
        self.archive.fail.clear()
        second = self.prepare()
        self.assertEqual(second["counts"], {"not_reviewed": 1, "ready": 2})
        self.assertTrue(next(e for e in second["names"] if e["ticker"] == "UTMD")["kept"])
        # Only FAIL was fetched again.
        self.assertTrue(all("0000706698" not in str(item) for item in self.archive.requests[before:]))

    def test_packet_joins_run_row_and_queue_row(self):
        packet = review_packet(
            {"ticker": "X"}, {"pit_revenue_ttm": 0.0, "pit_net_income_ttm": -1.0},
            {"screen_status": "passes", "v3_quintile": 3},
            run_id="RUN", anchor=ANCHOR, registrant=SecRegistrant("X", "1", "direct"),
        )
        self.assertIsNone(packet["net_margin_ttm"])
        self.assertEqual(packet["v3_quintile"], 3)
        self.assertEqual(packet["sec_cik"], "1")


class ArchiveTests(unittest.TestCase):
    def test_requests_are_paced_and_exhibits_chosen_by_form(self):
        from src.evidence import HttpCache

        index = (
            '<tr><td>1</td><td>x</td><td><a href="/Archives/edgar/data/5/0001/k.htm">k.htm</a></td><td>10-K</td></tr>'
            '<tr><td>2</td><td>x</td><td><a href="/Archives/edgar/data/5/0001/ex13.htm">ex13.htm</a></td><td>EX-13</td></tr>'
            '<tr><td>3</td><td>x</td><td><a href="/Archives/edgar/data/5/0001/ex21.htm">ex21.htm</a></td><td>EX-21</td></tr>'
        ).encode()
        fetched = []

        def fetcher(url, headers):
            fetched.append(url)
            return index if url.endswith("-index.htm") else b"{}"

        class Pacer:
            waits = 0

            def wait(self):
                Pacer.waits += 1

        with tempfile.TemporaryDirectory() as directory:
            archive = SecFilingArchive(
                "Test research@example.com", HttpCache(Path(directory), fetcher=fetcher), Pacer(),
            )
            documents = archive.filing_documents(
                "5", {"accessionNumber": "0000-26-0001", "primaryDocument": "k.htm", "role": "annual"},
            )
            self.assertEqual([doc["document"] for doc in documents], ["k.htm", "ex13.htm"])
            archive.document(documents[0]["url"])
        self.assertEqual(Pacer.waits, len(fetched))

    def test_user_agent_needs_a_contact(self):
        with self.assertRaises(ValueError):
            SecFilingArchive("no contact", object())


class CliTests(unittest.TestCase):
    def test_prepare_takes_a_run_and_top(self):
        args = parse_args(["--ai-review-prepare", "RUN", "--top", "25"])
        self.assertEqual(args.ai_review_prepare, "RUN")
        self.assertEqual(args.top, 25)

    def test_top_alone_is_still_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(["--top", "5"])


if __name__ == "__main__":
    unittest.main()
