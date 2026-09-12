"""Offline SQLite history index for completed Discovery Engine runs."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from src.run_state import load_saved_run
from src.price_snapshots import index_cached_price_snapshots


SCHEMA_VERSION = 2


def connect_history_read_only(database_path: Path) -> sqlite3.Connection:
    """Open an existing history database with SQLite write access disabled."""
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"History database not found: {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def index_saved_run(
    database_path: Path,
    run_directory: Path,
    run_id: str,
    price_cache_directory: Path | None = None,
    benchmarks: dict[str, str] | None = None,
) -> dict:
    """Idempotently index one completed file-backed run."""
    manifest, results = load_saved_run(run_directory, run_id)
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    indexed_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(database_path)) as connection, connection:
        _initialize(connection)
        connection.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
        connection.execute(
            """INSERT INTO runs (
                run_id, fingerprint, status, started_at, completed_at,
                duration_seconds, universe_size, completed_count, indexed_at,
                manifest_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                fingerprint=excluded.fingerprint, status=excluded.status,
                started_at=excluded.started_at, completed_at=excluded.completed_at,
                duration_seconds=excluded.duration_seconds,
                universe_size=excluded.universe_size,
                completed_count=excluded.completed_count,
                indexed_at=excluded.indexed_at,
                manifest_json=excluded.manifest_json""",
            (
                run_id, manifest.get("fingerprint"), manifest.get("status"),
                manifest.get("started_at"), manifest.get("completed_at"),
                manifest.get("duration_seconds"), manifest.get("universe_size"),
                manifest.get("completed_count"), indexed_at,
                json.dumps(manifest, sort_keys=True),
            ),
        )
        connection.executemany(
            """INSERT INTO results (
                run_id, position, ticker, status, country, exchange,
                discovery_score, market_cap, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id, position, str(row.get("ticker", "")),
                    str(row.get("status", "UNKNOWN")), row.get("country"),
                    row.get("exchange"), _number(row.get("discovery_score")),
                    _number(row.get("market_cap")),
                    json.dumps(row, sort_keys=True, default=str),
                )
                for position, row in enumerate(results)
            ],
        )
        price_snapshots = (
            index_cached_price_snapshots(
                connection, run_id, manifest, results,
                price_cache_directory, benchmarks or {},
            )
            if price_cache_directory is not None
            else {"indexed": 0, "skipped_missing": 0,
                  "skipped_newer_than_run": 0, "read_errors": 0}
        )
    return {
        "run_id": run_id,
        "indexed_results": len(results),
        "database_path": str(database_path),
        "indexed_at": indexed_at,
        "price_snapshots": price_snapshots,
    }


def history_summary(database_path: Path) -> dict:
    """Return a compact summary without initializing external providers."""
    path = Path(database_path)
    if not path.is_file():
        return {"run_count": 0, "result_count": 0, "latest_run": None}
    with closing(connect_history_read_only(path)) as connection:
        run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        result_count = connection.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        latest = connection.execute(
            """SELECT run_id, completed_at, universe_size, completed_count
               FROM runs ORDER BY completed_at DESC, run_id DESC LIMIT 1"""
        ).fetchone()
    return {
        "run_count": run_count,
        "result_count": result_count,
        "latest_run": None if latest is None else {
            "run_id": latest[0], "completed_at": latest[1],
            "universe_size": latest[2], "completed_count": latest[3],
        },
    }


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT NOT NULL,
            started_at TEXT, completed_at TEXT, duration_seconds REAL,
            universe_size INTEGER, completed_count INTEGER, indexed_at TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS results (
            run_id TEXT NOT NULL, position INTEGER NOT NULL, ticker TEXT NOT NULL,
            status TEXT NOT NULL, country TEXT, exchange TEXT,
            discovery_score REAL, market_cap REAL, result_json TEXT NOT NULL,
            PRIMARY KEY (run_id, position),
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_results_ticker ON results(ticker);
        CREATE INDEX IF NOT EXISTS idx_results_status ON results(status);
        CREATE TABLE IF NOT EXISTS price_snapshots (
            run_id TEXT NOT NULL, ticker TEXT NOT NULL, role TEXT NOT NULL,
            country TEXT, captured_at TEXT NOT NULL, source_mtime TEXT NOT NULL,
            point_count INTEGER NOT NULL, start_date TEXT NOT NULL,
            end_date TEXT NOT NULL, points_zlib BLOB NOT NULL,
            PRIMARY KEY (run_id, ticker),
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_price_snapshots_ticker
            ON price_snapshots(ticker);
    """)
    connection.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
