"""Insider histories that are current: the quarterly data sets plus recent Form 4s.

The quarterly data sets end at the last quarter the SEC has published.
Filings made since are read from each company's own Form 4s, in the same row
shape, and appended. A filing the data sets already hold is never added
twice. A company whose recent filings cannot be read keeps its data-set
history, so a failure costs freshness rather than the whole signal.

Fresh filings are fetched only for names that can be research candidates:
insider signals reach the research queue only through Score v3, which ranks
tradeable names, so a name below the liquidity floor gets its data-set
history alone.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import threading
from typing import Callable

from src.insider_signals import InsiderHistory


class SupplementedInsiderLookup:
    """Ticker -> insider history, from data-set rows plus recent Form 4s."""

    def __init__(
        self,
        transactions: list[dict],
        data_through: date | None,
        cik_for: Callable[[str], str | None],
        form4_source=None,
        config: dict | None = None,
    ) -> None:
        self.data_through = data_through
        self.cik_for = cik_for
        self.form4_source = form4_source
        self.config = config
        self.rows_by_cik: dict[str, list[dict]] = {}
        for row in transactions:
            cik = str(row.get("issuer_cik") or "").strip()
            if cik:
                self.rows_by_cik.setdefault(cik.zfill(10), []).append(row)
        self.stats: Counter = Counter()
        self.fresh_through: date | None = None
        self._lock = threading.Lock()

    def __call__(self, ticker: str) -> InsiderHistory | None:
        return self._history(ticker, fresh=True)

    def prefetch(self, tickers: list[str], workers: int = 6, progress=None) -> None:
        """Warm the filing cache for many companies at once.

        Callers that score rows one by one would otherwise wait on each
        filing in turn; a few workers sharing the SEC pacer fetch the same
        filings several times faster without exceeding its request rate.
        """
        if self.form4_source is None or not tickers:
            return
        from concurrent.futures import ThreadPoolExecutor

        ciks = sorted({str(cik).zfill(10) for cik in map(self.cik_for, tickers) if cik})

        def warm(cik: str) -> str:
            try:
                self.form4_source.transactions(cik, filed_after=self.data_through)
            except Exception:  # noqa: BLE001 - the lookup itself will fall back
                pass
            return cik

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, cik in enumerate(pool.map(warm, ciks), start=1):
                if progress is not None:
                    progress("form4-prefetch", index, len(ciks), cik)

    def investment_fund(self, ticker: str) -> str | None:
        """The fund type EDGAR's filing record implies, or None for a company.

        Read from the same submissions listing the recent Form 4s come from,
        so it costs no extra request. None also when the listing cannot be
        read: an unknown is never labelled a fund.
        """
        if self.form4_source is None:
            return None
        cik = self.cik_for(ticker)
        if cik is None:
            return None
        from src.data_sources.sec_form4_source import investment_fund_type

        try:
            listing = self.form4_source.ownership_filings(str(cik).zfill(10))
        except Exception:  # noqa: BLE001 - unknown means not labelled
            return None
        return investment_fund_type(listing)

    def bulk_only(self, ticker: str) -> InsiderHistory | None:
        """The data-set history alone, without fetching recent filings."""
        return self._history(ticker, fresh=False)

    def _history(self, ticker: str, fresh: bool) -> InsiderHistory | None:
        cik = self.cik_for(ticker)
        if cik is None:
            return None
        cik = str(cik).zfill(10)
        rows = list(self.rows_by_cik.get(cik, []))
        through = self.data_through
        if fresh and self.form4_source is not None:
            try:
                recent, covered, incomplete = self.form4_source.transactions(
                    cik, filed_after=self.data_through,
                )
            except Exception:  # noqa: BLE001 - keep the data-set history
                self._count("fresh_failed")
            else:
                known = {row.get("accession") for row in rows}
                added = [row for row in recent if row.get("accession") not in known]
                rows.extend(added)
                through = covered
                self._count("fresh")
                self._count("fresh_transactions", len(added))
                if incomplete:
                    self._count("fresh_incomplete")
                with self._lock:
                    if self.fresh_through is None or covered > self.fresh_through:
                        self.fresh_through = covered
        elif not fresh:
            self._count("data_set_only")
        if not rows:
            return None
        return InsiderHistory(rows, self.config, data_through=through)

    def _count(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.stats[name] += amount
