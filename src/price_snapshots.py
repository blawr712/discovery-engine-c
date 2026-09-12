"""Durable compressed price snapshots sourced from run-time cache artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib


def index_cached_price_snapshots(
    connection: sqlite3.Connection,
    run_id: str,
    manifest: dict,
    results: list[dict],
    cache_directory: Path,
    benchmarks: dict[str, str],
) -> dict:
    """Replace one run's snapshots using only provenance-compatible cache files."""
    completed_at = _timestamp(manifest.get("completed_at"))
    tickers = {
        str(row.get("ticker")): {
            "role": "equity", "country": row.get("country")
        }
        for row in results
        if row.get("status") == "OK" and row.get("ticker")
    }
    for country, ticker in benchmarks.items():
        tickers.setdefault(str(ticker), {"role": "benchmark", "country": country})
    indexed = skipped_missing = skipped_newer = read_errors = 0
    for ticker, metadata in sorted(tickers.items()):
        path = _cache_path(cache_directory, ticker)
        if not path.is_file():
            skipped_missing += 1
            continue
        source_mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if completed_at is not None and source_mtime > completed_at:
            skipped_newer += 1
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            points = _clean_points(payload.get("data"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            read_errors += 1
            continue
        if not points:
            read_errors += 1
            continue
        compressed = zlib.compress(
            json.dumps(points, separators=(",", ":")).encode("utf-8"), level=9
        )
        connection.execute(
            """INSERT INTO price_snapshots (
                run_id, ticker, role, country, captured_at, source_mtime,
                point_count, start_date, end_date, points_zlib
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, ticker) DO UPDATE SET
                role=excluded.role, country=excluded.country,
                captured_at=excluded.captured_at,
                source_mtime=excluded.source_mtime,
                point_count=excluded.point_count,
                start_date=excluded.start_date, end_date=excluded.end_date,
                points_zlib=excluded.points_zlib""",
            (
                run_id, ticker, metadata["role"], metadata["country"],
                datetime.now(timezone.utc).isoformat(), source_mtime.isoformat(),
                len(points), points[0]["date"], points[-1]["date"], compressed,
            ),
        )
        indexed += 1
    return {
        "indexed": indexed,
        "skipped_missing": skipped_missing,
        "skipped_newer_than_run": skipped_newer,
        "read_errors": read_errors,
    }


def load_price_snapshot(
    connection: sqlite3.Connection, run_id: str, ticker: str
) -> dict | None:
    try:
        row = connection.execute(
            """SELECT role, country, captured_at, source_mtime, point_count,
                      start_date, end_date, points_zlib
               FROM price_snapshots WHERE run_id = ? AND ticker = ?""",
            (run_id, ticker),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise
    if row is None:
        return None
    points = json.loads(zlib.decompress(row[7]).decode("utf-8"))
    return {
        "run_id": run_id, "ticker": ticker, "role": row[0], "country": row[1],
        "captured_at": row[2], "source_mtime": row[3], "point_count": row[4],
        "start_date": row[5], "end_date": row[6], "points": points,
    }


def _cache_path(cache_directory: Path, ticker: str) -> Path:
    digest = hashlib.sha256(f"{ticker}|1y".encode("utf-8")).hexdigest()
    return Path(cache_directory) / "prices" / f"{digest}.json"


def _clean_points(rows) -> list[dict]:
    if not isinstance(rows, list):
        return []
    points = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("Date"):
            continue
        close = _number(row.get("Close"))
        if close is None or close <= 0:
            continue
        open_price = _positive_number(row.get("Open"))
        high = _positive_number(row.get("High"))
        low = _positive_number(row.get("Low"))
        points.append({
            "date": str(row["Date"])[:10],
            "open": round(open_price, 6) if open_price is not None else None,
            "high": round(high, 6) if high is not None else None,
            "low": round(low, 6) if low is not None else None,
            "close": round(close, 6),
            "volume": _number(row.get("Volume")),
            "dividend": _number(row.get("Dividends")) or 0.0,
            "split": _number(row.get("Stock Splits")) or 0.0,
        })
    points.sort(key=lambda point: point["date"])
    return points


def _timestamp(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_number(value) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None
