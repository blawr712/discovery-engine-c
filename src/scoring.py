import numpy as np
import pandas as pd

from src.factors import (
    FactorResult,
    score_confidence,
    serialize_factor_breakdown,
)
from src.fundamental_scoring import calculate_fundamental_scores

from src.config import (
    SWEET_SPOT_MIN,
    SWEET_SPOT_MAX,
    MIN_MARKET_CAP,
    MAX_MARKET_CAP,
    SECTOR_BONUSES,
    WEIGHTS,
    SCORING_CONFIG,
)


def calculate_scores(
    stock_data: dict,
    price_history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
) -> dict:
    market_cap = stock_data.get("market_cap")
    sector = stock_data.get("sector")

    minimum_days = SCORING_CONFIG["minimum_price_history_days"]

    if price_history.empty or len(price_history) < minimum_days:
        return _failed_score(stock_data, "Insufficient price history")

    if market_cap is None:
        return _failed_score(stock_data, "Missing market cap")

    if market_cap < MIN_MARKET_CAP or market_cap > MAX_MARKET_CAP:
        return _failed_score(stock_data, "Outside market cap range")

    volume_score, volume_ratio = score_volume_acceleration(price_history)
    relative_strength_score, relative_strength = score_relative_strength(
        price_history,
        benchmark_history,
    )
    trend_score = score_trend_strength(price_history)
    market_cap_score = score_market_cap(market_cap)
    sector_score = score_sector_bonus(sector)
    liquidity_score = score_liquidity(price_history)

    factors = _build_factor_results(
        volume_score=volume_score,
        volume_ratio=volume_ratio,
        relative_strength_score=relative_strength_score,
        relative_strength=relative_strength,
        trend_score=trend_score,
        market_cap_score=market_cap_score,
        market_cap=market_cap,
        sector_score=sector_score,
        sector=sector,
        liquidity_score=liquidity_score,
        price_history=price_history,
    )

    discovery_score = (
        volume_score
        + relative_strength_score
        + trend_score
        + market_cap_score
        + sector_score
        + liquidity_score
    )

    reason_flags = build_reason_flags(
        volume_ratio=volume_ratio,
        relative_strength=relative_strength,
        market_cap=market_cap,
        trend_score=trend_score,
        sector=sector,
    )

    fundamental_scores = calculate_fundamental_scores(stock_data)

    return {
        **stock_data,
        **fundamental_scores,
        "volume_score": volume_score,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "relative_strength_score": relative_strength_score,
        "relative_strength_6m": round(relative_strength, 2)
        if relative_strength is not None
        else None,
        "trend_score": trend_score,
        "market_cap_score": market_cap_score,
        "sector_score": sector_score,
        "liquidity_score": liquidity_score,
        "discovery_score": round(discovery_score, 2),
        "score_confidence": score_confidence(factors),
        "factor_breakdown": serialize_factor_breakdown(factors),
        "reason_flags": "; ".join(reason_flags),
        "status": "OK",
    }


def score_volume_acceleration(df: pd.DataFrame) -> tuple[float, float | None]:
    short_window = SCORING_CONFIG["volume_short_window"]
    long_window = SCORING_CONFIG["volume_long_window"]
    max_points = WEIGHTS["volume_acceleration"]

    if len(df) < long_window:
        return 0, None

    avg_short = df["Volume"].tail(short_window).mean()
    avg_long = df["Volume"].tail(long_window).mean()

    if avg_long == 0 or np.isnan(avg_long):
        return 0, None

    ratio = avg_short / avg_long

    if ratio >= 3:
        return max_points, ratio
    if ratio >= 2:
        return max_points * 0.80, ratio
    if ratio >= 1.5:
        return max_points * 0.56, ratio
    if ratio >= 1.2:
        return max_points * 0.32, ratio
    return 0, ratio


def score_relative_strength(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
) -> tuple[float, float | None]:
    days = SCORING_CONFIG["relative_strength_days"]
    max_points = WEIGHTS["relative_strength"]

    if benchmark_df.empty or len(benchmark_df) < days:
        return 0, None

    stock_return = _period_return(stock_df, days)
    benchmark_return = _period_return(benchmark_df, days)

    if stock_return is None or benchmark_return is None:
        return 0, None

    relative_strength = stock_return - benchmark_return

    if relative_strength >= 50:
        return max_points, relative_strength
    if relative_strength >= 30:
        return max_points * 0.80, relative_strength
    if relative_strength >= 15:
        return max_points * 0.56, relative_strength
    if relative_strength >= 0:
        return max_points * 0.32, relative_strength
    return 0, relative_strength


def score_trend_strength(df: pd.DataFrame) -> float:
    short_ma = SCORING_CONFIG["moving_average_short"]
    long_ma = SCORING_CONFIG["moving_average_long"]
    max_points = WEIGHTS["trend_strength"]

    close = df["Close"]

    if len(close) < long_ma:
        return 0

    latest = close.iloc[-1]
    sma_short = close.tail(short_ma).mean()
    sma_long = close.tail(long_ma).mean()

    if latest > sma_short and latest > sma_long:
        return max_points
    if latest > sma_short or latest > sma_long:
        return max_points * 0.53
    return 0


def score_market_cap(market_cap: int | float) -> float:
    max_points = WEIGHTS["market_cap"]

    if SWEET_SPOT_MIN <= market_cap <= SWEET_SPOT_MAX:
        return max_points

    if 25_000_000 <= market_cap < SWEET_SPOT_MIN:
        return max_points * 0.53

    if SWEET_SPOT_MAX < market_cap <= MAX_MARKET_CAP:
        return max_points * 0.53

    if MIN_MARKET_CAP <= market_cap < 25_000_000:
        return max_points * 0.20

    return 0


def score_sector_bonus(sector: str | None) -> float:
    max_points = WEIGHTS["sector_bonus"]

    if not sector:
        return 0

    for key, score in SECTOR_BONUSES.items():
        if key.lower() in sector.lower():
            return min(score, max_points)

    return 0


def score_liquidity(df: pd.DataFrame) -> float:
    max_points = WEIGHTS["liquidity"]
    avg_dollar_volume = _average_dollar_volume(df)

    if avg_dollar_volume >= 5_000_000:
        return max_points
    if avg_dollar_volume >= 1_000_000:
        return max_points * 0.70
    if avg_dollar_volume >= 250_000:
        return max_points * 0.40
    return 0


def _build_factor_results(
    volume_score: float,
    volume_ratio: float | None,
    relative_strength_score: float,
    relative_strength: float | None,
    trend_score: float,
    market_cap_score: float,
    market_cap: int | float,
    sector_score: float,
    sector: str | None,
    liquidity_score: float,
    price_history: pd.DataFrame,
) -> list[FactorResult]:
    """Describe the existing v0.2 score through the v0.3 factor model."""
    trend_max = WEIGHTS["trend_strength"]
    trend_label = (
        "strong"
        if trend_score == trend_max
        else "mixed"
        if trend_score > 0
        else "weak"
    )
    average_dollar_volume = _average_dollar_volume(price_history)

    return [
        FactorResult(
            name="volume_acceleration",
            raw_value=volume_ratio,
            points=volume_score,
            max_points=WEIGHTS["volume_acceleration"],
            available=volume_ratio is not None,
            explanation=(
                f"30-day volume is {volume_ratio:.2f}x the 90-day average"
                if volume_ratio is not None
                else "Volume history unavailable"
            ),
        ),
        FactorResult(
            name="relative_strength",
            raw_value=relative_strength,
            points=relative_strength_score,
            max_points=WEIGHTS["relative_strength"],
            available=relative_strength is not None,
            explanation=(
                f"Six-month return exceeds benchmark by {relative_strength:.2f}%"
                if relative_strength is not None
                else "Benchmark-relative return unavailable"
            ),
        ),
        FactorResult(
            name="trend_strength",
            raw_value=trend_label,
            points=trend_score,
            max_points=trend_max,
            available=True,
            explanation=f"Price trend is {trend_label}",
        ),
        FactorResult(
            name="market_cap",
            raw_value=market_cap,
            points=market_cap_score,
            max_points=WEIGHTS["market_cap"],
            available=True,
            explanation=f"Market capitalization is {market_cap:,.0f}",
        ),
        FactorResult(
            name="sector_bonus",
            raw_value=sector,
            points=sector_score,
            max_points=WEIGHTS["sector_bonus"],
            available=bool(sector),
            explanation=(
                f"Configured sector classification: {sector}"
                if sector
                else "Sector classification unavailable"
            ),
        ),
        FactorResult(
            name="liquidity",
            raw_value=average_dollar_volume,
            points=liquidity_score,
            max_points=WEIGHTS["liquidity"],
            available=not np.isnan(average_dollar_volume),
            explanation=(
                f"30-day average dollar volume is {average_dollar_volume:,.0f}"
                if not np.isnan(average_dollar_volume)
                else "Liquidity history unavailable"
            ),
        ),
    ]


def _average_dollar_volume(df: pd.DataFrame) -> float:
    if df.empty or "Volume" not in df or "Close" not in df:
        return float("nan")
    avg_volume_30 = df["Volume"].tail(30).mean()
    avg_price_30 = df["Close"].tail(30).mean()
    return float(avg_volume_30 * avg_price_30)


def build_reason_flags(
    volume_ratio,
    relative_strength,
    market_cap,
    trend_score,
    sector,
) -> list[str]:
    flags = []

    if volume_ratio and volume_ratio >= 2:
        flags.append("Volume acceleration")

    if relative_strength and relative_strength >= 30:
        flags.append("Strong relative strength")

    if SWEET_SPOT_MIN <= market_cap <= SWEET_SPOT_MAX:
        flags.append("Sweet spot market cap")

    if market_cap < 25_000_000:
        flags.append("Tiny market cap / exceptional risk")

    if trend_score == WEIGHTS["trend_strength"]:
        flags.append("Strong technical trend")

    if sector:
        flags.append(f"Sector: {sector}")

    return flags


def _period_return(df: pd.DataFrame, days: int) -> float | None:
    if len(df) < days:
        return None

    start_price = df["Close"].iloc[-days]
    end_price = df["Close"].iloc[-1]

    if start_price == 0 or np.isnan(start_price):
        return None

    return ((end_price - start_price) / start_price) * 100


def _failed_score(stock_data: dict, reason: str) -> dict:
    fundamental_scores = calculate_fundamental_scores(stock_data)
    return {
        **stock_data,
        **fundamental_scores,
        "volume_score": 0,
        "volume_ratio": None,
        "relative_strength_score": 0,
        "relative_strength_6m": None,
        "trend_score": 0,
        "market_cap_score": 0,
        "sector_score": 0,
        "liquidity_score": 0,
        "discovery_score": 0,
        "score_confidence": 0,
        "factor_breakdown": "{}",
        "reason_flags": reason,
        "status": "FAILED",
    }
