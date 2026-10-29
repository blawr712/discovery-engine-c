import copy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.ai_review import not_reviewed_review
from src.ai_review_freeze import check_reviews, freeze_reviews, quote_found, validate_review
from src.cli import parse_args

ANCHOR = datetime(2026, 10, 20, 0, 55, tzinfo=timezone.utc)
TEXT_K = "Item 7. Results\n\nTotal revenue was $40.1 million for fiscal 2025.\n\nWe have no debt.\n"
TEXT_Q = "Our auditor expressed substantial doubt about our ability to continue as a going concern.\n"
SHA_K = hashlib.sha256(TEXT_K.encode("utf-8")).hexdigest()
SHA_Q = hashlib.sha256(TEXT_Q.encode("utf-8")).hexdigest()
PACKET = {"ticker": "UTMD", "pit_revenue_ttm": 40e6, "pit_reporting_currency": "USD"}
PEERS = {
    "status": "ok",
    "metrics": ["pit_earnings_yield", "pit_operating_margin_ttm"],
    "summary": {"pit_earnings_yield": {}, "pit_operating_margin_ttm": {}},
}


def good_review():
    return {
        "ticker": "UTMD",
        "verdict": "caution",
        "deciding_reason": "Cheap, but the filings raise a balance-sheet concern.",
        "data_check": [
            {"field": "pit_revenue_ttm", "ours": 40000000.0, "filing": 40100000, "agrees": True,
             "citation": {"sha256": SHA_K, "quote": "Total revenue was $40.1 million"}},
            {"field": "pit_reporting_currency", "ours": "USD", "filing": None, "agrees": True},
        ],
        "findings": [
            {"area": "balance_sheet", "severity": "concern", "text": "No debt, but see the 10-Q.",
             "citation": {"sha256": SHA_K, "quote": "We have no  debt."}},
        ],
        "peer_comparison": {
            "discount_explained": "partly",
            "text": "Cheaper than peers on earnings; margins are lower.",
            "metrics_cited": ["pit_earnings_yield", "pit_operating_margin_ttm"],
        },
        "bear_case": "Revenue is flat and the business is small.",
        "unanswered_questions": ["Why did margins fall?"],
        "evidence_through": "2026-08-05",
        "checklist_version": "review-v1",
        "skill_version": "ai-review-skill-1",
        "model": "claude-test",
    }


def validate(review, peers=PEERS):
    return validate_review(
        review, ticker="UTMD", packet=PACKET, peers=peers,
        documents={SHA_K: TEXT_K, SHA_Q: TEXT_Q}, anchor=date(2026, 10, 20),
    )


class ValidationTests(unittest.TestCase):
    def test_a_good_review_passes(self):
        self.assertEqual(validate(good_review()), [])

    def test_quotes_must_be_word_for_word_in_the_cited_filing(self):
        review = good_review()
        review["findings"][0]["citation"]["quote"] = "We have little debt."
        self.assertTrue(any("quote not found" in error for error in validate(review)))
        review = good_review()
        # Real text, but from the other filing.
        review["findings"][0]["citation"]["quote"] = "substantial doubt about our ability"
        self.assertTrue(any("quote not found" in error for error in validate(review)))
        review["findings"][0]["citation"]["sha256"] = "0" * 64
        self.assertTrue(any("not one of this name's saved filings" in error for error in validate(review)))
        self.assertTrue(quote_found("Total revenue\nwas  $40.1 million", TEXT_K))

    def test_cited_peer_metrics_must_exist(self):
        review = good_review()
        review["peer_comparison"]["metrics_cited"].append("pit_fcf_yield")
        self.assertTrue(any("'pit_fcf_yield'" in error for error in validate(review)))
        review = good_review()
        self.assertTrue(any(
            "insufficient_peers" in error for error in validate(review, peers={"status": "insufficient_peers"})
        ))

    def test_schema_and_forbidden_content(self):
        review = good_review()
        review["price_target"] = 50
        review["verdict"] = "buy"
        review["bear_case"] = "Our price target is $12."
        review["findings"][0]["severity"] = "huge"
        review["evidence_through"] = "2026-11-01"
        errors = " | ".join(validate(review))
        for expected in ("keys not in the schema: price_target", "verdict 'buy'", "forbidden content",
                         "severity 'huge'", "after the anchor date"):
            self.assertIn(expected, errors)

    def test_data_check_must_copy_our_figure(self):
        review = good_review()
        review["data_check"][0]["ours"] = 41e6
        self.assertTrue(any("is not the packet's pit_revenue_ttm" in error for error in validate(review)))
        review["data_check"][0]["field"] = "made_up"
        self.assertTrue(any("'made_up'" in error for error in validate(review)))
        # Any packet figure may be compared, including ratios.
        review = good_review()
        review["data_check"].append({"field": "pit_net_cash_to_market_cap", "ours": 0.06, "filing": None,
                                     "agrees": False})
        packet = {**PACKET, "pit_net_cash_to_market_cap": 0.06}
        self.assertEqual(validate_review(
            review, ticker="UTMD", packet=packet, peers=PEERS,
            documents={SHA_K: TEXT_K, SHA_Q: TEXT_Q}, anchor=date(2026, 10, 20),
        ), [])

    def test_avoid_needs_a_serious_finding_or_a_disagreement(self):
        review = good_review()
        review["verdict"] = "avoid"
        self.assertTrue(any("verdict avoid" in error for error in validate(review)))
        review["findings"].append({
            "area": "going_concern", "severity": "serious", "text": "Going-concern doubt.",
            "citation": {"sha256": SHA_Q, "quote": "substantial doubt about our ability to continue"},
        })
        self.assertEqual(validate(review), [])

    def test_only_preparation_may_mark_not_reviewed(self):
        review = not_reviewed_review("UTMD", "No SEC registrant.", ANCHOR)
        self.assertEqual(validate(review, peers=None), [])
        review.pop("written_by")
        self.assertTrue(any("only preparation" in error for error in validate(review, peers=None)))


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.run = self.root / "ai_review" / "RUN"
        self.exports = self.root / "exports"
        folder = self.run / "UTMD"
        (folder / "filings").mkdir(parents=True)
        (folder / "filings" / "k.txt").write_text(TEXT_K, encoding="utf-8", newline="\n")
        (folder / "filings" / "q.txt").write_text(TEXT_Q, encoding="utf-8", newline="\n")
        self.write(folder / "packet.json", PACKET)
        self.write(folder / "peers.json", PEERS)
        self.write(folder / "filings.json", {"status": "ready", "complete": True, "documents": [
            {"path": "filings/k.txt", "sha256": SHA_K, "form": "10-K"},
            {"path": "filings/q.txt", "sha256": SHA_Q, "form": "10-Q"},
        ]})
        canadian = self.run / "DR.TO"
        canadian.mkdir()
        self.write(canadian / "review.json", not_reviewed_review("DR.TO", "No SEC registrant.", ANCHOR))
        self.write(self.run / "selection.json", {
            "run_id": "RUN", "anchor_datetime": ANCHOR.isoformat(),
            "names": [
                {"ticker": "UTMD", "directory": "UTMD", "status": "ready", "research_rank": 1},
                {"ticker": "DR.TO", "directory": "DR.TO", "status": "not_reviewed", "research_rank": 2},
            ],
        })

    def tearDown(self):
        self.directory.cleanup()

    def write(self, path, value):
        Path(path).write_text(json.dumps(value), encoding="utf-8")

    def test_refuses_while_a_review_is_missing(self):
        self.assertEqual(check_reviews(self.run)["counts"], {"missing": 1, "not_reviewed": 1})
        with self.assertRaisesRegex(ValueError, "no review yet: UTMD"):
            freeze_reviews(self.run, self.exports)

    def test_freezes_once_and_records_failures(self):
        review = good_review()
        review["data_check"][0]["agrees"] = False
        self.write(self.run / "UTMD" / "review.json", review)
        artifact, path = freeze_reviews(self.run, self.exports, baseline_tickers={"UTMD"},
                                        clock=lambda: ANCHOR)
        self.assertTrue(path.endswith("ai_reviews_RUN.json"))
        self.assertEqual(artifact["counts"], {"not_reviewed": 1, "valid": 1})
        self.assertEqual(artifact["models"], ["claude-test"])
        self.assertEqual(artifact["data_check_disagreements"][0]["field"], "pit_revenue_ttm")
        utmd = artifact["names"][0]
        self.assertEqual(utmd["verdict"], "caution")
        self.assertTrue(utmd["in_research_baseline"])
        self.assertEqual(utmd["peers_sha256"], hashlib.sha256((self.run / "UTMD" / "peers.json").read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            freeze_reviews(self.run, self.exports)
        # A dry run is a separate artifact, outside the test.
        dry, dry_path = freeze_reviews(self.run, self.exports, dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertIn("ai_review_dry_run_RUN.json", dry_path)

    def test_a_changed_filing_fails_its_review(self):
        self.write(self.run / "UTMD" / "review.json", good_review())
        (self.run / "UTMD" / "filings" / "k.txt").write_text(TEXT_K + "edited", encoding="utf-8")
        result = check_reviews(self.run)["names"][0]
        self.assertEqual(result["status"], "validation_failed")
        self.assertTrue(any("changed since preparation" in error for error in result["errors"]))

    def test_a_failed_review_is_kept_as_failed(self):
        review = copy.deepcopy(good_review())
        review["findings"][0]["citation"]["quote"] = "invented sentence here"
        self.write(self.run / "UTMD" / "review.json", review)
        artifact, _ = freeze_reviews(self.run, self.exports)
        self.assertEqual(artifact["names"][0]["status"], "validation_failed")
        self.assertIsNone(artifact["names"][0]["verdict"])
        self.assertNotIn("review", artifact["names"][0])


class CliTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(parse_args(["--ai-review-check", "RUN"]).ai_review_check, "RUN")
        args = parse_args(["--ai-review-freeze", "RUN", "--dry-run"])
        self.assertTrue(args.dry_run)
        with self.assertRaises(SystemExit):
            parse_args(["--ai-review-check", "RUN", "--dry-run"])


if __name__ == "__main__":
    unittest.main()
