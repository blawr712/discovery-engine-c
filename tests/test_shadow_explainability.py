import json
import unittest

from src.shadow_explainability import (
    explain_fundamentals,
    explain_insiders,
    explain_shadow_models,
)


def _breakdown(**factors):
    return json.dumps({
        name: {"points": points, "max_points": maximum, "available": available,
               "applicable": applicable, "explanation": f"{name} note"}
        for name, (points, maximum, available, applicable) in factors.items()
    })


class ShadowModelTests(unittest.TestCase):
    def test_describes_scored_excluded_and_gated_models(self):
        row = {
            "score_v3": 70.0, "score_v3_rank": 3, "score_v3_confidence": 90.0,
            "score_v3_excluded": False, "score_v3_exclusion_reasons": "",
            "score_v3_model_version": "v3.0-shadow-composite",
            "score_v3_breakdown": _breakdown(pit_sales_yield=(20.0, 25.0, True, True),
                                             pit_operating_margin_ttm=(0.0, 5.0, False, False)),
            "pit_sales_yield": 1.2, "sector": "Financial Services",
            "score_v2": None, "score_v2_confidence": 100.0, "score_v2_excluded": False,
            "score_v2_breakdown": _breakdown(momentum_long=(0.0, 30.0, False, True)),
        }
        models = {m["model"]: m for m in explain_shadow_models(row)}

        v3 = models["score_v3"]
        self.assertEqual(v3["status"], "scored")
        self.assertEqual(v3["rank"], 3)
        sales = next(s for s in v3["signals"] if s["name"] == "pit_sales_yield")
        self.assertEqual(sales["percentile"], 80.0)
        self.assertEqual(sales["raw_value"], 1.2)
        margin = next(s for s in v3["signals"] if s["name"] == "pit_operating_margin_ttm")
        self.assertFalse(margin["applicable"])
        self.assertIn("Bottom-quintile operating margin", v3["exclusion_rules"])
        self.assertIn("Financial Services", v3["sector_exclusions"])
        v2 = models["score_v2"]
        self.assertEqual(v2["status"], "not_scored")

        gated = explain_shadow_models({"score_v3": None, "score_v3_confidence": 20.0})
        self.assertEqual(gated[-1]["status"], "below_confidence_gate")
        excluded = explain_shadow_models({
            "score_v3": None, "score_v3_excluded": True,
            "score_v3_exclusion_reasons": "Top-quintile share dilution; Top-quintile revenue growth",
        })
        self.assertEqual(excluded[-1]["status"], "excluded")
        self.assertEqual(len(excluded[-1]["exclusion_reasons"]), 2)

    def test_rows_without_shadow_fields_yield_nothing(self):
        self.assertEqual(explain_shadow_models({"ticker": "OLD", "discovery_score": 50}), [])


class FundamentalsAndInsidersTests(unittest.TestCase):
    def test_fundamentals_panel_reports_quality_currency_and_values(self):
        panel = explain_fundamentals({
            "fundamentals_status": "statements; converted CAD->USD",
            "pit_reporting_currency": "USD", "currency": "CAD",
            "pit_data_quality": "estimated_filing_dates", "pit_report_age_days": 62.0,
            "pit_revenue_ttm": 1.0e8, "pit_operating_margin_ttm": 0.1, "pit_cash": None,
        })
        self.assertTrue(panel["available"])
        self.assertEqual([v["key"] for v in panel["values"]],
                         ["pit_revenue_ttm", "pit_operating_margin_ttm"])
        self.assertIn("estimated", panel["data_quality_note"])
        self.assertEqual(panel["reporting_currency"], "USD")
        empty = explain_fundamentals({})
        self.assertFalse(empty["available"])
        self.assertIn("No fundamental data", empty["data_quality_note"])

    def test_insiders_panel(self):
        panel = explain_insiders({
            "insiders_status": "collected", "ins_data_through": "2026-03-31",
            "ins_purchase_count_short": 3.0, "ins_cluster_buy_short": 1.0,
            "ins_purchase_count_long": 5.0, "ins_days_since_last_purchase": 40.0,
        })
        self.assertTrue(panel["available"])
        self.assertEqual(panel["data_through"], "2026-03-31")
        self.assertEqual(len(panel["short_window"]), 2)
        self.assertEqual(len(panel["long_window"]), 1)
        self.assertFalse(explain_insiders({})["available"])


if __name__ == "__main__":
    unittest.main()
