from datetime import date
import unittest

from src.insider_supplement import SupplementedInsiderLookup


def _row(accession, filed, kind="purchase", cik="0000000001"):
    return {
        "accession": accession, "issuer_cik": cik, "filed": filed, "kind": kind,
        "code": "P" if kind == "purchase" else "S", "shares": 1000.0, "price": 5.0,
        "value": 5000.0, "owner_ciks": ["9"], "officer_or_director": True,
    }


class FakeForm4:
    def __init__(self, rows, fail=False, incomplete=False):
        self.rows, self.fail, self.incomplete, self.calls = rows, fail, incomplete, []

    def transactions(self, cik, filed_after):
        self.calls.append((cik, filed_after))
        if self.fail:
            raise OSError("EDGAR unreachable")
        return [r for r in self.rows if r["filed"] > filed_after.isoformat()], date(2026, 10, 26), self.incomplete


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.bulk = [_row("A1", "2026-02-10"), _row("A2", "2026-03-30", kind="sale")]
        self.cik_for = {"IMMR": "1", "NEW": "2"}.get

    def test_recent_filings_are_appended_and_the_history_is_current(self):
        form4 = FakeForm4([_row("A2", "2026-03-30", kind="sale"), _row("B1", "2026-06-15"),
                           _row("B2", "2026-09-01")])
        lookup = SupplementedInsiderLookup(self.bulk, date(2026, 3, 31), self.cik_for, form4)
        history = lookup("IMMR")

        self.assertEqual(history.data_through, date(2026, 10, 26))
        filed = [item.filed for item in history.transactions_between(date(2025, 1, 1), date(2026, 12, 31))]
        self.assertEqual(filed, [date(2026, 2, 10), date(2026, 3, 30), date(2026, 6, 15), date(2026, 9, 1)])
        self.assertEqual(lookup.stats["fresh_transactions"], 2)
        self.assertEqual(form4.calls, [("0000000001", date(2026, 3, 31))])
        self.assertEqual(lookup.fresh_through, date(2026, 10, 26))

    def test_a_filing_already_in_the_data_set_is_not_counted_twice(self):
        """If the boundaries overlap, the accession number decides."""
        form4 = FakeForm4([_row("A2", "2026-04-02", kind="sale"), _row("B1", "2026-06-15")])
        lookup = SupplementedInsiderLookup(self.bulk, date(2026, 3, 31), self.cik_for, form4)
        history = lookup("IMMR")
        self.assertEqual(len(history.transactions_between(date(2025, 1, 1), date(2026, 12, 31))), 3)

    def test_a_failed_fetch_keeps_the_data_set_history(self):
        lookup = SupplementedInsiderLookup(self.bulk, date(2026, 3, 31), self.cik_for, FakeForm4([], fail=True))
        history = lookup("IMMR")

        self.assertEqual(history.data_through, date(2026, 3, 31))
        self.assertEqual(len(history.transactions_between(date(2025, 1, 1), date(2026, 12, 31))), 2)
        self.assertEqual(lookup.stats["fresh_failed"], 1)

    def test_a_company_with_only_recent_filings_gets_a_history(self):
        form4 = FakeForm4([_row("C1", "2026-07-01", cik="0000000002")])
        lookup = SupplementedInsiderLookup(self.bulk, date(2026, 3, 31), self.cik_for, form4)
        self.assertTrue(lookup("NEW").has_data)

    def test_bulk_only_never_fetches(self):
        form4 = FakeForm4([_row("B1", "2026-06-15")])
        lookup = SupplementedInsiderLookup(self.bulk, date(2026, 3, 31), self.cik_for, form4)
        history = lookup.bulk_only("IMMR")

        self.assertEqual(form4.calls, [])
        self.assertEqual(history.data_through, date(2026, 3, 31))
        self.assertEqual(lookup.stats["data_set_only"], 1)

    def test_unknown_tickers_and_empty_histories(self):
        lookup = SupplementedInsiderLookup([], date(2026, 3, 31), self.cik_for, FakeForm4([]))
        self.assertIsNone(lookup("ZZZZ"))
        self.assertIsNone(lookup("IMMR"))

    def test_incomplete_listings_are_counted(self):
        lookup = SupplementedInsiderLookup(
            self.bulk, date(2026, 3, 31), self.cik_for, FakeForm4([], incomplete=True),
        )
        lookup("IMMR")
        self.assertEqual(lookup.stats["fresh_incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
