import unittest

from src.cli import parse_args, select_universe


class CliTests(unittest.TestCase):
    def setUp(self):
        self.universe = [
            {"ticker": "AAA", "country": "US", "exchange": "NASDAQ"},
            {"ticker": "BBB.TO", "country": "CA", "exchange": "TSX"},
            {"ticker": "CCC", "country": "US", "exchange": "NYSE"},
            {"ticker": "DDD.TO", "country": "CA", "exchange": "TSX"},
        ]

    def test_parses_tickers_and_normalizes_during_selection(self):
        args = parse_args(["--tickers", "bbb.to", "AAA"])
        selected = select_universe(self.universe, tickers=args.tickers)

        self.assertEqual(
            [item["ticker"] for item in selected],
            ["BBB.TO", "AAA"],
        )

    def test_limit_preserves_universe_order(self):
        args = parse_args(["--limit", "2"])

        self.assertEqual(
            select_universe(self.universe, limit=args.limit),
            self.universe[:2],
        )

    def test_deduplicates_requested_tickers(self):
        selected = select_universe(
            self.universe,
            tickers=["AAA", "aaa", "CCC"],
        )

        self.assertEqual(
            [item["ticker"] for item in selected],
            ["AAA", "CCC"],
        )

    def test_reports_tickers_missing_from_universe(self):
        with self.assertRaisesRegex(ValueError, "MISSING"):
            select_universe(self.universe, tickers=["AAA", "MISSING"])

    def test_rejects_nonpositive_limit(self):
        with self.assertRaises(SystemExit):
            parse_args(["--limit", "0"])

    def test_rejects_combined_selection_options(self):
        with self.assertRaises(SystemExit):
            parse_args(["--limit", "2", "--tickers", "AAA"])

    def test_no_options_returns_full_universe(self):
        args = parse_args([])

        self.assertIs(
            select_universe(
                self.universe,
                tickers=args.tickers,
                limit=args.limit,
                balanced_sample=args.balanced_sample,
            ),
            self.universe,
        )

    def test_balanced_sample_interleaves_countries(self):
        args = parse_args(["--balanced-sample", "2"])
        selected = select_universe(
            self.universe,
            balanced_sample=args.balanced_sample,
        )

        self.assertEqual(
            [item["ticker"] for item in selected],
            ["BBB.TO", "AAA", "DDD.TO", "CCC"],
        )

    def test_rejects_balanced_sample_with_other_selection(self):
        with self.assertRaises(SystemExit):
            parse_args(["--balanced-sample", "2", "--limit", "2"])

    def test_parses_offline_recalibration_as_exclusive_action(self):
        args = parse_args(["--recalibrate-run", "run-123"])

        self.assertEqual(args.recalibrate_run, "run-123")
        with self.assertRaises(SystemExit):
            parse_args(["--recalibrate-run", "run-123", "--limit", "2"])

    def test_moonshot_run_is_an_exclusive_offline_action(self):
        args = parse_args(["--moonshot-run", "run-123"])

        self.assertEqual(args.moonshot_run, "run-123")
        with self.assertRaises(SystemExit):
            parse_args(["--moonshot-run", "run-123", "--limit", "2"])

    def test_moonshot_market_collection_is_explicit_and_bounded(self):
        args = parse_args([
            "--moonshot-run", "run-123", "--collect-market-risk",
            "--moonshot-limit", "10",
        ])

        self.assertTrue(args.collect_market_risk)
        self.assertEqual(args.moonshot_limit, 10)
        with self.assertRaises(SystemExit):
            parse_args(["--collect-market-risk"])
        with self.assertRaises(SystemExit):
            parse_args(["--moonshot-run", "run-123", "--moonshot-limit", "10"])

    def test_moonshot_calibration_is_an_exclusive_offline_action(self):
        args = parse_args(["--calibrate-moonshot", "run-123"])

        self.assertEqual(args.calibrate_moonshot, "run-123")
        with self.assertRaises(SystemExit):
            parse_args([
                "--calibrate-moonshot", "run-123",
                "--moonshot-run", "run-123",
            ])

    def test_research_run_accepts_top_and_rejects_top_alone(self):
        args = parse_args(["--research-run", "run-123", "--top", "5"])

        self.assertEqual(args.research_run, "run-123")
        self.assertEqual(args.top, 5)
        with self.assertRaises(SystemExit):
            parse_args(["--top", "5"])

    def test_balanced_research_is_scoped_to_research_and_excludes_top(self):
        args = parse_args(["--research-run", "run-123", "--balanced-research", "3"])

        self.assertEqual(args.balanced_research, 3)
        with self.assertRaises(SystemExit):
            parse_args(["--balanced-research", "3"])
        with self.assertRaises(SystemExit):
            parse_args([
                "--research-run", "run-123", "--top", "5",
                "--balanced-research", "3",
            ])

    def test_source_collection_options_require_research_run(self):
        args = parse_args([
            "--research-run", "run-123", "--collect-sources",
            "--source-manifest", "sources.json",
        ])

        self.assertTrue(args.collect_sources)
        self.assertEqual(args.source_manifest, "sources.json")
        canadian = parse_args([
            "--research-run", "run-123",
            "--canadian-source-manifest", "canadian.json",
        ])
        self.assertEqual(canadian.canadian_source_manifest, "canadian.json")
        with self.assertRaises(SystemExit):
            parse_args(["--collect-sources"])

    def test_synthesis_requires_research_run_and_evidence_source(self):
        args = parse_args([
            "--research-run", "run-123", "--collect-sources", "--synthesize",
        ])

        self.assertTrue(args.synthesize)
        with self.assertRaises(SystemExit):
            parse_args(["--research-run", "run-123", "--synthesize"])
        with self.assertRaises(SystemExit):
            parse_args(["--synthesize", "--collect-sources"])

    def test_audit_research_is_an_exclusive_offline_action(self):
        args = parse_args(["--audit-research", "run-123"])

        self.assertEqual(args.audit_research, "run-123")
        with self.assertRaises(SystemExit):
            parse_args(["--audit-research", "run-123", "--research-run", "run-123"])

        final = parse_args(["--finalize-research-review", "run-123"])
        self.assertEqual(final.finalize_research_review, "run-123")

    def test_history_actions_are_exclusive(self):
        indexed = parse_args(["--index-run", "run-123"])
        summary = parse_args(["--history-summary"])

        self.assertEqual(indexed.index_run, "run-123")
        self.assertTrue(summary.history_summary)
        with self.assertRaises(SystemExit):
            parse_args(["--history-summary", "--index-run", "run-123"])

    def test_compare_runs_accepts_two_ids_and_is_exclusive(self):
        args = parse_args(["--compare-runs", "old-run", "new-run"])

        self.assertEqual(args.compare_runs, ["old-run", "new-run"])
        with self.assertRaises(SystemExit):
            parse_args(["--compare-runs", "old-run", "new-run", "--limit", "2"])

    def test_history_reporting_actions_are_exclusive(self):
        ticker = parse_args(["--ticker-history", "abc.to"])
        weekly = parse_args(["--weekly-report"])

        self.assertEqual(ticker.ticker_history, "abc.to")
        self.assertTrue(weekly.weekly_report)
        with self.assertRaises(SystemExit):
            parse_args(["--weekly-report", "--history-summary"])

    def test_dashboard_is_exclusive_and_validates_port(self):
        args = parse_args(["--dashboard", "--dashboard-port", "9000"])

        self.assertTrue(args.dashboard)
        self.assertEqual(args.dashboard_port, 9000)
        with self.assertRaises(SystemExit):
            parse_args(["--dashboard-port", "9000"])
        with self.assertRaises(SystemExit):
            parse_args(["--dashboard", "--dashboard-port", "70000"])
        with self.assertRaises(SystemExit):
            parse_args(["--dashboard", "--weekly-report"])


if __name__ == "__main__":
    unittest.main()
