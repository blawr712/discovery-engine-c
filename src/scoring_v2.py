"""Cross-sectional composite scoring shared by Score v2 and Score v3.

Both shadow models run beside the official Discovery Score without changing
it. Each company first receives point-in-time raw signals: price signals are
computed here from its own price history, and fundamental ``pit_*`` signals
come from ``src.fundamentals_pit``. A cross-sectional pass then converts every
configured signal into a percentile rank across the run's successful
candidates (within sector when the group is large enough), applies optional
percentile-based exclusion filters, and blends the percentiles into a 0-100
score with a per-signal explanation. Both stages are pure functions so the
backtest applies exactly the same logic at historical month-ends.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from src.config import SCORING_V2_CONFIG, SCORING_V3_CONFIG
from src.factors import FactorResult, score_confidence
from src.fundamentals_pit import SIGNAL_NAMES as FUNDAMENTAL_SIGNAL_NAMES
from src.insider_signals import SIGNAL_NAMES as INSIDER_SIGNAL_NAMES


RAW_PREFIX = "v2_"

DEFAULT_LOOKBACKS = {
    "long_window": 240,
    "medium_window": 126,
    "skip_window": 21,
    "reversal_window": 21,
    "volatility_window": 63,
    "volume_short_window": 21,
    "volume_long_window": 240,
    "high_window": 240,
}

PRICE_SIGNALS = (
    "momentum_long",
    "momentum_medium",
    "short_term_reversal",
    "high_proximity",
    "volatility",
    "volume_trend",
)

DEFAULT_CONFIG = {
    "model_version": "v2.0-shadow",
    "output_prefix": "score_v2",
    "minimum_price_history_days": 200,
    "sector_neutral": True,
    "minimum_sector_group": 20,
    "minimum_confidence": 0,
    "lookbacks": DEFAULT_LOOKBACKS,
    "exclusions": [],
    "sector_exclusions": {},
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
    "pit_revenue_growth_ttm": "TTM revenue growth",
    "pit_revenue_acceleration": "Revenue acceleration",
    "pit_gross_margin_ttm": "TTM gross margin",
    "pit_gross_margin_change": "Gross margin change",
    "pit_operating_margin_ttm": "TTM operating margin",
    "pit_ocf_margin_ttm": "TTM operating cash flow margin",
    "pit_fcf_margin_ttm": "TTM free cash flow margin",
    "pit_cash_conversion": "Cash conversion",
    "pit_share_change_1y": "One-year share count change",
    "pit_net_cash_to_market_cap": "Net cash to market cap",
    "pit_fcf_yield": "Free cash flow yield",
    "pit_earnings_yield": "Earnings yield",
    "pit_sales_yield": "Sales yield",
    "ins_purchase_count_short": "Insider open-market purchases (short window)",
    "ins_sale_count_short": "Insider open-market sales (short window)",
    "ins_net_count_short": "Net insider purchases less sales (short window)",
    "ins_distinct_buyers_short": "Distinct insider buyers (short window)",
    "ins_officer_purchase_count_short": "Officer and director purchases (short window)",
    "ins_purchase_value_short": "Insider purchase value (short window)",
    "ins_net_value_short": "Net insider purchase value (short window)",
    "ins_net_value_to_market_cap_short": "Net insider buying to market cap (short window)",
    "ins_cluster_buy_short": "Cluster buying by two or more insiders (short window)",
    "ins_purchase_count_long": "Insider open-market purchases (long window)",
    "ins_purchase_value_long": "Insider purchase value (long window)",
    "ins_net_value_long": "Net insider purchase value (long window)",
    "ins_net_value_to_market_cap_long": "Net insider buying to market cap (long window)",
    "ins_days_since_last_purchase": "Days since last insider purchase",
}


def raw_column(name: str) -> str:
    """Return the result-row column holding a signal's raw value."""
    if name.startswith("pit_") or name.startswith("ins_"):
        return name
    return f"{RAW_PREFIX}{name}"


def model_configs() -> list[dict]:
    """Return every configured shadow model, v2 first, validated."""
    configs = [validated_config(SCORING_V2_CONFIG)]
    if SCORING_V3_CONFIG:
        configs.append(validated_config(SCORING_V3_CONFIG))
    return configs


def compute_raw_signals(
    price_history: pd.DataFrame,
    config: dict | None = None,
) -> dict:
    """Return point-in-time raw price signals from one company's history.

    With no config every price signal is computed using the v2 lookbacks so
    any model can consume the result; with a config only its price signals
    are emitted.
    """
    config = validated_config(config)
    names = (
        [name for name in config["signals"] if name in PRICE_SIGNALS]
        if config is not None and config.get("_explicit")
        else list(PRICE_SIGNALS)
    )
    lookbacks = config["lookbacks"]
    minimum_days = config["minimum_price_history_days"]
    signals = {raw_column(name): None for name in names}

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
    for name in names:
        value = calculators[name]()
        signals[raw_column(name)] = (
            round(value, 6) if value is not None else None
        )
    return signals


def score_frame(frame: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Score one cross-section of rows holding raw signals and sectors.

    Returns a frame aligned to ``frame.index`` with ``score``, ``confidence``,
    ``excluded``, ``exclusion_reasons``, one ``pct_<signal>`` column per
    signal, and one ``group_<signal>`` column naming the ranking group used.
    """
    config = validated_config(config)
    signals = config["signals"]
    output = pd.DataFrame(index=frame.index)
    sectors = (
        frame["sector"].astype("string").fillna("")
        if "sector" in frame
        else pd.Series("", index=frame.index, dtype="string")
    )

    excluded = pd.Series(False, index=frame.index)
    reasons = pd.Series([[] for _ in range(len(frame))], index=frame.index, dtype=object)
    for rule in config["exclusions"]:
        values = _numeric_column(frame, raw_column(rule["signal"]))
        # A filter never judges a company on a signal that does not apply
        # to its sector.
        values = values.mask(_not_applicable(sectors, rule["signal"], config))
        valid = values.notna()
        if not valid.any():
            continue
        percentiles = _rank_percent(values[valid])
        hit = pd.Series(False, index=frame.index)
        below = rule.get("exclude_below_percentile")
        above = rule.get("exclude_above_percentile")
        if below is not None:
            hit[valid] |= percentiles < float(below)
        if above is not None:
            hit[valid] |= percentiles > float(above)
        excluded |= hit
        for index in frame.index[hit]:
            reasons.at[index].append(rule["reason"])

    weighted_sum = pd.Series(0.0, index=frame.index)
    available_weight = pd.Series(0.0, index=frame.index)
    total_weight = float(sum(spec["weight"] for spec in signals.values()))

    for name, spec in signals.items():
        values = _numeric_column(frame, raw_column(name))
        not_applicable = _not_applicable(sectors, name, config)
        values = values.mask(not_applicable)
        percentiles, groups = _percentiles(
            values,
            sectors,
            direction=spec["direction"],
            sector_neutral=config["sector_neutral"],
            minimum_group=config["minimum_sector_group"],
        )
        output[f"pct_{name}"] = percentiles
        output[f"group_{name}"] = groups.mask(not_applicable, "not_applicable")
        usable = percentiles.notna()
        weighted_sum[usable] += percentiles[usable] * spec["weight"]
        available_weight[usable] += spec["weight"]

    confidence = (
        (available_weight / total_weight * 100.0).round(2)
        if total_weight > 0
        else pd.Series(0.0, index=frame.index)
    )
    scored = (available_weight > 0) & ~excluded & (
        confidence >= float(config["minimum_confidence"])
    )
    score = pd.Series(np.nan, index=frame.index, dtype=float)
    score[scored] = (weighted_sum[scored] / available_weight[scored]).round(2)
    output["score"] = score
    output["confidence"] = confidence
    output["excluded"] = excluded
    output["exclusion_reasons"] = reasons.map("; ".join)
    return output


def apply_cross_sectional_scores(
    results: list[dict],
    config: dict | None = None,
) -> list[dict]:
    """Attach one shadow model's fields to successful rows of one run.

    Rows are copied; official Discovery Score fields are never modified.
    """
    config = validated_config(config)
    prefix = config["output_prefix"]
    updated = [dict(row) for row in results]
    indices = [
        index for index, row in enumerate(updated) if row.get("status") == "OK"
    ]
    if not indices:
        return updated

    columns = {"sector"} | {raw_column(name) for name in config["signals"]}
    columns |= {raw_column(rule["signal"]) for rule in config["exclusions"]}
    frame = pd.DataFrame(
        [
            {column: updated[index].get(column) for column in columns}
            for index in indices
        ],
        index=indices,
    )
    scored = score_frame(frame, config)
    ranks = (
        scored["score"]
        .rank(ascending=False, method="min")
        .where(scored["score"].notna())
    )

    for index in indices:
        row = updated[index]
        factors = _factor_results(row, scored.loc[index], config)
        score = scored.at[index, "score"]
        rank = ranks.at[index]
        row[prefix] = _finite(score)
        row[f"{prefix}_confidence"] = _finite(scored.at[index, "confidence"])
        row[f"{prefix}_rank"] = int(rank) if not pd.isna(rank) else None
        row[f"{prefix}_excluded"] = bool(scored.at[index, "excluded"])
        row[f"{prefix}_exclusion_reasons"] = scored.at[index, "exclusion_reasons"]
        row[f"{prefix}_breakdown"] = json.dumps(
            {factor.name: factor.to_dict() for factor in factors},
            sort_keys=True,
            separators=(",", ":"),
        )
        row[f"{prefix}_model_version"] = config["model_version"]
    return updated


def apply_all_models(results: list[dict]) -> list[dict]:
    """Apply every configured shadow model in order."""
    for config in model_configs():
        results = apply_cross_sectional_scores(results, config)
    return results


def validated_config(config: dict | None = None) -> dict:
    """Merge a model's settings over defaults and validate them."""
    if config is not None and config.get("_validated"):
        return config
    explicit = config is not None
    source = config if config is not None else SCORING_V2_CONFIG
    merged = {
        **DEFAULT_CONFIG,
        **source,
        "lookbacks": {**DEFAULT_LOOKBACKS, **source.get("lookbacks", {})},
        "signals": source.get("signals", DEFAULT_CONFIG["signals"]),
        "exclusions": source.get("exclusions", []),
        "sector_exclusions": source.get("sector_exclusions", {}),
    }
    for name, value in merged["lookbacks"].items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Score lookback {name!r} must be a positive integer.")
    minimum = merged["minimum_price_history_days"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
        raise ValueError("Score minimum_price_history_days must be at least 2.")
    group = merged["minimum_sector_group"]
    if isinstance(group, bool) or not isinstance(group, int) or group < 2:
        raise ValueError("Score minimum_sector_group must be at least 2.")
    minimum_confidence = merged["minimum_confidence"]
    if (
        isinstance(minimum_confidence, bool)
        or not isinstance(minimum_confidence, (int, float))
        or not 0 <= minimum_confidence <= 100
    ):
        raise ValueError("Score minimum_confidence must be between 0 and 100.")
    signals = merged["signals"]
    if not isinstance(signals, dict) or not signals:
        raise ValueError("Score signals must be a non-empty mapping.")
    unknown = (
        set(signals) - set(PRICE_SIGNALS) - set(FUNDAMENTAL_SIGNAL_NAMES)
        - set(INSIDER_SIGNAL_NAMES)
    )
    if unknown:
        raise ValueError(f"Unknown score signals: {sorted(unknown)}")
    validated_signals = {}
    for name, spec in signals.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Score signal {name!r} must be a mapping.")
        weight = spec.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise ValueError(f"Score signal {name!r} needs a positive weight.")
        direction = str(spec.get("direction", "higher"))
        if direction not in {"higher", "lower"}:
            raise ValueError(f"Score signal {name!r} direction must be higher or lower.")
        validated_signals[name] = {"weight": float(weight), "direction": direction}
    merged["signals"] = validated_signals
    exclusions = merged["exclusions"]
    if not isinstance(exclusions, list):
        raise ValueError("Score exclusions must be a list.")
    validated_exclusions = []
    for rule in exclusions:
        if not isinstance(rule, dict) or "signal" not in rule:
            raise ValueError("Each score exclusion needs a signal.")
        signal = str(rule["signal"])
        if (
            signal not in PRICE_SIGNALS
            and signal not in FUNDAMENTAL_SIGNAL_NAMES
            and signal not in INSIDER_SIGNAL_NAMES
        ):
            raise ValueError(f"Unknown exclusion signal {signal!r}.")
        below = rule.get("exclude_below_percentile")
        above = rule.get("exclude_above_percentile")
        if below is None and above is None:
            raise ValueError(f"Exclusion for {signal!r} needs a percentile bound.")
        for bound in (below, above):
            if bound is not None and (
                isinstance(bound, bool)
                or not isinstance(bound, (int, float))
                or not 0 <= bound <= 100
            ):
                raise ValueError(f"Exclusion bound for {signal!r} must be 0-100.")
        validated_exclusions.append({
            "signal": signal,
            "exclude_below_percentile": below,
            "exclude_above_percentile": above,
            "reason": str(rule.get("reason") or f"Excluded on {signal}"),
        })
    merged["exclusions"] = validated_exclusions
    sector_exclusions = merged["sector_exclusions"]
    if not isinstance(sector_exclusions, dict):
        raise ValueError("Score sector_exclusions must map sector names to signal lists.")
    known = set(PRICE_SIGNALS) | set(FUNDAMENTAL_SIGNAL_NAMES) | set(INSIDER_SIGNAL_NAMES)
    validated_sectors = {}
    for sector, names in sector_exclusions.items():
        if not isinstance(names, list) or any(str(name) not in known for name in names):
            raise ValueError(f"Sector exclusion for {sector!r} lists an unknown signal.")
        validated_sectors[str(sector)] = [str(name) for name in names]
    merged["sector_exclusions"] = validated_sectors
    merged["sector_neutral"] = bool(merged["sector_neutral"])
    merged["model_version"] = str(merged["model_version"])
    merged["output_prefix"] = str(merged["output_prefix"])
    merged["_explicit"] = explicit
    merged["_validated"] = True
    return merged


def _not_applicable(sectors: pd.Series, signal: str, config: dict) -> pd.Series:
    """Boolean mask of rows whose sector excludes ``signal``."""
    excluded_sectors = [
        sector for sector, names in config["sector_exclusions"].items()
        if signal in names
    ]
    if not excluded_sectors:
        return pd.Series(False, index=sectors.index)
    return sectors.isin(excluded_sectors).fillna(False).astype(bool)


def _numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


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
        raw = row.get(raw_column(name))
        percentile = scored.get(f"pct_{name}")
        group = scored.get(f"group_{name}")
        available = percentile is not None and not pd.isna(percentile)
        applicable = group != "not_applicable"
        weight = spec["weight"]
        points = (float(percentile) / 100.0 * weight) if available else 0.0
        if not applicable:
            explanation = (
                f"{SIGNAL_LABELS.get(name, name)} not applicable to "
                f"{row.get('sector') or 'this sector'}"
            )
        elif available:
            explanation = (
                f"{_describe(name, raw, lookbacks)}; ranks at the "
                f"{float(percentile):.0f}th percentile within {group}"
                + (" (lower is better)" if spec["direction"] == "lower" else "")
            )
        else:
            explanation = f"{SIGNAL_LABELS.get(name, name)} unavailable"
        factors.append(FactorResult(
            name=name,
            raw_value=raw,
            points=round(points, 2),
            max_points=weight,
            available=available,
            explanation=explanation,
            data_quality=(
                "not_applicable" if not applicable
                else "fresh" if available else "missing"
            ),
            applicable=applicable,
        ))
    return factors


def _describe(name: str, raw: object, lookbacks: dict) -> str:
    value = _finite(raw)
    label = SIGNAL_LABELS.get(name, name)
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
    if name == "pit_cash_conversion":
        return f"{label} is {value:.2f}x"
    if name.startswith("ins_") and "to_market_cap" in name:
        return f"{label} is {value:.2%} from Form 4 filings public on the scoring date"
    if name.startswith("ins_"):
        return f"{label} is {value:,.0f} from Form 4 filings public on the scoring date"
    if name.startswith("pit_"):
        return f"{label} is {value:.1%} from filings available on the scoring date"
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
