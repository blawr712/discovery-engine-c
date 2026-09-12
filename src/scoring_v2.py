"""Shadow Score v2: continuous, cross-sectional technical ranking.

Score v2 runs beside the official Discovery Score without changing it. Each
company first receives point-in-time raw signals computed only from its own
price history. A separate cross-sectional pass then converts every signal
into a percentile rank across the run's successful candidates, optionally
within sector, and blends the percentiles into a 0-100 score with a
per-signal explanation. Both stages are pure functions so the backtest can
apply exactly the same logic at historical month-ends.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from src.config import SCORING_V2_CONFIG
from src.factors import FactorResult, score_confidence


RAW_PREFIX = "v2_"

DEFAULT_CONFIG = {
    "model_version": "v2.0-shadow",
    "minimum_price_history_days": 200,
    "sector_neutral": True,
    "minimum_sector_group": 20,
    "lookbacks": {
        "long_window": 240,
        "medium_window": 126,
        "skip_window": 21,
        "reversal_window": 21,
        "volatility_window": 63,
        "volume_short_window": 21,
        "volume_long_window": 240,
        "high_window": 240,
    },
    "signals": {
        "momentum_long": {"weight": 30, "direction": "higher"},
        "high_proximity": {"weight": 20, "direction": "higher"},
        "momentum_medium": {"weight": 15, "direction": "higher"},
        "volatility": {"weight": 15, "direction": "lower"},
        "volume_trend": {"weight": 10, "direction": "higher"},
        "short_term_reversal": {"weight": 10, "direction": "lower"},
    },
}

SIGNAL_LABELS = {
    "momentum_long": "Long momentum",
    "momentum_medium": "Medium momentum",
    "high_proximity": "Proximity to trailing high",
    "volatility": "Annualized volatility",
    "volume_trend": "Volume trend",
    "short_term_reversal": "One-month return",
}


def compute_raw_signals(
    price_history: pd.DataFrame,
    config: dict | None = None,
) -> dict:
    """Return point-in-time raw v2 signals from one company's price history."""
    config = validated_config(config)
    lookbacks = config["lookbacks"]
    minimum_days = config["minimum_price_history_days"]
    signals = {f"{RAW_PREFIX}{name}": None for name in config["signals"]}

    if (
        price_history is None
        or price_history.empty
        or "Close" not in price_history
        or len(price_history) < minimum_days
    ):
        return signals

    close = pd.to_numeric(price_history["Close"], errors="coerce").to_numpy(
        dtype=float,
    )
    volume = (
        pd.to_numeric(price_history["Volume"], errors="coerce").to_numpy(
            dtype=float,
        )
        if "Volume" in price_history
        else np.full(len(close), np.nan)
    )
    if not math.isfinite(close[-1]) or close[-1] <= 0:
        return signals

    skip = lookbacks["skip_window"]
    calculators = {
        "momentum_long": lambda: _window_return(
            close, lookbacks["long_window"], skip,
        ),
        "momentum_medium": lambda: _window_return(
            close, lookbacks["medium_window"], skip,
        ),
        "short_term_reversal": lambda: _window_return(
            close, lookbacks["reversal_window"], 0,
        ),
        "high_proximity": lambda: _high_proximity(
            close, lookbacks["high_window"],
        ),
        "volatility": lambda: _annualized_volatility(
            close, lookbacks["volatility_window"],
        ),
        "volume_trend": lambda: _volume_trend(
            volume,
            lookbacks["volume_short_window"],
            lookbacks["volume_long_window"],
        ),
    }
    for name in config["signals"]:
        value = calculators[name]()
        signals[f"{RAW_PREFIX}{name}"] = (
            round(value, 6) if value is not None else None
        )
    return signals


def score_frame(frame: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Score one cross-section of rows holding raw v2 signals and sectors.

    Returns a frame aligned to ``frame.index`` with ``score_v2``,
    ``score_v2_confidence``, one ``pct_<signal>`` column per signal, and one
    ``group_<signal>`` column naming the ranking group used.
    """
    config = validated_config(config)
    signals = config["signals"]
    output = pd.DataFrame(index=frame.index)
    sectors = (
        frame["sector"].astype("string").fillna("")
        if "sector" in frame
        else pd.Series("", index=frame.index, dtype="string")
    )

    weighted_sum = pd.Series(0.0, index=frame.index)
    available_weight = pd.Series(0.0, index=frame.index)
    total_weight = float(sum(spec["weight"] for spec in signals.values()))

    for name, spec in signals.items():
        column = f"{RAW_PREFIX}{name}"
        values = (
            pd.to_numeric(frame[column], errors="coerce")
            if column in frame
            else pd.Series(np.nan, index=frame.index, dtype=float)
        )
        percentiles, groups = _percentiles(
            values,
            sectors,
            direction=spec["direction"],
            sector_neutral=config["sector_neutral"],
            minimum_group=config["minimum_sector_group"],
        )
        output[f"pct_{name}"] = percentiles
        output[f"group_{name}"] = groups
        usable = percentiles.notna()
        weighted_sum[usable] += percentiles[usable] * spec["weight"]
        available_weight[usable] += spec["weight"]

    scored = available_weight > 0
    output["score_v2"] = np.where(
        scored, weighted_sum / available_weight.where(scored, np.nan), np.nan,
    )
    output["score_v2"] = output["score_v2"].round(2)
    output["score_v2_confidence"] = (
        (available_weight / total_weight * 100.0).round(2)
        if total_weight > 0
        else 0.0
    )
    return output


def apply_cross_sectional_scores(
    results: list[dict],
    config: dict | None = None,
) -> list[dict]:
    """Attach shadow Score v2 fields to successful rows of one run.

    Rows are copied; official Discovery Score fields are never modified.
    """
    config = validated_config(config)
    updated = [dict(row) for row in results]
    indices = [
        index for index, row in enumerate(updated) if row.get("status") == "OK"
    ]
    if not indices:
        return updated

    frame = pd.DataFrame(
        [
            {
                "sector": updated[index].get("sector"),
                **{
                    f"{RAW_PREFIX}{name}": updated[index].get(
                        f"{RAW_PREFIX}{name}"
                    )
                    for name in config["signals"]
                },
            }
            for index in indices
        ],
        index=indices,
    )
    scored = score_frame(frame, config)
    ranks = (
        scored["score_v2"]
        .rank(ascending=False, method="min")
        .where(scored["score_v2"].notna())
    )

    for index in indices:
        row = updated[index]
        factors = _factor_results(row, scored.loc[index], config)
        score = scored.at[index, "score_v2"]
        rank = ranks.at[index]
        row["score_v2"] = _finite(score)
        row["score_v2_confidence"] = score_confidence(factors)
        row["score_v2_rank"] = int(rank) if not pd.isna(rank) else None
        row["score_v2_breakdown"] = json.dumps(
            {factor.name: factor.to_dict() for factor in factors},
            sort_keys=True,
            separators=(",", ":"),
        )
        row["score_v2_model_version"] = config["model_version"]
    return updated


def validated_config(config: dict | None = None) -> dict:
    """Merge configured Score v2 settings over defaults and validate them."""
    source = config if config is not None else SCORING_V2_CONFIG
    merged = {
        **DEFAULT_CONFIG,
        **source,
        "lookbacks": {**DEFAULT_CONFIG["lookbacks"], **source.get("lookbacks", {})},
        "signals": source.get("signals", DEFAULT_CONFIG["signals"]),
    }
    for name, value in merged["lookbacks"].items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Score v2 lookback {name!r} must be a positive integer.")
    minimum = merged["minimum_price_history_days"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
        raise ValueError("Score v2 minimum_price_history_days must be at least 2.")
    group = merged["minimum_sector_group"]
    if isinstance(group, bool) or not isinstance(group, int) or group < 2:
        raise ValueError("Score v2 minimum_sector_group must be at least 2.")
    signals = merged["signals"]
    if not isinstance(signals, dict) or not signals:
        raise ValueError("Score v2 signals must be a non-empty mapping.")
    unknown = set(signals) - set(DEFAULT_CONFIG["signals"])
    if unknown:
        raise ValueError(f"Unknown Score v2 signals: {sorted(unknown)}")
    validated_signals = {}
    for name, spec in signals.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Score v2 signal {name!r} must be a mapping.")
        weight = spec.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise ValueError(f"Score v2 signal {name!r} needs a positive weight.")
        direction = str(spec.get("direction", "higher"))
        if direction not in {"higher", "lower"}:
            raise ValueError(f"Score v2 signal {name!r} direction must be higher or lower.")
        validated_signals[name] = {"weight": float(weight), "direction": direction}
    merged["signals"] = validated_signals
    merged["sector_neutral"] = bool(merged["sector_neutral"])
    merged["model_version"] = str(merged["model_version"])
    return merged


def _percentiles(
    values: pd.Series,
    sectors: pd.Series,
    *,
    direction: str,
    sector_neutral: bool,
    minimum_group: int,
) -> tuple[pd.Series, pd.Series]:
    """Rank values 0-100 within sector when the group is large enough."""
    percentiles = pd.Series(np.nan, index=values.index, dtype=float)
    groups = pd.Series("unavailable", index=values.index, dtype=object)
    valid = values.notna()
    if not valid.any():
        return percentiles, groups

    universe = _rank_percent(values[valid])
    percentiles[valid] = universe
    groups[valid] = "universe"

    if sector_neutral:
        for sector, members in values[valid].groupby(sectors[valid]):
            if not sector or len(members) < minimum_group:
                continue
            percentiles[members.index] = _rank_percent(members)
            groups[members.index] = f"sector:{sector}"

    if direction == "lower":
        percentiles[valid] = 100.0 - percentiles[valid]
    return percentiles.round(2), groups


def _rank_percent(values: pd.Series) -> pd.Series:
    count = len(values)
    if count == 1:
        return pd.Series(50.0, index=values.index)
    ranks = values.rank(method="average")
    return (ranks - 1.0) / (count - 1.0) * 100.0


def _factor_results(row: dict, scored: pd.Series, config: dict) -> list[FactorResult]:
    factors = []
    lookbacks = config["lookbacks"]
    for name, spec in config["signals"].items():
        raw = row.get(f"{RAW_PREFIX}{name}")
        percentile = scored.get(f"pct_{name}")
        group = scored.get(f"group_{name}")
        available = percentile is not None and not pd.isna(percentile)
        weight = spec["weight"]
        points = (float(percentile) / 100.0 * weight) if available else 0.0
        if available:
            explanation = (
                f"{_describe(name, raw, lookbacks)}; ranks at the "
                f"{float(percentile):.0f}th percentile within {group}"
                + (" (lower is better)" if spec["direction"] == "lower" else "")
            )
        else:
            explanation = f"{SIGNAL_LABELS[name]} unavailable"
        factors.append(FactorResult(
            name=name,
            raw_value=raw,
            points=round(points, 2),
            max_points=weight,
            available=available,
            explanation=explanation,
            data_quality="fresh" if available else "missing",
        ))
    return factors


def _describe(name: str, raw: object, lookbacks: dict) -> str:
    value = _finite(raw)
    label = SIGNAL_LABELS[name]
    if value is None:
        return f"{label} unavailable"
    if name == "momentum_long":
        return (
            f"{label} is {value:.1%} from {lookbacks['long_window']} to "
            f"{lookbacks['skip_window']} sessions ago"
        )
    if name == "momentum_medium":
        return (
            f"{label} is {value:.1%} from {lookbacks['medium_window']} to "
            f"{lookbacks['skip_window']} sessions ago"
        )
    if name == "short_term_reversal":
        return f"{label} is {value:.1%} over {lookbacks['reversal_window']} sessions"
    if name == "high_proximity":
        return (
            f"Close is {value:.1%} of the {lookbacks['high_window']}-session high"
        )
    if name == "volatility":
        return (
            f"{label} is {value:.1%} over {lookbacks['volatility_window']} sessions"
        )
    if name == "volume_trend":
        return (
            f"{lookbacks['volume_short_window']}-session volume is {value:.2f}x "
            f"the {lookbacks['volume_long_window']}-session average"
        )
    return f"{label} is {value:.4f}"


def _window_return(close: np.ndarray, window: int, skip: int) -> float | None:
    """Return close[-1-skip] / close[-1-window] - 1 when both are usable."""
    if window <= skip or len(close) <= window:
        return None
    end = close[-1 - skip]
    start = close[-1 - window]
    if not (math.isfinite(start) and math.isfinite(end)) or start <= 0 or end <= 0:
        return None
    return float(end / start - 1.0)


def _high_proximity(close: np.ndarray, window: int) -> float | None:
    if len(close) < window:
        return None
    trailing = close[-window:]
    trailing = trailing[np.isfinite(trailing)]
    if len(trailing) == 0:
        return None
    high = float(trailing.max())
    if high <= 0:
        return None
    return float(close[-1] / high)


def _annualized_volatility(close: np.ndarray, window: int) -> float | None:
    if len(close) <= window:
        return None
    segment = close[-(window + 1):]
    if not np.all(np.isfinite(segment)) or np.any(segment <= 0):
        return None
    returns = np.diff(np.log(segment))
    if len(returns) < 2:
        return None
    return float(returns.std(ddof=1) * math.sqrt(252.0))


def _volume_trend(volume: np.ndarray, short: int, long: int) -> float | None:
    if short >= long or len(volume) < long:
        return None
    recent = volume[-short:]
    baseline = volume[-long:]
    recent = recent[np.isfinite(recent)]
    baseline = baseline[np.isfinite(baseline)]
    if len(recent) == 0 or len(baseline) == 0:
        return None
    baseline_mean = float(baseline.mean())
    if baseline_mean <= 0:
        return None
    return float(recent.mean() / baseline_mean)


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
