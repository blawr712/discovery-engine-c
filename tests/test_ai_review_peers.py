from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.ai_review_peers import (
    METRICS,
    SicIndex,
    build_peer_table,
    choose_sic_prefix,
    close_on_or_before,
    closest_peers,
    float_frame_periods,
    latest_public_floats,
    measure_filer,
    peer_metrics,
    shortlist_leaders,
)

ANCHOR = datetime(2026, 10, 20, 0, 55, tzinfo=timezone.utc)


def row(ticker, sic, market_cap, revenue=100.0, **extra):
    return {
        "ticker": ticker, "sic": sic, "cik": extra.pop("cik", None),
        "pit_market_cap": market_cap, "pit_revenue_ttm": revenue,
        "pit_net_income_ttm": revenue * 0.1 if revenue is not None else None,
        "pit_operating_margin_ttm": 0.12,
        "pit_earnings_yield": 0.05, "pit_reporting_currency": "USD",
        "screen_status": extra.pop("screen_status", "low_v3_quintile"), **extra,
    }


class MetricTests(unittest.TestCase):
    def test_net_margin_is_derived_and_money_is_in_dollars(self):
        values = peer_metrics(
            {"pit_market_cap": 1000.0, "pit_revenue_ttm": 200.0, "pit_net_income_ttm": 20.0,
             "pit_reporting_currency": "EUR"},
            {("EUR", "USD"): 1.1},
        )
        self.assertEqual(list(values), list(METRICS))
        self.assertEqual(values["net_margin_ttm"], 0.1)
        self.assertAlmostEqual(values["market_cap_usd"], 1100.0)
        self.assertAlmostEqual(values["revenue_ttm_usd"], 220.0)

    def test_no_rate_means_no_dollar_figures(self):
        values = peer_metrics({"pit_market_cap": 1000.0, "pit_reporting_currency": "CHF"}, {})
        self.assertIsNone(values["market_cap_usd"])


class ClosestPeerTests(unittest.TestCase):
    def test_nearest_by_market_cap_any_screen_status(self):
        company = row("CO", "3841", 100.0)
        rows = [company] + [row(f"P{size}", "3841", float(size)) for size in (10, 50, 90, 120, 400, 1000)]
        rows.append(row("OTHER", "3842", 100.0))
        rows.append(row("DUP", "3841", 100.0, duplicate_of="CO"))
        rows.append(row("NOFACTS", "3841", None, revenue=None))
        peers, unusable = closest_peers(company, rows, "3841", limit=3)
        self.assertEqual([peer["ticker"] for peer in peers], ["P90", "P120", "P50"])
        self.assertEqual(unusable, [{"ticker": "NOFACTS", "group": "closest", "reason": "no SEC fundamentals"}])

    def test_widens_to_three_digits_when_fewer_than_three_share_four(self):
        rows = [row("CO", "3841", 100.0), row("A", "3841", 90.0), row("B", "3842", 80.0), row("C", "3845", 70.0)]
        self.assertEqual(choose_sic_prefix("3841", rows, "CO"), ("384", True))
        rows.append(row("D", "3841", 60.0))
        rows.append(row("E", "3841", 50.0))
        self.assertEqual(choose_sic_prefix("3841", rows, "CO"), ("3841", False))
        self.assertEqual(choose_sic_prefix(None, rows, "CO"), (None, False))


class LeaderTests(unittest.TestCase):
    def test_shortlist_by_public_float_within_the_code(self):
        index = {
            "0000000001": {"sic": "3841", "tickers": ["BIG"], "name": "Big"},
            "0000000002": {"sic": "3841", "tickers": ["MID"], "name": "Mid"},
            "0000000003": {"sic": "3841", "tickers": [], "name": "No ticker"},
            "0000000004": {"sic": "2834", "tickers": ["PHARMA"], "name": "Pharma"},
            "0000000005": {"sic": "3841", "tickers": ["SELF"], "name": "Self"},
            "0000000006": {"sic": "3841", "tickers": ["NOFLOAT"], "name": "Foreign"},
        }
        floats = {"0000000001": 5e9, "0000000002": 1e9, "0000000003": 9e9, "0000000004": 9e9, "0000000005": 8e9}
        shortlist = shortlist_leaders("3841", index, floats, {"0000000005"})
        self.assertEqual([item["ticker"] for item in shortlist], ["BIG", "MID"])

    def test_latest_float_per_filer(self):
        frames = [
            {"data": [{"cik": 1, "end": "2026-03-31", "val": 10}, {"cik": 2, "end": "2026-03-31", "val": 5}]},
            {"data": [{"cik": 1, "end": "2026-06-30", "val": 20}, {"cik": 3, "end": "2026-06-30", "val": 0}]},
        ]
        self.assertEqual(latest_public_floats(frames), {"0000000001": 20.0, "0000000002": 5.0})

    def test_frames_are_the_four_quarters_before_the_anchor(self):
        self.assertEqual(
            float_frame_periods(date(2026, 10, 20)),
            ["CY2026Q3I", "CY2026Q2I", "CY2026Q1I", "CY2025Q4I"],
        )
        self.assertEqual(float_frame_periods(date(2026, 1, 5), 2), ["CY2025Q4I", "CY2025Q3I"])


class TableTests(unittest.TestCase):
    def setUp(self):
        self.company = {**row("CO", "3841", 100.0, cik="0000000009"), "company_name": "Company",
                        "industry": "Medical Devices", "pit_earnings_yield": 0.15}
        self.run_rows = [
            self.company,
            row("A", "3841", 90.0, cik="0000000011"),
            row("B", "3841", 110.0, cik="0000000012"),
            row("C", "3841", 300.0, cik="0000000001", screen_status="passes"),
        ]
        self.index = {
            "0000000001": {"sic": "3841", "tickers": ["C"], "name": "C Corp"},
            "0000000002": {"sic": "3841", "tickers": ["GIANT"], "name": "Giant"},
            "0000000003": {"sic": "3841", "tickers": ["NOPRICE"], "name": "No price"},
            "0000000009": {"sic": "3841", "tickers": ["CO"], "name": "Company"},
        }
        self.floats = {"0000000001": 1e3, "0000000002": 9e9, "0000000003": 5e9, "0000000009": 9e10}
        self.measured = []

    def measure(self, candidate):
        self.measured.append(candidate["ticker"])
        if candidate["ticker"] == "NOPRICE":
            return None, "no price on or before the anchor date"
        return row(candidate["ticker"], "3841", 5e9, revenue=2e9), None

    def table(self, **overrides):
        arguments = dict(
            company_sic="3841", company_sic_description="Surgical", company_cik="0000000009",
            run_rows=self.run_rows, sic_index=self.index, public_floats=self.floats,
            measure_leader=self.measure, anchor=ANCHOR, run_id="RUN",
        )
        arguments.update(overrides)
        return build_peer_table(self.company, **arguments)

    def test_groups_reasons_and_summary(self):
        table = self.table()
        self.assertEqual(table["status"], "ok")
        self.assertEqual(table["sic_used"], "3841")
        self.assertFalse(table["widened"])
        groups = [(peer["ticker"], peer["group"]) for peer in table["peers"]]
        self.assertEqual(groups, [("B", "closest"), ("A", "closest"), ("C", "closest"),
                                  ("GIANT", "leader"), ("C", "leader")])
        # A leader inside the run keeps the run's figures and is not re-measured.
        self.assertEqual(self.measured, ["GIANT", "NOPRICE"])
        self.assertEqual(next(p for p in table["peers"] if p["group"] == "leader" and p["ticker"] == "C")["figures_from"], "run")
        self.assertIn(
            {"ticker": "NOPRICE", "group": "leader", "reason": "no price on or before the anchor date"},
            table["unusable_peers"],
        )
        # The company is never its own leader.
        self.assertNotIn("CO", [peer["ticker"] for peer in table["peers"]])
        summary = table["summary"]["pit_earnings_yield"]
        self.assertEqual(summary["company"], 0.15)
        self.assertEqual(summary["closest_median"], 0.05)
        self.assertEqual(summary["company_percentile_among_closest"], 100.0)
        self.assertEqual(summary["leader_count"], 2)
        json.dumps(table)

    def test_insufficient_peers(self):
        table = self.table(run_rows=[self.company], public_floats={})
        self.assertEqual(table["status"], "insufficient_peers")
        self.assertIn("fewer than 3", table["status_reason"])

    def test_no_sic_code(self):
        table = self.table(company_sic=None)
        self.assertEqual(table["status"], "insufficient_peers")
        self.assertEqual(table["peers"], [])


class MeasureTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({
            "Date": pd.to_datetime(["2026-10-16", "2026-10-19", "2026-10-21"]),
            "Close": [9.0, 10.0, 50.0],
            "Stock Splits": [0.0, 0.0, 0.0],
        })

    def test_close_is_the_last_on_or_before_the_anchor(self):
        self.assertEqual(close_on_or_before(self.frame(), date(2026, 10, 20)), 10.0)
        self.assertIsNone(close_on_or_before(self.frame(), date(2026, 1, 1)))
        self.assertIsNone(close_on_or_before(None, date(2026, 1, 1)))

    def test_measures_like_a_live_run(self):
        from tests.test_fundamentals_pit import _extract

        extract = _extract()
        signals, reason = measure_filer(
            "TEST", date(2026, 3, 1), facts_lookup=lambda t: extract,
            price_frame_lookup=lambda t: pd.DataFrame({
                "Date": pd.to_datetime(["2026-02-27", "2026-03-02"]), "Close": [2.0, 9.0],
            }),
            fx_rates={},
        )
        self.assertIsNone(reason)
        self.assertEqual(signals["latest_close"], 2.0)
        self.assertEqual(signals["pit_market_cap"], 2200.0)
        self.assertEqual(signals["pit_revenue_ttm"], 640.0)

    def test_reasons_when_a_filer_cannot_be_measured(self):
        self.assertEqual(
            measure_filer("X", date(2026, 10, 20), facts_lookup=lambda t: None,
                          price_frame_lookup=lambda t: self.frame(), fx_rates={}),
            (None, "no SEC fundamentals"),
        )

        def boom(ticker):
            raise LookupError("no CIK")

        self.assertEqual(
            measure_filer("X", date(2026, 10, 20), facts_lookup=boom,
                          price_frame_lookup=lambda t: self.frame(), fx_rates={})[1],
            "no SEC fundamentals (LookupError)",
        )


class SicIndexTests(unittest.TestCase):
    def test_reads_each_filer_once_and_retries_failures(self):
        calls = []

        def submissions(cik):
            calls.append(cik)
            if cik == "0000000003":
                raise ConnectionError("blocked")
            return {"sic": "3841", "sic_description": "Surgical", "name": f"Co {cik}",
                    "tickers": ["T"], "exchanges": ["Nasdaq"], "entity_type": "operating"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sic.json"
            index = SicIndex(path, submissions, clock=lambda: ANCHOR)
            counts = index.update(["1", "2", "3"])
            self.assertEqual((counts["indexed"], counts["failed"]), (2, 1))
            reopened = SicIndex(path, submissions, clock=lambda: ANCHOR)
            self.assertEqual(reopened.entries["0000000001"]["sic"], "3841")
            calls.clear()
            reopened.update(["1", "2", "3"])
            self.assertEqual(calls, ["0000000003"])


if __name__ == "__main__":
    unittest.main()
