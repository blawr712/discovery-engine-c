import os
import json
import sqlite3
from pathlib import Path

from src.universe import UniverseBuilder
from src.data_sources.yfinance_source import YFinanceSource
from src.data_sources.cached_source import CachedMarketDataSource
from src.data_sources.retrying_source import RetryingMarketDataSource
from src.data_sources.rate_limited_source import RateLimitedMarketDataSource
from src.report import (
    export_candidate_report,
    export_experimental_research_reports,
    export_report,
)
from src.analytics import export_run_analytics
from src.backtest import (
    build_backtest,
    build_backtest_universe,
    collect_price_histories,
    export_backtest,
)
from src.calibration import build_calibration, export_calibration
from src.engine import DiscoveryEngine
from src.run_state import (
    RunState,
    build_run_fingerprint,
    load_saved_run,
    load_saved_manifest,
    record_recalibration,
    record_backtest,
    record_moonshot_analysis,
    record_moonshot_calibration,
    record_research_packets,
    record_research_audit,
    record_research_acceptance,
)
from src.research import (
    ResearchCache,
    ResearchRunner,
    build_research_packets,
    export_research_packets,
)
from src.evidence import (
    CanadianIssuerManifestProvider,
    HttpCache,
    ManifestEvidenceProvider,
    SecEdgarProvider,
    attach_evidence,
    collect_evidence,
    export_evidence,
)
from src.openai_research import OpenAIResearchProvider
from src.research_audit import export_research_audit, finalize_research_review
from src.history import history_summary, index_saved_run
from src.history_comparison import compare_indexed_runs, export_run_comparison
from src.history_reporting import (
    build_ticker_history,
    build_weekly_report,
    export_ticker_history,
    export_weekly_report,
)
from src.dashboard import run_dashboard
from src.moonshot import build_moonshot_analysis, export_moonshot_analysis
from src.moonshot_market import (
    collect_market_risk_evidence,
    export_market_risk_evidence,
    load_market_risk_evidence,
    load_run_compatible_price_evidence,
)
from src.moonshot_calibration import (
    build_forward_baseline,
    build_moonshot_calibration,
    export_forward_baseline,
    export_moonshot_calibration,
)
from src.cli import parse_args, select_universe
from src.config import (
    BACKTEST_CONFIG,
    BENCHMARKS,
    CACHE_DIR,
    CACHE_ENABLED,
    CACHE_METADATA_TTL_HOURS,
    CACHE_METADATA_VERSION,
    CACHE_PRICE_HISTORY_TTL_HOURS,
    CACHE_SHARE_HISTORY_TTL_HOURS,
    MAX_CONCURRENT_DOWNLOADS,
    METADATA_CONCURRENT_DOWNLOADS,
    PRICE_CONCURRENT_DOWNLOADS,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_ENABLED,
    RETRY_JITTER_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY_SECONDS,
    RESUME_ENABLED,
    RETRY_ERRORS_ON_RESUME,
    RUN_DIR,
    OUTPUT_DIR,
    SETTINGS,
    STRATEGY,
    RATE_LIMIT_ENABLED,
    METADATA_INTERVAL_SECONDS,
    PRICE_INTERVAL_SECONDS,
    RATE_LIMIT_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_EVENTS,
    RESEARCH_DEFAULT_TOP,
    RESEARCH_AI_ENABLED,
    RESEARCH_CACHE_DIR,
    RESEARCH_PROMPT_VERSION,
    SYNTHESIS_MODEL,
    SYNTHESIS_PROVIDER,
    HISTORY_DATABASE,
)


def print_progress(
    phase: str,
    completed: int,
    total: int,
    ticker: str,
) -> None:
    """Print compact progress for each collection phase."""
    print(f"[{phase}] {completed}/{total}: {ticker}")


def build_market_data_source(
    price_history_ttl_hours: float | None = None,
) -> CachedMarketDataSource:
    """Build the shared paced, retried, persistent market-data provider."""
    provider = RateLimitedMarketDataSource(
        YFinanceSource(),
        metadata_interval_seconds=METADATA_INTERVAL_SECONDS,
        price_interval_seconds=PRICE_INTERVAL_SECONDS,
        cooldown_seconds=RATE_LIMIT_COOLDOWN_SECONDS,
        max_cooldown_events=MAX_RATE_LIMIT_COOLDOWN_EVENTS,
        enabled=RATE_LIMIT_ENABLED,
    )
    retry_source = RetryingMarketDataSource(
        provider,
        max_attempts=RETRY_MAX_ATTEMPTS if RETRY_ENABLED else 1,
        base_delay_seconds=RETRY_BASE_DELAY_SECONDS,
        max_delay_seconds=RETRY_MAX_DELAY_SECONDS,
        jitter_seconds=RETRY_JITTER_SECONDS,
    )
    return CachedMarketDataSource(
        retry_source,
        cache_directory=CACHE_DIR,
        metadata_ttl_hours=CACHE_METADATA_TTL_HOURS,
        metadata_version=CACHE_METADATA_VERSION,
        price_history_ttl_hours=(
            CACHE_PRICE_HISTORY_TTL_HOURS
            if price_history_ttl_hours is None
            else price_history_ttl_hours
        ),
        share_history_ttl_hours=CACHE_SHARE_HISTORY_TTL_HOURS,
        enabled=CACHE_ENABLED,
    )


def main(arguments=None):
    args = parse_args(arguments)
    if args.backtest_run:
        backtest_saved_run(args.backtest_run, limit=args.backtest_limit)
        return
    if args.index_run:
        index_history_run(args.index_run)
        return
    if args.history_summary:
        print_history_summary()
        return
    if args.compare_runs:
        compare_history_runs(*args.compare_runs)
        return
    if args.ticker_history:
        export_history_timeline(args.ticker_history)
        return
    if args.weekly_report:
        generate_weekly_history_report()
        return
    if args.dashboard:
        try:
            run_dashboard(
                HISTORY_DATABASE,
                port=args.dashboard_port,
                moonshot_directory=OUTPUT_DIR,
            )
        except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
            raise SystemExit(f"Unable to start history dashboard: {error}") from error
        return
    if args.recalibrate_run:
        recalibrate_saved_run(args.recalibrate_run)
        return
    if args.moonshot_run:
        analyze_moonshot_run(
            args.moonshot_run,
            source=(build_market_data_source() if args.collect_market_risk else None),
            collection_limit=args.moonshot_limit,
        )
        return
    if args.calibrate_moonshot:
        calibrate_saved_moonshot(args.calibrate_moonshot)
        return
    if args.audit_research:
        audit_saved_research(args.audit_research)
        return
    if args.finalize_research_review:
        finalize_saved_research_review(args.finalize_research_review)
        return
    if args.research_run:
        research_saved_run(
            args.research_run,
            (
                args.top
                or (args.balanced_research * 2 if args.balanced_research else None)
                or RESEARCH_DEFAULT_TOP
            ),
            collect_sources=args.collect_sources,
            source_manifest=args.source_manifest,
            canadian_source_manifest=args.canadian_source_manifest,
            synthesize=args.synthesize,
            balanced_per_country=args.balanced_research,
        )
        return
    source = build_market_data_source()
    universe = UniverseBuilder().build_universe()
    try:
        universe = select_universe(
            universe,
            tickers=args.tickers,
            limit=args.limit,
            balanced_sample=args.balanced_sample,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    fingerprint = build_run_fingerprint(universe, STRATEGY, SETTINGS)
    run_state = RunState.start_or_resume(
        RUN_DIR,
        fingerprint,
        len(universe),
        resume_enabled=RESUME_ENABLED,
    )
    prior_results = run_state.load_results(
        retry_errors=RETRY_ERRORS_ON_RESUME,
    )

    print(f"Selected {len(universe)} tickers for analysis")
    print(f"Concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}")
    print(f"Metadata workers: {METADATA_CONCURRENT_DOWNLOADS}")
    print(f"Price workers: {PRICE_CONCURRENT_DOWNLOADS}")
    print(f"Run ID: {run_state.run_id}")
    print(f"Resumed results: {len(prior_results)}")

    engine = DiscoveryEngine(
        source,
        benchmarks=BENCHMARKS,
        max_workers=MAX_CONCURRENT_DOWNLOADS,
        metadata_workers=METADATA_CONCURRENT_DOWNLOADS,
        price_workers=PRICE_CONCURRENT_DOWNLOADS,
        progress_callback=print_progress,
        result_callback=run_state.record_result,
    )
    results = engine.run(universe, prior_results=prior_results)

    output_path = export_report(results, run_id=run_state.run_id)
    candidate_output_path = export_candidate_report(
        results,
        run_id=run_state.run_id,
    )
    analytics_output_path = export_run_analytics(
        results,
        run_id=run_state.run_id,
        output_directory=OUTPUT_DIR,
    )
    intelligence = export_intelligence_artifacts(results, run_state.run_id)
    calibration_csv_path = intelligence["calibration_csv_path"]
    calibration_json_path = intelligence["calibration_json_path"]
    run_state.complete(
        results,
        output_path,
        candidate_report_path=candidate_output_path,
        analytics_path=analytics_output_path,
        calibration_csv_path=calibration_csv_path,
        calibration_json_path=calibration_json_path,
        research_artifacts=intelligence["research_artifacts"],
    )

    passed = sum(1 for r in results if r.get("status") == "OK")
    filtered = sum(1 for r in results if r.get("status") == "FILTERED")
    scoring_failed = sum(1 for r in results if r.get("status") == "FAILED")
    errors = sum(1 for r in results if r.get("status") == "ERROR")

    print("\nRun Summary")
    print(f"Total: {len(results)}")
    print(f"Passed: {passed}")
    print(f"Filtered: {filtered}")
    print(f"Scoring failed: {scoring_failed}")
    print(f"Errors: {errors}")
    if source.enabled:
        print(f"Cache hits: {source.stats.hits}")
        print(f"Cache misses: {source.stats.misses}")
        print(f"Cache expired: {source.stats.expired}")
        print(f"Cache read errors: {source.stats.read_errors}")
    print(f"Provider retries: {retry_source.stats.retries}")
    print(f"Retries exhausted: {retry_source.stats.exhausted}")
    if provider.enabled:
        print(f"Pacing waits: {provider.stats.pacing_waits}")
        print(f"Rate-limit cooldowns: {provider.stats.cooldown_events}")
        print(f"Cooldown waits: {provider.stats.cooldown_waits}")
        print(f"Circuit opened: {provider.stats.circuit_open_events}")
        print(f"Circuit rejections: {provider.stats.circuit_rejections}")

    print("\nDone.")
    print(f"Run status: {run_state.manifest['status']}")
    print(f"Report saved to: {output_path}")
    print(f"Candidate report saved to: {candidate_output_path}")
    print(f"Analytics saved to: {analytics_output_path}")
    print(f"Calibration rows saved to: {calibration_csv_path}")
    print(f"Calibration analysis saved to: {calibration_json_path}")
    print(
        "Research review saved to: "
        f"{intelligence['research_artifacts']['review_report_path']}"
    )
    print(
        "Selected v0.3 research queue saved to: "
        f"{intelligence['research_artifacts']['selected_research_report_path']}"
    )
    print(f"Manifest saved to: {run_state.manifest_path}")


def index_history_run(run_id: str) -> None:
    try:
        result = index_saved_run(
            HISTORY_DATABASE, RUN_DIR, run_id,
            price_cache_directory=CACHE_DIR, benchmarks=BENCHMARKS,
        )
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
        raise SystemExit(f"Unable to index saved run: {error}") from error
    print(f"History index complete for run: {result['run_id']}")
    print(f"Indexed results: {result['indexed_results']}")
    print(f"Indexed price snapshots: {result['price_snapshots']['indexed']}")
    print(
        "Price snapshots skipped (missing/newer/read errors): "
        f"{result['price_snapshots']['skipped_missing']}/"
        f"{result['price_snapshots']['skipped_newer_than_run']}/"
        f"{result['price_snapshots']['read_errors']}"
    )
    print(f"History database: {result['database_path']}")


def print_history_summary() -> None:
    try:
        result = history_summary(HISTORY_DATABASE)
    except (OSError, sqlite3.Error) as error:
        raise SystemExit(f"Unable to read history index: {error}") from error
    print("Discovery Engine history summary")
    print(f"Indexed runs: {result['run_count']}")
    print(f"Indexed results: {result['result_count']}")
    if result["latest_run"]:
        print(f"Latest run: {result['latest_run']['run_id']}")


def compare_history_runs(old_run_id: str, new_run_id: str) -> None:
    try:
        result = compare_indexed_runs(HISTORY_DATABASE, old_run_id, new_run_id)
        csv_path, json_path = export_run_comparison(result, OUTPUT_DIR)
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
        raise SystemExit(f"Unable to compare indexed runs: {error}") from error
    summary = result["summary"]
    print(f"History comparison complete: {old_run_id} -> {new_run_id}")
    print(f"Entrants: {summary['entrants']}")
    print(f"Exits: {summary['exits']}")
    print(f"Retained: {summary['retained']}")
    print(f"Status transitions: {summary['status_transitions']}")
    print(f"Rank changes: {summary['rank_changes']}")
    print(f"Score changes: {summary['score_changes']}")
    print(f"Data-quality changes: {summary['data_quality_changes']}")
    print(f"Comparison CSV saved to: {csv_path}")
    print(f"Comparison JSON saved to: {json_path}")


def export_history_timeline(ticker: str) -> None:
    try:
        history = build_ticker_history(HISTORY_DATABASE, ticker)
        csv_path, json_path = export_ticker_history(history, OUTPUT_DIR)
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
        raise SystemExit(f"Unable to build ticker history: {error}") from error
    print(f"Ticker history complete: {history['ticker']}")
    print(f"Indexed appearances: {history['appearances']}")
    print(f"Best rank: {history['best_rank']}")
    print(f"Worst rank: {history['worst_rank']}")
    print(f"Timeline CSV saved to: {csv_path}")
    print(f"Timeline JSON saved to: {json_path}")


def generate_weekly_history_report() -> None:
    try:
        report = build_weekly_report(HISTORY_DATABASE)
        markdown_path, csv_path, json_path = export_weekly_report(report, OUTPUT_DIR)
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
        raise SystemExit(f"Unable to build weekly history report: {error}") from error
    print(
        f"Weekly history report complete: {report['old_run_id']} -> "
        f"{report['new_run_id']}"
    )
    print(f"Compatibility: {report['compatibility']['classification']}")
    print(f"Promotions to OK: {len(report['promotions_to_ok'])}")
    print(f"Demotions from OK: {len(report['demotions_from_ok'])}")
    print(f"Markdown briefing saved to: {markdown_path}")
    print(f"Comparison CSV saved to: {csv_path}")
    print(f"Comparison JSON saved to: {json_path}")


def export_intelligence_artifacts(results: list[dict], run_id: str) -> dict:
    """Export calibration and decision-ready research artifacts once."""
    calibration = build_calibration(results)
    calibration_csv_path, calibration_json_path = export_calibration(
        results,
        run_id=run_id,
        output_directory=OUTPUT_DIR,
        calibration=calibration,
    )
    research_artifacts = export_experimental_research_reports(
        results,
        calibration,
        run_id,
        output_directory=OUTPUT_DIR,
    )
    return {
        "calibration_csv_path": calibration_csv_path,
        "calibration_json_path": calibration_json_path,
        "research_artifacts": research_artifacts,
    }


def recalibrate_saved_run(run_id: str) -> None:
    """Regenerate intelligence artifacts without provider initialization."""
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error
    intelligence = export_intelligence_artifacts(results, run_id)
    artifacts = {
        **intelligence,
        "source_fingerprint": manifest.get("fingerprint"),
        "source_completed_at": manifest.get("completed_at"),
    }
    manifest_path = record_recalibration(RUN_DIR, run_id, artifacts)
    research = intelligence["research_artifacts"]

    print(f"Offline recalibration complete for run: {run_id}")
    print(f"Loaded saved results: {len(results)}")
    print(
        "Calibration rows saved to: "
        f"{intelligence['calibration_csv_path']}"
    )
    print(
        "Calibration analysis saved to: "
        f"{intelligence['calibration_json_path']}"
    )
    for scenario, path in research["scenario_report_paths"].items():
        print(f"{scenario} research report saved to: {path}")
    print(f"Top-25 review saved to: {research['review_report_path']}")
    print(f"Scenario summary saved to: {research['scenario_summary_path']}")
    print(
        "Scenario comparison saved to: "
        f"{research['scenario_comparison_path']}"
    )
    print(
        "Research decision saved to: "
        f"{research['research_decision_path']}"
    )
    print(
        "Selected v0.3 research queue saved to: "
        f"{research['selected_research_report_path']}"
    )
    print(f"Manifest updated: {manifest_path}")


def analyze_moonshot_run(
    run_id: str,
    source: CachedMarketDataSource | None = None,
    collection_limit: int | None = None,
) -> None:
    """Build Moonshot analysis offline, with explicitly optional collection."""
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
        preliminary = build_moonshot_analysis(
            results, run_id, manifest.get("completed_at"),
        )
        cached_evidence, cache_stats = load_run_compatible_price_evidence(
            CACHE_DIR,
            preliminary["candidates"],
            manifest.get("completed_at"),
        )
        saved_evidence = load_market_risk_evidence(OUTPUT_DIR, run_id)
        market_evidence = {**cached_evidence, **saved_evidence}
        collection_stats = None
        if source is not None:
            selected = preliminary["candidates"][:collection_limit]
            collected, collection_stats = collect_market_risk_evidence(
                selected,
                source,
                max_workers=PRICE_CONCURRENT_DOWNLOADS,
                progress_callback=print_progress,
            )
            market_evidence.update(collected)
        market_evidence_path = export_market_risk_evidence(
            market_evidence,
            OUTPUT_DIR,
            run_id,
            manifest.get("completed_at"),
        )
        analysis = build_moonshot_analysis(
            results,
            run_id,
            manifest.get("completed_at"),
            market_evidence=market_evidence,
        )
        csv_path, json_path = export_moonshot_analysis(analysis, OUTPUT_DIR)
        summary = analysis["summary"]
        market_summary = analysis["market_evidence_summary"]
        manifest_path = record_moonshot_analysis(
            RUN_DIR,
            run_id,
            {
                "model_version": analysis["model_version"],
                "source_fingerprint": manifest.get("fingerprint"),
                "source_completed_at": manifest.get("completed_at"),
                "candidate_count": summary["candidate_count"],
                "nano_cap_count": summary["nano_cap_count"],
                "market_evidence_count": len(market_evidence),
                "market_data_status": summary["market_data_status"],
                "market_risk_collected": source is not None,
                "market_cross_section_comparable": market_summary[
                    "cross_section_comparable"
                ],
                "market_minimum_cross_section_coverage_percent": market_summary[
                    "minimum_coverage_percent"
                ],
                "market_excluded_candidates": market_summary[
                    "excluded_candidates"
                ],
                "official_scores_and_ranks_unchanged": True,
                "moonshot_candidates_csv_path": str(csv_path),
                "moonshot_analysis_json_path": str(json_path),
                "moonshot_market_evidence_json_path": str(market_evidence_path),
            },
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise SystemExit(f"Unable to build Moonshot analysis: {error}") from error
    action = (
        "Moonshot market collection and analysis"
        if source is not None else "Offline Moonshot Discovery"
    )
    print(f"{action} complete for run: {run_id}")
    print(f"Model: {analysis['model_version']}")
    print(f"Eligible candidates: {summary['candidate_count']}")
    print(f"Sub-$10M nano-cap candidates: {summary['nano_cap_count']}")
    for name, count in summary["classifications"].items():
        print(f"{name.replace('_', ' ').title()}: {count}")
    print(f"Market evidence records: {len(market_evidence)}")
    print(
        "Market evidence coverage: "
        f"{market_summary['coverage_percent']}%"
    )
    print(
        "Cross-section comparable: "
        f"{'yes' if market_summary['cross_section_comparable'] else 'no'}"
    )
    if market_summary["excluded_candidates"]:
        print(
            "Excluded from market cohort: "
            + ", ".join(
                row["ticker"] for row in market_summary["excluded_candidates"]
            )
        )
    print(
        "Run-compatible price cache: "
        f"{cache_stats['loaded']} loaded, {cache_stats['missing']} missing, "
        f"{cache_stats['newer_than_run']} newer, "
        f"{cache_stats['read_errors']} read errors"
    )
    if collection_stats is not None:
        print(
            "Market-risk collection: "
            f"{collection_stats['complete']} complete, "
            f"{collection_stats['price_only']} price-only, "
            f"{collection_stats['partial']} partial, "
            f"{collection_stats['unavailable']} unavailable, "
            f"{collection_stats['with_errors']} with errors"
        )
    print("Official Discovery scores and ranks: unchanged")
    print(f"Candidate CSV saved to: {csv_path}")
    print(f"Analysis JSON saved to: {json_path}")
    print(f"Market evidence saved to: {market_evidence_path}")
    print(f"Manifest updated: {manifest_path}")


def backtest_saved_run(run_id: str, limit: int | None = None) -> None:
    """Replay technical scoring point-in-time for a completed run's universe."""
    period = str(BACKTEST_CONFIG.get("price_history_period", "10y"))
    ttl_hours = float(BACKTEST_CONFIG.get("price_history_ttl_hours", 168))
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
        universe = build_backtest_universe(results)
        if limit is not None:
            universe = universe[:limit]
        source = build_market_data_source(price_history_ttl_hours=ttl_hours)
        print(f"Backtest universe: {len(universe)} tickers from run {run_id}")
        print(f"Price history period: {period}")
        histories, benchmark_histories, errors = collect_price_histories(
            universe,
            BENCHMARKS,
            source,
            period=period,
            max_workers=PRICE_CONCURRENT_DOWNLOADS,
            progress_callback=print_progress,
        )
        analysis = build_backtest(
            universe,
            histories,
            benchmark_histories,
            BENCHMARKS,
            run_id,
            collection_errors=errors,
        )
        artifacts = export_backtest(analysis, OUTPUT_DIR)
        coverage = analysis["coverage"]
        manifest_path = record_backtest(
            RUN_DIR,
            run_id,
            {
                "model_version": analysis["model_version"],
                "source_fingerprint": manifest.get("fingerprint"),
                "price_history_period": period,
                "limit": limit,
                "universe_tickers": coverage["universe_tickers"],
                "usable_tickers": coverage["usable_tickers"],
                "collection_errors": coverage["collection_errors"],
                "periods": coverage["periods"],
                "first_period": coverage["first_period"],
                "last_period": coverage["last_period"],
                "observations": coverage["observations"],
                "official_scores_and_ranks_unchanged": True,
                **artifacts,
            },
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise SystemExit(f"Unable to build backtest: {error}") from error

    print(f"\nBacktest complete for run: {run_id}")
    print(f"Model: {analysis['model_version']}")
    print(
        "Tickers usable / collected / requested: "
        f"{coverage['usable_tickers']} / {coverage['collected_tickers']} / "
        f"{coverage['universe_tickers']}"
    )
    print(f"Collection errors: {coverage['collection_errors']}")
    print(
        f"Periods: {coverage['periods']} "
        f"({coverage['first_period']} to {coverage['last_period']})"
    )
    print(f"Observations: {coverage['observations']}")
    if source.enabled:
        print(f"Cache hits: {source.stats.hits}")
        print(f"Cache misses: {source.stats.misses}")
        print(f"Cache expired: {source.stats.expired}")
    ic = analysis["aggregate"]["information_coefficient"]
    for column in ("technical_score", "discovery_score_static"):
        for horizon, stats in ic.get(column, {}).items():
            if stats:
                print(
                    f"IC {column} {horizon}: mean {stats['mean']:.4f} "
                    f"(t {stats['t_stat'] if stats['t_stat'] is not None else 'n/a'}, "
                    f"{stats['periods']} periods)"
                )
    print("Official Discovery scores and ranks: unchanged")
    for label, path in artifacts.items():
        print(f"{label.replace('_', ' ').capitalize()}: {path}")
    print(f"Manifest updated: {manifest_path}")


def calibrate_saved_moonshot(run_id: str) -> None:
    """Calibrate saved Moonshot evidence without market or AI providers."""
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
        market_evidence = load_market_risk_evidence(OUTPUT_DIR, run_id)
        if not market_evidence:
            raise ValueError(
                "No saved Moonshot market evidence; run --moonshot-run first."
            )
        analysis = build_moonshot_analysis(
            results,
            run_id,
            manifest.get("completed_at"),
            market_evidence=market_evidence,
        )
        calibration = build_moonshot_calibration(analysis)
        scenario_csv, calibration_json, validation_csv = (
            export_moonshot_calibration(calibration, OUTPUT_DIR)
        )
        baseline = build_forward_baseline(analysis)
        baseline_csv, baseline_json = export_forward_baseline(
            baseline, OUTPUT_DIR,
        )
        manifest_path = record_moonshot_calibration(
            RUN_DIR,
            run_id,
            {
                "model_version": analysis["model_version"],
                "automated_status": calibration["automated_status"],
                "scenario_count": len(calibration["scenarios"]),
                "baseline_id": baseline["baseline_id"],
                "baseline_candidate_count": baseline["candidate_count"],
                "official_scores_and_ranks_unchanged": True,
                "moonshot_calibration_csv_path": str(scenario_csv),
                "moonshot_calibration_json_path": str(calibration_json),
                "moonshot_validation_csv_path": str(validation_csv),
                "moonshot_forward_baseline_csv_path": str(baseline_csv),
                "moonshot_forward_baseline_json_path": str(baseline_json),
            },
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise SystemExit(f"Unable to calibrate Moonshot analysis: {error}") from error
    gates_passed = sum(
        gate["passed"] for gate in calibration["validation_gates"]
    )
    gates_total = len(calibration["validation_gates"])
    print(f"Offline Moonshot calibration complete for run: {run_id}")
    print(f"Automated status: {calibration['automated_status']}")
    print(f"Scenarios tested: {len(calibration['scenarios'])}")
    print(f"Validation gates passed: {gates_passed}/{gates_total}")
    for gate in calibration["validation_gates"]:
        print(f"[{'PASS' if gate['passed'] else 'FAIL'}] {gate['name']}: {gate['actual']}")
    print(f"Forward baseline candidates: {baseline['candidate_count']}")
    print("Official Discovery and Moonshot ranks: unchanged")
    print(f"Calibration rows saved to: {scenario_csv}")
    print(f"Calibration analysis saved to: {calibration_json}")
    print(f"Candidate validation saved to: {validation_csv}")
    print(f"Forward baseline CSV saved to: {baseline_csv}")
    print(f"Forward baseline JSON saved to: {baseline_json}")
    print(f"Manifest updated: {manifest_path}")


def research_saved_run(
    run_id: str,
    top_n: int,
    collect_sources: bool = False,
    source_manifest: str | None = None,
    canadian_source_manifest: str | None = None,
    synthesize: bool = False,
    balanced_per_country: int | None = None,
) -> None:
    """Build deterministic research packets from a completed local run."""
    try:
        _, results = load_saved_run(RUN_DIR, run_id)
        calibration = build_calibration(results)
        packets, metadata = build_research_packets(
            results,
            top_n,
            calibration=calibration,
            balanced_per_country=balanced_per_country,
        )
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error
    evidence_artifact = None
    providers = []
    if collect_sources:
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not user_agent:
            raise SystemExit(
                "--collect-sources requires SEC_USER_AGENT, for example "
                "'Discovery Engine your-email@example.com'."
            )
        providers.append(SecEdgarProvider(user_agent, HttpCache()))
    if source_manifest:
        try:
            providers.append(ManifestEvidenceProvider(Path(source_manifest)))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"Invalid source manifest: {error}") from error
    if canadian_source_manifest:
        user_agent = (
            os.environ.get("SOURCE_USER_AGENT", "").strip()
            or os.environ.get("SEC_USER_AGENT", "").strip()
        )
        if not user_agent:
            raise SystemExit(
                "--canadian-source-manifest requires SOURCE_USER_AGENT or "
                "SEC_USER_AGENT with a contact email."
            )
        try:
            providers.append(CanadianIssuerManifestProvider(
                Path(canadian_source_manifest),
                user_agent,
                HttpCache(),
            ))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"Invalid Canadian source manifest: {error}") from error
    if providers:
        evidence = collect_evidence(packets, providers)
        attach_evidence(packets, evidence)
        evidence_artifact = export_evidence(evidence, run_id, OUTPUT_DIR)
        metadata["evidence"] = {
            "collection_enabled": True,
            "providers": [provider.name for provider in providers],
            "document_count": evidence["document_count"],
            "failure_count": evidence["failure_count"],
            "cache": evidence["cache"],
            "evidence_manifest_path": evidence_artifact,
        }
    else:
        metadata["evidence"] = {"collection_enabled": False}
    synthesis_provider = None
    if synthesize:
        if not RESEARCH_AI_ENABLED:
            raise SystemExit(
                "AI synthesis is disabled in config/settings.json. Set "
                "research.ai_synthesis_enabled to true before using --synthesize."
            )
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("--synthesize requires OPENAI_API_KEY.")
        if SYNTHESIS_PROVIDER != "openai":
            raise SystemExit(f"Unsupported synthesis provider: {SYNTHESIS_PROVIDER}")
        synthesis_provider = OpenAIResearchProvider(api_key)
        metadata["synthesis"] = {
            "enabled": True,
            "provider": synthesis_provider.name,
            "model": synthesis_provider.model,
            "prompt_version": RESEARCH_PROMPT_VERSION,
        }
    else:
        metadata["synthesis"] = {"enabled": False}
    outputs = ResearchRunner(
        provider=synthesis_provider,
        cache=ResearchCache(
            RESEARCH_CACHE_DIR,
            RESEARCH_PROMPT_VERSION,
            f"openai:{SYNTHESIS_MODEL}" if synthesis_provider else "packet-only",
        ),
    ).run(packets)
    artifacts = export_research_packets(
        packets,
        outputs,
        metadata,
        run_id,
        output_directory=OUTPUT_DIR,
    )
    if evidence_artifact:
        artifacts["evidence_manifest_path"] = evidence_artifact
    manifest_path = record_research_packets(
        RUN_DIR,
        run_id,
        artifacts,
    )

    print(f"Offline research packets complete for run: {run_id}")
    print(f"Selected scenario: {metadata['selected_scenario']}")
    print(f"Research packets: {len(packets)}")
    if synthesis_provider:
        statuses = artifacts["synthesis_statuses"]
        print(f"AI synthesis: enabled ({synthesis_provider.model})")
        print(f"Synthesis complete: {statuses.get('complete', 0)}")
        print(f"Synthesis skipped (no evidence): {statuses.get('skipped_no_evidence', 0)}")
        print(f"Synthesis errors: {statuses.get('error', 0)}")
        print(f"Synthesis cache hits: {artifacts['synthesis_cache_hits']}")
        print(f"Validated claims: {artifacts['validated_claim_count']}")
        print(f"Validated citations: {artifacts['validated_citation_count']}")
        usage = artifacts["synthesis_usage"]
        print(f"Synthesis input tokens: {usage['input_tokens']}")
        print(f"Synthesis output tokens: {usage['output_tokens']}")
        print(f"Synthesis total tokens: {usage['total_tokens']}")
    else:
        print("AI synthesis: disabled (packet-only mode)")
    if evidence_artifact:
        cache = metadata["evidence"]["cache"]
        print(f"Evidence documents: {metadata['evidence']['document_count']}")
        print(f"Evidence failures: {metadata['evidence']['failure_count']}")
        print(f"Evidence cache hits: {cache['hits']}")
        print(f"Evidence cache misses: {cache['misses']}")
        print(f"Evidence cache expired: {cache['expired']}")
        print(f"Evidence cache read errors: {cache['read_errors']}")
        print(f"Evidence manifest saved to: {evidence_artifact}")
    print(f"JSON packets saved to: {artifacts['research_packets_json_path']}")
    print(
        "Markdown packets saved to: "
        f"{artifacts['research_packets_markdown_path']}"
    )
    print(
        "Research briefs saved to: "
        f"{artifacts['research_briefs_markdown_path']}"
    )
    print(f"Manifest updated: {manifest_path}")


def audit_saved_research(run_id: str) -> None:
    """Audit the latest saved research artifact without external providers."""
    try:
        manifest = load_saved_manifest(RUN_DIR, run_id)
        artifact = manifest.get("research_packet_artifacts") or {}
        source_value = artifact.get("research_packets_json_path")
        if not source_value:
            raise ValueError("Saved run has no research packet artifact to audit.")
        source_path = Path(source_value).resolve()
        if not source_path.is_relative_to(OUTPUT_DIR.resolve()):
            raise ValueError("Research artifact is outside the configured export directory.")
        with source_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if payload.get("run_id") != run_id:
            raise ValueError("Research artifact run ID does not match the requested run.")
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to audit saved research: {error}") from error

    artifacts = export_research_audit(payload, run_id, OUTPUT_DIR)
    manifest_path = record_research_audit(RUN_DIR, run_id, artifacts)
    metrics = artifacts["metrics"]
    print(f"Offline research audit complete for run: {run_id}")
    print(f"Automated status: {artifacts['automated_status']}")
    print(f"Release status: {artifacts['release_status']}")
    print(f"Candidates audited: {metrics['candidate_count']}")
    print(f"Evidence coverage: {metrics['evidence_coverage_percent']}%")
    print(f"Synthesis completion: {metrics['synthesis_completion_percent']}%")
    print(f"Citation coverage: {metrics['citation_coverage_percent']}%")
    print(f"Section coverage: {metrics['section_coverage_percent']}%")
    print(f"Human review rows: {artifacts['review_row_count']}")
    sampling = artifacts["review_sampling"]
    print(
        "Human review sampling: "
        f"{sampling['selected_claim_count']}/{sampling['total_claim_count']} "
        f"({sampling['coverage_percent']}%)"
    )
    if artifacts["failed_gates"]:
        print(f"Failed gates: {', '.join(artifacts['failed_gates'])}")
    if artifacts["not_evaluated_gates"]:
        print(f"Not evaluated: {', '.join(artifacts['not_evaluated_gates'])}")
    print(f"Audit saved to: {artifacts['research_audit_json_path']}")
    print(f"Human review queue saved to: {artifacts['research_human_review_csv_path']}")
    print(f"Candidate audit saved to: {artifacts['research_candidate_audit_csv_path']}")
    print(f"Claim triage saved to: {artifacts['research_claim_triage_csv_path']}")
    print(f"Manifest updated: {manifest_path}")


def finalize_saved_research_review(run_id: str) -> None:
    """Finalize a completed claim-level human review for a saved run."""
    try:
        manifest = load_saved_manifest(RUN_DIR, run_id)
        artifacts = manifest.get("research_audit_artifacts") or {}
        audit_path = _validated_export_path(artifacts.get("research_audit_json_path"))
        review_path = _validated_export_path(artifacts.get("research_human_review_csv_path"))
        decision = finalize_research_review(audit_path, review_path, run_id, OUTPUT_DIR)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to finalize research review: {error}") from error
    manifest_path = record_research_acceptance(RUN_DIR, run_id, decision)
    print(f"Research review finalized for run: {run_id}")
    print(f"Human decision: {decision['human_review_decision']}")
    print(f"Review rows: {decision['review_row_count']}")
    if decision["pending_csv_rows"]:
        print(f"Pending CSV rows: {decision['pending_csv_rows']}")
    if decision["rejected_csv_rows"]:
        print(f"Rejected CSV rows: {decision['rejected_csv_rows']}")
    print(f"Acceptance record saved to: {decision['research_acceptance_json_path']}")
    print(f"Candidate release report saved to: {decision['research_release_csv_path']}")
    print(f"Manifest updated: {manifest_path}")


def _validated_export_path(value: object) -> Path:
    if not value:
        raise ValueError("Saved run is missing a required research review artifact.")
    path = Path(str(value)).resolve()
    if not path.is_relative_to(OUTPUT_DIR.resolve()):
        raise ValueError("Research review artifact is outside the export directory.")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


if __name__ == "__main__":
    main()
