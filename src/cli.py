"""Command-line options for full and controlled Discovery Engine runs."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse mutually exclusive universe-selection options."""
    parser = argparse.ArgumentParser(
        description="Analyze the Discovery Engine equity universe.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="analyze only these tickers from the configured universe",
    )
    selection.add_argument(
        "--limit",
        type=_positive_integer,
        metavar="COUNT",
        help="analyze only the first COUNT universe entries",
    )
    selection.add_argument(
        "--balanced-sample",
        type=_positive_integer,
        metavar="PER_COUNTRY",
        help="interleave up to PER_COUNTRY Canadian and U.S. listings",
    )
    selection.add_argument(
        "--recalibrate-run",
        metavar="RUN_ID",
        help="regenerate calibration artifacts from a completed saved run",
    )
    selection.add_argument(
        "--moonshot-run",
        metavar="RUN_ID",
        help="build the offline Moonshot Discovery shadow analysis",
    )
    selection.add_argument(
        "--calibrate-moonshot",
        metavar="RUN_ID",
        help="validate Moonshot scenarios and freeze a forward baseline",
    )
    selection.add_argument(
        "--research-run",
        metavar="RUN_ID",
        help="build research packets from a completed saved run",
    )
    selection.add_argument(
        "--audit-research",
        metavar="RUN_ID",
        help="audit saved research artifacts without network or AI calls",
    )
    selection.add_argument(
        "--finalize-research-review",
        metavar="RUN_ID",
        help="finalize an audited research run from its completed review CSV",
    )
    selection.add_argument(
        "--index-run",
        metavar="RUN_ID",
        help="index a completed saved run into the local history database",
    )
    selection.add_argument(
        "--history-summary",
        action="store_true",
        help="show the local offline history database summary",
    )
    selection.add_argument(
        "--compare-runs",
        nargs=2,
        metavar=("OLDER_RUN_ID", "NEWER_RUN_ID"),
        help="compare two completed runs in the local history database",
    )
    selection.add_argument(
        "--ticker-history",
        metavar="TICKER",
        help="export one ticker's timeline across all indexed runs",
    )
    selection.add_argument(
        "--weekly-report",
        action="store_true",
        help="compare the latest compatible indexed runs and export a briefing",
    )
    selection.add_argument(
        "--dashboard",
        action="store_true",
        help="serve the local history and watchlist dashboard on loopback",
    )
    parser.add_argument(
        "--dashboard-port",
        type=_port,
        default=8765,
        metavar="PORT",
        help="loopback port for --dashboard (default: 8765)",
    )
    parser.add_argument(
        "--top",
        type=_positive_integer,
        metavar="COUNT",
        help="research only the top COUNT selected candidates",
    )
    parser.add_argument(
        "--collect-market-risk",
        action="store_true",
        help="collect cached price/share evidence for --moonshot-run",
    )
    parser.add_argument(
        "--moonshot-limit",
        type=_positive_integer,
        metavar="COUNT",
        help="collect market-risk evidence for only the first COUNT Moonshot names",
    )
    parser.add_argument(
        "--balanced-research",
        type=_positive_integer,
        metavar="PER_COUNTRY",
        help="research up to PER_COUNTRY selected Canadian and U.S. candidates",
    )
    parser.add_argument(
        "--collect-sources",
        action="store_true",
        help="collect cached primary-source evidence for research packets",
    )
    parser.add_argument(
        "--source-manifest",
        metavar="PATH",
        help="attach curated, hashed evidence from a local JSON manifest",
    )
    parser.add_argument(
        "--canadian-source-manifest",
        metavar="PATH",
        help="fetch allowlisted Canadian issuer-primary URLs from a local manifest",
    )
    parser.add_argument(
        "--synthesize",
        action="store_true",
        help="generate validated cited briefs from attached evidence",
    )
    args = parser.parse_args(arguments)
    if args.dashboard_port != 8765 and not args.dashboard:
        parser.error("--dashboard-port requires --dashboard")
    if args.top is not None and args.research_run is None:
        parser.error("--top requires --research-run")
    if args.collect_market_risk and args.moonshot_run is None:
        parser.error("--collect-market-risk requires --moonshot-run")
    if args.moonshot_limit is not None and not args.collect_market_risk:
        parser.error("--moonshot-limit requires --collect-market-risk")
    if args.balanced_research is not None and args.research_run is None:
        parser.error("--balanced-research requires --research-run")
    if args.top is not None and args.balanced_research is not None:
        parser.error("--top and --balanced-research cannot be combined")
    if (
        args.collect_sources or args.source_manifest or args.canadian_source_manifest
    ) and args.research_run is None:
        parser.error("source options require --research-run")
    if args.synthesize and args.research_run is None:
        parser.error("--synthesize requires --research-run")
    if args.synthesize and not (
        args.collect_sources or args.source_manifest or args.canadian_source_manifest
    ):
        parser.error(
            "--synthesize requires --collect-sources, --source-manifest, "
            "or --canadian-source-manifest"
        )
    return args


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def select_universe(
    universe: list[dict],
    tickers: list[str] | None = None,
    limit: int | None = None,
    balanced_sample: int | None = None,
) -> list[dict]:
    """Select a reproducible subset while retaining universe metadata."""
    selections = sum(
        value is not None for value in (tickers, limit, balanced_sample)
    )
    if selections > 1:
        raise ValueError("ticker, limit, and balanced sample options cannot be combined")

    if balanced_sample is not None:
        if (
            isinstance(balanced_sample, bool)
            or not isinstance(balanced_sample, int)
            or balanced_sample < 1
        ):
            raise ValueError("balanced sample must be a positive integer")
        by_country = {
            country: [
                item
                for item in universe
                if str(item.get("country", "")).upper() == country
            ][:balanced_sample]
            for country in ("CA", "US")
        }
        selected = []
        for index in range(max(len(group) for group in by_country.values())):
            for country in ("CA", "US"):
                if index < len(by_country[country]):
                    selected.append(by_country[country][index])
        return selected

    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        return universe[:limit]

    if tickers is None:
        return universe

    lookup = {
        str(item["ticker"]).strip().upper(): item
        for item in universe
    }
    requested = list(dict.fromkeys(
        str(ticker).strip().upper()
        for ticker in tickers
        if str(ticker).strip()
    ))
    missing = [ticker for ticker in requested if ticker not in lookup]

    if missing:
        raise ValueError(
            "Tickers not found in the configured universe: "
            + ", ".join(missing)
        )

    return [lookup[ticker] for ticker in requested]


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error

    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")

    return number
