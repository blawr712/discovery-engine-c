import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.config import MOONSHOT_CONFIG
from src.moonshot import build_moonshot_analysis
from src.moonshot_calibration import (
    build_forward_baseline,
    build_moonshot_calibration,
    export_forward_baseline,
    export_moonshot_calibration,
)


class MoonshotCalibrationTests(unittest.TestCase):
    COMPLETED_AT = "2026-08-23T12:00:00+00:00"
    FUNDAMENTAL_DATE = 1782777600

    def test_baseline_scenario_reproduces_official_moonshot_order(self):
        analysis = self._analysis()

        calibration = build_moonshot_calibration(analysis)

        self.assertEqual(calibration["baseline_integrity"]["mismatch_count"], 0)
        baseline = calibration["scenarios"]["baseline"]["candidates"]
        self.assertEqual(
            [row["ticker"] for row in baseline],
            [row["ticker"] for row in analysis["candidates"]],
        )
        self.assertTrue(calibration["official_scores_and_ranks_unchanged"])
        self.assertEqual(len(calibration["scenarios"]), 4)

    def test_reports_scenario_overlap_rank_sensitivity_and_cohorts(self):
        calibration = build_moonshot_calibration(self._analysis())

        self.assertIn("growth_emphasis", calibration["scenario_overlaps"])
        self.assertEqual(
            len(calibration["rank_sensitivity"]["candidates"]), 4,
        )
        self.assertEqual(calibration["cohorts"]["country"]["CA"]["total"], 1)
        self.assertEqual(calibration["cohorts"]["size_tier"]["nano_cap"]["total"], 2)
        self.assertIn("liquidity", calibration["factor_distributions"]["risk"])
        self.assertTrue(any(
            gate["name"] == "country_selection_rate_dispersion"
            for gate in calibration["validation_gates"]
        ))

    def test_zero_liquidity_integrity_failure_is_detected(self):
        analysis = self._analysis()
        candidate = analysis["candidates"][0]
        candidate["average_dollar_volume_30d"] = 0
        liquidity = next(
            factor for factor in candidate["risk_factors"]
            if factor["name"] == "liquidity"
        )
        liquidity["available"] = False
        liquidity["points"] = 0

        calibration = build_moonshot_calibration(analysis)
        gate = next(
            row for row in calibration["validation_gates"]
            if row["name"] == "zero_liquidity_penalty"
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["severity"], "fail")
        self.assertEqual(calibration["automated_status"], "fail")

    def test_rejects_invalid_calibration_thresholds(self):
        config = copy.deepcopy(MOONSHOT_CONFIG)
        config["calibration"]["maximum_country_selection_rate_ratio"] = .5

        with self.assertRaisesRegex(ValueError, "must be at least 1"):
            build_moonshot_calibration(self._analysis(), config=config)

    def test_forward_baseline_has_fixed_horizons_and_is_immutable(self):
        baseline = build_forward_baseline(self._analysis())

        self.assertEqual(baseline["candidate_count"], 4)
        self.assertEqual(
            baseline["candidates"][0]["horizons"]["1M"]["eligible_at"],
            "2026-09-22T12:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            csv_path, json_path = export_forward_baseline(
                baseline, Path(directory),
            )
            changed = copy.deepcopy(baseline)
            changed["baseline_id"] = "different"
            with self.assertRaisesRegex(ValueError, "immutable"):
                export_forward_baseline(changed, Path(directory))

        self.assertTrue(csv_path.name.endswith("v0.2-market-risk.csv"))
        self.assertTrue(json_path.name.endswith("v0.2-market-risk.json"))

    def test_exports_scenario_validation_and_json_artifacts(self):
        calibration = build_moonshot_calibration(self._analysis())
        with tempfile.TemporaryDirectory() as directory:
            csv_path, json_path, validation_path = export_moonshot_calibration(
                calibration, Path(directory),
            )
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            with validation_path.open("r", encoding="utf-8", newline="") as handle:
                validation = list(csv.DictReader(handle))

        self.assertEqual(saved["run_id"], "run-1")
        self.assertEqual(len(rows), 16)
        self.assertTrue(validation)
        self.assertIn("strongest_upside_driver", validation[0])

    def _analysis(self):
        rows = [
            self._row(
                "GROW", 5_000_000, revenue_growth=.8, earnings_growth=.7,
                operating_margin=.08, operating_cash_flow=500_000,
                free_cash_flow=-200_000, total_cash=1_000_000,
                total_debt=200_000, debt_to_equity=20, price_to_sales=1.2,
            ),
            self._row(
                "SAFE", 20_000_000, revenue_growth=.2, earnings_growth=.2,
                operating_margin=.25, operating_cash_flow=5_000_000,
                free_cash_flow=3_000_000, total_cash=8_000_000,
                total_debt=0, debt_to_equity=0, price_to_sales=.8,
            ),
            self._row(
                "RISK", 3_000_000, revenue_growth=.7, earnings_growth=.6,
                operating_margin=-.4, operating_cash_flow=-1_000_000,
                free_cash_flow=-2_000_000, total_cash=100_000,
                total_debt=4_000_000, debt_to_equity=300, price_to_sales=.5,
            ),
            self._row(
                "CAN.V", 30_000_000, country="CA", exchange="TSXV",
                revenue_growth=.3, earnings_growth=.2, operating_margin=.12,
                operating_cash_flow=2_000_000, free_cash_flow=1_000_000,
                total_cash=3_000_000, total_debt=500_000,
                debt_to_equity=15, price_to_sales=1.5,
            ),
        ]
        evidence = {
            "GROW": self._market(volatility=65, drawdown=35, dilution=10),
            "SAFE": self._market(volatility=25, drawdown=12, dilution=0),
            "RISK": self._market(
                dollar_volume=20_000, volatility=150, drawdown=80,
                dilution=80, reverse_splits=1,
            ),
            "CAN.V": self._market(volatility=45, drawdown=25, dilution=5),
        }
        return build_moonshot_analysis(
            rows,
            "run-1",
            self.COMPLETED_AT,
            market_evidence=evidence,
        )

    def _row(self, ticker, market_cap, **overrides):
        row = {
            "ticker": ticker, "company_name": f"{ticker} Corp",
            "country": "US", "exchange": "NASDAQ", "sector": "Technology",
            "market_cap": market_cap, "asset_type": "operating_equity",
            "quote_type": "EQUITY", "status": "FILTERED",
            "reason_flags": "Moonshot market-cap cohort",
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
    def _market(
        dollar_volume=2_000_000,
        volatility=35,
        drawdown=20,
        dilution=0,
        reverse_splits=0,
    ):
        return {
            "data_status": "complete",
            "source": "moonshot_collection",
            "captured_at": "2026-08-23T12:00:00+00:00",
            "average_dollar_volume_30d": dollar_volume,
            "annualized_volatility_percent": volatility,
            "maximum_drawdown_percent": drawdown,
            "share_count_change_percent": dilution,
            "dilution_percent": dilution,
            "reverse_split_count_1y": reverse_splits,
            "errors": [],
        }


if __name__ == "__main__":
    unittest.main()
