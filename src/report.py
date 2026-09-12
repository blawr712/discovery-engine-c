from datetime import datetime
import json
from pathlib import Path
import re
import pandas as pd

from src.config import OUTPUT_DIR, REPORTS_CONFIG


def export_report(
    results: list[dict],
    run_id: str | None = None,
    output_directory: Path | None = None,
) -> str:
    df = pd.DataFrame(results)

    if df.empty:
        raise ValueError("No results to export.")

    df = df.sort_values("discovery_score", ascending=False)

    report_id = run_id or datetime.now().strftime("%Y-%m-%d")
    output_directory = Path(output_directory or OUTPUT_DIR)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"discovery_scores_{report_id}.csv"

    df.to_csv(output_path, index=False)

    return str(output_path)


def export_candidate_report(
    results: list[dict],
    run_id: str,
    output_directory: Path | None = None,
    top_n: int | None = None,
) -> str:
    """Export a curated research queue containing successful companies only."""
    top_n = top_n or int(REPORTS_CONFIG.get("top_stocks", 100))
    candidates = [dict(row) for row in results if row.get("status") == "OK"]
    candidates.sort(
        key=lambda row: (
            -_numeric(row.get("discovery_score"), default=0),
            str(row.get("ticker", "")),
        )
    )

    fundamental_order = sorted(
        candidates,
        key=lambda row: (
            -_numeric(row.get("fundamental_score_normalized"), default=-1),
            str(row.get("ticker", "")),
        ),
    )
    fundamental_ranks = {
        str(row.get("ticker")): rank
        for rank, row in enumerate(fundamental_order, start=1)
        if _numeric(row.get("fundamental_score_normalized")) is not None
    }

    report_rows = []
    for rank, row in enumerate(candidates, start=1):
        strengths, risks = _factor_highlights(row)
        report_rows.append(
            {
                "rank": rank,
                "fundamental_rank": fundamental_ranks.get(str(row.get("ticker"))),
                "ticker": row.get("ticker"),
                "company_name": row.get("company_name"),
                "country": row.get("country"),
                "exchange": row.get("exchange"),
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "asset_type": row.get("asset_type"),
                "market_cap": row.get("market_cap"),
                "discovery_score": row.get("discovery_score"),
                "score_confidence": row.get("score_confidence"),
                "fundamental_score": row.get("fundamental_score"),
                "fundamental_score_normalized": row.get("fundamental_score_normalized"),
                "fundamental_confidence": row.get("fundamental_confidence"),
                "fundamental_data_quality": row.get("fundamental_data_quality"),
                "fundamental_data_as_of": row.get("fundamental_data_as_of"),
                "strongest_signals": strengths,
                "principal_risks": risks,
                "reason_flags": row.get("reason_flags"),
                "factor_breakdown": row.get("factor_breakdown"),
                "fundamental_breakdown": row.get("fundamental_breakdown"),
            }
        )

    columns = list(report_rows[0]) if report_rows else [
        "rank", "fundamental_rank", "ticker", "company_name", "country",
        "exchange", "sector", "industry", "asset_type", "market_cap",
        "discovery_score", "score_confidence", "fundamental_score",
        "fundamental_score_normalized", "fundamental_confidence",
        "fundamental_data_quality", "fundamental_data_as_of",
        "strongest_signals", "principal_risks", "reason_flags",
        "factor_breakdown", "fundamental_breakdown",
    ]
    frame = pd.DataFrame(report_rows, columns=columns).head(top_n)
    output_directory = Path(output_directory or OUTPUT_DIR)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"research_candidates_{run_id}.csv"
    frame.to_csv(output_path, index=False)
    return str(output_path)


def export_experimental_research_reports(
    results: list[dict],
    calibration: dict,
    run_id: str,
    output_directory: Path | None = None,
    review_n: int = 25,
) -> dict:
    """Export decision-ready reports for passing experimental scenarios."""
    output_directory = Path(output_directory or OUTPUT_DIR)
    output_directory.mkdir(parents=True, exist_ok=True)
    summary = calibration["summary"]
    calibration_rows = calibration["rows"]
    result_lookup = {
        str(row.get("ticker", "")): row
        for row in results
        if row.get("status") == "OK"
    }
    core_factors = set(
        summary.get("coverage_neutral_model", {}).get("core_factors", [])
    )
    report_paths = {}
    scenario_rows_by_name = {}
    review_rows = []
    composition = {}

    for scenario, acceptance in summary.get("scenario_acceptance", {}).items():
        weights = summary.get("blend_scenarios", {}).get(scenario, {})
        if (
            acceptance.get("status") != "pass"
            or float(weights.get("fundamental_weight", 0)) <= 0
        ):
            continue
        rank_field = f"experimental_{scenario}_rank"
        score_field = f"experimental_{scenario}_score"
        scenario_rows = []
        for calibration_row in calibration_rows:
            experimental_rank = calibration_row.get(rank_field)
            if experimental_rank is None:
                continue
            ticker = str(calibration_row.get("ticker", ""))
            source = result_lookup.get(ticker, {})
            strengths, weaknesses, treatments = _fundamental_review(
                source,
                core_factors,
            )
            movement = (
                calibration_row["official_rank"] - experimental_rank
            )
            scenario_rows.append({
                "scenario": scenario,
                "scenario_acceptance": acceptance.get("status"),
                "experimental_rank": experimental_rank,
                "official_rank": calibration_row.get("official_rank"),
                "rank_movement": movement,
                "movement_explanation": _movement_explanation(
                    movement,
                    calibration_row,
                ),
                "ticker": ticker,
                "company_name": calibration_row.get("company_name"),
                "country": calibration_row.get("country"),
                "sector": calibration_row.get("sector"),
                "industry": source.get("industry"),
                "discovery_score": calibration_row.get("discovery_score"),
                "technical_percentile": calibration_row.get(
                    "technical_percentile"
                ),
                "experimental_score": calibration_row.get(score_field),
                "core_fundamental_score": calibration_row.get(
                    "core_fundamental_score"
                ),
                "core_fundamental_confidence": calibration_row.get(
                    "core_fundamental_confidence"
                ),
                "peer_fundamental_percentile": calibration_row.get(
                    "peer_fundamental_percentile"
                ),
                "fundamental_peer_group": calibration_row.get(
                    "fundamental_peer_group"
                ),
                "core_strengths": strengths,
                "core_weaknesses": weaknesses,
                "data_treatments": treatments,
                "outlier_flags": calibration_row.get("outlier_flags"),
                "reason_flags": source.get("reason_flags"),
            })
        scenario_rows.sort(
            key=lambda row: (row["experimental_rank"], row["ticker"])
        )
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", scenario).strip("_")
        path = output_directory / f"research_{slug}_{run_id}.csv"
        pd.DataFrame(scenario_rows).to_csv(path, index=False)
        report_paths[scenario] = str(path)
        scenario_rows_by_name[scenario] = scenario_rows
        review_rows.extend(scenario_rows[:review_n])
        composition[scenario] = _composition_summary(scenario_rows)

    review_path = output_directory / f"research_review_top{review_n}_{run_id}.csv"
    pd.DataFrame(review_rows).to_csv(review_path, index=False)
    summary_path = output_directory / f"research_scenarios_{run_id}.json"
    with summary_path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "run_id": run_id,
                "official_order_authoritative": True,
                "review_size_per_scenario": review_n,
                "scenario_reports": report_paths,
                "scenario_acceptance": summary.get("scenario_acceptance", {}),
                "composition": composition,
            },
            file,
            indent=2,
            sort_keys=True,
        )
    selection_artifacts = _export_scenario_selection(
        scenario_rows_by_name,
        summary,
        run_id,
        output_directory,
    )
    return {
        "scenario_report_paths": report_paths,
        "review_report_path": str(review_path),
        "scenario_summary_path": str(summary_path),
        **selection_artifacts,
    }


def _export_scenario_selection(
    scenarios: dict[str, list[dict]],
    calibration_summary: dict,
    run_id: str,
    output_directory: Path,
) -> dict:
    passing = sorted(scenarios)
    weights = calibration_summary.get("blend_scenarios", {})
    recommendation = (
        max(
            passing,
            key=lambda name: (
                float(weights.get(name, {}).get("technical_weight", 0)),
                name,
            ),
        )
        if passing
        else None
    )
    ranking_config = calibration_summary.get("research_ranking_config", {})
    configured = ranking_config.get("selected_scenario")
    selected = configured if configured in passing else recommendation
    sensitivity_threshold = int(
        ranking_config.get("rank_sensitivity_threshold", 5)
    )
    row_lookup = {
        scenario: {row["ticker"]: row for row in rows}
        for scenario, rows in scenarios.items()
    }
    tickers = sorted({
        ticker for lookup in row_lookup.values() for ticker in lookup
    })
    comparison_rows = []
    for ticker in tickers:
        ranks = {
            scenario: int(lookup[ticker]["experimental_rank"])
            for scenario, lookup in row_lookup.items()
            if ticker in lookup
        }
        if not ranks:
            continue
        selected_source = (
            row_lookup.get(selected, {}).get(ticker, {})
            if selected
            else {}
        )
        rank_values = list(ranks.values())
        comparison_row = {
            "ticker": ticker,
            "company_name": selected_source.get("company_name"),
            "country": selected_source.get("country"),
            "sector": selected_source.get("sector"),
            "official_rank": selected_source.get("official_rank"),
            "selected_scenario": selected,
            "selected_rank": ranks.get(selected),
            "consensus_rank_average": round(
                sum(rank_values) / len(rank_values),
                2,
            ),
            "scenario_rank_min": min(rank_values),
            "scenario_rank_max": max(rank_values),
            "scenario_rank_range": max(rank_values) - min(rank_values),
            "rank_sensitivity": (
                "stable"
                if max(rank_values) - min(rank_values)
                <= sensitivity_threshold
                else "weight_sensitive"
            ),
        }
        for scenario in passing:
            comparison_row[f"{scenario}_rank"] = ranks.get(scenario)
        comparison_rows.append(comparison_row)
    comparison_rows.sort(
        key=lambda row: (
            row["consensus_rank_average"],
            row["ticker"],
        )
    )
    for consensus_rank, row in enumerate(comparison_rows, start=1):
        row["consensus_rank"] = consensus_rank

    comparison_path = (
        output_directory / f"research_scenario_comparison_{run_id}.csv"
    )
    pd.DataFrame(comparison_rows).to_csv(comparison_path, index=False)

    selected_path = None
    if selected:
        comparison_lookup = {row["ticker"]: row for row in comparison_rows}
        selected_rows = []
        for source in scenarios[selected]:
            row = dict(source)
            comparison = comparison_lookup[source["ticker"]]
            row["selected_research_scenario"] = selected
            row["consensus_rank"] = comparison["consensus_rank"]
            row["scenario_rank_range"] = comparison["scenario_rank_range"]
            row["rank_sensitivity"] = comparison["rank_sensitivity"]
            selected_rows.append(row)
        selected_path = (
            output_directory / f"v0.3_research_candidates_{run_id}.csv"
        )
        pd.DataFrame(selected_rows).to_csv(selected_path, index=False)

    agreement = _scenario_agreement(row_lookup, passing)
    stable_count = sum(
        row["rank_sensitivity"] == "stable" for row in comparison_rows
    )
    decision = {
        "run_id": run_id,
        "passing_weighted_scenarios": passing,
        "configured_scenario": configured,
        "recommended_scenario": recommendation,
        "selected_scenario": selected,
        "selection_status": "selected" if selected else "no_passing_scenario",
        "recommendation_reason": (
            "Highest technical weight among passing weighted scenarios"
            if recommendation
            else "No weighted scenario passed calibration gates"
        ),
        "official_discovery_score_unchanged": True,
        "rank_sensitivity_threshold": sensitivity_threshold,
        "compared_candidates": len(comparison_rows),
        "stable_candidates": stable_count,
        "weight_sensitive_candidates": len(comparison_rows) - stable_count,
        "scenario_agreement": agreement,
        "comparison_report_path": str(comparison_path),
        "selected_research_report_path": (
            str(selected_path) if selected_path else None
        ),
    }
    decision_path = output_directory / f"research_decision_{run_id}.json"
    with decision_path.open("w", encoding="utf-8") as file:
        json.dump(decision, file, indent=2, sort_keys=True)
    return {
        "scenario_comparison_path": str(comparison_path),
        "research_decision_path": str(decision_path),
        "selected_research_report_path": (
            str(selected_path) if selected_path else None
        ),
        "selected_research_scenario": selected,
    }


def _scenario_agreement(
    row_lookup: dict[str, dict[str, dict]],
    scenarios: list[str],
) -> dict:
    if not scenarios:
        return {}
    agreement = {}
    for cutoff in (10, 25, 50, 100):
        top_sets = [
            {
                ticker
                for ticker, row in row_lookup[scenario].items()
                if int(row["experimental_rank"]) <= cutoff
            }
            for scenario in scenarios
        ]
        common = set.intersection(*top_sets) if top_sets else set()
        union = set.union(*top_sets) if top_sets else set()
        agreement[f"top_{cutoff}"] = {
            "common_candidates": len(common),
            "union_candidates": len(union),
            "jaccard_percentage": (
                round(len(common) / len(union) * 100, 2)
                if union
                else None
            ),
        }
    return agreement


def _fundamental_review(
    row: dict,
    core_factors: set[str],
) -> tuple[str, str, str]:
    breakdown = _json_dict(row.get("fundamental_breakdown"))
    scored = []
    treatments = []
    for name, factor in breakdown.items():
        if not isinstance(factor, dict):
            continue
        quality = str(factor.get("data_quality") or "unknown")
        if factor.get("applicable", True) is False:
            treatments.append(f"{name}: not applicable")
            continue
        if quality in {"missing", "invalid", "stale", "capped", "flagged"}:
            treatments.append(f"{name}: {quality}")
        if name not in core_factors or factor.get("available") is not True:
            continue
        maximum = _numeric(factor.get("max_points"), 0)
        points = _numeric(factor.get("points"), 0)
        ratio = points / maximum if maximum > 0 else 0
        scored.append((ratio, name, str(factor.get("explanation", ""))))
    strengths = [
        f"{name}: {explanation}"
        for ratio, name, explanation in sorted(scored, reverse=True)
        if ratio >= 0.7
    ][:3]
    weaknesses = [
        f"{name}: {explanation}"
        for ratio, name, explanation in sorted(scored)
        if ratio <= 0.4
    ][:3]
    return "; ".join(strengths), "; ".join(weaknesses), "; ".join(treatments)


def _movement_explanation(movement: int, row: dict) -> str:
    peer = _numeric(row.get("peer_fundamental_percentile"))
    confidence = _numeric(row.get("core_fundamental_confidence"))
    if movement > 0:
        direction = f"Promoted {movement} positions"
    elif movement < 0:
        direction = f"Demoted {abs(movement)} positions"
    else:
        direction = "Rank unchanged"
    if peer is None:
        return f"{direction}; core peer percentile unavailable"
    return (
        f"{direction}; core peer percentile {peer:.1f} "
        f"at {confidence or 0:.1f}% confidence"
    )


def _composition_summary(rows: list[dict]) -> dict:
    result = {}
    for cutoff in (25, 100):
        top = rows[:cutoff]
        result[f"top_{cutoff}"] = {
            "companies": len(top),
            "countries": _value_counts(top, "country"),
            "sectors": _value_counts(top, "sector"),
        }
    return result


def _value_counts(rows: list[dict], field: str) -> dict:
    counts = {}
    for row in rows:
        value = str(row.get(field) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _json_dict(value: object) -> dict:
    if not value:
        return {}
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _factor_highlights(row: dict) -> tuple[str, str]:
    factors = []
    for field in ("factor_breakdown", "fundamental_breakdown"):
        value = row.get(field)
        if not value:
            continue
        try:
            breakdown = json.loads(value) if isinstance(value, str) else value
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(breakdown, dict):
            continue
        for factor in breakdown.values():
            if not isinstance(factor, dict) or not factor.get("available"):
                continue
            maximum = _numeric(factor.get("max_points"), default=0)
            points = _numeric(factor.get("points"), default=0)
            ratio = points / maximum if maximum > 0 else 0
            factors.append((ratio, str(factor.get("explanation", ""))))

    strengths = [text for ratio, text in sorted(factors, reverse=True) if ratio >= 0.75][:3]
    risks = [text for ratio, text in sorted(factors) if ratio <= 0.40][:3]
    return "; ".join(strengths), "; ".join(risks)


def _numeric(value: object, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
