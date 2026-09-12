"""Concurrent orchestration for the Discovery Engine pipeline."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from src.data_sources.base import MarketDataSource
from src.pre_filter import evaluate_stock, filtered_result
from src.scoring import calculate_scores


ProgressCallback = Callable[[str, int, int, str], None]
ResultCallback = Callable[[int, dict], None]


@dataclass(frozen=True)
class Candidate:
    """A company that passed metadata screening and is ready for prices."""

    index: int
    ticker: str
    stock_data: dict
    benchmark_ticker: str


class DiscoveryEngine:
    """Run metadata and price collection with bounded concurrency."""

    def __init__(
        self,
        source: MarketDataSource,
        benchmarks: dict[str, str],
        max_workers: int = 5,
        metadata_workers: int | None = None,
        price_workers: int | None = None,
        progress_callback: ProgressCallback | None = None,
        result_callback: ResultCallback | None = None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise TypeError("max_workers must be an integer.")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1.")

        if metadata_workers is None:
            metadata_workers = max_workers
        if price_workers is None:
            price_workers = max_workers
        for name, value in (
            ("metadata_workers", metadata_workers),
            ("price_workers", price_workers),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer.")
            if value < 1:
                raise ValueError(f"{name} must be at least 1.")

        self.source = source
        self.benchmarks = benchmarks
        self.max_workers = max_workers
        self.metadata_workers = metadata_workers
        self.price_workers = price_workers
        self.progress_callback = progress_callback
        self.result_callback = result_callback

    def run(
        self,
        universe: list[dict],
        prior_results: dict[int, dict] | None = None,
    ) -> list[dict]:
        """Analyze a universe and return rows in the original input order."""
        if not universe:
            return []

        results: list[dict | None] = [None] * len(universe)
        pending_universe = []

        for index, item in enumerate(universe):
            prior_result = (prior_results or {}).get(index)
            if prior_result is not None:
                results[index] = prior_result
            else:
                pending_universe.append((index, item))

        candidates = self._collect_metadata(pending_universe, results)
        benchmark_data, benchmark_errors = self._load_benchmarks(candidates)
        self._collect_prices(
            candidates,
            benchmark_data,
            benchmark_errors,
            results,
        )

        return [
            result
            if result is not None
            else _error_result("UNKNOWN", "Pipeline produced no result")
            for result in results
        ]

    def _collect_metadata(
        self,
        universe: list[tuple[int, dict]],
        results: list[dict | None],
    ) -> list[Candidate]:
        candidates: list[Candidate] = []

        with ThreadPoolExecutor(max_workers=self.metadata_workers) as executor:
            futures = {
                executor.submit(self._prepare_candidate, index, item): (
                    index,
                    str(item.get("ticker", "UNKNOWN")),
                )
                for index, item in universe
            }

            for completed, future in enumerate(as_completed(futures), start=1):
                index, ticker = futures[future]

                try:
                    candidate, result = future.result()
                except Exception as error:
                    candidate = None
                    result = _error_result(ticker, error, "Metadata")

                if candidate is not None:
                    candidates.append(candidate)
                else:
                    self._store_result(results, index, result)

                self._report_progress(
                    "metadata",
                    completed,
                    len(futures),
                    ticker,
                )

        return sorted(candidates, key=lambda candidate: candidate.index)

    def _prepare_candidate(
        self,
        index: int,
        item: dict,
    ) -> tuple[Candidate | None, dict | None]:
        ticker = str(item["ticker"])
        stock_data = self.source.get_stock_data(ticker)
        pre_filter = evaluate_stock(stock_data)
        stock_data = {
            **stock_data,
            "asset_type": pre_filter.asset_type,
        }

        if not pre_filter.passed:
            return None, filtered_result(
                stock_data,
                pre_filter.reason or "Failed pre-filter",
            )

        country = item.get("country") or stock_data.get("country")
        benchmark_ticker = self.benchmarks.get(country, "SPY")

        return Candidate(
            index=index,
            ticker=ticker,
            stock_data=stock_data,
            benchmark_ticker=benchmark_ticker,
        ), None

    def _load_benchmarks(
        self,
        candidates: list[Candidate],
    ) -> tuple[dict[str, pd.DataFrame], dict[str, Exception]]:
        tickers = sorted(
            {candidate.benchmark_ticker for candidate in candidates}
        )
        data: dict[str, pd.DataFrame] = {}
        errors: dict[str, Exception] = {}

        for completed, ticker in enumerate(tickers, start=1):
            try:
                data[ticker] = self.source.get_price_history(ticker)
            except Exception as error:
                errors[ticker] = error

            self._report_progress(
                "benchmarks",
                completed,
                len(tickers),
                ticker,
            )

        return data, errors

    def _collect_prices(
        self,
        candidates: list[Candidate],
        benchmark_data: dict[str, pd.DataFrame],
        benchmark_errors: dict[str, Exception],
        results: list[dict | None],
    ) -> None:
        ready: list[Candidate] = []

        for candidate in candidates:
            error = benchmark_errors.get(candidate.benchmark_ticker)

            if error is not None:
                self._store_result(results, candidate.index, _error_result(
                    candidate.ticker,
                    error,
                    f"Benchmark {candidate.benchmark_ticker}",
                ))
            else:
                ready.append(candidate)

        with ThreadPoolExecutor(max_workers=self.price_workers) as executor:
            futures: dict[Future, Candidate] = {
                executor.submit(
                    self._score_candidate,
                    candidate,
                    benchmark_data[candidate.benchmark_ticker],
                ): candidate
                for candidate in ready
            }

            for completed, future in enumerate(as_completed(futures), start=1):
                candidate = futures[future]

                try:
                    result = future.result()
                except Exception as error:
                    result = _error_result(
                        candidate.ticker,
                        error,
                        "Price history",
                    )

                self._store_result(results, candidate.index, result)

                self._report_progress(
                    "prices",
                    completed,
                    len(futures),
                    candidate.ticker,
                )

    def _score_candidate(
        self,
        candidate: Candidate,
        benchmark_history: pd.DataFrame,
    ) -> dict:
        price_history = self.source.get_price_history(candidate.ticker)
        return calculate_scores(
            candidate.stock_data,
            price_history,
            benchmark_history,
        )

    def _report_progress(
        self,
        phase: str,
        completed: int,
        total: int,
        ticker: str,
    ) -> None:
        if self.progress_callback is not None:
            self.progress_callback(phase, completed, total, ticker)

    def _store_result(
        self,
        results: list[dict | None],
        index: int,
        result: dict | None,
    ) -> None:
        if result is None:
            result = _error_result("UNKNOWN", "Pipeline produced no result")
        results[index] = result
        if self.result_callback is not None:
            self.result_callback(index, result)


def _error_result(
    ticker: str,
    error: Exception | str,
    stage: str | None = None,
) -> dict:
    """Create a stable report row for an isolated pipeline failure."""
    if isinstance(error, Exception):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = error

    reason = f"{stage}: {detail}" if stage else detail

    return {
        "ticker": ticker,
        "status": "ERROR",
        "reason_flags": reason,
        "discovery_score": 0,
    }
