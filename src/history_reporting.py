"""Offline ticker timelines and recurring history briefings."""

from __future__ import annotations

from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3

from src.history import connect_history_read_only
from src.history_comparison import (
    _candidate_ranks,
    compare_indexed_runs,
    export_run_comparison,
)


TIMELINE_FIELDS = (
    "run_id", "completed_at", "run_fingerprint", "run_universe_size",
    "ticker", "status", "rank", "discovery_score", "score_change",
    "rank_change", "score_confidence", "fundamental_confidence",
    "fundamental_data_quality", "country", "sector", "exchange",
)


def build_ticker_history(database_path: Path, ticker: str) -> dict:
    """Build a chronological timeline for one ticker across indexed runs."""
    path = Path(database_path)
    ticker = str(ticker).strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    if not path.is_file():
        raise FileNotFoundError(f"History database not found: {path}")
    with closing(connect_history_read_only(path)) as connection:
        runs = connection.execute(
            """SELECT run_id, completed_at, fingerprint, universe_size
               FROM runs ORDER BY completed_at, run_id"""
        ).fetchall()
        timeline = []
        previous = None
        for run_id, completed_at, fingerprint, universe_size in runs:
            payloads = connection.execute(
                "SELECT result_json FROM results WHERE run_id = ? ORDER BY position",
                (run_id,),
            ).fetchall()
            rows = [json.loads(payload) for payload, in payloads]
            lookup = {str(row.get("ticker", "")).upper(): row for row in rows}
            row = lookup.get(ticker)
            if row is None:
                continue
            rank = _candidate_ranks({str(item.get("ticker")): item for item in rows}).get(
                str(row.get("ticker"))
            )
            point = {
                "run_id": run_id,
                "completed_at": completed_at,
                "run_fingerprint": fingerprint,
                "run_universe_size": universe_size,
                "ticker": ticker,
                "status": row.get("status"),
                "rank": rank,
                "discovery_score": _number(row.get("discovery_score")),
                "score_change": None,
                "rank_change": None,
                "score_confidence": _number(row.get("score_confidence")),
                "fundamental_confidence": _number(row.get("fundamental_confidence")),
                "fundamental_data_quality": row.get("fundamental_data_quality"),
                "country": row.get("country"),
                "sector": row.get("sector"),
                "exchange": row.get("exchange"),
            }
            if previous is not None:
                point["score_change"] = _difference(
                    previous["discovery_score"], point["discovery_score"]
                )
                point["rank_change"] = _difference(
                    previous["rank"], point["rank"], invert=True
                )
            timeline.append(point)
            previous = point
    if not timeline:
        raise ValueError(f"Ticker is not present in indexed history: {ticker}")
    ranks = [point["rank"] for point in timeline if point["rank"] is not None]
    statuses = [point["status"] for point in timeline]
    return {
        "ticker": ticker,
        "appearances": len(timeline),
        "first_seen": timeline[0]["completed_at"],
        "latest_seen": timeline[-1]["completed_at"],
        "best_rank": min(ranks) if ranks else None,
        "worst_rank": max(ranks) if ranks else None,
        "status_transition_count": sum(
            old != new for old, new in zip(statuses, statuses[1:])
        ),
        "timeline": timeline,
    }


def export_ticker_history(history: dict, output_directory: Path) -> tuple[Path, Path]:
    """Export ticker history to JSON and CSV."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    safe_ticker = history["ticker"].replace(".", "_")
    json_path = output_directory / f"ticker_history_{safe_ticker}.json"
    csv_path = output_directory / f"ticker_history_{safe_ticker}.csv"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2, sort_keys=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TIMELINE_FIELDS)
        writer.writeheader()
        writer.writerows(history["timeline"])
    return csv_path, json_path


def build_weekly_report(database_path: Path) -> dict:
    """Compare the latest pair of indexed runs with matching universe sizes."""
    old_run, new_run = _latest_compatible_pair(database_path)
    comparison = compare_indexed_runs(database_path, old_run["run_id"], new_run["run_id"])
    same_fingerprint = old_run["fingerprint"] == new_run["fingerprint"]
    same_membership = not (
        comparison["summary"]["entrants"] or comparison["summary"]["exits"]
    )
    warnings = []
    if not same_fingerprint:
        warnings.append(
            "Run fingerprints differ; movements may include configuration or scoring changes."
        )
    if not same_membership:
        warnings.append(
            "Universe membership differs despite equal size; entrants and exits affect ranks."
        )
    compatibility = {
        "classification": (
            "same_configuration_and_universe" if same_fingerprint and same_membership
            else "same_universe_configuration_changed" if same_membership
            else "same_universe_size_membership_changed"
        ),
        "same_fingerprint": same_fingerprint,
        "same_universe_size": True,
        "same_universe_membership": same_membership,
        "old_universe_size": old_run["universe_size"],
        "new_universe_size": new_run["universe_size"],
        "old_completed_at": old_run["completed_at"],
        "new_completed_at": new_run["completed_at"],
        "warnings": warnings,
    }
    comparison["compatibility"] = compatibility
    comparison["promotions_to_ok"] = [
        _brief_row(row) for row in comparison["rows"]
        if row["presence"] == "retained"
        and row["old_status"] != "OK" and row["new_status"] == "OK"
    ]
    comparison["demotions_from_ok"] = [
        _brief_row(row) for row in comparison["rows"]
        if row["presence"] == "retained"
        and row["old_status"] == "OK" and row["new_status"] != "OK"
    ]
    return comparison


def export_weekly_report(report: dict, output_directory: Path) -> tuple[Path, Path, Path]:
    """Export weekly JSON/CSV details plus a concise Markdown briefing."""
    csv_path, json_path = export_run_comparison(
        report, output_directory, report_prefix="weekly_history_report"
    )
    report_id = f"{report['old_run_id']}_to_{report['new_run_id']}"
    markdown_path = Path(output_directory) / f"weekly_history_report_{report_id}.md"
    markdown_path.write_text(_weekly_markdown(report), encoding="utf-8")
    return markdown_path, csv_path, json_path


def _latest_compatible_pair(database_path: Path) -> tuple[dict, dict]:
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"History database not found: {path}")
    with closing(connect_history_read_only(path)) as connection:
        rows = connection.execute(
            """SELECT run_id, completed_at, fingerprint, universe_size, completed_count
               FROM runs WHERE status = 'complete'
               ORDER BY completed_at DESC, run_id DESC"""
        ).fetchall()
    runs = [
        {"run_id": row[0], "completed_at": row[1], "fingerprint": row[2],
         "universe_size": row[3], "completed_count": row[4]}
        for row in rows if row[3] == row[4]
    ]
    for newer_index, newer in enumerate(runs):
        for older in runs[newer_index + 1:]:
            if older["universe_size"] == newer["universe_size"]:
                return older, newer
    raise ValueError(
        "At least two complete indexed runs with the same universe size are required"
    )


def _weekly_markdown(report: dict) -> str:
    summary = report["summary"]
    compatibility = report["compatibility"]
    lines = [
        "# Discovery Engine Weekly Change Report", "",
        f"- Older run: `{report['old_run_id']}`",
        f"- Newer run: `{report['new_run_id']}`",
        f"- Compatibility: `{compatibility['classification']}`",
        f"- Entrants / exits / retained: {summary['entrants']} / {summary['exits']} / {summary['retained']}",
        f"- Status transitions: {summary['status_transitions']}",
        f"- Rank changes: {summary['rank_changes']}",
        f"- Score changes: {summary['score_changes']}",
        f"- Data-quality changes: {summary['data_quality_changes']}", "",
    ]
    if compatibility["warnings"]:
        lines.extend(["## Compatibility warnings", ""])
        lines.extend(f"- {warning}" for warning in compatibility["warnings"])
        lines.append("")
    lines.extend(_markdown_movers("Largest rank gains", report["biggest_rank_gains"]))
    lines.extend(_markdown_movers("Largest rank declines", report["biggest_rank_declines"]))
    lines.extend(_markdown_statuses("Promotions to OK", report["promotions_to_ok"]))
    lines.extend(_markdown_statuses("Demotions from OK", report["demotions_from_ok"]))
    return "\n".join(lines).rstrip() + "\n"


def _markdown_movers(title, rows):
    lines = [f"## {title}", ""]
    lines.extend(
        f"- `{row['ticker']}`: {row['old_rank']} -> {row['new_rank']} ({row['rank_change']:+g})"
        for row in rows
    )
    return lines + (["- None"] if not rows else []) + [""]


def _markdown_statuses(title, rows):
    lines = [f"## {title}", ""]
    lines.extend(
        f"- `{row['ticker']}`: {row['old_status']} -> {row['new_status']}"
        for row in rows[:25]
    )
    return lines + (["- None"] if not rows else []) + [""]


def _brief_row(row):
    return {key: row[key] for key in (
        "ticker", "country", "sector", "old_status", "new_status",
        "old_discovery_score", "new_discovery_score", "score_change",
    )}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _difference(old, new, invert=False):
    if old is None or new is None:
        return None
    return round(old - new if invert else new - old, 6)
