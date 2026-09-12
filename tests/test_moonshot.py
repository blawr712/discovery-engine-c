import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.config import MOONSHOT_CONFIG
from src.moonshot import build_moonshot_analysis, export_moonshot_analysis


class MoonshotTests(unittest.TestCase):
    COMPLETED_AT = "2026-08-23T12:00:00+00:00"
    FUNDAMENTAL_DATE = 1782777600  # 2026-06-30 UTC

    def test_builds_separate_upside_and_risk_scores_without_mutation(self):
        strong = self._row(
            "STRONG", 5_000_000, revenue_growth=.6, earnings_growth=.6,
            operating_margin=.25, operating_cash_flow=2_000_000,
            free_cash_flow=1_000_000, total_cash=5_000_000,
            total_debt=0, debt_to_equity=0, price_to_sales=.8,
        )
        risky = self._row(
            "RISKY", 3_000_000, revenue_growth=.6, earnings_growth=.6,
            operating_margin=-.5, operating_cash_flow=-2_000_000,
            free_cash_flow=-1_000_000, total_cash=100_000,
            total_debt=5_000_000, debt_to_equity=500, price_to_sales=1,
        )
        source = [strong, risky]
        original = copy.deepcopy(source)

        analysis = build_moonshot_analysis(
            source,
            "run-1",
            self.COMPLETED_AT,
            market_evidence={
                "STRONG": self._market(),
                "RISKY": self._market(
                    average_dollar_volume_30d=10_000,
                    annualized_volatility_percent=150,
                    maximum_drawdown_percent=80,
                    dilution_percent=110,
                    reverse_split_count_1y=1,
                ),
            },
        )

        self.assertEqual(source, original)
        self.assertTrue(analysis["official_scores_and_ranks_unchanged"])
        by_ticker = {row["ticker"]: row for row in analysis["candidates"]}
        self.assertEqual(by_ticker["STRONG"]["classification"], "priority_research")
        self.assertEqual(by_ticker["STRONG"]["upside_score"], 100)
        self.assertLess(by_ticker["STRONG"]["risk_of_ruin_score"], 10)
        self.assertEqual(by_ticker["RISKY"]["classification"], "speculative_watch")
        self.assertEqual(by_ticker["RISKY"]["risk_band"], "severe")
        self.assertTrue(
            analysis["market_evidence_summary"]["cross_section_comparable"]
        )

    def test_selects_configured_market_cap_lane_and_operating_equities(self):
        rows = [
            self._row("TOO_SMALL", 1_999_999),
            self._row("NANO", 2_000_000),
            self._row("MICRO", 50_000_000),
            self._row("TOO_LARGE", 50_000_001),
            self._row("SHELL", 5_000_000, asset_type="shell_company"),
            self._row("ETF", 5_000_000, quote_type="ETF"),
        ]

        analysis = build_moonshot_analysis(rows, "run-1", self.COMPLETED_AT)

        self.assertEqual(
            {row["ticker"] for row in analysis["candidates"]},
            {"NANO", "MICRO"},
        )
        self.assertEqual(analysis["summary"]["nano_cap_count"], 1)
        self.assertEqual(analysis["summary"]["micro_cap_count"], 1)

    def test_missing_and_stale_inputs_cannot_receive_priority_status(self):
        missing = self._row("MISSING", 5_000_000, fundamental_data_timestamp=None)
        stale = self._row(
            "STALE", 5_000_000, fundamental_data_timestamp=946684800,
            revenue_growth=1, earnings_growth=1, operating_margin=.5,
            operating_cash_flow=5_000_000, free_cash_flow=1_000_000,
            total_cash=5_000_000, total_debt=0, debt_to_equity=0,
            price_to_sales=.5,
        )

        analysis = build_moonshot_analysis(
            [missing, stale],
            "run-1",
            self.COMPLETED_AT,
            market_evidence={"MISSING": self._market(), "STALE": self._market()},
        )
        by_ticker = {row["ticker"]: row for row in analysis["candidates"]}

        self.assertEqual(by_ticker["MISSING"]["classification"], "insufficient_evidence")
        self.assertEqual(by_ticker["STALE"]["classification"], "insufficient_evidence")
        self.assertEqual(by_ticker["STALE"]["fundamental_data_quality"], "stale")
        self.assertEqual(by_ticker["STALE"]["moonshot_confidence"], 0)
        self.assertEqual(by_ticker["STALE"]["risk_band"], "unresolved")

    def test_priority_requires_stronger_confidence_than_general_classification(self):
        row = self._row(
            "PARTIAL", 5_000_000, revenue_growth=.6, operating_margin=.25,
            operating_cash_flow=2_000_000, total_cash=5_000_000,
            total_debt=0,
        )

        candidate = build_moonshot_analysis(
            [row],
            "run-1",
            self.COMPLETED_AT,
            market_evidence={"PARTIAL": self._market()},
        )["candidates"][0]

        self.assertGreaterEqual(candidate["upside_score"], 75)
        self.assertLess(candidate["moonshot_confidence"], 80)
        self.assertNotEqual(candidate["classification"], "priority_research")

    def test_requires_market_risk_evidence_before_watch_classification(self):
        row = self._row(
            "NO_MARKET", 5_000_000, revenue_growth=.6, earnings_growth=.6,
            operating_margin=.25, operating_cash_flow=2_000_000,
            free_cash_flow=1_000_000, total_cash=5_000_000,
            total_debt=0, debt_to_equity=0, price_to_sales=.8,
        )

        candidate = build_moonshot_analysis(
            [row], "run-1", self.COMPLETED_AT,
        )["candidates"][0]

        self.assertEqual(candidate["classification"], "market_data_required")
        self.assertEqual(candidate["risk_band"], "unresolved")
        self.assertEqual(candidate["market_risk_confidence"], 0)

    def test_zero_dollar_volume_is_available_and_maximum_liquidity_risk(self):
        row = self._row(
            "ILLIQUID", 5_000_000, revenue_growth=.6, earnings_growth=.6,
            operating_margin=.25, operating_cash_flow=2_000_000,
            free_cash_flow=1_000_000, total_cash=5_000_000,
            total_debt=0, debt_to_equity=0, price_to_sales=.8,
        )
        evidence = self._market(average_dollar_volume_30d=0)

        candidate = build_moonshot_analysis(
            [row], "run-1", self.COMPLETED_AT,
            market_evidence={"ILLIQUID": evidence},
        )["candidates"][0]
        liquidity = next(
            factor for factor in candidate["risk_factors"]
            if factor["name"] == "liquidity"
        )

        self.assertTrue(liquidity["available"])
        self.assertEqual(liquidity["points"], liquidity["max_points"])
        self.assertEqual(candidate["market_risk_confidence"], 100)
        self.assertNotIn("Trading liquidity", candidate["missing_inputs"])

    def test_comparable_cohort_allows_explicit_residual_exclusion(self):
        rows = [
            self._row(f"T{index:03d}", 5_000_000)
            for index in range(100)
        ]
        evidence = {
            row["ticker"]: self._market()
            for row in rows[:99]
        }

        summary = build_moonshot_analysis(
            rows,
            "run-1",
            self.COMPLETED_AT,
            market_evidence=evidence,
        )["market_evidence_summary"]

        self.assertEqual(summary["coverage_percent"], 99)
        self.assertEqual(summary["minimum_coverage_percent"], 99)
        self.assertTrue(summary["cross_section_comparable"])
        self.assertEqual(
            summary["excluded_candidates"],
            [{"ticker": "T099", "reason": "unavailable_market_evidence"}],
        )
        below_threshold = build_moonshot_analysis(
            rows,
            "run-1",
            self.COMPLETED_AT,
            market_evidence={key: value for key, value in evidence.items()
                             if key != "T098"},
        )["market_evidence_summary"]
        self.assertFalse(below_threshold["cross_section_comparable"])

    def test_exports_deterministic_csv_and_json_artifacts(self):
        analysis = build_moonshot_analysis(
            [self._row("ONE", 5_000_000)], "run-1", self.COMPLETED_AT,
        )
        with tempfile.TemporaryDirectory() as directory:
            csv_path, json_path = export_moonshot_analysis(
                analysis, Path(directory),
            )
            with json_path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(saved["run_id"], "run-1")
        self.assertEqual(rows[0]["ticker"], "ONE")
        self.assertIn("market_cap_headroom", rows[0]["upside_factor_breakdown"])

    def test_rejects_invalid_weight_configuration(self):
        config = copy.deepcopy(MOONSHOT_CONFIG)
        config["upside_weights"]["revenue_growth"] = 24

        with self.assertRaisesRegex(ValueError, "must total 100"):
            build_moonshot_analysis(
                [self._row("ONE", 5_000_000)],
                "run-1", self.COMPLETED_AT, config,
            )

    def test_rejects_invalid_cross_section_coverage_threshold(self):
        config = copy.deepcopy(MOONSHOT_CONFIG)
        config["minimum_cross_section_coverage_percent"] = 101

        with self.assertRaisesRegex(ValueError, "coverage threshold"):
            build_moonshot_analysis(
                [self._row("ONE", 5_000_000)],
                "run-1", self.COMPLETED_AT, config,
            )

    def _row(self, ticker, market_cap, **overrides):
        row = {
            "ticker": ticker, "company_name": f"{ticker} Corp",
            "country": "US", "exchange": "NASDAQ", "sector": "Technology",
            "market_cap": market_cap, "asset_type": "operating_equity",
            "quote_type": "EQUITY", "status": "FILTERED",
            "reason_flags": "Below minimum market cap",
            "fundamental_data_timestamp": self.FUNDAMENTAL_DATE,
            "revenue_growth": None, "earnings_growth": None,
            "operating_margin": None, "profit_margin": None,
            "operating_cash_flow": None, "free_cash_flow": None,
            "total_cash": None, "total_debt": None,
            "debt_to_equity": None, "price_to_sales": None,
        }
        row.update(overrides)
        return row

    @staticmethod
    def _market(**overrides):
        evidence = {
            "data_status": "complete",
            "captured_at": "2026-08-23T12:00:00+00:00",
            "average_dollar_volume_30d": 2_000_000,
            "annualized_volatility_percent": 25,
            "maximum_drawdown_percent": 10,
            "dilution_percent": 0,
            "reverse_split_count_1y": 0,
            "errors": [],
        }
        evidence.update(overrides)
        return evidence


if __name__ == "__main__":
    unittest.main()
