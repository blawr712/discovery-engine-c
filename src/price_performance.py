"""Offline percentage-return metrics and normalized chart series."""

from __future__ import annotations

from datetime import date, timedelta
import math


PERIODS = {"1W": 7, "1M": 30, "3M": 91, "6M": 182, "1Y": 365}
BENCHMARK_DEFINITIONS = {
    "SPY": "SPDR S&P 500 ETF Trust, used as the broad U.S. equity benchmark.",
    "XIU.TO": "iShares S&P/TSX 60 Index ETF, used as the Canadian equity benchmark.",
}


def build_price_performance(equity: dict | None, benchmark: dict | None) -> dict | None:
    if not equity or not equity.get("points"):
        return None
    equity_points, corporate_actions = _adjust_corporate_actions(equity["points"])
    benchmark_points, benchmark_actions = _adjust_corporate_actions(
        benchmark["points"] if benchmark else []
    )
    anomalies = _price_anomalies(equity_points)
    benchmark_anomalies = _price_anomalies(benchmark_points)
    equity_returns = _period_returns(equity_points, anomalies)
    benchmark_returns = (
        _period_returns(benchmark_points, benchmark_anomalies)
        if benchmark_points else {}
    )
    metrics = {
        period: {
            "ticker_return": result["return"],
            "benchmark_return": benchmark_returns.get(period, {}).get("return"),
            "relative_return": _difference(
                result["return"], benchmark_returns.get(period, {}).get("return")
            ),
            "reliable": result["reliable"],
            "warning": result["warning"],
        }
        for period, result in equity_returns.items()
    }
    metrics["YTD"] = _metric_for_target(
        equity_points,
        date(date.fromisoformat(equity_points[-1]["date"]).year, 1, 1),
        anomalies,
        benchmark_points,
        benchmark_anomalies,
    )
    common_start = max(
        equity_points[0]["date"],
        benchmark_points[0]["date"] if benchmark_points else equity_points[0]["date"],
    )
    ohlcv = _ohlcv_series(equity_points)
    ohlcv_count = len(ohlcv)
    return {
        "ticker": equity["ticker"],
        "benchmark": benchmark.get("ticker") if benchmark else None,
        "benchmark_definition": BENCHMARK_DEFINITIONS.get(
            benchmark.get("ticker") if benchmark else None,
            "Configured market benchmark used for relative performance.",
        ),
        "start_date": equity["start_date"],
        "end_date": equity["end_date"],
        "point_count": equity["point_count"],
        "source_mtime": equity["source_mtime"],
        "capabilities": {
            "ohlcv": ohlcv_count >= 2,
            "ohlcv_point_count": ohlcv_count,
            "ohlcv_coverage_percent": round(
                ohlcv_count / len(equity_points) * 100, 2
            ),
            "legacy_close_only": ohlcv_count == 0,
        },
        "summary": _price_summary(equity_points),
        "period_returns": metrics,
        "data_quality": (
            "unresolved_discontinuity" if anomalies
            else "verified_adjusted" if any(
                event["status"] == "verified_adjusted" for event in corporate_actions
            )
            else "verified" if corporate_actions else "clean"
        ),
        "anomalies": anomalies,
        "corporate_actions": corporate_actions,
        "benchmark_corporate_actions": benchmark_actions,
        "series": {
            "price": [
                {"date": point["date"], "value": point["close"]}
                for point in equity_points
            ],
            "ticker": _normalized(equity_points, common_start),
            "benchmark": _normalized(benchmark_points, common_start),
            "moving_averages": {
                str(window): _moving_average(equity_points, window)
                for window in (20, 50, 200)
            },
            "exponential_moving_averages": {
                str(window): _exponential_moving_average(equity_points, window)
                for window in (20, 50)
            },
            "bollinger_bands": {"20": _bollinger_bands(equity_points, 20, 2)},
            "ohlcv": ohlcv,
        },
        "volume": [
            {"date": point["date"], "value": point.get("volume")}
            for point in equity_points
        ],
        "interpretation": (
            "Historical adjusted-close performance from the immutable snapshot "
            "linked to this run; past performance is not a forecast."
        ),
    }


def _metric_for_target(
    equity_points, target, anomalies, benchmark_points, benchmark_anomalies,
):
    start, ticker_return = _return_from(equity_points, target)
    crossed = [
        anomaly for anomaly in anomalies
        if start is not None and anomaly["date"] > start["date"]
    ]
    benchmark_return = None
    if benchmark_points:
        benchmark_start, benchmark_return = _return_from(benchmark_points, target)
        benchmark_crossed = [
            anomaly for anomaly in benchmark_anomalies
            if benchmark_start is not None and anomaly["date"] > benchmark_start["date"]
        ]
        if benchmark_crossed:
            benchmark_return = None
    reliable = not crossed and ticker_return is not None
    if crossed:
        ticker_return = None
    return {
        "ticker_return": ticker_return,
        "benchmark_return": benchmark_return,
        "relative_return": _difference(ticker_return, benchmark_return),
        "reliable": reliable,
        "warning": (
            "Possible split or corporate action crosses this period"
            if crossed else None
        ),
    }


def _price_summary(points):
    closes = [point["close"] for point in points if point.get("close") is not None]
    highs = [point["high"] for point in points if point.get("high") is not None]
    lows = [point["low"] for point in points if point.get("low") is not None]
    volumes = [
        point.get("volume") for point in points[-30:]
        if point.get("volume") is not None
    ]
    latest = closes[-1]
    previous = closes[-2] if len(closes) > 1 else None
    latest_point = points[-1]
    return {
        "latest_close": round(latest, 4),
        "latest_open": _rounded(latest_point.get("open")),
        "latest_high": _rounded(latest_point.get("high")),
        "latest_low": _rounded(latest_point.get("low")),
        "latest_date": points[-1]["date"],
        "daily_change": (
            round((latest / previous - 1) * 100, 2) if previous else None
        ),
        "period_high": round(max(highs or closes), 4),
        "period_low": round(min(lows or closes), 4),
        "average_volume_30d": (
            round(sum(volumes) / len(volumes), 2) if volumes else None
        ),
    }


def _moving_average(points, window):
    values = []
    for index in range(window - 1, len(points)):
        closes = [
            point.get("close") for point in points[index - window + 1:index + 1]
        ]
        if any(value is None for value in closes):
            continue
        values.append({
            "date": points[index]["date"],
            "value": round(sum(closes) / window, 4),
        })
    return values


def _exponential_moving_average(points, window):
    closes = [point.get("close") for point in points]
    if len(closes) < window or any(value is None for value in closes[:window]):
        return []
    multiplier = 2 / (window + 1)
    average = sum(closes[:window]) / window
    values = [{"date": points[window - 1]["date"], "value": round(average, 4)}]
    for index in range(window, len(points)):
        close = closes[index]
        if close is None:
            continue
        average = (close - average) * multiplier + average
        values.append({"date": points[index]["date"], "value": round(average, 4)})
    return values


def _bollinger_bands(points, window, deviations):
    values = []
    for index in range(window - 1, len(points)):
        closes = [
            point.get("close") for point in points[index - window + 1:index + 1]
        ]
        if any(value is None for value in closes):
            continue
        middle = sum(closes) / window
        variance = sum((value - middle) ** 2 for value in closes) / window
        spread = math.sqrt(variance) * deviations
        values.append({
            "date": points[index]["date"],
            "middle": round(middle, 4),
            "upper": round(middle + spread, 4),
            "lower": round(middle - spread, 4),
        })
    return values


def _ohlcv_series(points):
    fields = ("open", "high", "low", "close")
    return [
        {
            "date": point["date"],
            "open": point["open"], "high": point["high"],
            "low": point["low"], "close": point["close"],
            "volume": point.get("volume"),
        }
        for point in points
        if all(_number(point.get(field)) is not None for field in fields)
        and point["low"] <= min(point["open"], point["close"])
        and point["high"] >= max(point["open"], point["close"])
        and point["low"] <= point["high"]
    ]


def _period_returns(points: list[dict], anomalies: list[dict]) -> dict[str, dict]:
    if len(points) < 2:
        return {
            period: {"return": None, "reliable": False,
                     "warning": "Insufficient price history"}
            for period in PERIODS
        }
    end = date.fromisoformat(points[-1]["date"])
    results = {}
    for period, days in PERIODS.items():
        target = end - timedelta(days=days)
        start, value = _return_from(points, target)
        crossed = [
            anomaly for anomaly in anomalies
            if start is not None and anomaly["date"] > start["date"]
        ]
        results[period] = {
            "return": None if crossed else value,
            "reliable": not crossed and value is not None,
            "warning": (
                "Possible split or corporate action crosses this period"
                if crossed else None
            ),
        }
    return results


def _return_from(points: list[dict], target: date) -> tuple[dict | None, float | None]:
    eligible = [point for point in points if date.fromisoformat(point["date"]) >= target]
    start = eligible[0] if eligible else points[0]
    start_price = start["close"]
    end_price = points[-1]["close"]
    if not start_price:
        return start, None
    return start, round((end_price / start_price - 1) * 100, 2)


def _price_anomalies(points: list[dict]) -> list[dict]:
    anomalies = []
    for previous, current in zip(points, points[1:]):
        if not previous.get("close"):
            continue
        change = (current["close"] / previous["close"] - 1) * 100
        if change >= 300 or change <= -75:
            anomalies.append({
                "date": current["date"],
                "change_percent": round(change, 2),
                "previous_close": previous["close"],
                "close": current["close"],
                "classification": "possible_split_or_corporate_action",
            })
    return anomalies


def _adjust_corporate_actions(points: list[dict]) -> tuple[list[dict], list[dict]]:
    adjusted = [dict(point) for point in points]
    events = []
    for index, point in enumerate(points):
        dividend = _number(point.get("dividend")) or 0
        split = _number(point.get("split")) or 0
        if dividend:
            events.append({
                "date": point["date"], "type": "dividend", "value": dividend,
                "status": "reported",
            })
        if not split or split == 1:
            continue
        observed = None
        if index > 0 and points[index - 1].get("close"):
            observed = point["close"] / points[index - 1]["close"]
        expected = 1 / split
        explains_jump = (
            observed is not None and observed > 0
            and abs(math.log(observed / expected)) <= math.log(1.5)
        )
        status = "verified_adjusted" if explains_jump else "reported_already_adjusted"
        if explains_jump:
            for prior in adjusted[:index]:
                for field in ("open", "high", "low", "close"):
                    if prior.get(field) is not None:
                        prior[field] = round(prior[field] / split, 6)
                if prior.get("volume") is not None:
                    prior["volume"] = prior["volume"] * split
        events.append({
            "date": point["date"], "type": "split", "value": split,
            "status": status, "observed_price_ratio": (
                round(observed, 4) if observed is not None else None
            ),
            "expected_price_ratio": round(expected, 4),
        })
    return adjusted, events


def _normalized(points: list[dict], start_date: str) -> list[dict]:
    selected = [point for point in points if point["date"] >= start_date]
    if not selected or not selected[0]["close"]:
        return []
    base = selected[0]["close"]
    return [
        {"date": point["date"], "value": round(point["close"] / base * 100, 3)}
        for point in selected
    ]


def _difference(left, right):
    if left is None or right is None:
        return None
    return round(left - right, 2)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rounded(value):
    number = _number(value)
    return round(number, 4) if number is not None else None
