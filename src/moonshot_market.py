"""Market-risk evidence collection and offline cache reuse for Moonshot Discovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
from typing import Callable

import pandas as pd

from src.data_sources.base import MarketDataSource


ProgressCallback = Callable[[str, int, int, str], None]


def build_market_risk_snapshot(
    ticker: str,
    price_history: pd.DataFrame | None,
    share_history: pd.DataFrame | None = None,
    *,
    captured_at: str | None = None,
    source: str = "moonshot_collection",
    errors: list[str] | None = None,
) -> dict:
    """Calculate auditable market-risk metrics from provider data frames."""
    prices = _price_rows(price_history)
    shares = _share_rows(share_history)
    has_split_column = (
        isinstance(price_history, pd.DataFrame)
        and "Stock Splits" in price_history.columns
    )
    average_dollar_volume = _average_dollar_volume(prices)
    volatility = _annualized_volatility(prices)
    drawdown = _maximum_drawdown(prices)
    share_count_change = _share_count_change(shares, prices)
    dilution = (
        max(0.0, share_count_change)
        if share_count_change is not None else None
    )
    reverse_splits = (
        sum(0 < row["split"] < 1 for row in prices)
        if has_split_column else None
    )
    metric_values = (
        average_dollar_volume, volatility, drawdown, dilution, reverse_splits,
    )
    available = sum(value is not None for value in metric_values)
    price_metrics_complete = all(
        value is not None
        for value in (
            average_dollar_volume, volatility, drawdown, reverse_splits,
        )
    )
    status = (
        "complete" if available == len(metric_values)
        else "price_only" if price_metrics_complete
        else "partial" if available
        else "unavailable"
    )
    return {
        "ticker": str(ticker),
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "data_status": status,
        "price_start_date": prices[0]["date"] if prices else None,
        "price_end_date": prices[-1]["date"] if prices else None,
        "price_observations": len(prices),
        "share_start_date": shares[0]["date"] if shares else None,
        "share_end_date": shares[-1]["date"] if shares else None,
        "share_observations": len(shares),
        "average_dollar_volume_30d": average_dollar_volume,
        "annualized_volatility_percent": volatility,
        "maximum_drawdown_percent": drawdown,
        "share_count_change_percent": share_count_change,
        "dilution_percent": dilution,
        "reverse_split_count_1y": reverse_splits,
        "errors": list(errors or []),
    }


def collect_market_risk_evidence(
    candidates: list[dict],
    source: MarketDataSource,
    *,
    max_workers: int = 5,
    progress_callback: ProgressCallback | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[dict[str, dict], dict]:
    """Collect price and share history with per-company failure isolation."""
    if max_workers < 1:
        raise ValueError("Moonshot market-data workers must be at least 1.")
    clock = clock or (lambda: datetime.now(timezone.utc))
    captured_at = _utc_iso(clock())
    evidence = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _collect_one, str(candidate["ticker"]), source, captured_at,
            ): str(candidate["ticker"])
            for candidate in candidates
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            evidence[ticker] = future.result()
            if progress_callback is not None:
                progress_callback(
                    "moonshot-market", completed, len(futures), ticker,
                )
    ordered = {ticker: evidence[ticker] for ticker in sorted(evidence)}
    return ordered, _collection_summary(ordered)


def load_run_compatible_price_evidence(
    cache_directory: Path,
    candidates: list[dict],
    completed_at: str,
) -> tuple[dict[str, dict], dict]:
    """Derive market metrics from price cache files no newer than the source run."""
    cutoff = _timestamp(completed_at)
    evidence = {}
    stats = {"loaded": 0, "missing": 0, "newer_than_run": 0, "read_errors": 0}
    for candidate in candidates:
        ticker = str(candidate["ticker"])
        path = _cache_path(cache_directory, "prices", f"{ticker}|1y")
        if not path.is_file():
            stats["missing"] += 1
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified > cutoff:
            stats["newer_than_run"] += 1
            continue
        try:
            history = pd.read_json(path, orient="table")
            snapshot = build_market_risk_snapshot(
                ticker,
                history,
                captured_at=modified.isoformat(),
                source="run_compatible_price_cache",
            )
        except (OSError, ValueError, TypeError, KeyError):
            stats["read_errors"] += 1
            continue
        if snapshot["data_status"] == "unavailable":
            stats["read_errors"] += 1
            continue
        evidence[ticker] = snapshot
        stats["loaded"] += 1
    return evidence, stats


def load_market_risk_evidence(
    output_directory: Path,
    run_id: str,
) -> dict[str, dict]:
    """Load a previously exported Moonshot market-evidence artifact."""
    path = Path(output_directory) / f"moonshot_market_evidence_{run_id}.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("run_id") != run_id:
        raise ValueError("Moonshot market-evidence artifact has invalid provenance.")
    if payload.get("schema_version") != 2:
        return {}
    if not isinstance(payload.get("evidence"), dict):
        raise ValueError("Moonshot market-evidence artifact has invalid evidence.")
    return payload["evidence"]


def export_market_risk_evidence(
    evidence: dict[str, dict],
    output_directory: Path,
    run_id: str,
    source_completed_at: str,
) -> Path:
    """Atomically persist reusable Moonshot market evidence."""
    path = Path(output_directory) / f"moonshot_market_evidence_{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "source_completed_at": source_completed_at,
        "evidence_count": len(evidence),
        "summary": _collection_summary(evidence),
        "evidence": {ticker: evidence[ticker] for ticker in sorted(evidence)},
    }
    _atomic_json(path, payload)
    return path


def _collect_one(ticker, source, captured_at):
    errors = []
    try:
        prices = source.get_price_history(ticker, "1y")
    except Exception as error:
        prices = None
        errors.append(f"price_history: {type(error).__name__}: {error}")
    try:
        shares = source.get_share_history(ticker, "18mo")
    except Exception as error:
        shares = None
        errors.append(f"share_history: {type(error).__name__}: {error}")
    return build_market_risk_snapshot(
        ticker,
        prices,
        shares,
        captured_at=captured_at,
        errors=errors,
    )


def _price_rows(history):
    if not isinstance(history, pd.DataFrame) or history.empty:
        return []
    rows = []
    for record in history.to_dict("records"):
        close = _positive_number(record.get("Close"))
        day = _date_text(record.get("Date"))
        if close is None or day is None:
            continue
        rows.append({
            "date": day,
            "close": close,
            "volume": _nonnegative_number(record.get("Volume")),
            "split": _nonnegative_number(record.get("Stock Splits")) or 0.0,
        })
    rows.sort(key=lambda row: row["date"])
    return rows


def _share_rows(history):
    if not isinstance(history, pd.DataFrame) or history.empty:
        return []
    rows = []
    for record in history.to_dict("records"):
        shares = _positive_number(record.get("Shares"))
        day = _date_text(record.get("Date"))
        if shares is not None and day is not None:
            rows.append({"date": day, "shares": shares})
    rows.sort(key=lambda row: row["date"])
    return rows


def _average_dollar_volume(rows):
    recent = [
        row["close"] * row["volume"] for row in rows[-30:]
        if row["volume"] is not None
    ]
    if len(recent) < 20:
        return None
    return round(sum(recent) / len(recent), 2)


def _annualized_volatility(rows):
    closes = [row["close"] for row in rows]
    if len(closes) < 60:
        return None
    returns = [current / previous - 1 for previous, current in zip(closes, closes[1:])]
    if len(returns) < 2:
        return None
    return round(statistics.stdev(returns) * math.sqrt(252) * 100, 2)


def _maximum_drawdown(rows):
    closes = [row["close"] for row in rows]
    if len(closes) < 60:
        return None
    peak = closes[0]
    maximum = 0.0
    for close in closes:
        peak = max(peak, close)
        maximum = max(maximum, (peak - close) / peak)
    return round(maximum * 100, 2)


def _share_count_change(shares, prices):
    if len(shares) < 2 or not prices:
        return None
    overlapping = [
        row for row in shares
        if prices[0]["date"] <= row["date"] <= prices[-1]["date"]
    ]
    if len(overlapping) < 2:
        return None
    start = datetime.fromisoformat(overlapping[0]["date"])
    end = datetime.fromisoformat(overlapping[-1]["date"])
    if (end - start).days < 180:
        return None
    split_multiplier = math.prod(
        row["split"] for row in prices
        if overlapping[0]["date"] < row["date"] <= overlapping[-1]["date"]
        and row["split"] > 0
    )
    adjusted_start = overlapping[0]["shares"] * split_multiplier
    if adjusted_start <= 0:
        return None
    return round(
        (overlapping[-1]["shares"] / adjusted_start - 1) * 100,
        2,
    )


def _collection_summary(evidence):
    rows = list(evidence.values())
    return {
        "evidence_count": len(rows),
        "complete": sum(row.get("data_status") == "complete" for row in rows),
        "price_only": sum(row.get("data_status") == "price_only" for row in rows),
        "partial": sum(row.get("data_status") == "partial" for row in rows),
        "unavailable": sum(row.get("data_status") == "unavailable" for row in rows),
        "with_errors": sum(bool(row.get("errors")) for row in rows),
    }


def _cache_path(cache_directory, category, key):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(cache_directory) / category / f"{digest}.json"


def _timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utc_iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _date_text(value):
    if value is None:
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _positive_number(value):
    number = _number(value)
    return number if number is not None and number > 0 else None


def _nonnegative_number(value):
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _atomic_json(path, payload):
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
