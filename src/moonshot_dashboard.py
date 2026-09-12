"""Read-only Moonshot artifact queries for the local research terminal."""

from __future__ import annotations

import json
from pathlib import Path
import re


SELECTED_CLASSIFICATIONS = {
    "priority_research", "asymmetric_watch", "speculative_watch",
}
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")


class MoonshotDashboardStore:
    """Load bounded dashboard views from deterministic Moonshot artifacts."""

    def __init__(self, artifact_directory: Path):
        self.artifact_directory = Path(artifact_directory)
        self._cache = {}

    def candidates(
        self,
        run_id: str,
        classification: str | None = "selected",
        country: str | None = None,
        size_tier: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Return a filtered Moonshot cohort plus calibration summaries."""
        run_id = self._run_id(run_id)
        limit = max(1, min(int(limit), 200))
        analysis = self._document("moonshot_analysis", run_id, required=False)
        if analysis is None:
            return {
                "available": False,
                "run_id": run_id,
                "message": "No Moonshot analysis is available for this run.",
                "total": 0,
                "rows": [],
            }
        calibration = self._document(
            "moonshot_calibration", run_id, required=False,
        )
        baseline = self._baseline(run_id)
        explanations = {
            row["ticker"]: row
            for row in (calibration or {}).get("candidate_explanations", [])
        }
        sensitivities = {
            row["ticker"]: row
            for row in (calibration or {}).get("rank_sensitivity", {}).get(
                "candidates", []
            )
        }
        baseline_lookup = {
            row["ticker"]: row for row in (baseline or {}).get("candidates", [])
        }
        normalized_classification = str(classification or "").strip().lower()
        normalized_country = str(country or "").strip().upper()
        normalized_size = str(size_tier or "").strip().lower()
        needle = str(search or "").strip().upper()
        rows = []
        for candidate in analysis.get("candidates", []):
            candidate_classification = candidate.get("classification")
            if (
                normalized_classification == "selected"
                and candidate_classification not in SELECTED_CLASSIFICATIONS
            ):
                continue
            if (
                normalized_classification not in {"", "selected"}
                and candidate_classification != normalized_classification
            ):
                continue
            if normalized_country and candidate.get("country") != normalized_country:
                continue
            if normalized_size and candidate.get("size_tier") != normalized_size:
                continue
            ticker = str(candidate.get("ticker") or "")
            company = str(candidate.get("company_name") or "")
            if needle and needle not in ticker.upper() and needle not in company.upper():
                continue
            explanation = explanations.get(ticker, {})
            sensitivity = sensitivities.get(ticker, {})
            frozen = baseline_lookup.get(ticker)
            rows.append({
                "moonshot_rank": candidate.get("moonshot_rank"),
                "ticker": ticker,
                "company_name": company or None,
                "country": candidate.get("country"),
                "sector": candidate.get("sector"),
                "size_tier": candidate.get("size_tier"),
                "classification": candidate_classification,
                "upside_score": candidate.get("upside_score"),
                "risk_of_ruin_score": candidate.get("risk_of_ruin_score"),
                "moonshot_confidence": candidate.get("moonshot_confidence"),
                "market_risk_confidence": candidate.get("market_risk_confidence"),
                "average_dollar_volume_30d": candidate.get(
                    "average_dollar_volume_30d"
                ),
                "annualized_volatility_percent": candidate.get(
                    "annualized_volatility_percent"
                ),
                "maximum_drawdown_percent": candidate.get(
                    "maximum_drawdown_percent"
                ),
                "strongest_upside_driver": explanation.get(
                    "strongest_upside_driver"
                ),
                "largest_risk_driver": explanation.get("largest_risk_driver"),
                "risk_warnings": explanation.get(
                    "risk_warnings", _risk_warnings(candidate)
                ),
                "scenario_rank_range": sensitivity.get("rank_range"),
                "forward_tracking_status": (
                    frozen.get("tracking_status") if frozen else "excluded"
                ),
            })
        return {
            "available": True,
            "run_id": run_id,
            "model_version": analysis.get("model_version"),
            "analysis_warning": analysis.get("analysis_warning"),
            "summary": analysis.get("summary", {}),
            "market_evidence_summary": analysis.get("market_evidence_summary", {}),
            "calibration": _calibration_summary(calibration),
            "forward_baseline": _baseline_summary(baseline),
            "total": len(rows),
            "rows": rows[:limit],
        }

    def candidate(self, run_id: str, ticker: str) -> dict:
        """Return factor, scenario, warning, and forward-test detail."""
        run_id = self._run_id(run_id)
        ticker = str(ticker or "").strip().upper()
        if not ticker:
            raise ValueError("Moonshot ticker is required")
        analysis = self._document("moonshot_analysis", run_id)
        calibration = self._document("moonshot_calibration", run_id, required=False)
        baseline = self._baseline(run_id)
        candidate = next(
            (
                row for row in analysis.get("candidates", [])
                if str(row.get("ticker") or "").upper() == ticker
            ),
            None,
        )
        if candidate is None:
            raise ValueError(
                f"Ticker is not present in Moonshot run {run_id}: {ticker}"
            )
        explanation = next(
            (
                row for row in (calibration or {}).get("candidate_explanations", [])
                if row.get("ticker") == ticker
            ),
            None,
        )
        sensitivity = next(
            (
                row for row in (calibration or {}).get(
                    "rank_sensitivity", {}
                ).get("candidates", []) if row.get("ticker") == ticker
            ),
            None,
        )
        frozen = next(
            (
                row for row in (baseline or {}).get("candidates", [])
                if row.get("ticker") == ticker
            ),
            None,
        )
        scenario_rows = {}
        for name, scenario in (calibration or {}).get("scenarios", {}).items():
            row = next(
                (item for item in scenario.get("candidates", [])
                 if item.get("ticker") == ticker),
                None,
            )
            if row:
                scenario_rows[name] = {
                    "rank": row.get("scenario_rank"),
                    "rank_change": row.get("rank_change"),
                    "classification": row.get("classification"),
                    "upside_score": row.get("upside_score"),
                    "risk_of_ruin_score": row.get("risk_of_ruin_score"),
                }
        return {
            "run_id": run_id,
            "model_version": analysis.get("model_version"),
            "candidate": candidate,
            "explanation": explanation or {
                "risk_warnings": _risk_warnings(candidate),
            },
            "rank_sensitivity": sensitivity,
            "scenarios": scenario_rows,
            "forward_baseline": frozen,
            "interpretation_warning": analysis.get("analysis_warning"),
        }

    def _document(self, prefix: str, run_id: str, required: bool = True):
        path = self.artifact_directory / f"{prefix}_{run_id}.json"
        if not path.is_file():
            if required:
                raise FileNotFoundError(f"Moonshot artifact not found: {path.name}")
            return None
        document = self._read(path, "artifact")
        if document.get("run_id") != run_id:
            raise ValueError(f"Moonshot artifact run mismatch: {path.name}")
        return document

    def _baseline(self, run_id: str):
        matches = sorted(self.artifact_directory.glob(
            f"moonshot_forward_baseline_{run_id}_*.json"
        ))
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"Multiple Moonshot baselines found for run: {run_id}")
        document = self._read(matches[0], "baseline")
        if document.get("run_id") != run_id:
            raise ValueError(f"Moonshot baseline run mismatch: {matches[0].name}")
        return document

    def _read(self, path: Path, label: str):
        """Reuse unchanged artifacts while noticing atomic export replacements."""
        metadata = path.stat()
        signature = (metadata.st_mtime_ns, metadata.st_size)
        cached = self._cache.get(path)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid Moonshot {label}: {path.name}") from error
        if not isinstance(document, dict):
            raise ValueError(f"Invalid Moonshot {label}: {path.name}")
        self._cache[path] = (signature, document)
        return document

    @staticmethod
    def _run_id(run_id: str) -> str:
        run_id = str(run_id or "").strip()
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("Invalid Moonshot run ID")
        return run_id


def _calibration_summary(calibration):
    if calibration is None:
        return {"available": False}
    gates = calibration.get("validation_gates", [])
    return {
        "available": True,
        "automated_status": calibration.get("automated_status"),
        "gates_passed": sum(bool(gate.get("passed")) for gate in gates),
        "gates_total": len(gates),
        "validation_gates": gates,
        "scenario_overlaps": calibration.get("scenario_overlaps", {}),
        "rank_sensitivity": {
            key: calibration.get("rank_sensitivity", {}).get(key)
            for key in (
                "median_rank_range", "p90_rank_range", "maximum_rank_range",
            )
        },
        "cohorts": calibration.get("cohorts", {}),
        "outliers": calibration.get("outliers", {}),
        "interpretation_warning": calibration.get("interpretation_warning"),
    }


def _baseline_summary(baseline):
    if baseline is None:
        return {"available": False}
    first = baseline.get("candidates", [{}])[0] if baseline.get("candidates") else {}
    return {
        "available": True,
        "baseline_id": baseline.get("baseline_id"),
        "baseline_as_of": baseline.get("baseline_as_of"),
        "candidate_count": baseline.get("candidate_count"),
        "excluded_candidates": baseline.get("excluded_candidates", []),
        "horizons": first.get("horizons", {}),
        "interpretation_warning": baseline.get("interpretation_warning"),
    }


def _risk_warnings(candidate):
    warnings = []
    rules = (
        ("very_low_liquidity", "average_dollar_volume_30d", lambda value: value <= 100_000),
        ("extreme_volatility", "annualized_volatility_percent", lambda value: value >= 80),
        ("severe_drawdown", "maximum_drawdown_percent", lambda value: value >= 50),
        ("material_dilution", "dilution_percent", lambda value: value >= 20),
        ("reverse_split", "reverse_split_count_1y", lambda value: value >= 1),
    )
    for label, field, check in rules:
        value = candidate.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and check(value):
            warnings.append(label)
    if (candidate.get("moonshot_confidence") or 0) < 80:
        warnings.append("limited_overall_confidence")
    return warnings
