import os
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load local secrets such as SEC_USER_AGENT from an untracked .env file
# without overriding variables already present in the environment.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from src.universe import UniverseBuilder  # noqa: E402
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
    build_insider_histories,
    collect_fundamental_histories,
    collect_price_histories,
    export_backtest,
)
from src.data_sources.sec_insider_source import (
    SecInsiderTransactionsSource,
    current_quarter,
    quarter_labels,
)
from src.data_sources.sec_xbrl_source import SecXbrlSource
from src.fundamentals_pit import FundamentalHistory
from src.split_adjustment import SplitHistory
from src.keep_awake import keep_awake
from src.data_sources.sec_filings_source import SecFilingsSource, quarters
from src.data_sources.sec_form4_source import SecForm4Source
from src.data_sources.sec_pacing import SecPacer
from src.insider_supplement import SupplementedInsiderLookup
from src.liquidity import liquidity_eligible
from src.survivorship import classify_exit, dead_observations, delisted_candidates
from src.survivorship_report import build_markdown, build_report
from src.monthly import (
    TMX_STALE_AFTER,
    build_cycle_report,
    default_cycle,
    discovery_quality,
    due_baselines,
    run_cycle,
    universe_source_age,
    validate_cycle,
)
from src.insider_signals import build_insider_index, quarter_end
from src.calibration import build_calibration, export_calibration
from src.scoring_v2 import apply_all_models
from src.engine import DiscoveryEngine
from src.run_state import (
    COMPLETED_RUN_STATUSES,
    RunState,
    build_run_fingerprint,
    load_saved_run,
    load_saved_manifest,
    record_recalibration,
    record_backtest,
    record_baseline_resolution,
    record_research_baseline,
    record_rescore,
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
from src.research_ranking import build_research_queue, export_research_queue
from src.baseline_resolution import (
    available_baseline_kinds,
    export_baseline_resolution,
    load_forward_baseline,
    load_price_series,
    resolve_baseline,
    tickers_needing_prices,
)
from src.research_baseline import (
    build_research_baseline,
    export_research_baseline,
)
from src.rescore import rescore_results
from src.sec_mapping import resolve_sec_ticker
from src.statements_extract import extract_from_statements
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
from collections import Counter

from src.config import (
    BACKTEST_CONFIG,
    BASE_DIR,
    BENCHMARKS,
    FUNDAMENTALS_CONFIG,
    INSIDERS_CONFIG,
    INSIDER_SIGNALS_CONFIG,
    RESEARCH_RANKING_CONFIG,
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
    MAX_RESUME_AGE_HOURS,
    ISOLATED_ROOT,
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
        backtest_saved_run(
            args.backtest_run,
            limit=args.backtest_limit,
            with_fundamentals=args.with_fundamentals,
            with_insiders=args.with_insiders,
            static_universe=args.static_universe,
            no_liquidity_floor=args.no_liquidity_floor,
        )
        return
    if args.freeze_research_baseline:
        freeze_saved_research_baseline(args.freeze_research_baseline)
        return
    if args.resolve_baselines:
        resolve_saved_baseline(
            args.resolve_baselines,
            collect_prices=args.collect_prices,
            kind=args.baseline,
        )
        return
    if args.rescore_run:
        rescore_saved_run(
            args.rescore_run, refresh_fundamentals=args.refresh_fundamentals,
        )
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
    if args.monthly:
        if args.rehearsal and ISOLATED_ROOT is None:
            rehearse_monthly(arguments)
            return
        run_monthly(
            cycle=args.cycle,
            skip_universe=args.skip_universe,
            rehearsal_size=args.rehearsal_size if args.rehearsal else None,
        )
        return
    if args.survivorship_test:
        survivorship_test(args.survivorship_test)
        return
    if args.test_v3_change:
        test_v3_change(args.test_v3_change, args.change)
        return
    if args.screen_research:
        screen_research(args.screen_research)
        return
    if args.unscored_research:
        unscored_research(args.unscored_research)
        return
    run_discovery(
        tickers=args.tickers,
        limit=args.limit,
        balanced_sample=args.balanced_sample,
    )


def run_discovery(
    tickers=None,
    limit=None,
    balanced_sample=None,
    cycle: str | None = None,
) -> str:
    """Run (or resume) a full discovery run and return its run ID."""
    source = build_market_data_source()
    universe = UniverseBuilder().build_universe()
    try:
        universe = select_universe(
            universe,
            tickers=tickers,
            limit=limit,
            balanced_sample=balanced_sample,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    fingerprint = build_run_fingerprint(universe, STRATEGY, SETTINGS, cycle=cycle)
    run_state = RunState.start_or_resume(
        RUN_DIR,
        fingerprint,
        len(universe),
        resume_enabled=RESUME_ENABLED,
        max_resume_age_hours=MAX_RESUME_AGE_HOURS,
    )
    for stale in run_state.skipped_stale_runs:
        print(
            f"Not resuming run {stale}: last updated more than "
            f"{MAX_RESUME_AGE_HOURS:g} hours ago, so its prices are stale. "
            "Starting a new run."
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

    fundamentals_source = None
    statements_source = None
    resolver = None
    fx_rates = {}
    if FUNDAMENTALS_CONFIG.get("live_collection_enabled", True):
        fundamentals_source = build_sec_xbrl_source(required=False)
        if fundamentals_source is not None:
            resolver = build_sec_resolver(fundamentals_source)
        if FUNDAMENTALS_CONFIG.get("statements_fallback_enabled", True):
            statements_source = source
        fx_rates = load_fx_rates(source)
    print(
        "SEC fundamentals: "
        + ("enabled (U.S. filers and verified interlisted Canadian filers)" if fundamentals_source else
           "disabled (set SEC_USER_AGENT in .env to enable)")
    )
    print(
        "Statement fundamentals fallback: "
        + ("enabled (estimated filing dates)" if statements_source is not None else "disabled")
        + (f"; FX pairs {sorted(set(a for a, _ in fx_rates))}" if fx_rates else "; no FX rates")
    )
    insider_lookup = None
    insider_index_stats = None
    if INSIDERS_CONFIG.get("live_collection_enabled", True) and fundamentals_source is not None:
        insider_source = build_sec_insider_source(required=False)
        if insider_source is not None:
            insider_lookup, insider_index_stats = build_live_insider_lookup(
                insider_source, fundamentals_source.cik_for, live_insider_start_quarter(),
            )
    print(
        "SEC insiders: "
        + (
            f"enabled, data sets through {insider_index_stats['data_through']}"
            + (
                "; recent Form 4 filings read for tradeable candidates"
                if insider_index_stats["form4_supplement"] else ""
            )
            if insider_index_stats else "disabled"
        )
    )

    engine = DiscoveryEngine(
        source,
        benchmarks=BENCHMARKS,
        max_workers=MAX_CONCURRENT_DOWNLOADS,
        metadata_workers=METADATA_CONCURRENT_DOWNLOADS,
        price_workers=PRICE_CONCURRENT_DOWNLOADS,
        progress_callback=print_progress,
        result_callback=run_state.record_result,
        fundamentals_source=fundamentals_source,
        insider_lookup=insider_lookup,
        fundamentals_countries=tuple(FUNDAMENTALS_CONFIG.get("countries", ["US", "CA"])),
        statements_source=statements_source,
        sec_ticker_resolver=resolver,
        fx_rates=fx_rates,
    )
    results = engine.run(universe, prior_results=prior_results)

    # Shadow models need the full cross-section, so they are applied after
    # collection and written back to checkpoints; official scores are unchanged.
    results = apply_all_models(results)
    for index, result in enumerate(results):
        if result.get("status") == "OK":
            run_state.record_result(index, result)

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
    retry_source = source.source
    provider = retry_source.source
    print(f"Provider retries: {retry_source.stats.retries}")
    print(f"Retries exhausted: {retry_source.stats.exhausted}")
    if fundamentals_source is not None or statements_source is not None:
        statuses = Counter(
            re.split(r"[;:(]| via ", str(row.get("fundamentals_status", "not_requested")))[0].strip()
            for row in results if row.get("status") == "OK"
        )
        print(
            "SEC fundamentals: "
            + ", ".join(f"{name} {count}" for name, count in sorted(statuses.items()))
            + (
                f"; {fundamentals_source.stats.requests} SEC requests, "
                f"{fundamentals_source.stats.hits} SEC cache hits"
                if fundamentals_source is not None else ""
            )
        )
    if insider_lookup is not None:
        insider_statuses = Counter(
            str(row.get("insiders_status", "not_requested")).split(":")[0]
            for row in results if row.get("status") == "OK"
        )
        print(
            "SEC insiders: "
            + ", ".join(f"{name} {count}" for name, count in sorted(insider_statuses.items()))
        )
        print_insider_supplement(insider_lookup)
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
    print(
        "Research queue (v3 > v2 > official) saved to: "
        f"{intelligence['research_artifacts']['research_queue_csv_path']}"
    )
    print(f"Manifest saved to: {run_state.manifest_path}")
    return run_state.run_id


def rehearse_monthly(arguments=None) -> None:
    """Run the monthly cycle on a small sample in an isolated folder.

    The rehearsal is a separate process because the isolation has to be in
    place before any module reads its output paths.
    """
    import subprocess
    import sys

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = BASE_DIR / "data" / "rehearsal" / stamp
    root.mkdir(parents=True, exist_ok=False)
    forwarded = list(arguments) if arguments is not None else sys.argv[1:]
    print(f"Rehearsing the monthly cycle in {root}")
    print("Real runs, exports, cohorts, and the history database are not touched.")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *forwarded],
        env={**os.environ, "DISCOVERY_ISOLATED_ROOT": str(root)},
        cwd=str(BASE_DIR),
    )
    print()
    print(f"Rehearsal folder: {root}")
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def run_monthly(
    cycle: str | None = None,
    skip_universe: bool = False,
    rehearsal_size: int | None = None,
) -> None:
    """Run every unfinished step of this month's research cycle."""
    state_directory = RUN_DIR / "monthly"
    now = datetime.now(timezone.utc)
    if rehearsal_size is not None and ISOLATED_ROOT is None:
        raise SystemExit("A rehearsal must run in an isolated folder; use --monthly --rehearsal.")
    try:
        if cycle:
            label = validate_cycle(cycle)
        elif rehearsal_size is not None:
            label = validate_cycle(f"{now:%Y-%m}-rehearsal")
        else:
            label = default_cycle(state_directory, now)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Monthly cycle: {label}" + (
        f" (rehearsal, {rehearsal_size} names per country)" if rehearsal_size else ""
    ))
    canadian_universe = BASE_DIR / "data" / "raw" / "universe" / "universe_ca_auto.csv"

    def universe_step(context):
        if skip_universe:
            detail = "rebuild skipped (--skip-universe)"
        else:
            from src import build_canadian_universe, build_universe

            build_universe.main()
            build_canadian_universe.main()
            detail = "U.S. and Canadian universes rebuilt"
        source_date, age = universe_source_age(canadian_universe, context.now)
        if age is None:
            context.warnings.append(
                "the Canadian universe carries no TMX source date; check the workbook"
            )
        elif age > TMX_STALE_AFTER:
            context.warnings.append(
                f"TMX workbook data is from {source_date}, {age.days} days old; "
                "download 'TSX & TSXV Listed Companies' from "
                "https://www.tsx.com/en/listings/current-market-statistics to "
                "data/raw/canada/tsx-and-tsxv-listed-companies.xlsx before the next cycle"
            )
        return {"detail": f"{detail}; TMX data as of {source_date or 'unknown'}"}

    def discovery_step(context):
        run_id = run_discovery(cycle=label, balanced_sample=rehearsal_size)
        manifest, results = load_saved_run(RUN_DIR, run_id)
        if manifest.get("status") not in COMPLETED_RUN_STATUSES:
            raise RuntimeError(f"run {run_id} ended with status {manifest.get('status')}")
        ok, detail = discovery_quality(results)
        if not ok:
            raise RuntimeError(f"run {run_id}: {detail}")
        return {"run_id": run_id, "detail": detail}

    def index_step(context):
        index_history_run(context.run_id)
        return {"detail": "indexed"}

    def moonshot_step(context):
        analyze_moonshot_run(context.run_id, source=build_market_data_source())
        return {"detail": "analysis and market-risk evidence saved"}

    def calibrate_step(context):
        calibrate_saved_moonshot(context.run_id)
        return {"detail": "calibrated; Moonshot baseline frozen"}

    def freeze_step(context):
        if "research" in available_baseline_kinds(OUTPUT_DIR, context.run_id):
            return {"detail": "research baseline already frozen"}
        freeze_saved_research_baseline(context.run_id)
        return {"detail": "research baseline frozen"}

    def resolve_step(context):
        due = due_baselines(OUTPUT_DIR, context.now)
        if not due:
            return {"detail": "no horizon newly due"}
        failures = []
        for entry in due:
            print(
                f"\nResolving {entry['kind']} baseline {entry['run_id']} "
                f"({', '.join(entry['horizons'])} newly due)"
            )
            try:
                resolve_saved_baseline(
                    entry["run_id"], collect_prices=True, kind=entry["kind"],
                )
            except (Exception, SystemExit) as error:  # noqa: BLE001 - one baseline at a time
                failures.append(f"{entry['kind']} {entry['run_id']}: {error}")
                continue
            context.outputs.setdefault("resolved", []).append(
                {**entry, "summary": _resolution_headline(entry)}
            )
        if failures:
            raise RuntimeError("; ".join(failures))
        return {"detail": f"{len(due)} baseline(s) resolved"}

    try:
        with keep_awake() as awake:
            if awake:
                print("Keeping this PC from idle-sleeping until the cycle finishes.")
            state = run_cycle(
                label,
                {
                    "universe": universe_step,
                    "discovery": discovery_step,
                    "index": index_step,
                    "moonshot": moonshot_step,
                    "calibrate": calibrate_step,
                    "freeze": freeze_step,
                    "resolve": resolve_step,
                },
                state_directory,
            )
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    resolved = (state["steps"]["resolve"].get("outputs") or {}).get("resolved")
    report_path = OUTPUT_DIR / f"monthly_cycle_{label}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_cycle_report(state, resolved), encoding="utf-8")

    print(f"\nMonthly cycle {label}: {state['status']}")
    for name, record in state["steps"].items():
        print(f"  {name:<10} {record['status']:<8} {record.get('detail') or ''}")
    for warning in state.get("warnings", []):
        print(f"  warning: {warning}")
    print(f"Cycle report: {report_path}")
    print(f"Cycle state: {state_directory / (label + '.json')}")
    if state["status"] != "completed":
        raise SystemExit(
            f"Monthly cycle {label} finished with failures. Run --monthly again "
            "to retry the failed and blocked steps; finished steps are not repeated."
        )


def test_v3_change(run_id: str, change_text: str) -> None:
    """Run the pre-stated paired test for one proposed Score v3 change."""
    import pandas as pd

    from src.model_tests import export_result, paired_test, rescore

    try:
        change = json.loads(change_text)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"--change must be JSON, for example '{{\"momentum_long\": null}}': {error}")
    if not isinstance(change, dict) or not change:
        raise SystemExit("--change must be a non-empty JSON object of signal changes.")
    path = OUTPUT_DIR / f"backtest_observations_{run_id}.csv.gz"
    if not path.is_file():
        raise SystemExit(f"No backtest observations for {run_id}; run --backtest-run first.")
    observations = pd.read_csv(path, low_memory=False)
    observations["period"] = observations["period"].astype(str)
    base = STRATEGY.get("scoring_v3") or {}
    print(f"Observations: {path.name}, {len(observations):,} rows")
    if "score_v3" in observations:
        drift = (rescore(observations, base) - observations["score_v3"]).abs().max()
        if drift > 1e-6:
            print(
                "Warning: the current v3 config does not reproduce the scores this "
                f"backtest recorded (largest gap {drift:.2f} points); the test compares "
                "against the current config, not the one the backtest ran."
            )
        else:
            print("Current v3 config reproduces the backtest's own v3 scores.")
    try:
        result = paired_test(observations, base, change)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Change: {json.dumps(change)}")
    print(f"{'horizon':<8} {'fit diff':>9} {'NW t':>6} {'holdout diff':>13} {'NW t':>6}")
    for horizon, windows in result["horizons"].items():
        fit, holdout = windows["fit"], windows["holdout"]
        print(
            f"{horizon:<8} {fit['mean_difference']:>+9.4f} {fit['newey_west_t']:>6.1f} "
            f"{holdout['mean_difference']:>+13.4f} {holdout['newey_west_t']:>6.1f}"
        )
    print(
        f"Verdict on {result['decision_horizon']}: "
        + ("ACCEPT" if result["accepted"] else "reject")
        + " (positive in both windows, |t| >= 2 in one, significantly negative in neither)"
    )
    print(f"Result saved to: {export_result(result, run_id, OUTPUT_DIR)}")


def screen_research(run_id: str) -> None:
    """Which signals separate winners inside the v3 screen, by a rule fixed in advance."""
    import pandas as pd

    from src.screen_research import export_screen_research, scan_screen

    path = OUTPUT_DIR / f"backtest_observations_{run_id}.csv.gz"
    if not path.is_file():
        raise SystemExit(f"No backtest observations for {run_id}; run --backtest-run first.")
    observations = pd.read_csv(path, low_memory=False)
    print(f"Observations: {path.name}, {len(observations):,} rows")
    result = scan_screen(observations)
    population = result["population"]
    print(
        f"Screen: {population['observations']:,} observations over {population['periods']} "
        f"month-ends, median {population['median_names_per_period']:.0f} names"
    )
    for verdict in ("separates", "suggestive"):
        names = [
            f"{name} ({entry['direction']})"
            for name, entry in result["signals"].items() if entry["verdict"] == verdict
        ]
        print(f"{verdict.capitalize()}: {', '.join(names) if names else 'none'}")
    holdout = result["tiebreaker"]["holdout"]
    chosen = ", ".join(result["tiebreaker"]["chosen_on_fit_window"]) or "none"
    print(
        f"Tie-breaker chosen on 2017-21 ({chosen}): holdout IC {holdout['mean_ic']:+.3f} "
        f"(t {holdout['newey_west_t']:+.1f})"
    )
    json_path, markdown_path = export_screen_research(result, run_id, OUTPUT_DIR)
    print(f"Report: {markdown_path}")
    print(f"Data: {json_path}")


def unscored_research(run_id: str) -> None:
    """How the names Score v3 never scores compare with the screen."""
    import pandas as pd

    from src.unscored_research import export_unscored_research, unscored_research as study

    path = OUTPUT_DIR / f"backtest_observations_{run_id}.csv.gz"
    if not path.is_file():
        raise SystemExit(f"No backtest observations for {run_id}; run --backtest-run first.")
    observations = pd.read_csv(path, low_memory=False)
    print(f"Observations: {path.name}, {len(observations):,} rows")
    result = study(observations)
    for name in ("never_scored", "never_scored:financials", "never_scored:no_fundamentals",
                 "never_scored:other"):
        against = result["groups"].get(name, {}).get("against_screen") or {}
        parts = [
            f"{window} median {values['median_minus_screen']:+.1f} pts, "
            f"above the screen in {values['periods_median_above_screen']}/{values['periods']} months"
            for window, values in against.items()
        ]
        print(f"{name} vs screen (1Y): " + ("; ".join(parts) if parts else "not measured"))
    separating = [n for n, e in result["never_scored_signals"].items() if e["verdict"] == "separates"]
    print(f"Signals that sort the never-scored group: {', '.join(separating) or 'none'}")
    json_path, markdown_path = export_unscored_research(result, run_id, OUTPUT_DIR)
    print(f"Report: {markdown_path}")
    print(f"Data: {json_path}")


def survivorship_test(run_id: str) -> None:
    """Bound the backtest's survivorship with SEC records of delisted companies."""
    import pandas as pd

    from src.backtest import MAX_MARKET_CAP, MIN_MARKET_CAP
    from src.scoring_v2 import model_configs, score_frame

    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise SystemExit("--survivorship-test requires SEC_USER_AGENT in .env.")
    filings = SecFilingsSource(
        user_agent=user_agent,
        cache_directory=BASE_DIR / "data" / "cache" / "sec_filings",
        request_interval_seconds=float(FUNDAMENTALS_CONFIG.get("request_interval_seconds", 0.11)),
    )
    sec = build_sec_xbrl_source()
    try:
        _, results = load_saved_run(RUN_DIR, run_id)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise SystemExit(f"Unable to load run {run_id}: {error}") from error

    path = OUTPUT_DIR / f"backtest_observations_{run_id}.csv.gz"
    if not path.is_file():
        raise SystemExit(f"No backtest observations for {run_id}; run --backtest-run first.")
    observations_probe = pd.read_csv(path, nrows=1)
    basis = (
        "point-in-time universe" if "universe_market_cap" in observations_probe
        else "today's universe (rerun --backtest-run for the point-in-time universe)"
    )
    print(f"Backtest observations: {path.name} ({basis})")
    observations = pd.read_csv(path, low_memory=False)
    observations["period"] = observations["period"].astype(str)
    period_dates = (
        observations[["period", "date"]].drop_duplicates("period").sort_values("period")
    )
    periods = [
        (str(label), datetime.strptime(str(value)[:10], "%Y-%m-%d").date())
        for label, value in period_dates.itertuples(index=False)
    ]
    start, end = periods[0][1], periods[-1][1]

    current = {
        cik for cik in (
            sec.cik_for(row.get("ticker")) for row in results
            if str(row.get("country") or "").upper() == "US"
        ) if cik
    }
    today = datetime.now(timezone.utc)
    quarter_list = quarters(
        (start.year, (start.month - 1) // 3 + 1), (today.year, (today.month - 1) // 3 + 1),
    )
    notices = []
    for index, (year, quarter) in enumerate(quarter_list, start=1):
        print_progress("survivorship-notices", index, len(quarter_list), f"{year}q{quarter}")
        notices.extend(filings.listing_notices(year, quarter))
    candidates = delisted_candidates(notices, current, start, end)

    # The SEC answers one request in roughly half a second, so a few workers
    # sharing the source's pacing lock fetch faster without exceeding its
    # request rate.
    from concurrent.futures import ThreadPoolExecutor

    def fetched(label, keys, fetch, names):
        results, errors = {}, Counter()

        def one(key):
            try:
                return key, fetch(key), None
            except Exception as error:  # noqa: BLE001 - one company at a time
                return key, None, type(error).__name__

        with ThreadPoolExecutor(max_workers=3) as pool:
            for index, (key, value, error) in enumerate(pool.map(one, keys), start=1):
                print_progress(label, index, len(keys), str(names.get(key, key))[:40])
                if error:
                    errors[error] += 1
                else:
                    results[key] = value
        return results, errors

    ordered = sorted(candidates)
    submissions_by_cik, errors = fetched(
        "survivorship-exits", ordered, filings.submissions,
        {cik: entry.get("name") for cik, entry in candidates.items()},
    )
    failures = Counter({f"submissions: {name}": count for name, count in errors.items()})
    exits = []
    for cik in ordered:
        submissions = submissions_by_cik.get(cik)
        if submissions is None:
            continue
        if submissions.get("entity_type") not in (None, "operating"):
            failures["not an operating company"] += 1
            continue
        entry = candidates[cik]
        exits.append(classify_exit(cik, entry.get("name"), entry["notices"], submissions))
    population = [item for item in exits if not item.blank_check]

    extracts, errors = fetched(
        "survivorship-facts", [item.cik for item in population], filings.company_facts,
        {item.cik: item.name for item in population},
    )
    failures.update({f"facts: {name}": count for name, count in errors.items()})

    funnel = {
        "delisting notices in window": len(notices),
        "issuers delisted and not in today's universe": len(candidates),
        "operating companies classified": len(exits),
        "blank-check companies dropped": len(exits) - len(population),
        "with SEC fundamentals": len(extracts),
        "exit types": dict(Counter(item.exit_type for item in population)),
        "lookup failures": dict(failures),
    }
    model = next(m for m in model_configs() if m["output_prefix"] == "score_v3")
    variants = {}
    for name, unknown in (("in_band_only", False), ("including_unknown_size", True)):
        dead = dead_observations(
            population, extracts, periods, MIN_MARKET_CAP, MAX_MARKET_CAP,
            include_unknown_size=unknown,
        )
        variants[name] = build_report(
            observations, dead, model, score_frame,
            {**funnel, "dead companies with observations": int(dead["ticker"].nunique()) if not dead.empty else 0},
        )
    payload = {"run_id": run_id, "basis": basis, "variants": variants}
    json_path = OUTPUT_DIR / f"survivorship_{run_id}.json"
    markdown_path = OUTPUT_DIR / f"survivorship_{run_id}.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(
        build_markdown(variants["in_band_only"], run_id, basis)
        + "\n\n# Sensitivity: including delisted companies of unknown size\n\n"
        + build_markdown(variants["including_unknown_size"], run_id, basis).split("\n", 2)[2],
        encoding="utf-8",
    )
    print()
    print(markdown_path.read_text(encoding="utf-8"))
    print(f"Survivorship JSON: {json_path}")
    print(f"Survivorship report: {markdown_path}")


def _resolution_headline(entry: dict) -> dict:
    """Selected and control outcomes for each newly due horizon of a baseline."""
    path = OUTPUT_DIR / f"baseline_resolution_{entry['kind']}_{entry['run_id']}.json"
    try:
        resolution = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    headline = {}
    for horizon in entry["horizons"]:
        measured = (resolution.get("horizons") or {}).get(horizon) or {}
        headline[horizon] = {
            side: {
                key: (measured.get(side) or {}).get(key)
                for key in ("median_excess_percent", "win_rate_percent", "resolved")
            }
            for side in ("selected", "control")
        }
        common = (measured.get("model_comparison") or {}).get("common")
        if common:
            headline[horizon]["head_to_head"] = {
                "resolved": common["resolved"],
                **common["information_coefficient"],
            }
        screen = measured.get("screen_comparison")
        if screen:
            headline[horizon]["inside_screen"] = {
                "resolved": screen["resolved"],
                **screen["information_coefficient"],
                "most_minus_least_defensive_median_percent":
                    screen["most_minus_least_defensive_median_percent"],
            }
    return headline


def freeze_saved_research_baseline(run_id: str) -> None:
    """Freeze a completed run's research queue as a forward-test cohort."""
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
        baseline_as_of = str(manifest["completed_at"])
        queue, summary = build_research_queue(results)
        if not any(
            str(row.get("ranking_basis")) == "score_v3" for row in queue
        ):
            raise SystemExit(
                f"Run {run_id} has no Score v3 rankings to freeze. "
                "Rescore it first with --rescore-run."
            )
        baseline = build_research_baseline(queue, run_id, baseline_as_of)
        artifacts = export_research_baseline(baseline, OUTPUT_DIR)
        manifest_path = record_research_baseline(
            RUN_DIR,
            run_id,
            {
                "baseline_id": baseline["baseline_id"],
                "baseline_as_of": baseline["baseline_as_of"],
                "model_version": baseline["model_version"],
                "candidate_count": baseline["candidate_count"],
                "cohort_counts": baseline["cohort_counts"],
                "queue_unchanged": True,
                "official_scores_and_ranks_unchanged": True,
                **artifacts,
            },
        )
    except FileExistsError as error:
        raise SystemExit(str(error)) from error
    except (FileNotFoundError, OSError, TypeError, ValueError, KeyError) as error:
        raise SystemExit(f"Unable to freeze research baseline: {error}") from error

    print(f"Research baseline frozen for run: {run_id}")
    print(f"Model: {baseline['model_version']} · anchored {baseline['baseline_as_of']}")
    print(f"Baseline ID: {baseline['baseline_id'][:16]}")
    print(f"Candidates: {baseline['candidate_count']}")
    for name, count in baseline["cohort_counts"].items():
        role = (
            "selected" if name in baseline["selected_classifications"]
            else "control" if name in baseline["control_classifications"]
            else "observed"
        )
        print(f"  {name}: {count} ({role})")
    horizons = baseline["config"]["horizon_days"]
    first = baseline["candidates"][0]["horizons"] if baseline["candidates"] else {}
    for label in sorted(horizons):
        print(f"  {label} eligible {first.get(label, {}).get('eligible_at', '—')[:10]}")
    print("Research queue and official scores: unchanged")
    print(f"Baseline JSON saved to: {artifacts['research_baseline_json_path']}")
    print(f"Baseline CSV saved to: {artifacts['research_baseline_csv_path']}")
    print(f"Manifest updated: {manifest_path}")


def resolve_saved_baseline(
    run_id: str,
    collect_prices: bool = False,
    kind: str = "all",
) -> None:
    """Measure every requested frozen forward baseline against realized prices."""
    settings = BACKTEST_CONFIG.get("baseline_resolution", {})
    available = available_baseline_kinds(OUTPUT_DIR, run_id)
    wanted_kinds = available if kind == "all" else [kind]
    if not wanted_kinds or (kind == "all" and not available):
        raise SystemExit(
            f"No frozen baseline is exported for run {run_id}. Run "
            "--calibrate-moonshot or --freeze-research-baseline first."
        )
    for index, baseline_kind in enumerate(wanted_kinds):
        if index:
            print()
        _resolve_one_baseline(run_id, baseline_kind, collect_prices, settings)


def _resolve_one_baseline(
    run_id: str,
    kind: str,
    collect_prices: bool,
    settings: dict,
) -> None:
    try:
        baseline = load_forward_baseline(OUTPUT_DIR, run_id, kind=kind)
        tickers = [str(row["ticker"]) for row in baseline["candidates"]]
        benchmarks = sorted({
            str(row.get("benchmark") or "") for row in baseline["candidates"]
            if row.get("benchmark")
        })
        series, cache_stats = load_price_series(
            CACHE_DIR, benchmarks + tickers,
        )
        collection = None
        if collect_prices:
            wanted = tickers_needing_prices(baseline, series, config=settings)
            collection = {"requested": len(wanted), "errors": 0}
            if wanted:
                source = build_market_data_source(price_history_ttl_hours=0)
                for completed, ticker in enumerate(wanted, start=1):
                    try:
                        source.get_price_history(ticker)
                    except Exception:  # noqa: BLE001 - isolate failures
                        collection["errors"] += 1
                    print_progress("resolve-prices", completed, len(wanted), ticker)
                series, cache_stats = load_price_series(
                    CACHE_DIR, benchmarks + tickers,
                )
        resolution = resolve_baseline(baseline, series, config=settings)
        artifacts = export_baseline_resolution(resolution, OUTPUT_DIR)
        manifest_path = record_baseline_resolution(
            RUN_DIR,
            run_id,
            {
                "baseline_id": resolution["baseline_id"],
                "baseline_as_of": resolution["baseline_as_of"],
                "model_version": resolution["model_version"],
                "resolved_at": resolution["resolved_at"],
                "candidate_count": resolution["candidate_count"],
                "eligible_horizons": resolution["eligible_horizons"],
                "pending_horizons": resolution["pending_horizons"],
                "prices_collected": collect_prices,
                "baseline_unchanged": True,
                "official_scores_and_ranks_unchanged": True,
                **artifacts,
            },
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, KeyError) as error:
        raise SystemExit(f"Unable to resolve forward baseline: {error}") from error

    print(f"Forward baseline resolved for run: {run_id} ({kind})")
    print(f"Baseline frozen {resolution['baseline_as_of']} · model {resolution['model_version']}")
    print(f"Candidates: {resolution['candidate_count']}")
    print(
        "Price series: "
        f"{cache_stats['loaded']} loaded, {cache_stats['missing']} missing, "
        f"{cache_stats['read_errors']} read errors"
    )
    if collection is not None:
        print(
            "Price collection: "
            f"{collection['requested']} requested, {collection['errors']} errors"
        )
    if not resolution["eligible_horizons"]:
        print("No horizon is eligible yet; nothing to measure.")
    for label in resolution["eligible_horizons"]:
        report = resolution["horizons"][label]
        selected, control = report["selected"], report["control"]
        difference = report["selected_minus_control"]
        print(f"\n{label} horizon ({'comparable' if report['comparable'] else 'cohorts too small'})")
        for name, cohort in (("Selected", selected), ("Control", control)):
            print(
                f"  {name}: {cohort['resolved']}/{cohort['candidates']} resolved, "
                f"median {cohort['median_excess_percent']}%, "
                f"win rate {cohort['win_rate_percent']}%, "
                f"{cohort['unresolved_stale']} stopped trading"
            )
            if cohort["resolved_without_benchmark"]:
                print(
                    f"    {cohort['resolved_without_benchmark']} have a return but no "
                    "benchmark for this window; rerun with --collect-prices"
                )
        print(
            f"  Selected minus control: median "
            f"{difference['median_excess_percent']} pts, win rate "
            f"{difference['win_rate_percent']} pts"
        )
        comparison = report.get("model_comparison") or {}
        common = comparison.get("common")
        for name, measured in comparison.items():
            if name == "common":
                continue
            ladder = ", ".join(
                f"Q{q['quintile']} {q['median_excess_percent']}%"
                for q in measured.get("quintiles", [])
            )
            print(
                f"  {name}: rank IC {measured['information_coefficient']} over "
                f"{measured['resolved']} names" + (f"; medians {ladder}" if ladder else "")
            )
        if common:
            coefficients = ", ".join(
                f"{name} {value}" for name, value in common["information_coefficient"].items()
            )
            print(f"  Head to head on {common['resolved']} names both scored: {coefficients}")
        screen = report.get("screen_comparison")
        if screen:
            coefficients = screen["information_coefficient"]
            ladder = ", ".join(
                f"T{t['tercile']} {t['median_excess_percent']}%" for t in screen["defensive_terciles"]
            )
            print(
                f"  Inside the screen ({screen['resolved']} names): defensive IC "
                f"{coefficients['score_defensive']}, v3 IC {coefficients['score_v3']}"
                + (f"; defensive terciles, least to most, {ladder}" if ladder else "")
            )
    if resolution["pending_horizons"]:
        print(f"\nStill pending: {', '.join(resolution['pending_horizons'])}")
    print("Forward baseline file: unchanged")
    print(f"Resolution JSON saved to: {artifacts['baseline_resolution_json_path']}")
    print(f"Resolution CSV saved to: {artifacts['baseline_resolution_csv_path']}")
    print(f"Manifest updated: {manifest_path}")


def rescore_saved_run(run_id: str, refresh_fundamentals: bool = False) -> None:
    """Reapply shadow models to a completed run using only cached data."""
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
        as_of = datetime.fromisoformat(str(manifest["completed_at"]))
        sec_source = build_sec_xbrl_source(required=False)
        market_source = build_market_data_source()
        fx_rates = load_fx_rates(market_source, cached_only=True)
        fundamentals_lookup = None
        insider_lookup = None
        insider_index_stats = None
        resolver = build_sec_resolver(sec_source) if sec_source is not None else None
        rows_by_ticker = {str(row.get("ticker")): row for row in results}

        def fundamentals_lookup(ticker: str):
            row = rows_by_ticker.get(ticker, {})
            if sec_source is not None:
                sec_ticker, _ = resolver(row)
                if sec_ticker is not None:
                    extract = sec_source.get_cached_company_facts(sec_ticker)
                    # An extract can exist and still hold no usable facts;
                    # the live run falls back to statements in that case, so
                    # rescoring has to fall back too rather than return an
                    # empty history and lose the fundamentals entirely.
                    if extract:
                        history = FundamentalHistory(extract)
                        if history.has_data:
                            return history
            # The statements this run collected, however long ago: a cache
            # that has merely aged must not clear a name's fundamentals.
            statements = market_source.get_cached_financial_statements(ticker, any_age=True)
            if statements:
                return FundamentalHistory(extract_from_statements(ticker, statements))
            return None

        def splits_lookup(ticker: str):
            """Split events from the same cached frame the run priced from."""
            frame = market_source.get_cached_price_history(ticker)
            if frame is None:
                return None
            return SplitHistory.from_price_history(frame)

        if sec_source is not None:
            insider_source = build_sec_insider_source(required=False)
            if insider_source is not None:
                lookback = int(INSIDERS_CONFIG.get("lookback_quarters", 6))
                labels = quarter_labels(
                    "2000q1", f"{as_of.year}q{(as_of.month - 1) // 3 + 1}",
                )
                insider_lookup, insider_index_stats = build_live_insider_lookup(
                    insider_source, sec_source.cik_for, labels[max(0, len(labels) - lookback)],
                )
                # Rescoring walks rows one at a time; fetch tradeable names'
                # recent filings concurrently first so that pass reads cache.
                insider_lookup.prefetch(
                    [
                        str(row.get("ticker")) for row in results
                        if row.get("status") == "OK"
                        and str(row.get("country") or "").upper() == "US"
                        and liquidity_eligible(row)
                    ],
                    progress=print_progress,
                )
        prefetch_counts = None
        if refresh_fundamentals and sec_source is not None:
            # Recomputing reads cached extracts only, and a missing or expired
            # one would clear that name's fundamentals. Fetch them first.
            countries = {str(c).upper() for c in FUNDAMENTALS_CONFIG.get("countries", ["US", "CA"])}
            sec_tickers = []
            for row in results:
                if row.get("status") != "OK" or str(row.get("country") or "").upper() not in countries:
                    continue
                sec_ticker, _ = resolver(row)
                if sec_ticker is not None:
                    sec_tickers.append(sec_ticker)
            prefetch_counts = sec_source.prefetch(sec_tickers, progress=print_progress)
        rescored, stats = rescore_results(
            results, as_of,
            fundamentals_lookup=fundamentals_lookup,
            insider_lookup=insider_lookup,
            splits_lookup=splits_lookup,
            countries=tuple(FUNDAMENTALS_CONFIG.get("countries", ["US", "CA"])),
            fx_rates=fx_rates,
            refresh_fundamentals=refresh_fundamentals,
        )
        run_state = RunState.open(RUN_DIR, run_id)
        for index, row in enumerate(rescored):
            if row.get("status") == "OK":
                run_state.record_result(index, row)
        report_path = export_report(rescored, run_id=run_id)
        candidate_path = export_candidate_report(rescored, run_id=run_id)
        queue_paths = export_research_queue(rescored, run_id, OUTPUT_DIR)
        manifest_path = record_rescore(
            RUN_DIR,
            run_id,
            {
                **stats,
                "insider_data_through": (
                    insider_index_stats["data_through"] if insider_index_stats else None
                ),
                "official_scores_and_ranks_unchanged": True,
                "report_path": report_path,
                "candidate_report_path": candidate_path,
                **queue_paths,
            },
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
        raise SystemExit(f"Unable to rescore run: {error}") from error
    print(f"Rescored run: {run_id} as of {stats['as_of']}")
    print("Models: " + ", ".join(f"{k} {v}" for k, v in stats["models"].items()))
    if stats.get("refresh_fundamentals"):
        print("Fundamentals: recomputed from cached extracts")
    if prefetch_counts:
        print(
            f"SEC facts: {prefetch_counts['requested']} issuers, {prefetch_counts['cached']} cached, "
            f"{prefetch_counts['fetched']} fetched, {prefetch_counts['failed']} failed"
        )
    print(
        f"Successful rows: {stats['successful_rows']}; fundamentals filled "
        f"{stats['fundamentals_filled']}, cleared {stats['fundamentals_cleared']}, "
        f"unavailable {stats['fundamentals_unavailable']}; "
        f"insiders filled {stats['insiders_filled']}, unavailable "
        f"{stats['insiders_unavailable']}; non-U.S. {stats['not_applicable']}"
    )
    if sec_source is None:
        print("SEC data skipped: set SEC_USER_AGENT in .env to use cached SEC extracts")
    if insider_lookup is not None:
        print_insider_supplement(insider_lookup)
    print("Official Discovery scores and ranks: unchanged")
    print(f"Report refreshed: {report_path}")
    print(f"Candidate report refreshed: {candidate_path}")
    print(f"Research queue refreshed: {queue_paths['research_queue_csv_path']}")
    print(f"Manifest updated: {manifest_path}")


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
    research_artifacts.update(
        export_research_queue(results, run_id, OUTPUT_DIR)
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


def _insider_lookup(index: dict, cik_for):
    """Adapt a CIK-keyed insider index to a ticker lookup."""
    def lookup(ticker: str):
        cik = cik_for(ticker)
        return index.get(str(cik).zfill(10)) if cik is not None else None
    return lookup


def load_fx_rates(source, pairs: list[str] | None = None, cached_only: bool = False) -> dict:
    """Load latest FX multipliers from Yahoo pair histories such as CADUSD=X."""
    rates: dict[tuple[str, str], float] = {}
    for symbol in pairs or FUNDAMENTALS_CONFIG.get("fx_pairs", ["CADUSD=X"]):
        base, quote = str(symbol)[:3].upper(), str(symbol)[3:6].upper()
        try:
            history = (
                source.get_cached_price_history(symbol) if cached_only
                else source.get_price_history(symbol)
            )
        except Exception:  # noqa: BLE001 - FX is optional
            history = None
        if history is None or history.empty or "Close" not in history:
            continue
        closes = history["Close"].dropna()
        if closes.empty or float(closes.iloc[-1]) <= 0:
            continue
        rate = float(closes.iloc[-1])
        rates[(base, quote)] = rate
        rates[(quote, base)] = 1.0 / rate
    return rates


def build_sec_resolver(sec_source):
    """Resolve U.S. tickers directly and interlisted Canadian names by verified root symbol."""
    def resolver(stock_data: dict):
        return resolve_sec_ticker(stock_data, sec_source.cik_for, sec_source.title_for)
    return resolver


_SEC_PACER: SecPacer | None = None


def sec_pacer() -> SecPacer:
    """The one request schedule every SEC source in this process shares."""
    global _SEC_PACER
    if _SEC_PACER is None:
        _SEC_PACER = SecPacer(float(FUNDAMENTALS_CONFIG.get("request_interval_seconds", 0.11)))
    return _SEC_PACER


def build_sec_form4_source() -> SecForm4Source | None:
    """Recent Form 4 filings, read per company, when SEC access is configured."""
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent or not INSIDERS_CONFIG.get("form4_supplement_enabled", True):
        return None
    return SecForm4Source(
        user_agent=user_agent,
        cache_directory=BASE_DIR / "data" / "cache" / "sec_form4",
        pacer=sec_pacer(),
    )


def build_live_insider_lookup(insider_source, cik_for, start_quarter: str):
    """Insider histories from the data sets plus filings made since they end."""
    transactions, loaded, unavailable = insider_source.load_quarters(start_quarter)
    data_through = quarter_end(loaded[-1]) if loaded else None
    form4 = build_sec_form4_source()
    lookup = SupplementedInsiderLookup(
        transactions, data_through, cik_for, form4, INSIDER_SIGNALS_CONFIG,
    )
    return lookup, {
        "quarters_loaded": loaded,
        "quarters_unavailable": unavailable,
        "data_through": data_through.isoformat() if data_through else None,
        "transactions": len(transactions),
        "issuers": len(lookup.rows_by_cik),
        "form4_supplement": form4 is not None,
    }


def print_insider_supplement(lookup) -> None:
    stats = getattr(lookup, "stats", None)
    if not stats:
        return
    form4 = getattr(lookup, "form4_source", None)
    parts = [
        f"{stats.get('fresh', 0)} companies refreshed"
        + (f" through {lookup.fresh_through}" if lookup.fresh_through else ""),
        f"{stats.get('fresh_transactions', 0)} recent transactions",
        f"{stats.get('data_set_only', 0)} below the liquidity floor on data sets only",
    ]
    if stats.get("fresh_failed"):
        parts.append(f"{stats['fresh_failed']} kept data sets only after a failed fetch")
    if stats.get("fresh_incomplete"):
        parts.append(f"{stats['fresh_incomplete']} with possibly incomplete filing lists")
    if form4 is not None:
        parts.append(
            f"{form4.stats.filings_parsed} filings read, {form4.stats.filings_cached} from cache"
        )
    print("Form 4 supplement: " + "; ".join(parts))


def build_sec_xbrl_source(required: bool = True) -> SecXbrlSource | None:
    """Build the paced, compact-cached SEC company-facts source."""
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        if not required:
            return None
        raise SystemExit(
            "--with-fundamentals requires SEC_USER_AGENT, for example "
            "'Discovery Engine research@example.com'."
        )
    settings = FUNDAMENTALS_CONFIG
    return SecXbrlSource(
        user_agent=user_agent,
        cache_directory=BASE_DIR / settings.get("cache_directory", "data/cache/sec_facts"),
        ttl_hours=float(settings.get("cache_ttl_hours", 168)),
        request_interval_seconds=float(settings.get("request_interval_seconds", 0.11)),
        pacer=sec_pacer(),
    )


def build_sec_insider_source(required: bool = True) -> SecInsiderTransactionsSource | None:
    """Build the cached SEC insider-transactions data-set source."""
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        if not required:
            return None
        raise SystemExit(
            "--with-insiders requires SEC_USER_AGENT, for example "
            "'Discovery Engine research@example.com'."
        )
    settings = INSIDERS_CONFIG
    return SecInsiderTransactionsSource(
        user_agent,
        BASE_DIR / settings.get("cache_directory", "data/cache/sec_insiders"),
        request_interval_seconds=float(settings.get("request_interval_seconds", 0.11)),
        unavailable_ttl_hours=float(settings.get("unavailable_ttl_hours", 168)),
    )


def load_insider_index(source: SecInsiderTransactionsSource, start_quarter: str) -> tuple[dict, dict]:
    """Load insider quarters from ``start_quarter`` and index them by CIK."""
    transactions, loaded, unavailable = source.load_quarters(start_quarter)
    data_through = quarter_end(loaded[-1]) if loaded else None
    index = build_insider_index(
        transactions, INSIDER_SIGNALS_CONFIG, data_through=data_through,
    )
    return index, {
        "quarters_loaded": loaded,
        "quarters_unavailable": unavailable,
        "data_through": data_through.isoformat() if data_through else None,
        "transactions": len(transactions),
        "issuers": len(index),
    }


def live_insider_start_quarter() -> str:
    """Quarter from which live runs need insider history for their windows."""
    lookback = int(INSIDERS_CONFIG.get("lookback_quarters", 6))
    labels = quarter_labels("2000q1", current_quarter())
    return labels[max(0, len(labels) - lookback)]


def backtest_saved_run(
    run_id: str,
    limit: int | None = None,
    with_fundamentals: bool = False,
    with_insiders: bool = False,
    static_universe: bool = False,
    no_liquidity_floor: bool = False,
) -> None:
    """Replay technical scoring point-in-time for a completed run's universe."""
    overrides = {"point_in_time_universe": False} if static_universe else {}
    if no_liquidity_floor:
        overrides["minimum_average_dollar_volume"] = 0
    period = str(BACKTEST_CONFIG.get("price_history_period", "10y"))
    ttl_hours = float(BACKTEST_CONFIG.get("price_history_ttl_hours", 168))
    fundamental_settings = BACKTEST_CONFIG.get("sec_fundamentals", {})
    sec_source = (
        build_sec_xbrl_source() if (with_fundamentals or with_insiders) else None
    )
    insider_source = build_sec_insider_source() if with_insiders else None
    fundamental_stats = None
    insider_stats = None
    try:
        manifest, results = load_saved_run(RUN_DIR, run_id)
        universe = build_backtest_universe(results, overrides or None)
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
        fundamental_histories = None
        insider_histories = None
        if insider_source is not None:
            index, index_stats = load_insider_index(
                insider_source,
                str(BACKTEST_CONFIG.get("sec_insiders", {}).get("start_quarter", "2015q1")),
            )
            insider_histories, insider_stats = build_insider_histories(
                universe, index, sec_source.cik_for,
            )
            insider_stats = {**insider_stats, **index_stats}
        if with_fundamentals and sec_source is not None:
            fundamental_histories, fundamental_errors, fundamental_stats = (
                collect_fundamental_histories(
                    universe,
                    sec_source,
                    max_workers=int(fundamental_settings.get("collection_workers", 2)),
                    progress_callback=print_progress,
                    config={
                        "maximum_report_age_days": int(
                            fundamental_settings.get("maximum_report_age_days", 400)
                        ),
                    },
                )
            )
            errors.update({
                f"{ticker} (SEC)": message
                for ticker, message in fundamental_errors.items()
            })
        analysis = build_backtest(
            universe,
            histories,
            benchmark_histories,
            BENCHMARKS,
            run_id,
            config=overrides or None,
            collection_errors=errors,
            fundamental_histories=fundamental_histories,
            fundamental_stats=fundamental_stats,
            limit=limit,
            insider_histories=insider_histories,
            insider_stats=insider_stats,
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
                "with_fundamentals": with_fundamentals,
                "fundamental_coverage": analysis["fundamental_coverage"],
                "with_insiders": with_insiders,
                "insider_coverage": analysis["insider_coverage"],
                "point_in_time_universe": analysis["point_in_time_universe"],
                "universe_band": analysis["universe_band"],
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
    band = analysis.get("universe_band")
    if band:
        print(
            "Point-in-time universe: "
            f"{band.get('inside_band', 0)} observations inside the market-cap band, "
            f"{band.get('outside_band', 0)} outside; basis "
            + ", ".join(f"{k[6:]} {v}" for k, v in band.items() if k.startswith("basis_"))
        )
    if source.enabled:
        print(f"Cache hits: {source.stats.hits}")
        print(f"Cache misses: {source.stats.misses}")
        print(f"Cache expired: {source.stats.expired}")
    if sec_source is not None and fundamental_stats is not None:
        print(
            "SEC fundamentals: "
            f"{fundamental_stats['with_data']} with data, "
            f"{fundamental_stats['without_data']} without, "
            f"{fundamental_stats['errors']} errors, "
            f"{fundamental_stats['skipped_non_us']} non-U.S. skipped; "
            f"{sec_source.stats.requests} SEC requests, "
            f"{sec_source.stats.hits} cache hits"
        )
    if insider_stats is not None:
        print(
            "SEC insiders: "
            f"{insider_stats['with_data']} tickers with Form 4 history, "
            f"{insider_stats['without_data']} without, "
            f"{insider_stats['unmapped']} unmapped; data through "
            f"{insider_stats['data_through']}, "
            f"{len(insider_stats['quarters_loaded'])} quarters"
        )
    ic = analysis["aggregate"]["information_coefficient"]
    for column in ("technical_score", "discovery_score_static", "score_v2", "score_v3"):
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
        queue = None
        if RESEARCH_RANKING_CONFIG.get("packet_source", "research_queue") == "research_queue":
            queue, _queue_summary = build_research_queue(results)
        packets, metadata = build_research_packets(
            results,
            top_n,
            calibration=calibration,
            balanced_per_country=balanced_per_country,
            queue=queue,
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
