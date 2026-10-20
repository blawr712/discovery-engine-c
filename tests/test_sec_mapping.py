import unittest

from src.sec_mapping import name_similarity, names_match, resolve_sec_ticker


CIKS = {"ACT": "1", "BCHT": "2", "BRK-B": "3", "WPRT": "4", "QTWO": "5"}
TITLES = {
    "ACT": "Enact Holdings, Inc.",
    "BCHT": "Birchtech Corp.",
    "BRK-B": "Berkshire Hathaway Inc",
    "WPRT": "WESTPORT FUEL SYSTEMS INC.",
    "QTWO": "Q2 Holdings, Inc.",
}


def _resolve(row):
    return resolve_sec_ticker(row, CIKS.get, TITLES.get)


class MappingTests(unittest.TestCase):
    def test_us_tickers_map_directly(self):
        self.assertEqual(_resolve({"ticker": "bcht", "country": "US"}), ("BCHT", "direct"))
        self.assertEqual(_resolve({"ticker": "ZZZZ", "country": "US"})[0], None)

    def test_canadian_names_need_interlisting_and_a_name_match(self):
        self.assertEqual(
            _resolve({"ticker": "WELL.TO", "country": "CA", "company_name": "WELL Health"})[1],
            "not interlisted",
        )
        sec, reason = _resolve({
            "ticker": "BCHT.TO", "country": "CA", "universe_root_ticker": "BCHT",
            "universe_interlisted": "NYSE Mkt", "universe_company_name": "Birchtech Corp.",
        })
        self.assertEqual(sec, "BCHT")
        self.assertTrue(reason.startswith("interlisted:BCHT"))
        sec, reason = _resolve({
            "ticker": "WPRT.TO", "country": "CA", "universe_root_ticker": "WPRT",
            "universe_interlisted": "NasdaqGS",
            "universe_company_name": "Westport Fuel Systems Inc.",
        })
        self.assertEqual(sec, "WPRT")

    def test_root_symbol_collisions_are_rejected(self):
        sec, reason = _resolve({
            "ticker": "ACT.TO", "country": "CA", "universe_root_ticker": "ACT",
            "universe_interlisted": "NasdaqCM",
            "universe_company_name": "Aduro Clean Technologies Inc.",
        })
        self.assertIsNone(sec)
        self.assertIn("Enact Holdings", reason)

    def test_class_share_roots_try_dash_form(self):
        sec, _ = _resolve({
            "ticker": "BRK.B.TO", "country": "CA", "universe_root_ticker": "BRK.B",
            "universe_interlisted": "NYSE", "universe_company_name": "Berkshire Hathaway Inc.",
        })
        self.assertEqual(sec, "BRK-B")

    def test_name_similarity_ignores_corporate_suffixes(self):
        self.assertEqual(name_similarity("Birchtech Corp.", "BIRCHTECH CORP"), 1.0)
        self.assertLess(name_similarity("Aduro Clean Technologies Inc.", "Enact Holdings, Inc."), 0.2)
        self.assertEqual(name_similarity("", "Anything"), 0.0)


class SingleSharedTokenTests(unittest.TestCase):
    """One shared token is not evidence of a shared issuer.

    Stopword removal reduces "Q2 Holdings, Inc." to the single token ``q2``,
    which overlaps "Q2 Metals Corp" at exactly the 0.5 threshold. A lithium
    explorer inherited a fintech's revenue through that gap.
    """

    def test_single_shared_token_is_rejected_despite_clearing_threshold(self):
        self.assertEqual(name_similarity("Q2 Metals Corp", "Q2 Holdings, Inc."), 0.5)
        sec, reason = _resolve({
            "ticker": "QTWO.V", "country": "CA", "universe_root_ticker": "QTWO",
            "universe_interlisted": "NYSE",
            "universe_company_name": "Q2 METALS CORP",
        })
        self.assertIsNone(sec)
        self.assertIn("Q2 Holdings", reason)
        self.assertIn("1 shared name token", reason)

    def test_identical_single_token_names_still_match(self):
        """Many genuine interlistings reduce to one distinctive word."""
        self.assertTrue(names_match("Largo Inc", "Largo Inc."))
        self.assertTrue(names_match("ELECTROVAYA INC", "Electrovaya Inc."))
        sec, _ = _resolve({
            "ticker": "BCHT.TO", "country": "CA", "universe_root_ticker": "BCHT",
            "universe_interlisted": "NYSE Mkt",
            "universe_company_name": "BIRCHTECH CORP",
        })
        self.assertEqual(sec, "BCHT")

    def test_two_shared_tokens_are_enough(self):
        self.assertTrue(
            names_match("WESTPORT FUEL SYSTEMS INC", "Westport Fuel Systems Inc.")
        )
        self.assertTrue(names_match("Trilogy Metals Inc", "Trilogy Metals Inc."))

    def test_disjoint_names_are_rejected(self):
        self.assertFalse(names_match("Aduro Clean Technologies", "Enact Holdings"))
        self.assertFalse(names_match("", "Anything"))
        self.assertFalse(names_match("Largo Inc", ""))


if __name__ == "__main__":
    unittest.main()
