from datetime import date
import unittest

import pandas as pd

from src.survivorship import (
    SCENARIOS,
    Exit,
    classify_exit,
    compounded,
    dead_observations,
    delisted_candidates,
    estimated_market_cap,
    monthly_rates,
    public_float_as_of,
    sector_for_sic,
)


def _submissions(filings, sic="3674"):
    return {"name": "DEAD CO", "sic": sic, "exit_filings": filings}


class CandidateTests(unittest.TestCase):
    def test_only_issuers_not_listed_today_within_the_window(self):
        notices = [
            {"cik": "1", "name": "GONE", "form": "25-NSE", "filed": "2021-03-01"},
            {"cik": "1", "name": "GONE", "form": "25-NSE", "filed": "2019-05-01"},
            {"cik": "2", "name": "MOVED EXCHANGE", "form": "25", "filed": "2021-08-13"},
            {"cik": "3", "name": "TOO EARLY", "form": "25-NSE", "filed": "2014-01-01"},
        ]
        found = delisted_candidates(notices, {"0000000002"}, date(2016, 1, 1), date(2026, 10, 1))
        self.assertEqual(list(found), ["0000000001"])
        self.assertEqual(found["0000000001"]["notices"], [date(2019, 5, 1), date(2021, 3, 1)])


class ClassificationTests(unittest.TestCase):
    def _classify(self, filings, notices=(date(2021, 7, 1),), sic="3674"):
        return classify_exit("1", "DEAD CO", list(notices), _submissions(filings, sic))

    def test_exit_date_is_the_last_notice(self):
        exit = self._classify([], notices=[date(2019, 5, 1), date(2021, 7, 1)])
        self.assertEqual(exit.exit_date, date(2021, 7, 1))
        self.assertEqual(exit.exit_type, "other")

    def test_bankruptcy_wins_even_with_an_asset_sale(self):
        exit = self._classify([
            {"form": "8-K", "filed": "2020-11-02", "items": ["1.03"]},
            {"form": "DEFM14A", "filed": "2021-03-01"},
        ])
        self.assertEqual(exit.exit_type, "bankruptcy")

    def test_acquisition_from_merger_documents_or_change_in_control(self):
        self.assertEqual(self._classify([{"form": "DEFM14A", "filed": "2021-03-01"}]).exit_type, "acquired")
        self.assertEqual(
            self._classify([{"form": "8-K", "filed": "2021-06-30", "items": ["2.01", "3.01", "5.01"]}]).exit_type,
            "acquired",
        )

    def test_noncompliance_without_a_deal(self):
        exit = self._classify([{"form": "8-K", "filed": "2021-02-01", "items": ["3.01"]}])
        self.assertEqual(exit.exit_type, "delisted_noncompliance")

    def test_evidence_outside_the_window_is_ignored(self):
        exit = self._classify([
            {"form": "8-K", "filed": "2017-01-01", "items": ["1.03"]},
            {"form": "DEFM14A", "filed": "2019-01-01"},
        ])
        self.assertEqual(exit.exit_type, "other")

    def test_blank_checks_and_sectors(self):
        self.assertTrue(self._classify([], sic="6770").blank_check)
        self.assertEqual(sector_for_sic("6022"), "Financial Services")
        self.assertEqual(sector_for_sic("6798"), "Real Estate")
        self.assertEqual(sector_for_sic("3674"), "Unknown")
        self.assertEqual(sector_for_sic(None), "Unknown")


def _extract():
    quarters = [("2019-01-01", "2019-03-31", "2019-05-01"), ("2019-04-01", "2019-06-30", "2019-08-01"),
                ("2019-07-01", "2019-09-30", "2019-11-01"), ("2019-10-01", "2019-12-31", "2020-02-15")]
    return {"facts": {
        "revenue": [{"start": s, "end": e, "filed": f, "val": 10.0, "tag": "us-gaap:Revenues"}
                    for s, e, f in quarters],
        "operating_income": [{"start": s, "end": e, "filed": f, "val": -8.0,
                              "tag": "us-gaap:OperatingIncomeLoss"} for s, e, f in quarters],
        "cash": [{"start": None, "end": "2019-09-30", "filed": "2019-11-01", "val": 6.0,
                  "tag": "us-gaap:CashAndCashEquivalentsAtCarryingValue"},
                 {"start": None, "end": "2019-12-31", "filed": "2020-02-15", "val": 5.0,
                  "tag": "us-gaap:CashAndCashEquivalentsAtCarryingValue"}],
        "public_float": [
            {"start": None, "end": "2019-06-30", "filed": "2020-02-15", "val": 60e6,
             "tag": "dei:EntityPublicFloat"},
        ],
    }}


class SizeTests(unittest.TestCase):
    def test_public_float_is_point_in_time(self):
        extract = _extract()
        self.assertIsNone(public_float_as_of(extract, date(2020, 1, 31)))  # not yet filed
        self.assertEqual(public_float_as_of(extract, date(2020, 3, 31)), 60e6)
        self.assertIsNone(public_float_as_of(extract, date(2021, 6, 30)))  # too stale

    def test_market_cap_estimate(self):
        self.assertAlmostEqual(estimated_market_cap(75e6, 0.25), 100e6)
        self.assertIsNone(estimated_market_cap(None))
        self.assertIsNone(estimated_market_cap(0.0))


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.exit = Exit("0000000001", "DEAD CO", date(2020, 6, 15), "bankruptcy", "3674", "Unknown", ())
        self.periods = [("2020-01", date(2020, 1, 31)), ("2020-03", date(2020, 3, 31)),
                        ("2020-05", date(2020, 5, 31)), ("2020-07", date(2020, 7, 31))]

    def test_rows_only_while_alive_reporting_and_in_band(self):
        frame = dead_observations([self.exit], {self.exit.cik: _extract()}, self.periods, 10e6, 1e9)
        # Jan: float not filed yet; Jul: after exit.
        self.assertEqual(list(frame["period"]), ["2020-03", "2020-05"])
        self.assertTrue((frame["size_basis"] == "in_band").all())
        self.assertAlmostEqual(frame["estimated_market_cap"].iloc[0], 80e6)
        self.assertLess(frame["pit_operating_margin_ttm"].iloc[0], 0)

    def test_unknown_size_is_opt_in(self):
        frame = dead_observations(
            [self.exit], {self.exit.cik: _extract()}, self.periods, 10e6, 1e9, include_unknown_size=True,
        )
        self.assertEqual(list(frame["size_basis"]), ["unknown", "in_band", "in_band"])

    def test_out_of_band_and_blank_checks_are_dropped(self):
        self.assertTrue(dead_observations([self.exit], {self.exit.cik: _extract()}, self.periods, 10e6, 50e6).empty)
        blank = Exit("0000000002", "SPAC", date(2020, 6, 15), "other", "6770", "Financial Services", ())
        self.assertTrue(dead_observations([blank], {blank.cik: _extract()}, self.periods, 10e6, 1e9).empty)


class ReturnTests(unittest.TestCase):
    def test_lifetime_return_is_spread_evenly(self):
        dead = pd.DataFrame({
            "ticker": ["A", "A", "B"], "period": ["1", "2", "1"],
            "exit_type": ["bankruptcy", "bankruptcy", "acquired"],
        })
        rates = monthly_rates(dead, SCENARIOS["central"])
        self.assertAlmostEqual(compounded(rates[:2]), -95.0, places=6)
        self.assertAlmostEqual(rates.iloc[2], 10.0, places=6)

    def test_compounded_ignores_missing_months(self):
        self.assertAlmostEqual(compounded(pd.Series([10.0, None, 10.0])), 21.0)


if __name__ == "__main__":
    unittest.main()
