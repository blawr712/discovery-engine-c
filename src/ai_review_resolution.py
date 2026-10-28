"""Join frozen AI reviews to a resolved research cohort.

Adds ``ai_review_comparison`` to a research-baseline resolution: for each
resolved horizon, outcomes by verdict, and the spread the forward test in
DECISIONS.md judges (median excess of ``clear`` minus that of ``caution`` and
``avoid`` together). Reported alongside, not decided on: ``avoid`` against the
rest, the spread inside defensive thirds, and unexplained against explained
discounts. The baseline file and the review artifact are only read.
"""

from __future__ import annotations

import json
from pathlib import Path
import statistics

FLAGGED = ("caution", "avoid")


def load_ai_reviews(output_directory: Path, run_id: str) -> dict | None:
    """The run's frozen reviews; a dry run only when no real artifact exists."""
    for name in (f"ai_reviews_{run_id}.json", f"ai_review_dry_run_{run_id}.json"):
        path = Path(output_directory) / name
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    return None


def ai_review_comparison(resolution: dict, reviews: dict) -> dict:
    """Outcomes of the reviewed names by verdict, per resolved horizon."""
    verdicts = {
        str(entry["ticker"]): entry
        for entry in reviews.get("names") or []
    }
    candidates = {str(row["ticker"]): row for row in resolution.get("candidates") or []}
    joined = [ticker for ticker in verdicts if ticker in candidates]
    result = {
        "reviews_run_id": reviews.get("run_id"),
        "dry_run": bool(reviews.get("dry_run")),
        "checklist_version": reviews.get("checklist_version"),
        "skill_version": reviews.get("skill_version"),
        "models": reviews.get("models"),
        "reviews_artifact_sha256": reviews.get("artifact_sha256"),
        "reviewed": len(verdicts),
        "joined_to_cohort": len(joined),
        "not_in_cohort": sorted(ticker for ticker in verdicts if ticker not in candidates),
        "note": (
            "Dry run: not part of the forward test." if reviews.get("dry_run")
            else "Judged only by the rule in DECISIONS.md, pooled over cohorts; one cohort decides nothing."
        ),
        "horizons": {},
    }
    for label in resolution.get("eligible_horizons") or []:
        rows = []
        for ticker in joined:
            horizon = (candidates[ticker].get("horizons") or {}).get(label) or {}
            if horizon.get("status") != "resolved" or "excess_return_percent" not in horizon:
                continue
            entry = verdicts[ticker]
            review = entry.get("review") or {}
            rows.append({
                "ticker": ticker,
                "verdict": entry.get("verdict") or entry.get("status"),
                "discount": (review.get("peer_comparison") or {}).get("discount_explained"),
                "defensive": (candidates[ticker].get("scores") or {}).get("score_defensive"),
                "excess": float(horizon["excess_return_percent"]),
            })
        result["horizons"][label] = _horizon(rows)
    return result


def _horizon(rows: list[dict]) -> dict:
    by_verdict = {}
    for verdict in ("clear", "caution", "avoid", "not_reviewed", "validation_failed"):
        by_verdict[verdict] = _group([row["excess"] for row in rows if row["verdict"] == verdict])
    clear = [row["excess"] for row in rows if row["verdict"] == "clear"]
    flagged = [row["excess"] for row in rows if row["verdict"] in FLAGGED]
    avoid = [row["excess"] for row in rows if row["verdict"] == "avoid"]
    rest = [row["excess"] for row in rows if row["verdict"] in ("clear", "caution")]
    report = {
        "resolved": len(rows),
        "by_verdict": by_verdict,
        "clear_minus_flagged_median_percent": _spread(clear, flagged),
        "reported_only": {
            "avoid_minus_rest_median_percent": _spread(avoid, rest),
            "unexplained_minus_explained_discount_median_percent": _spread(
                [row["excess"] for row in rows if row["discount"] == "no"],
                [row["excess"] for row in rows if row["discount"] == "yes"],
            ),
            "clear_minus_flagged_by_defensive_third": _by_defensive_third(rows),
        },
    }
    return report


def _by_defensive_third(rows: list[dict]) -> list[dict]:
    scored = sorted(
        (row for row in rows if isinstance(row["defensive"], (int, float))
         and row["verdict"] in ("clear",) + FLAGGED),
        key=lambda row: (row["defensive"], row["ticker"]),
    )
    if len(scored) < 3:
        return []
    thirds = []
    for third in range(3):
        part = scored[third * len(scored) // 3:(third + 1) * len(scored) // 3]
        thirds.append({
            "third": third + 1,
            "names": len(part),
            "clear_minus_flagged_median_percent": _spread(
                [row["excess"] for row in part if row["verdict"] == "clear"],
                [row["excess"] for row in part if row["verdict"] in FLAGGED],
            ),
        })
    return thirds


def _group(values: list[float]) -> dict:
    return {
        "resolved": len(values),
        "median_excess_percent": round(statistics.median(values), 2) if values else None,
        "win_rate_percent": round(100.0 * sum(1 for v in values if v > 0) / len(values), 2) if values else None,
    }


def _spread(left: list[float], right: list[float]) -> float | None:
    if not left or not right:
        return None
    return round(statistics.median(left) - statistics.median(right), 2)
