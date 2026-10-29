"""Test a proposed change to Score v3 the way past changes were tested.

Every v3 weight decision so far was made the same way: re-score the saved
backtest observations offline under the proposed config, compare its
per-period rank correlation with the current model's period by period, so
shared market conditions cancel, and judge the difference with Newey-West
standard errors, because monthly 1Y returns overlap by eleven months and a
naive t-statistic overstates significance about 3.5 times. The rule is fixed
before the result is seen:

- the difference is positive in both the fitting window and the holdout,
- it is significant (|t| >= 2) in at least one, and
- it is significantly negative in neither.

Applied mechanically, earlier tests rejected four changes the raw evidence
seemed to call for and accepted one. This module makes the procedure a
command instead of a script that disappears with a session.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.scoring_v2 import score_frame, validated_config

SPLIT = "2022-01"
NEWEY_WEST_LAGS = {"1M": 0, "3M": 2, "6M": 5, "1Y": 11}
MINIMUM_PAIRS = 50


def apply_change(config: dict, change: dict) -> dict:
    """A copy of a raw model config with signals added, reweighted, or removed.

    ``change`` maps a signal name to ``None`` (remove it), a number (its
    weight; new signals rank higher-is-better), or a full signal spec. A spec
    may name ``"replaces": OLD``: OLD is removed and the new signal takes its
    place in ``sector_exclusions``, so a swapped variant stays out of the
    sectors the original was kept out of.
    """
    changed = copy.deepcopy(config)
    signals = changed.setdefault("signals", {})
    for name, value in change.items():
        if value is None:
            if name not in signals:
                raise ValueError(f"Cannot remove {name!r}: it is not a v3 signal.")
            signals.pop(name)
        elif isinstance(value, dict):
            spec = dict(value)
            replaced = spec.pop("replaces", None)
            if replaced is not None:
                if replaced not in signals:
                    raise ValueError(f"Cannot replace {replaced!r}: it is not a v3 signal.")
                signals.pop(replaced)
                for excluded in changed.get("sector_exclusions", {}).values():
                    if replaced in excluded:
                        excluded[excluded.index(replaced)] = name
            signals[name] = spec
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            spec = signals.get(name, {"direction": "higher"})
            signals[name] = {**spec, "weight": float(value)}
        else:
            raise ValueError(f"Change for {name!r} must be null, a weight, or a signal spec.")
    if not signals:
        raise ValueError("A change cannot remove every signal.")
    return changed


def rescore(observations: pd.DataFrame, config: dict) -> pd.Series:
    """One model's score for every observation, cross-section by cross-section."""
    model = validated_config(config)
    scores = pd.Series(np.nan, index=observations.index)
    for _, group in observations.groupby("period", sort=True):
        scores.loc[group.index] = score_frame(group, model)["score"]
    return scores


def ic_series(observations: pd.DataFrame, scores: pd.Series, horizon: str) -> pd.Series:
    """Per-period Spearman correlation of a score with forward excess returns."""
    column = f"excess_{horizon}"
    frame = pd.DataFrame({
        "period": observations["period"].astype(str),
        "score": scores,
        "excess": observations[column],
    }).dropna()
    values = {}
    for period, group in frame.groupby("period", sort=True):
        if len(group) >= MINIMUM_PAIRS and group["score"].nunique() > 1:
            values[period] = group["score"].rank().corr(group["excess"].rank())
    return pd.Series(values, dtype=float).sort_index()


def newey_west_t(values: pd.Series | np.ndarray, lags: int) -> float:
    """Mean over its Newey-West standard error, for overlapping observations."""
    x = np.asarray(values, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < lags + 3:
        return float("nan")
    d = x - x.mean()
    variance = d @ d / n
    for lag in range(1, lags + 1):
        variance += 2.0 * (1.0 - lag / (lags + 1)) * (d[lag:] @ d[:-lag]) / n
    return float(x.mean() / np.sqrt(variance / n)) if variance > 0 else float("nan")


def paired_test(
    observations: pd.DataFrame,
    base_config: dict,
    change: dict,
    horizons: tuple[str, ...] = ("1Y", "3M"),
    split: str = SPLIT,
) -> dict:
    """Compare a changed v3 with the current one and apply the decision rule."""
    observations = observations.copy()
    observations["period"] = observations["period"].astype(str)
    changed_config = apply_change(base_config, change)
    base_scores = rescore(observations, base_config)
    changed_scores = rescore(observations, changed_config)
    result = {"change": change, "split": split, "horizons": {}}
    for horizon in horizons:
        difference = (
            ic_series(observations, changed_scores, horizon)
            - ic_series(observations, base_scores, horizon)
        ).dropna()
        lags = NEWEY_WEST_LAGS.get(horizon, 0)
        windows = {}
        for name, part in (
            ("fit", difference[difference.index < split]),
            ("holdout", difference[difference.index >= split]),
        ):
            windows[name] = {
                "periods": int(len(part)),
                "mean_difference": float(part.mean()) if len(part) else float("nan"),
                "newey_west_t": newey_west_t(part, lags),
            }
        result["horizons"][horizon] = windows
    primary = result["horizons"][horizons[0]]
    result["decision_horizon"] = horizons[0]
    result["accepted"] = decision(primary["fit"], primary["holdout"])
    return result


def decision(fit: dict, holdout: dict) -> bool:
    """The rule fixed before any change was tested."""
    means = (fit["mean_difference"], holdout["mean_difference"])
    stats = (fit["newey_west_t"], holdout["newey_west_t"])
    if any(not np.isfinite(value) for value in means + stats):
        return False
    return min(means) > 0 and max(stats) >= 2.0 and min(stats) > -2.0


def export_result(result: dict, run_id: str, output_directory: Path) -> Path:
    """Write the verdict so a decision can cite it after the session ends."""
    digest = hashlib.sha256(
        json.dumps(result["change"], sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    path = Path(output_directory) / f"model_test_{run_id}_{digest}.json"
    path.write_text(json.dumps({"run_id": run_id, **result}, indent=2, default=float), encoding="utf-8")
    return path
