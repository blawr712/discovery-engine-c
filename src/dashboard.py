"""Local dashboard for read-only history and explicit watchlist workflows."""

from __future__ import annotations

from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlparse

from src.history import connect_history_read_only
from src.history_comparison import _candidate_ranks, _compare_ticker, _load_run
from src.history_reporting import build_ticker_history, build_weekly_report
from src.moonshot_dashboard import MoonshotDashboardStore
from src.score_explainability import (
    SCORE_GLOSSARY,
    explain_candidate,
    percentile_descriptor,
    score_percentiles,
)
from src.config import BENCHMARKS
from src.price_performance import build_price_performance
from src.price_snapshots import load_price_snapshot
from src.watchlists import WatchlistStore


DASHBOARD_HTML = Path(__file__).with_name("dashboard_assets") / "index.html"


class DashboardStore:
    """Bounded dashboard queries with isolated watchlist persistence."""

    def __init__(
        self,
        database_path: Path,
        watchlist_path: Path | None = None,
        moonshot_directory: Path | None = None,
    ):
        self.database_path = Path(database_path)
        self.watchlist_store = WatchlistStore(
            watchlist_path or self.database_path.with_name("watchlists.json")
        )
        self.moonshot_store = MoonshotDashboardStore(
            moonshot_directory
            or self.database_path.parent.parent / "exports"
        )

    def overview(self) -> dict:
        with closing(connect_history_read_only(self.database_path)) as connection:
            runs = connection.execute(
                """SELECT run_id, completed_at, universe_size, completed_count,
                          fingerprint
                   FROM runs ORDER BY completed_at DESC, run_id DESC"""
            ).fetchall()
            result_count = connection.execute(
                "SELECT COUNT(*) FROM results"
            ).fetchone()[0]
            latest_run_id = runs[0][0] if runs else None
            latest_candidates = connection.execute(
                "SELECT COUNT(*) FROM results WHERE run_id = ? AND status = 'OK'",
                (latest_run_id,),
            ).fetchone()[0] if latest_run_id else 0
            try:
                latest_price_snapshots = connection.execute(
                    """SELECT COUNT(*) FROM price_snapshots
                       WHERE run_id = ? AND role = 'equity'""",
                    (latest_run_id,),
                ).fetchone()[0] if latest_run_id else 0
            except sqlite3.OperationalError as error:
                if "no such table" not in str(error).lower():
                    raise
                latest_price_snapshots = 0
        return {
            "run_count": len(runs),
            "result_count": result_count,
            "latest_run_id": runs[0][0] if runs else None,
            "latest_price_snapshot_count": latest_price_snapshots,
            "latest_candidate_count": latest_candidates,
            "latest_price_coverage_percent": round(
                latest_price_snapshots / latest_candidates * 100, 2
            ) if latest_candidates else 0,
            "runs": [
                {
                    "run_id": row[0], "completed_at": row[1],
                    "universe_size": row[2], "completed_count": row[3],
                    "fingerprint": row[4],
                }
                for row in runs
            ],
        }

    def candidates(
        self,
        run_id: str | None = None,
        status: str = "OK",
        country: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> dict:
        limit = max(1, min(int(limit), 200))
        with closing(connect_history_read_only(self.database_path)) as connection:
            if not run_id:
                latest = connection.execute(
                    "SELECT run_id FROM runs ORDER BY completed_at DESC, run_id DESC LIMIT 1"
                ).fetchone()
                if latest is None:
                    return {"run_id": None, "total": 0, "rows": []}
                run_id = latest[0]
            exists = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"Run is not indexed: {run_id}")
            payloads = connection.execute(
                "SELECT result_json FROM results WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
        all_rows = [json.loads(payload) for payload, in payloads]
        ranks = _candidate_ranks({str(row.get("ticker")): row for row in all_rows})
        discovery_percentiles = score_percentiles(all_rows, "discovery_score")
        normalized_status = status.strip().upper() if status else ""
        normalized_country = country.strip().upper() if country else ""
        needle = search.strip().upper() if search else ""
        filtered = []
        for row in all_rows:
            ticker = str(row.get("ticker", ""))
            company = str(row.get("company_name", ""))
            if normalized_status and str(row.get("status", "")).upper() != normalized_status:
                continue
            if normalized_country and str(row.get("country", "")).upper() != normalized_country:
                continue
            if needle and needle not in ticker.upper() and needle not in company.upper():
                continue
            filtered.append({
                "ticker": ticker,
                "company_name": company or None,
                "status": row.get("status"),
                "rank": ranks.get(ticker),
                "discovery_percentile": discovery_percentiles.get(ticker),
                "discovery_descriptor": percentile_descriptor(
                    discovery_percentiles.get(ticker)
                ),
                "discovery_score": row.get("discovery_score"),
                "score_confidence": row.get("score_confidence"),
                "fundamental_confidence": row.get("fundamental_confidence"),
                "fundamental_data_quality": row.get("fundamental_data_quality"),
                "country": row.get("country"),
                "sector": row.get("sector"),
                "exchange": row.get("exchange"),
            })
        filtered.sort(key=lambda row: (
            row["rank"] is None,
            row["rank"] if row["rank"] is not None else 10**9,
            row["ticker"],
        ))
        return {"run_id": run_id, "total": len(filtered), "rows": filtered[:limit]}

    def ticker_history(self, ticker: str) -> dict:
        return build_ticker_history(self.database_path, ticker)

    def candidate_detail(self, ticker: str, run_id: str | None = None) -> dict:
        ticker = str(ticker).strip().upper()
        with closing(connect_history_read_only(self.database_path)) as connection:
            if not run_id:
                latest = connection.execute(
                    "SELECT run_id FROM runs ORDER BY completed_at DESC, run_id DESC LIMIT 1"
                ).fetchone()
                if latest is None:
                    raise ValueError("No indexed runs are available")
                run_id = latest[0]
            payloads = connection.execute(
                "SELECT result_json FROM results WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
        rows = [json.loads(payload) for payload, in payloads]
        lookup = {str(row.get("ticker", "")).upper(): row for row in rows}
        row = lookup.get(ticker)
        if row is None:
            raise ValueError(f"Ticker is not present in indexed run {run_id}: {ticker}")
        ranks = _candidate_ranks({str(item.get("ticker")): item for item in rows})
        discovery = score_percentiles(rows, "discovery_score")
        fundamental = score_percentiles(rows, "fundamental_score_normalized")
        with closing(connect_history_read_only(self.database_path)) as connection:
            equity_snapshot = load_price_snapshot(connection, run_id, ticker)
            benchmark_ticker = BENCHMARKS.get(str(row.get("country") or ""))
            benchmark_snapshot = (
                load_price_snapshot(connection, run_id, benchmark_ticker)
                if benchmark_ticker else None
            )
        performance = build_price_performance(equity_snapshot, benchmark_snapshot)
        if performance is not None:
            performance["currency"] = row.get("currency")
        return {
            "run_id": run_id,
            "candidate": explain_candidate(
                row,
                ranks.get(str(row.get("ticker"))),
                discovery.get(str(row.get("ticker"))),
                fundamental.get(str(row.get("ticker"))),
                candidate_count=len(ranks),
                discovery_median=_score_median(rows, "discovery_score"),
            ),
            "history": self.ticker_history(ticker),
            "glossary": SCORE_GLOSSARY,
            "price_performance": performance,
        }

    def weekly_report(self) -> dict:
        report = build_weekly_report(self.database_path)
        report = dict(report)
        report.pop("rows", None)
        return report

    def compare_candidates(self, tickers: list[str], run_id: str | None = None) -> dict:
        normalized = list(dict.fromkeys(
            str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()
        ))
        if not 2 <= len(normalized) <= 5:
            raise ValueError("Candidate comparison requires 2 to 5 unique tickers")
        with closing(connect_history_read_only(self.database_path)) as connection:
            if not run_id:
                latest = connection.execute(
                    "SELECT run_id FROM runs ORDER BY completed_at DESC, run_id DESC LIMIT 1"
                ).fetchone()
                if latest is None:
                    raise ValueError("No indexed runs are available")
                run_id = latest[0]
            payloads = connection.execute(
                "SELECT result_json FROM results WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
            rows = [json.loads(payload) for payload, in payloads]
            lookup = {str(row.get("ticker", "")).upper(): row for row in rows}
            missing = [ticker for ticker in normalized if ticker not in lookup]
            if missing:
                raise ValueError(
                    f"Tickers are not present in indexed run {run_id}: "
                    + ", ".join(missing)
                )
            ranks = _candidate_ranks({str(row.get("ticker")): row for row in rows})
            discovery = score_percentiles(rows, "discovery_score")
            fundamental = score_percentiles(rows, "fundamental_score_normalized")
            discovery_median = _score_median(rows, "discovery_score")
            details = []
            for ticker in normalized:
                row = lookup[ticker]
                candidate = explain_candidate(
                    row, ranks.get(str(row.get("ticker"))),
                    discovery.get(str(row.get("ticker"))),
                    fundamental.get(str(row.get("ticker"))),
                    candidate_count=len(ranks),
                    discovery_median=discovery_median,
                )
                equity = load_price_snapshot(connection, run_id, ticker)
                benchmark_ticker = BENCHMARKS.get(str(row.get("country") or ""))
                benchmark = (
                    load_price_snapshot(connection, run_id, benchmark_ticker)
                    if benchmark_ticker else None
                )
                performance = build_price_performance(equity, benchmark)
                if performance is not None:
                    performance["currency"] = row.get("currency")
                details.append({"candidate": candidate, "performance": performance})
        candidates = []
        for detail in details:
            candidate = detail["candidate"]
            performance = detail["performance"]
            candidates.append({
                "ticker": candidate["ticker"],
                "company_name": candidate["company_name"],
                "status": candidate["status"],
                "rank": candidate["rank"],
                "discovery_score": candidate["scores"]["discovery"],
                "fundamental_score": candidate["scores"]["fundamental"],
                "technical_confidence": candidate["confidence"]["technical"],
                "fundamental_confidence": candidate["confidence"]["fundamental"],
                "technical_factors": _factor_strengths(
                    candidate["technical_factors"]
                ),
                "fundamental_factors": _factor_strengths(
                    candidate["fundamental_factors"]
                ),
                "period_returns": (
                    performance["period_returns"] if performance else {}
                ),
                "price_quality": (
                    performance["data_quality"] if performance else "unavailable"
                ),
                "price_series": (
                    performance["series"]["price"] if performance else []
                ),
                "currency": performance.get("currency") if performance else None,
            })
        return {
            "run_id": run_id,
            "candidate_count": len(candidates),
            "tickers": normalized,
            "candidates": candidates,
            "interpretation_warning": (
                "Comparison fields describe historical signals and data coverage; "
                "they are not recommendations or forecasts."
            ),
        }

    def watchlists(self, run_id: str | None = None) -> dict:
        """Return named watchlists enriched with a two-run change snapshot."""
        watchlists = self.watchlist_store.list_all()
        with closing(connect_history_read_only(self.database_path)) as connection:
            runs = connection.execute(
                """SELECT run_id, fingerprint, universe_size FROM runs
                   ORDER BY completed_at DESC, run_id DESC"""
            ).fetchall()
            run_ids = [row[0] for row in runs]
            if not run_ids:
                return {
                    "selected_run_id": None, "previous_run_id": None,
                    "comparison_warnings": [], "watchlists": watchlists,
                }
            selected_run_id = run_id or run_ids[0]
            if selected_run_id not in run_ids:
                raise ValueError(f"Run is not indexed: {selected_run_id}")
            selected_index = run_ids.index(selected_run_id)
            previous_run_id = (
                run_ids[selected_index + 1]
                if selected_index + 1 < len(run_ids) else None
            )
            comparison_warnings = _watchlist_warnings(
                runs[selected_index],
                runs[selected_index + 1]
                if selected_index + 1 < len(runs) else None,
            )
            if not any(watchlist["items"] for watchlist in watchlists):
                return {
                    "selected_run_id": selected_run_id,
                    "previous_run_id": previous_run_id,
                    "comparison_warnings": comparison_warnings,
                    "watchlists": watchlists,
                }
            selected_raw = _load_run(connection, selected_run_id)
            previous_raw = (
                _load_run(connection, previous_run_id) if previous_run_id else {}
            )
        selected = {ticker.upper(): row for ticker, row in selected_raw.items()}
        previous = {ticker.upper(): row for ticker, row in previous_raw.items()}
        selected_ranks = {
            ticker.upper(): rank
            for ticker, rank in _candidate_ranks(selected_raw).items()
        }
        previous_ranks = {
            ticker.upper(): rank
            for ticker, rank in _candidate_ranks(previous_raw).items()
        }
        enriched = []
        for watchlist in watchlists:
            output = {key: value for key, value in watchlist.items() if key != "items"}
            output["items"] = [
                _watchlist_item(
                    item, previous.get(item["ticker"]), selected.get(item["ticker"]),
                    previous_ranks, selected_ranks,
                )
                for item in watchlist["items"]
            ]
            enriched.append(output)
        return {
            "selected_run_id": selected_run_id,
            "previous_run_id": previous_run_id,
            "comparison_warnings": comparison_warnings,
            "watchlists": enriched,
        }

    def create_watchlist(self, name: str) -> dict:
        return self.watchlist_store.create(name)

    def delete_watchlist(self, watchlist_id: str) -> None:
        self.watchlist_store.delete(watchlist_id)

    def add_to_watchlist(self, watchlist_id: str, ticker: str) -> dict:
        normalized = str(ticker or "").strip().upper()
        with closing(connect_history_read_only(self.database_path)) as connection:
            exists = connection.execute(
                "SELECT 1 FROM results WHERE ticker = ? COLLATE NOCASE LIMIT 1",
                (normalized,),
            ).fetchone()
        if exists is None:
            raise ValueError(f"Ticker is not present in indexed history: {normalized}")
        return self.watchlist_store.add_item(watchlist_id, normalized)

    def remove_from_watchlist(self, watchlist_id: str, ticker: str) -> None:
        self.watchlist_store.remove_item(watchlist_id, ticker)

    def moonshot_candidates(self, run_id: str, **filters) -> dict:
        """Return a bounded read-only Moonshot terminal view."""
        return self.moonshot_store.candidates(run_id, **filters)

    def moonshot_candidate(self, run_id: str, ticker: str) -> dict:
        """Return one candidate's Moonshot factors and scenario sensitivity."""
        return self.moonshot_store.candidate(run_id, ticker)


def create_dashboard_server(
    database_path: Path,
    port: int = 8765,
    host: str = "127.0.0.1",
    watchlist_path: Path | None = None,
    moonshot_directory: Path | None = None,
) -> ThreadingHTTPServer:
    """Create a loopback-only dashboard server without starting its loop."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    store = DashboardStore(database_path, watchlist_path, moonshot_directory)
    store.overview()
    html = DASHBOARD_HTML.read_bytes()

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path in ("/", "/index.html"):
                    self._send(200, html, "text/html; charset=utf-8")
                elif parsed.path == "/api/overview":
                    self._json(200, store.overview())
                elif parsed.path == "/api/candidates":
                    self._json(200, store.candidates(
                        run_id=_one(query, "run_id"),
                        status=_one(query, "status", "OK"),
                        country=_one(query, "country"),
                        search=_one(query, "search"),
                        limit=int(_one(query, "limit", "100")),
                    ))
                elif parsed.path.startswith("/api/ticker/"):
                    ticker = unquote(parsed.path.removeprefix("/api/ticker/"))
                    self._json(200, store.ticker_history(ticker))
                elif parsed.path.startswith("/api/candidate/"):
                    ticker = unquote(parsed.path.removeprefix("/api/candidate/"))
                    self._json(200, store.candidate_detail(
                        ticker, run_id=_one(query, "run_id")
                    ))
                elif parsed.path == "/api/weekly":
                    self._json(200, store.weekly_report())
                elif parsed.path == "/api/compare":
                    tickers = (_one(query, "tickers", "")).split(",")
                    self._json(200, store.compare_candidates(
                        tickers, run_id=_one(query, "run_id")
                    ))
                elif parsed.path == "/api/watchlists":
                    self._json(200, store.watchlists(
                        run_id=_one(query, "run_id")
                    ))
                elif parsed.path == "/api/moonshot":
                    self._json(200, store.moonshot_candidates(
                        run_id=_one(query, "run_id"),
                        classification=_one(query, "classification", "selected"),
                        country=_one(query, "country"),
                        size_tier=_one(query, "size_tier"),
                        search=_one(query, "search"),
                        limit=int(_one(query, "limit", "100")),
                    ))
                elif parsed.path.startswith("/api/moonshot/candidate/"):
                    ticker = unquote(
                        parsed.path.removeprefix("/api/moonshot/candidate/")
                    )
                    self._json(200, store.moonshot_candidate(
                        run_id=_one(query, "run_id"), ticker=ticker,
                    ))
                else:
                    self._json(404, {"error": "not_found"})
            except (FileNotFoundError, OSError, ValueError) as error:
                self._json(400, {"error": str(error)})
            except Exception:
                self._json(500, {"error": "internal_server_error"})

        def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            try:
                path = unquote(urlparse(self.path).path)
                payload = self._body()
                parts = [part for part in path.split("/") if part]
                if parts == ["api", "watchlists"]:
                    self._json(201, store.create_watchlist(payload.get("name")))
                elif len(parts) == 4 and parts[:2] == ["api", "watchlists"] and parts[3] == "items":
                    self._json(201, store.add_to_watchlist(
                        parts[2], payload.get("ticker")
                    ))
                else:
                    self._json(404, {"error": "not_found"})
            except (FileNotFoundError, OSError, ValueError) as error:
                self._json(400, {"error": str(error)})
            except Exception:
                self._json(500, {"error": "internal_server_error"})

        def do_DELETE(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            try:
                parts = [
                    part for part in unquote(urlparse(self.path).path).split("/")
                    if part
                ]
                if len(parts) == 3 and parts[:2] == ["api", "watchlists"]:
                    store.delete_watchlist(parts[2])
                elif len(parts) == 5 and parts[:2] == ["api", "watchlists"] and parts[3] == "items":
                    store.remove_from_watchlist(parts[2], parts[4])
                else:
                    self._json(404, {"error": "not_found"})
                    return
                self._json(200, {"status": "deleted"})
            except (FileNotFoundError, OSError, ValueError) as error:
                self._json(400, {"error": str(error)})
            except Exception:
                self._json(500, {"error": "internal_server_error"})

        def _json(self, status, payload):
            self._send(
                status,
                json.dumps(payload, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid request body length") from error
            if not 1 <= length <= 16_384:
                raise ValueError("JSON request body must contain 1 to 16384 bytes")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Request body must be valid JSON") from error
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            return payload

        def _send(self, status, payload, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    return ThreadingHTTPServer((host, port), DashboardHandler)


def run_dashboard(
    database_path: Path,
    port: int = 8765,
    moonshot_directory: Path | None = None,
) -> None:
    """Serve the dashboard on loopback until interrupted."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    server = create_dashboard_server(
        database_path, port=port, moonshot_directory=moonshot_directory,
    )
    print(f"Discovery Engine dashboard: http://127.0.0.1:{port}")
    print("Indexed history is read-only; watchlist edits stay local. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


def _one(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default


def _factor_strengths(factors: list[dict]) -> dict[str, dict]:
    return {
        factor["name"]: {
            "label": factor["label"],
            "points": factor["points"],
            "max_points": factor["max_points"],
            "percent": round(
                factor["points"] / factor["max_points"] * 100, 2
            ) if factor["points"] is not None and factor["max_points"] else None,
            "data_quality": factor["data_quality"],
        }
        for factor in factors
        if factor.get("applicable")
    }


def _watchlist_item(item, previous, selected, previous_ranks, selected_ranks):
    ticker = item["ticker"]
    if previous is None and selected is None:
        comparison = {
            "new_status": None, "old_status": None,
            "new_rank": None, "old_rank": None, "rank_change": None,
            "new_discovery_score": None, "old_discovery_score": None,
            "score_change": None, "presence": "absent",
            "change_types": ["absent"],
        }
    else:
        comparison = _compare_ticker(
            ticker, previous, selected, previous_ranks, selected_ranks
        )
    current = selected or {}
    prior = previous or {}
    return {
        **item,
        "company_name": current.get("company_name") or prior.get("company_name"),
        "country": current.get("country") or prior.get("country"),
        "current_status": comparison["new_status"],
        "previous_status": comparison["old_status"],
        "current_rank": comparison["new_rank"],
        "previous_rank": comparison["old_rank"],
        "rank_change": comparison["rank_change"],
        "current_score": comparison["new_discovery_score"],
        "previous_score": comparison["old_discovery_score"],
        "score_change": comparison["score_change"],
        "presence": comparison["presence"],
        "change_types": comparison["change_types"],
    }


def _watchlist_warnings(selected_run, previous_run):
    if previous_run is None:
        return []
    warnings = []
    if selected_run[1] != previous_run[1]:
        warnings.append(
            "Run fingerprints differ; changes may include configuration effects."
        )
    if selected_run[2] != previous_run[2]:
        warnings.append(
            "Universe sizes differ; presence and rank changes may not be directly comparable."
        )
    return warnings


def _score_median(rows, field):
    values = sorted(
        float(row[field]) for row in rows
        if row.get("status") == "OK" and isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
    )
    if not values:
        return None
    middle = len(values) // 2
    value = (
        values[middle] if len(values) % 2
        else (values[middle - 1] + values[middle]) / 2
    )
    return round(value, 2)
