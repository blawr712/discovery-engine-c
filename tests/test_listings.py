import unittest

from src.listings import duplicate_listings
from src.research_baseline import build_research_baseline
from src.research_ranking import build_research_queue
from src.scoring_v2 import apply_all_models


def _row(ticker, country, name, dollar_volume=1e6, interlisted=None, root=None, momentum=0.1):
    return {
        "ticker": ticker, "status": "OK", "country": country, "company_name": name,
        "universe_company_name": name if country == "CA" else None,
        "universe_interlisted": interlisted, "universe_root_ticker": root,
        "sector": "Tech", "discovery_score": 50.0, "score_confidence": 100,
        "average_dollar_volume_30d": dollar_volume,
        "v2_momentum_long": momentum, "v2_high_proximity": momentum, "v2_volatility": 0.3,
        "v2_momentum_medium": momentum, "v2_volume_trend": 1.0, "v2_short_term_reversal": 0.0,
    }


class DuplicateRuleTests(unittest.TestCase):
    def test_the_us_listing_is_kept_when_it_is_tradeable(self):
        rows = [_row("HITI", "US", "High Tide Inc."),
                _row("HITI.V", "CA", "HIGH TIDE INC", 300_000, interlisted="Nasdaq", root="HITI")]
        self.assertEqual(duplicate_listings(rows), {"HITI.V": "HITI"})

    def test_the_canadian_listing_is_kept_when_only_it_is_tradeable(self):
        rows = [_row("ABC", "US", "Abc Mining Corp.", 20_000),
                _row("ABC.TO", "CA", "ABC MINING CORP", 900_000, interlisted="NYSE", root="ABC")]
        self.assertEqual(duplicate_listings(rows), {"ABC": "ABC.TO"})

    def test_a_shared_root_with_a_different_name_is_not_a_duplicate(self):
        rows = [_row("VHI", "US", "Valhi, Inc."),
                _row("VHI.TO", "CA", "VITALHUB CORP", interlisted="OTC", root="VHI")]
        self.assertEqual(duplicate_listings(rows), {})

    def test_listings_not_flagged_interlisted_are_left_alone(self):
        rows = [_row("TLRY", "US", "Tilray Brands, Inc."),
                _row("TLRY.TO", "CA", "TILRAY BRANDS INC", root="TLRY")]
        self.assertEqual(duplicate_listings(rows), {})


class DownstreamTests(unittest.TestCase):
    def setUp(self):
        self.rows = apply_all_models(
            [_row(f"L{i}", "US", f"Company {i} Corp", momentum=i / 10) for i in range(5)]
            + [_row("TLRY", "US", "Tilray Brands, Inc.", momentum=0.9),
               _row("TLRY.TO", "CA", "TILRAY BRANDS INC", interlisted="Nasdaq", root="TLRY", momentum=0.9)]
        )
        self.by = {row["ticker"]: row for row in self.rows}

    def test_the_second_listing_gets_no_shadow_score(self):
        self.assertEqual(self.by["TLRY.TO"]["duplicate_of"], "TLRY")
        self.assertIsNone(self.by["TLRY.TO"]["score_v2"])
        self.assertIsNotNone(self.by["TLRY"]["score_v2"])
        self.assertIsNone(self.by["TLRY"]["duplicate_of"])
        self.assertEqual(self.by["TLRY.TO"]["discovery_score"], 50.0)

    def test_queue_and_cohorts_keep_one_place_per_company(self):
        queue, _ = build_research_queue(self.rows)
        self.assertEqual(queue[-1]["ticker"], "TLRY.TO")
        self.assertEqual(queue[-1]["ranking_basis"], "duplicate_listing")
        self.assertEqual(queue[-1]["duplicate_of"], "TLRY")
        baseline = build_research_baseline(queue, "RUN", "2026-11-16T00:00:00+00:00", {})
        by = {row["ticker"]: row["classification"] for row in baseline["candidates"]}
        self.assertEqual(by["TLRY.TO"], "duplicate_listing")


class BacktestUniverseTests(unittest.TestCase):
    def test_each_company_is_replayed_once(self):
        from src.backtest import build_backtest_universe

        rows = [_row("HITI", "US", "High Tide Inc."),
                _row("HITI.V", "CA", "HIGH TIDE INC", 300_000, interlisted="Nasdaq", root="HITI")]
        tickers = [row["ticker"] for row in build_backtest_universe(rows)]
        self.assertEqual(tickers, ["HITI"])


if __name__ == "__main__":
    unittest.main()
