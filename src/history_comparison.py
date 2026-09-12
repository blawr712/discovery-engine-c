"""Deterministic offline comparison of two indexed Discovery Engine runs."""

from __future__ import annotations

from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3

from src.history import connect_history_read_only


COMPARISON_FIELDS = (
    "ticker", "country", "sector", "exchange", "presence", "change_types",
    "old_status", "new_status", "old_rank", "new_rank", "rank_change",
    "old_discovery_score", "new_discovery_score", "score_change",
    "old_score_confidence", "new_score_confidence", "score_confidence_change",
    "old_fundamental_confidence", "new_fundamental_confidence",
    "fundamental_confidence_change", "old_data_quality", "new_data_quality",
)


def compare_indexed_runs(database_path: Path, old_run_id: str, new_run_id: str) -> dict:
    """Compare two indexed runs without contacting any external provider."""
    if old_run_id == new_run_id:
        raise ValueError("comparison requires two different run IDs")
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"History database not found: {path}")
    with closing(connect_history_read_only(path)) as connection:
        old = _load_run(connection, old_run_id)
        new = _load_run(connection, new_run_id)

    old_ranks = _candidate_ranks(old)
    new_ranks = _candidate_ranks(new)
    rows = [
        _compare_ticker(
            ticker, old.get(ticker), new.get(ticker), old_ranks, new_ranks
        )
        for ticker in sorted(set(old) | set(new))
    ]
    counts = {
        "entrants": sum(row["presence"] == "entrant" for row in rows),
        "exits": sum(row["presence"] == "exit" for row in rows),
        "retained": sum(row["presence"] == "retained" for row in rows),
        "status_transitions": sum(
            "status_transition" in row["change_types"] for row in rows
        ),
        "rank_changes": sum("rank_change" in row["change_types"] for row in rows),
        "score_changes": sum("score_change" in row["change_types"] for row in rows),
        "data_quality_changes": sum(
            "data_quality_change" in row["change_types"] for row in rows
        ),
    }
    return {
        "old_run_id": old_run_id,
        "new_run_id": new_run_id,
        "summary": counts,
        "status_transition_matrix": _status_transition_matrix(rows),
        "composition_changes": {
            "country": _composition_changes(old, new, "country"),
            "sector": _composition_changes(old, new, "sector"),
        },
        "biggest_rank_gains": _rank_movers(rows, reverse=True),
        "biggest_rank_declines": _rank_movers(rows, reverse=False),
        "rows": rows,
    }


def export_run_comparison(
    comparison: dict,
    output_directory: Path,
    report_prefix: str = "history_comparison",
) -> tuple[Path, Path]:
    """Export full machine-readable JSON and flat review-ready CSV reports."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    report_id = f"{comparison['old_run_id']}_to_{comparison['new_run_id']}"
    json_path = output_directory / f"{report_prefix}_{report_id}.json"
    csv_path = output_directory / f"{report_prefix}_{report_id}.csv"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(comparison, handle, indent=2, sort_keys=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMPARISON_FIELDS)
        writer.writeheader()
        for row in comparison["rows"]:
            exported = dict(row)
            exported["change_types"] = "|".join(row["change_types"])
            writer.writerow(exported)
    return csv_path, json_path


def _load_run(connection: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    exists = connection.execute(
        "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if exists is None:
        raise ValueError(f"Run is not indexed: {run_id}")
    rows = connection.execute(
        "SELECT result_json FROM results WHERE run_id = ? ORDER BY position",
        (run_id,),
    ).fetchall()
    return {
        str(row["ticker"]): row
        for payload, in rows
        for row in [json.loads(payload)]
        if row.get("ticker")
    }


def _candidate_ranks(rows: dict[str, dict]) -> dict[str, int]:
    candidates = [row for row in rows.values() if row.get("status") == "OK"]
    candidates.sort(key=lambda row: (-(_number(row.get("discovery_score")) or 0), str(row["ticker"])))
    return {str(row["ticker"]): rank for rank, row in enumerate(candidates, 1)}


def _compare_ticker(ticker, old, new, old_ranks, new_ranks) -> dict:
    presence = "entrant" if old is None else "exit" if new is None else "retained"
    old = old or {}
    new = new or {}
    old_rank = old_ranks.get(ticker)
    new_rank = new_ranks.get(ticker)
    values = {
        "ticker": ticker,
        "country": new.get("country") or old.get("country"),
        "sector": new.get("sector") or old.get("sector"),
        "exchange": new.get("exchange") or old.get("exchange"),
        "presence": presence,
        "old_status": old.get("status"),
        "new_status": new.get("status"),
        "old_rank": old_rank,
        "new_rank": new_rank,
        "rank_change": _difference(old_rank, new_rank, invert=True),
        "old_discovery_score": _number(old.get("discovery_score")),
        "new_discovery_score": _number(new.get("discovery_score")),
        "old_score_confidence": _number(old.get("score_confidence")),
        "new_score_confidence": _number(new.get("score_confidence")),
        "old_fundamental_confidence": _number(old.get("fundamental_confidence")),
        "new_fundamental_confidence": _number(new.get("fundamental_confidence")),
        "old_data_quality": old.get("fundamental_data_quality"),
        "new_data_quality": new.get("fundamental_data_quality"),
    }
    values["score_change"] = _difference(
        values["old_discovery_score"], values["new_discovery_score"]
    )
    values["score_confidence_change"] = _difference(
        values["old_score_confidence"], values["new_score_confidence"]
    )
    values["fundamental_confidence_change"] = _difference(
        values["old_fundamental_confidence"], values["new_fundamental_confidence"]
    )
    changes = [presence] if presence != "retained" else []
    if presence == "retained" and values["old_status"] != values["new_status"]:
        changes.append("status_transition")
    if values["rank_change"] not in (None, 0):
        changes.append("rank_change")
    if values["score_change"] not in (None, 0):
        changes.append("score_change")
    if values["score_confidence_change"] not in (None, 0):
        changes.append("score_confidence_change")
    if values["fundamental_confidence_change"] not in (None, 0):
        changes.append("fundamental_confidence_change")
    if presence == "retained" and values["old_data_quality"] != values["new_data_quality"]:
        changes.append("data_quality_change")
    values["change_types"] = changes or ["unchanged"]
    return values


def _difference(old, new, invert=False):
    if old is None or new is None:
        return None
    difference = old - new if invert else new - old
    return round(difference, 6)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rank_movers(rows: list[dict], reverse: bool) -> list[dict]:
    movers = [row for row in rows if row["rank_change"] not in (None, 0)]
    movers.sort(key=lambda row: (row["rank_change"], row["ticker"]), reverse=reverse)
    return [
        {"ticker": row["ticker"], "old_rank": row["old_rank"],
         "new_rank": row["new_rank"], "rank_change": row["rank_change"]}
        for row in movers[:10]
    ]


def _status_transition_matrix(rows: list[dict]) -> list[dict]:
    counts = {}
    for row in rows:
        if row["presence"] != "retained":
            continue
        key = (row["old_status"] or "UNKNOWN", row["new_status"] or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return [
        {"old_status": old, "new_status": new, "count": count}
        for (old, new), count in sorted(counts.items())
    ]


def _composition_changes(old: dict, new: dict, field: str) -> list[dict]:
    def grouped(rows):
        counts = {}
        for row in rows.values():
            group = str(row.get(field) or "UNKNOWN")
            counts[group] = counts.get(group, 0) + 1
        return counts

    old_counts = grouped(old)
    new_counts = grouped(new)
    return [
        {
            field: group,
            "old_count": old_counts.get(group, 0),
            "new_count": new_counts.get(group, 0),
            "change": new_counts.get(group, 0) - old_counts.get(group, 0),
        }
        for group in sorted(set(old_counts) | set(new_counts))
    ]
