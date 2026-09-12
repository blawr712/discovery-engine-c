"""Offline calibration and forward-baseline tooling for Moonshot Discovery."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile

from src.config import BENCHMARKS, MOONSHOT_CONFIG
from src.moonshot import (
    CLASSIFICATION_ORDER,
    FUNDAMENTAL_RISK_FACTORS,
    MARKET_RISK_FACTORS,
)


SELECTED_CLASSIFICATIONS = {
    "priority_research", "asymmetric_watch", "speculative_watch",
}

SCENARIO_CSV_FIELDS = (
    "scenario", "scenario_rank", "baseline_rank", "rank_change", "ticker",
    "company_name", "country", "sector", "size_tier", "classification",
    "upside_score", "risk_of_ruin_score", "moonshot_confidence",
    "market_risk_confidence",
)

VALIDATION_CSV_FIELDS = (
    "moonshot_rank", "ticker", "company_name", "country", "sector",
    "size_tier", "classification", "upside_score", "risk_of_ruin_score",
    "moonshot_confidence", "market_risk_confidence",
    "strongest_upside_driver", "largest_risk_driver", "risk_warnings",
    "maximum_scenario_rank_change",
)

BASELINE_CSV_FIELDS = (
    "moonshot_rank", "ticker", "company_name", "country", "sector",
    "size_tier", "classification", "upside_score", "risk_of_ruin_score",
    "moonshot_confidence", "market_risk_confidence", "benchmark",
    "baseline_as_of", "tracking_status",
)


def build_moonshot_calibration(
    analysis: dict,
    config: dict | None = None,
) -> dict:
    """Stress-test one Moonshot cohort without changing its official ordering."""
    settings = config or MOONSHOT_CONFIG
    calibration = _validated_calibration_config(settings)
    candidates = analysis.get("candidates", [])
    scenario_results = {}
    for scenario_name, scenario in calibration["scenarios"].items():
        upside_weights = _scenario_weights(
            settings["upside_weights"], scenario["upside_multipliers"],
        )
        risk_weights = _scenario_weights(
            settings["risk_weights"], scenario["risk_multipliers"],
        )
        rows = [
            _score_scenario(candidate, upside_weights, risk_weights, settings)
            for candidate in candidates
        ]
        rows.sort(key=_scenario_sort_key)
        baseline_ranks = {
            candidate["ticker"]: candidate["moonshot_rank"]
            for candidate in candidates
        }
        for rank, row in enumerate(rows, start=1):
            row["scenario"] = scenario_name
            row["scenario_rank"] = rank
            row["baseline_rank"] = baseline_ranks[row["ticker"]]
            row["rank_change"] = row["baseline_rank"] - rank
        scenario_results[scenario_name] = {
            "upside_weights": upside_weights,
            "risk_weights": risk_weights,
            "summary": _scenario_summary(rows),
            "candidates": rows,
        }
    baseline = scenario_results["baseline"]["candidates"]
    overlaps = _scenario_overlaps(scenario_results)
    sensitivity = _rank_sensitivity(scenario_results)
    integrity = _baseline_integrity(candidates, baseline)
    factor_distributions = {
        "upside": _factor_distributions(candidates, "upside_factors"),
        "risk": _factor_distributions(candidates, "risk_factors"),
    }
    cohorts = {
        "country": _cohorts(candidates, "country"),
        "size_tier": _cohorts(candidates, "size_tier"),
        "sector": _cohorts(candidates, "sector"),
    }
    outliers = _outliers(candidates)
    explanations = _candidate_explanations(candidates, sensitivity)
    gates = _validation_gates(
        analysis,
        candidates,
        overlaps,
        sensitivity,
        integrity,
        cohorts,
        calibration,
    )
    automated_status = (
        "fail" if any(not gate["passed"] and gate["severity"] == "fail" for gate in gates)
        else "needs_review" if any(not gate["passed"] for gate in gates)
        else "pass"
    )
    return {
        "schema_version": 1,
        "run_id": analysis["run_id"],
        "model_version": analysis["model_version"],
        "official_scores_and_ranks_unchanged": True,
        "automated_status": automated_status,
        "interpretation_warning": (
            "Calibration measures model stability and data integrity. It does not "
            "demonstrate future returns or identify investment recommendations."
        ),
        "market_evidence_summary": analysis["market_evidence_summary"],
        "validation_gates": gates,
        "baseline_integrity": integrity,
        "scenario_overlaps": overlaps,
        "rank_sensitivity": sensitivity,
        "factor_distributions": factor_distributions,
        "cohorts": cohorts,
        "outliers": outliers,
        "candidate_explanations": explanations,
        "scenarios": scenario_results,
    }


def build_forward_baseline(analysis: dict) -> dict:
    """Freeze the comparable candidate cohort for future outcome measurement."""
    market_summary = analysis["market_evidence_summary"]
    if not market_summary.get("cross_section_comparable"):
        raise ValueError(
            "Moonshot market cohort is not comparable; forward baseline withheld."
        )
    excluded = {
        row["ticker"] for row in market_summary.get("excluded_candidates", [])
    }
    baseline_as_of = market_summary.get("last_captured_at")
    baseline_time = datetime.fromisoformat(
        str(baseline_as_of).replace("Z", "+00:00")
    )
    horizon_days = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}
    candidates = [
        {
            "moonshot_rank": row["moonshot_rank"],
            "ticker": row["ticker"],
            "company_name": row.get("company_name"),
            "country": row.get("country"),
            "sector": row.get("sector"),
            "size_tier": row["size_tier"],
            "classification": row["classification"],
            "upside_score": row["upside_score"],
            "risk_of_ruin_score": row["risk_of_ruin_score"],
            "moonshot_confidence": row["moonshot_confidence"],
            "market_risk_confidence": row["market_risk_confidence"],
            "benchmark": BENCHMARKS.get(row.get("country"), "SPY"),
            "baseline_as_of": baseline_as_of,
            "tracking_status": "pending",
            "horizons": {
                horizon: {
                    "status": "pending",
                    "eligible_at": (
                        baseline_time + timedelta(days=days)
                    ).isoformat(),
                }
                for horizon, days in horizon_days.items()
            },
        }
        for row in analysis["candidates"]
        if row["ticker"] not in excluded
    ]
    identity = {
        "run_id": analysis["run_id"],
        "model_version": analysis["model_version"],
        "baseline_as_of": baseline_as_of,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    baseline_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "baseline_id": baseline_id,
        **identity,
        "excluded_candidates": market_summary.get("excluded_candidates", []),
        "official_scores_and_ranks_unchanged": True,
        "interpretation_warning": (
            "This immutable cohort supports future observation. It is not a "
            "backtest, forecast, or investment recommendation."
        ),
    }


def export_moonshot_calibration(
    calibration: dict,
    output_directory: Path,
) -> tuple[Path, Path, Path]:
    """Export complete JSON, scenario rows, and candidate validation CSV."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_id = calibration["run_id"]
    json_path = output_directory / f"moonshot_calibration_{run_id}.json"
    scenarios_path = output_directory / f"moonshot_calibration_{run_id}.csv"
    validation_path = output_directory / f"moonshot_validation_{run_id}.csv"
    _atomic_json(json_path, calibration)
    scenario_rows = [
        row
        for scenario in calibration["scenarios"].values()
        for row in scenario["candidates"]
    ]
    _atomic_csv(scenarios_path, SCENARIO_CSV_FIELDS, scenario_rows)
    validation_rows = []
    for explanation in calibration["candidate_explanations"]:
        row = dict(explanation)
        row["risk_warnings"] = "|".join(row["risk_warnings"])
        validation_rows.append(row)
    _atomic_csv(validation_path, VALIDATION_CSV_FIELDS, validation_rows)
    return scenarios_path, json_path, validation_path


def export_forward_baseline(
    baseline: dict,
    output_directory: Path,
) -> tuple[Path, Path]:
    """Write an immutable baseline, refusing to overwrite changed contents."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    suffix = f"{baseline['run_id']}_{baseline['model_version']}"
    json_path = output_directory / f"moonshot_forward_baseline_{suffix}.json"
    csv_path = output_directory / f"moonshot_forward_baseline_{suffix}.csv"
    rows = [
        {field: candidate.get(field) for field in BASELINE_CSV_FIELDS}
        for candidate in baseline["candidates"]
    ]
    if json_path.is_file():
        existing = json.loads(json_path.read_text(encoding="utf-8"))
        if existing.get("baseline_id") != baseline["baseline_id"]:
            raise ValueError(
                "Existing Moonshot forward baseline differs; model baselines are immutable."
            )
        if not csv_path.is_file():
            _atomic_csv(csv_path, BASELINE_CSV_FIELDS, rows)
    else:
        _atomic_json(json_path, baseline)
        _atomic_csv(csv_path, BASELINE_CSV_FIELDS, rows)
    return csv_path, json_path


def _score_scenario(candidate, upside_weights, risk_weights, config):
    quality_ratio = (
        config["undated_confidence_ratio"]
        if candidate["fundamental_data_quality"] == "undated"
        else 1.0 if candidate["fundamental_data_quality"] == "fresh" else 0.0
    )
    upside_score, upside_confidence = _weighted_factor_score(
        candidate["upside_factors"], upside_weights, quality_ratio,
    )
    risk_score, risk_confidence = _weighted_factor_score(
        candidate["risk_factors"],
        risk_weights,
        quality_ratio,
        quality_adjusted_names=FUNDAMENTAL_RISK_FACTORS,
    )
    market_confidence = _subset_confidence(
        candidate["risk_factors"], risk_weights, MARKET_RISK_FACTORS,
    )
    confidence = round(min(upside_confidence, risk_confidence), 2)
    classification = _classification(
        upside_score, risk_score, confidence, market_confidence, config,
    )
    return {
        "scenario": None,
        "scenario_rank": None,
        "baseline_rank": None,
        "rank_change": None,
        "ticker": candidate["ticker"],
        "company_name": candidate.get("company_name"),
        "country": candidate.get("country"),
        "sector": candidate.get("sector"),
        "size_tier": candidate["size_tier"],
        "classification": classification,
        "upside_score": upside_score,
        "risk_of_ruin_score": risk_score,
        "moonshot_confidence": confidence,
        "market_risk_confidence": market_confidence,
    }


def _weighted_factor_score(
    factors,
    weights,
    quality_ratio,
    quality_adjusted_names=None,
):
    applicable = [factor for factor in factors if factor["applicable"]]
    available = [factor for factor in applicable if factor["available"]]
    available_weight = sum(weights[factor["name"]] for factor in available)
    points = sum(
        _factor_ratio(factor) * weights[factor["name"]]
        for factor in available
    )
    score = round(points / available_weight * 100, 2) if available_weight else 0.0
    quality_adjusted_names = quality_adjusted_names or {
        factor["name"] for factor in factors
    }
    confident_weight = sum(
        weights[factor["name"]] * (
            quality_ratio if factor["name"] in quality_adjusted_names else 1
        )
        for factor in available
    )
    applicable_weight = sum(weights[factor["name"]] for factor in applicable)
    confidence = (
        round(confident_weight / applicable_weight * 100, 2)
        if applicable_weight else 0.0
    )
    return score, confidence


def _subset_confidence(factors, weights, names):
    applicable = [
        factor for factor in factors
        if factor["name"] in names and factor["applicable"]
    ]
    maximum = sum(weights[factor["name"]] for factor in applicable)
    available = sum(
        weights[factor["name"]] for factor in applicable if factor["available"]
    )
    return round(available / maximum * 100, 2) if maximum else 0.0


def _scenario_weights(base, multipliers):
    weighted = {
        name: float(value) * float(multipliers.get(name, 1))
        for name, value in base.items()
    }
    total = sum(weighted.values())
    return {
        name: round(value / total * 100, 8)
        for name, value in weighted.items()
    }


def _classification(upside, risk, confidence, market_confidence, config):
    if confidence < config["minimum_classification_confidence"]:
        return "insufficient_evidence"
    if market_confidence < config["minimum_market_risk_confidence"]:
        return "market_data_required"
    if (
        confidence >= config["priority_confidence_minimum"]
        and upside >= config["priority_upside_minimum"]
        and risk <= config["priority_risk_maximum"]
    ):
        return "priority_research"
    if upside >= config["watch_upside_minimum"] and risk <= config["watch_risk_maximum"]:
        return "asymmetric_watch"
    if upside >= config["speculative_upside_minimum"]:
        return "speculative_watch"
    return "low_upside_signal"


def _scenario_sort_key(row):
    return (
        CLASSIFICATION_ORDER[row["classification"]],
        -row["upside_score"],
        row["risk_of_ruin_score"],
        -row["moonshot_confidence"],
        row["ticker"],
    )


def _scenario_summary(rows):
    return {
        "candidate_count": len(rows),
        "classifications": _counts(row["classification"] for row in rows),
        "selected_count": sum(
            row["classification"] in SELECTED_CLASSIFICATIONS for row in rows
        ),
    }


def _scenario_overlaps(scenarios):
    baseline = scenarios["baseline"]["candidates"]
    output = {}
    for name, scenario in scenarios.items():
        if name == "baseline":
            continue
        output[name] = {}
        for cutoff in (10, 25, 50):
            actual = min(cutoff, len(baseline))
            baseline_set = {row["ticker"] for row in baseline[:actual]}
            scenario_set = {
                row["ticker"] for row in scenario["candidates"][:actual]
            }
            overlap = len(baseline_set & scenario_set)
            output[name][f"top_{cutoff}"] = {
                "overlap_count": overlap,
                "overlap_percent": round(overlap / actual * 100, 2) if actual else 0.0,
            }
    return output


def _rank_sensitivity(scenarios):
    ranks = {}
    baseline_ranks = {}
    for scenario_name, scenario in scenarios.items():
        for row in scenario["candidates"]:
            ranks.setdefault(row["ticker"], {})[scenario_name] = row["scenario_rank"]
            baseline_ranks[row["ticker"]] = row["baseline_rank"]
    rows = []
    for ticker, scenario_ranks in ranks.items():
        values = list(scenario_ranks.values())
        rows.append({
            "ticker": ticker,
            "baseline_rank": baseline_ranks[ticker],
            "minimum_rank": min(values),
            "maximum_rank": max(values),
            "rank_range": max(values) - min(values),
            "rank_standard_deviation": round(statistics.pstdev(values), 2),
            "scenario_ranks": scenario_ranks,
        })
    rows.sort(key=lambda row: (-row["rank_range"], row["baseline_rank"], row["ticker"]))
    ranges = [row["rank_range"] for row in rows]
    return {
        "median_rank_range": _percentile(ranges, 50),
        "p90_rank_range": _percentile(ranges, 90),
        "maximum_rank_range": max(ranges) if ranges else 0,
        "candidates": rows,
    }


def _baseline_integrity(candidates, baseline):
    original = {row["ticker"]: row for row in candidates}
    mismatches = []
    for row in baseline:
        expected = original[row["ticker"]]
        if (
            row["scenario_rank"] != expected["moonshot_rank"]
            or row["classification"] != expected["classification"]
            or abs(row["upside_score"] - expected["upside_score"]) > .05
            or abs(row["risk_of_ruin_score"] - expected["risk_of_ruin_score"]) > .05
        ):
            mismatches.append(row["ticker"])
    return {
        "candidate_count": len(candidates),
        "mismatch_count": len(mismatches),
        "mismatch_tickers": mismatches,
    }


def _factor_distributions(candidates, field):
    names = sorted({
        factor["name"] for candidate in candidates for factor in candidate[field]
    })
    output = {}
    for name in names:
        factors = [
            factor for candidate in candidates for factor in candidate[field]
            if factor["name"] == name and factor["applicable"]
        ]
        values = [
            round(_factor_ratio(factor) * 100, 2)
            for factor in factors if factor["available"]
        ]
        output[name] = {
            "available": len(values),
            "applicable": len(factors),
            "coverage_percent": round(len(values) / len(factors) * 100, 2)
            if factors else None,
            "score_ratio_percent": _distribution(values),
        }
    return output


def _cohorts(candidates, field):
    groups = {}
    for candidate in candidates:
        key = str(candidate.get(field) or "UNKNOWN")
        group = groups.setdefault(key, {
            "total": 0, "priority_research": 0, "asymmetric_watch": 0,
            "speculative_watch": 0, "selected": 0,
        })
        group["total"] += 1
        classification = candidate["classification"]
        if classification in SELECTED_CLASSIFICATIONS:
            group[classification] += 1
            group["selected"] += 1
    for group in groups.values():
        group["selection_rate_percent"] = round(
            group["selected"] / group["total"] * 100, 2
        )
    return dict(sorted(groups.items()))


def _outliers(candidates):
    rules = {
        "zero_dollar_volume": lambda row: row.get("average_dollar_volume_30d") == 0,
        "very_low_dollar_volume": lambda row: _at_most(
            row.get("average_dollar_volume_30d"), 25_000,
        ),
        "extreme_volatility": lambda row: _at_least(
            row.get("annualized_volatility_percent"), 120,
        ),
        "severe_drawdown": lambda row: _at_least(
            row.get("maximum_drawdown_percent"), 70,
        ),
        "heavy_dilution": lambda row: _at_least(row.get("dilution_percent"), 50),
        "reverse_split": lambda row: _at_least(row.get("reverse_split_count_1y"), 1),
    }
    output = {}
    for name, rule in rules.items():
        matches = [row for row in candidates if rule(row)]
        output[name] = {
            "count": len(matches),
            "tickers": [row["ticker"] for row in matches[:50]],
            "truncated": len(matches) > 50,
        }
    return output


def _candidate_explanations(candidates, sensitivity):
    ranges = {
        row["ticker"]: row["rank_range"]
        for row in sensitivity["candidates"]
    }
    output = []
    for candidate in candidates:
        if candidate["classification"] not in {
            "priority_research", "asymmetric_watch",
        }:
            continue
        upside = max(
            (factor for factor in candidate["upside_factors"] if factor["available"]),
            key=_factor_ratio,
            default=None,
        )
        risk = max(
            (factor for factor in candidate["risk_factors"] if factor["available"]),
            key=_factor_ratio,
            default=None,
        )
        output.append({
            "moonshot_rank": candidate["moonshot_rank"],
            "ticker": candidate["ticker"],
            "company_name": candidate.get("company_name"),
            "country": candidate.get("country"),
            "sector": candidate.get("sector"),
            "size_tier": candidate["size_tier"],
            "classification": candidate["classification"],
            "upside_score": candidate["upside_score"],
            "risk_of_ruin_score": candidate["risk_of_ruin_score"],
            "moonshot_confidence": candidate["moonshot_confidence"],
            "market_risk_confidence": candidate["market_risk_confidence"],
            "strongest_upside_driver": _factor_text(upside),
            "largest_risk_driver": _factor_text(risk),
            "risk_warnings": _risk_warnings(candidate),
            "maximum_scenario_rank_change": ranges.get(candidate["ticker"], 0),
        })
    return output


def _validation_gates(
    analysis, candidates, overlaps, sensitivity, integrity, cohorts, calibration,
):
    evidence = analysis["market_evidence_summary"]
    minimum_overlap = float(calibration["minimum_top_25_overlap_percent"])
    overlap_values = [
        scenario["top_25"]["overlap_percent"] for scenario in overlaps.values()
    ]
    minimum_actual_overlap = min(overlap_values) if overlap_values else 100.0
    sensitive_range = float(calibration["sensitive_rank_range"])
    sensitive_top = [
        row for row in sensitivity["candidates"]
        if row["baseline_rank"] <= 25 and row["rank_range"] >= sensitive_range
    ]
    zero_liquidity_errors = []
    for candidate in candidates:
        if candidate.get("average_dollar_volume_30d") != 0:
            continue
        factor = next(
            row for row in candidate["risk_factors"] if row["name"] == "liquidity"
        )
        if not factor["available"] or factor["points"] != factor["max_points"]:
            zero_liquidity_errors.append(candidate["ticker"])
    unresolved_priority = [
        row["ticker"] for row in candidates
        if row["classification"] == "priority_research"
        and row["risk_band"] == "unresolved"
    ]
    country_ratio, country_ratio_text = _selection_rate_ratio(
        cohorts["country"]
    )
    size_ratio, size_ratio_text = _selection_rate_ratio(
        cohorts["size_tier"]
    )
    maximum_country_ratio = float(
        calibration["maximum_country_selection_rate_ratio"]
    )
    maximum_size_ratio = float(
        calibration["maximum_size_tier_selection_rate_ratio"]
    )
    return [
        _gate("comparable_market_cohort", evidence["cross_section_comparable"],
              evidence["coverage_percent"],
              f">={evidence['minimum_coverage_percent']}% synchronized", "fail"),
        _gate("baseline_reproduction", integrity["mismatch_count"] == 0,
              integrity["mismatch_count"], "0 mismatches", "fail"),
        _gate("priority_risk_resolved", not unresolved_priority,
              unresolved_priority, "no unresolved Priority candidates", "fail"),
        _gate("zero_liquidity_penalty", not zero_liquidity_errors,
              zero_liquidity_errors, "all zero-volume names receive maximum risk", "fail"),
        _gate("top_25_scenario_overlap", minimum_actual_overlap >= minimum_overlap,
              minimum_actual_overlap, f">={minimum_overlap}%", "review"),
        _gate(
            "top_25_rank_sensitivity",
            len(sensitive_top) <= int(calibration["maximum_sensitive_top_25_candidates"]),
            len(sensitive_top),
            f"<={int(calibration['maximum_sensitive_top_25_candidates'])} candidates "
            f"moving {sensitive_range:g}+ ranks",
            "review",
        ),
        _gate(
            "country_selection_rate_dispersion",
            country_ratio is not None and country_ratio <= maximum_country_ratio,
            country_ratio_text,
            f"<={maximum_country_ratio:g}x highest/lowest country rate",
            "review",
        ),
        _gate(
            "size_tier_selection_rate_dispersion",
            size_ratio is not None and size_ratio <= maximum_size_ratio,
            size_ratio_text,
            f"<={maximum_size_ratio:g}x highest/lowest size-tier rate",
            "review",
        ),
    ]


def _risk_warnings(candidate):
    warnings = []
    if _at_most(candidate.get("average_dollar_volume_30d"), 100_000):
        warnings.append("very_low_liquidity")
    if _at_least(candidate.get("annualized_volatility_percent"), 80):
        warnings.append("extreme_volatility")
    if _at_least(candidate.get("maximum_drawdown_percent"), 50):
        warnings.append("severe_drawdown")
    if _at_least(candidate.get("dilution_percent"), 20):
        warnings.append("material_dilution")
    if _at_least(candidate.get("reverse_split_count_1y"), 1):
        warnings.append("reverse_split")
    if candidate["moonshot_confidence"] < 80:
        warnings.append("limited_overall_confidence")
    return warnings


def _factor_text(factor):
    if factor is None:
        return "unavailable"
    return (
        f"{factor['label']}: {_factor_ratio(factor) * 100:.1f}% of factor capacity"
    )


def _factor_ratio(factor):
    maximum = float(factor["max_points"])
    return float(factor["points"]) / maximum if maximum else 0.0


def _distribution(values):
    return {
        "minimum": min(values) if values else None,
        "p25": _percentile(values, 25),
        "median": _percentile(values, 50),
        "p75": _percentile(values, 75),
        "maximum": max(values) if values else None,
    }


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(float(ordered[lower]), 2)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(float(value), 2)


def _validated_calibration_config(config):
    calibration = config.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("Moonshot calibration configuration is missing.")
    required = {
        "minimum_top_25_overlap_percent", "sensitive_rank_range",
        "maximum_sensitive_top_25_candidates", "scenarios",
        "maximum_country_selection_rate_ratio",
        "maximum_size_tier_selection_rate_ratio",
    }
    if required - set(calibration):
        raise ValueError("Moonshot calibration configuration is incomplete.")
    overlap = float(calibration["minimum_top_25_overlap_percent"])
    if not 0 <= overlap <= 100:
        raise ValueError("Moonshot minimum overlap must be between 0 and 100.")
    if float(calibration["sensitive_rank_range"]) < 0:
        raise ValueError("Moonshot sensitive rank range cannot be negative.")
    if int(calibration["maximum_sensitive_top_25_candidates"]) < 0:
        raise ValueError("Moonshot maximum sensitive candidates cannot be negative.")
    for field in (
        "maximum_country_selection_rate_ratio",
        "maximum_size_tier_selection_rate_ratio",
    ):
        if float(calibration[field]) < 1:
            raise ValueError(f"Moonshot {field} must be at least 1.")
    scenarios = calibration["scenarios"]
    if "baseline" not in scenarios or len(scenarios) < 2:
        raise ValueError("Moonshot calibration requires baseline and stress scenarios.")
    valid_upside = set(config["upside_weights"])
    valid_risk = set(config["risk_weights"])
    for name, scenario in scenarios.items():
        for field, valid in (
            ("upside_multipliers", valid_upside),
            ("risk_multipliers", valid_risk),
        ):
            multipliers = scenario.get(field)
            if not isinstance(multipliers, dict) or set(multipliers) - valid:
                raise ValueError(f"Invalid Moonshot {field} for scenario {name}.")
            if any(float(value) <= 0 for value in multipliers.values()):
                raise ValueError("Moonshot scenario multipliers must be positive.")
    return calibration


def _selection_rate_ratio(cohorts):
    rates = [
        float(group["selection_rate_percent"])
        for group in cohorts.values() if group["total"] > 0
    ]
    if not rates:
        return None, "unavailable"
    highest = max(rates)
    lowest = min(rates)
    if lowest == 0:
        return (1.0, "1.0x") if highest == 0 else (None, "unbounded")
    ratio = round(highest / lowest, 2)
    return ratio, f"{ratio}x"


def _gate(name, passed, actual, required, severity):
    return {
        "name": name,
        "passed": bool(passed),
        "actual": actual,
        "required": required,
        "severity": severity,
    }


def _counts(values):
    return dict(sorted(Counter(values).items()))


def _at_least(value, threshold):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= threshold


def _at_most(value, threshold):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= threshold


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


def _atomic_csv(path, fields, rows):
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for source in rows:
                writer.writerow({field: source.get(field) for field in fields})
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
