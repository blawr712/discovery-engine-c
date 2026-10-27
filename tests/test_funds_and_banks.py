"""Investment funds leave the candidates; banks get a lane of their own."""

from datetime import date
import unittest

from src.baseline_resolution import resolve_baseline
from src.data_sources.sec_form4_source import compact_listing, investment_fund_type
from src.insider_supplement import SupplementedInsiderLookup
from src.research_baseline import build_research_baseline
from src.research_ranking import SCREEN_STATUSES, build_research_queue, is_bank
from tests.test_baseline_resolution import _baseline, _candidate, _series
from tests.test_screen import _row, _v3_scored


def _submissions(*forms):
    """Forms as names (filed 2026-10-01) or (name, filing date) pairs."""
    pairs = [form if isinstance(form, tuple) else (form, "2026-10-01") for form in forms]
    forms = [name for name, _ in pairs]
    return {"cik": "1", "filings": {"recent": {
        "form": forms, "filingDate": [filed for _, filed in pairs],
        "accessionNumber": [f"a{i}" for i in range(len(forms))],
        "primaryDocument": ["x.xml"] * len(forms),
    }}}


class FundTypeTests(unittest.TestCase):
    def test_edgar_forms_say_which_issuers_are_funds(self):
        def kind(*forms):
            return investment_fund_type(compact_listing(_submissions(*forms), date(2026, 10, 27)))

        self.assertEqual(kind("4", "N-CSR", "NPORT-P"), "closed_end_fund")
        self.assertEqual(kind("8-K", "10-K", "N-54A", "N-2"), "business_development_company")
        self.assertIsNone(kind("10-K", "10-Q", "8-K", "4"))
        # A registered fund that also filed N-2 is a closed-end fund.
        self.assertEqual(kind("N-2", "N-CSRS"), "closed_end_fund")
        # A shelf registration alone does not make a BDC.
        self.assertIsNone(kind(("N-2", "2015-08-31"), "10-K"))

    def test_companies_that_used_to_be_funds_are_companies(self):
        def kind(*forms):
            return investment_fund_type(compact_listing(_submissions(*forms), date(2026, 10, 27)))

        # NewtekOne and Medallion withdrew their BDC elections.
        self.assertIsNone(kind(("N-54A", "2013-02-07"), ("N-54C", "2019-12-27"), "10-K"))
        # A fund that became a REIT stopped filing fund reports in 2022.
        self.assertIsNone(kind(("N-CSR", "2022-03-11"), ("NPORT-P", "2022-08-29"), "10-K"))
        # An election made long ago and never withdrawn still counts.
        self.assertEqual(kind(("N-54A", "2011-05-19"), "10-K"), "business_development_company")

    def test_lookup_reads_the_cached_listing_and_never_guesses(self):
        class Source:
            def __init__(self, listing=None, error=False):
                self.listing, self.error = listing, error

            def ownership_filings(self, cik):
                if self.error:
                    raise OSError("offline")
                return self.listing

        fund = compact_listing(_submissions("N-CSR"), date(2026, 10, 27))
        lookup = SupplementedInsiderLookup([], None, lambda t: "1", form4_source=Source(fund))
        self.assertEqual(lookup.investment_fund("BGR"), "closed_end_fund")
        broken = SupplementedInsiderLookup([], None, lambda t: "1", form4_source=Source(error=True))
        self.assertIsNone(broken.investment_fund("BGR"))
        unmapped = SupplementedInsiderLookup([], None, lambda t: None, form4_source=Source(fund))
        self.assertIsNone(unmapped.investment_fund("BGR"))


def _bank(ticker, earnings_yield, **extra):
    return _row(ticker, 0.3, sector="Financial Services", industry="Banks - Regional",
                pit_earnings_yield=earnings_yield, **extra)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        rows = [_row(f"S{i:02d}", i / 20) for i in range(10)]
        rows += [_bank(f"B{i}", 0.02 * (i + 1)) for i in range(6)]   # yields .02 .. .12
        rows += [
            _bank("NOEY", None),
            _bank("THINB", 0.5, dollar_volume=5_000),
            _row("FUND", 0.4, sector="Financial Services", industry="Asset Management",
                 investment_fund="closed_end_fund"),
            _row("ADVISER", 0.4, sector="Financial Services", industry="Asset Management"),
        ]
        self.queue, self.summary = build_research_queue(_v3_scored(rows))
        self.by = {row["ticker"]: row for row in self.queue}

    def test_cheaper_half_of_banks_passes_in_its_own_lane(self):
        statuses = {f"B{i}": self.by[f"B{i}"]["screen_status"] for i in range(6)}
        self.assertEqual(statuses, {
            "B0": "bank_dearer_half", "B1": "bank_dearer_half", "B2": "bank_dearer_half",
            "B3": "passes_bank_lane", "B4": "passes_bank_lane", "B5": "passes_bank_lane",
        })
        self.assertAlmostEqual(self.by["B5"]["bank_earnings_yield_percentile"], 100.0)
        self.assertIsNone(self.by["S00"]["bank_earnings_yield_percentile"])

    def test_banks_without_earnings_or_liquidity_do_not_pass(self):
        self.assertEqual(self.by["NOEY"]["screen_status"], "not_scored_by_v3")
        self.assertEqual(self.by["THINB"]["screen_status"], "below_liquidity_floor")

    def test_funds_are_labelled_and_listed_after_candidates(self):
        self.assertEqual(self.by["FUND"]["screen_status"], "investment_fund")
        self.assertEqual(self.by["FUND"]["investment_fund"], "closed_end_fund")
        self.assertEqual(self.by["ADVISER"]["screen_status"], "not_scored_by_v3")
        ranks = {row["ticker"]: row["research_rank"] for row in self.queue}
        candidates = [r for t, r in ranks.items() if t not in ("FUND", "THINB")]
        self.assertGreater(ranks["FUND"], max(candidates))
        self.assertLess(ranks["FUND"], ranks["THINB"])

    def test_v3_lane_and_defensive_scores_are_untouched_by_banks(self):
        quintiles = [self.by[f"S{i:02d}"]["v3_quintile"] for i in range(10)]
        self.assertEqual(quintiles, [5, 5, 4, 4, 3, 3, 2, 2, 1, 1])
        self.assertIsNone(self.by["B5"]["score_defensive"])
        self.assertEqual(self.summary["by_screen"]["passes_bank_lane"], 3)
        self.assertEqual(self.summary["by_screen"]["investment_fund"], 1)
        self.assertTrue(set(self.summary["by_screen"]) <= set(SCREEN_STATUSES))

    def test_lane_can_be_switched_off(self):
        rows = [_bank(f"B{i}", 0.02 * (i + 1)) for i in range(4)]
        queue, _ = build_research_queue(_v3_scored(rows), {"bank_lane": False})
        self.assertEqual({row["screen_status"] for row in queue}, {"not_scored_by_v3"})

    def test_is_bank(self):
        self.assertTrue(is_bank({"sector": "Financial Services", "industry": "Banks - Diversified"}))
        self.assertFalse(is_bank({"sector": "Financial Services", "industry": "Asset Management"}))

    def test_cohort_freezes_the_lane_and_the_fund_label(self):
        baseline = build_research_baseline(self.queue, "RUN", "2026-11-16T00:00:00+00:00", {"top_n": 2})
        by = {row["ticker"]: row for row in baseline["candidates"]}
        self.assertEqual(by["B5"]["screen_status"], "passes_bank_lane")
        self.assertAlmostEqual(by["B5"]["bank_earnings_yield_percentile"], 100.0)
        self.assertAlmostEqual(by["B5"]["pit_earnings_yield"], 0.12)
        self.assertEqual(by["FUND"]["classification"], "investment_fund")


class ResolutionTests(unittest.TestCase):
    def test_bank_lane_is_reported_beside_the_v3_lane(self):
        series = {"SPY": _series("2025-12-01", 200, 100.0, 0.0)}
        candidates = []
        for index in range(12):
            for prefix, status, drift in (("C", "passes_bank_lane", 0.002), ("D", "bank_dearer_half", -0.002),
                                          ("V", "passes", 0.0)):
                ticker = f"{prefix}{index:02d}"
                series[ticker] = _series("2025-12-01", 200, 10.0, drift + index / 100000)
                row = _candidate(ticker, "v2_fallback" if prefix != "V" else "v3_quintile_4")
                row["screen_status"] = status
                if prefix != "V":
                    row["bank_earnings_yield_percentile"] = (60.0 if prefix == "C" else 20.0) + index
                candidates.append(row)
        resolution = resolve_baseline(_baseline(candidates), series, as_of=date(2026, 2, 10),
                                      config={"minimum_cohort_size": 1})
        lane = resolution["horizons"]["1M"]["bank_lane_comparison"]
        self.assertEqual(lane["passes_bank_lane"]["resolved"], 12)
        self.assertEqual(lane["bank_dearer_half"]["resolved"], 12)
        self.assertEqual(lane["passes"]["resolved"], 12)
        self.assertGreater(lane["cheaper_minus_dearer_median_percent"], 0)
        self.assertGreater(lane["bank_lane_minus_v3_lane_median_percent"], 0)

    def test_cohorts_without_a_bank_lane_have_no_section(self):
        candidates = [_candidate("A", "v3_quintile_4")]
        resolution = resolve_baseline(
            _baseline(candidates), {"SPY": _series("2025-12-01", 200, 100.0, 0.0),
                                    "A": _series("2025-12-01", 200, 10.0, 0.0)},
            as_of=date(2026, 2, 10), config={"minimum_cohort_size": 1},
        )
        self.assertNotIn("bank_lane_comparison", resolution["horizons"]["1M"])



class ReportTests(unittest.TestCase):
    def test_cycle_report_shows_the_bank_lane(self):
        from src.monthly import STEP_ORDER, build_cycle_report

        state = {"cycle": "2026-12", "status": "completed", "attempts": 1, "run_id": "RUN",
                 "warnings": [], "steps": {name: {"status": "done"} for name in STEP_ORDER}}
        report = build_cycle_report(state, [{
            "run_id": "NOV", "kind": "research", "horizons": ["1M"],
            "summary": {"1M": {"selected": {}, "control": {}, "bank_lane": {
                "resolved": 58, "median_excess_percent": 1.5,
                "cheaper_minus_dearer_median_percent": 2.0,
                "bank_lane_minus_v3_lane_median_percent": -0.5,
            }}},
        }])
        self.assertIn("## Bank lane", report)
        self.assertIn("research `NOV` 1M, 58 banks", report)


if __name__ == "__main__":
    unittest.main()
