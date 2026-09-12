"""Common result model for explainable Discovery Score factors."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from numbers import Real


@dataclass(frozen=True)
class FactorResult:
    """Capture one factor's value, score contribution, and explanation."""

    name: str
    raw_value: object
    points: float
    max_points: float
    available: bool
    explanation: str
    data_quality: str = "fresh"
    as_of: str | None = None
    applicable: bool = True
    source_value: object = None

    def to_dict(self) -> dict:
        """Return a JSON-safe representation for reports and checkpoints."""
        result = asdict(self)
        if isinstance(self.raw_value, Real) and not isinstance(
            self.raw_value,
            bool,
        ):
            result["raw_value"] = round(float(self.raw_value), 4)
        if isinstance(self.source_value, Real) and not isinstance(
            self.source_value,
            bool,
        ):
            result["source_value"] = round(float(self.source_value), 4)
        result["points"] = round(float(self.points), 2)
        result["max_points"] = round(float(self.max_points), 2)
        return result


def score_confidence(factors: list[FactorResult]) -> float:
    """Return quality-adjusted configured weight with usable data."""
    total_weight = sum(
        factor.max_points for factor in factors if factor.applicable
    )
    if total_weight <= 0:
        return 0.0
    from src.config import FUNDAMENTAL_DATA_POLICY

    quality_ratios = {
        "fresh": 1.0,
        "capped": 1.0,
        "flagged": 1.0,
        "undated": float(
            FUNDAMENTAL_DATA_POLICY.get("undated_confidence_ratio", 0.75)
        ),
    }
    available_weight = sum(
        factor.max_points * quality_ratios.get(factor.data_quality, 0.0)
        for factor in factors
        if factor.available and factor.applicable
    )
    return round((available_weight / total_weight) * 100, 2)


def serialize_factor_breakdown(factors: list[FactorResult]) -> str:
    """Serialize factors as stable JSON keyed by factor name."""
    return json.dumps(
        {factor.name: factor.to_dict() for factor in factors},
        sort_keys=True,
        separators=(",", ":"),
    )
