"""Descriptive, non-strategy information study for derivatives market features."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from collections.abc import Sequence
from math import sqrt
from statistics import mean, median
from typing import Any

import numpy as np

HORIZONS = (1, 3, 6, 12, 24, 48, 72)
RANDOM_SEED = 20260812
RANDOM_SAMPLES = 500
MIN_EVENTS = 100


def run_information_study(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unconditional = _unconditional(rows)
    analyses: dict[str, list[dict[str, Any]]] = {}
    definitions = {
        "funding": (
            "funding_state",
            tuple(sorted({str(row["funding_state"]) for row in rows if row.get("funding_state")})),
        ),
        "basis": (
            "basis_state",
            tuple(sorted({str(row["basis_state"]) for row in rows if row.get("basis_state")})),
        ),
        "oi": ("oi_direction", ("expansion", "contraction")),
        "price_only": ("price_direction", ("up", "down")),
        "price_oi_quadrants": (
            "price_oi_quadrant",
            tuple(
                sorted(
                    {str(row["price_oi_quadrant"]) for row in rows if row.get("price_oi_quadrant")}
                )
            ),
        ),
    }
    enriched = [
        {
            **row,
            "oi_direction": (
                "expansion"
                if row.get("oi_change_1h") is not None and float(row["oi_change_1h"]) > 0
                else "contraction"
                if row.get("oi_change_1h") is not None
                else None
            ),
        }
        for row in rows
    ]
    scoreboard: list[dict[str, Any]] = []
    for feature, (field, states) in definitions.items():
        feature_rows: list[dict[str, Any]] = []
        for state in states:
            events = _episodes(enriched, field, state)
            random24 = _random_percentile(enriched, events, 24, f"{feature}|{state}")
            temporal = _temporal_means(enriched, events, 24)
            for horizon in HORIZONS:
                values, mfe, mae = _forward(enriched, events, horizon)
                baseline = unconditional[horizon]
                low, high = _bootstrap(values, f"{feature}|{state}|{horizon}")
                feature_rows.append(
                    {
                        "feature": feature,
                        "state": state,
                        "horizon_hours": horizon,
                        "event_count": len(values),
                        "mean": mean(values) if values else None,
                        "median": median(values) if values else None,
                        "positive_rate": sum(value > 0 for value in values) / len(values)
                        if values
                        else None,
                        "bootstrap_ci_low": low,
                        "bootstrap_ci_high": high,
                        "unconditional_mean": baseline,
                        "excess_vs_unconditional": mean(values) - baseline if values else None,
                        "median_mfe": median(mfe) if mfe else None,
                        "median_mae": median(mae) if mae else None,
                        "random_percentile": random24 if horizon == 24 else None,
                    }
                )
            row24 = feature_rows[-len(HORIZONS) + HORIZONS.index(24)]
            positive_years = sum(value > 0 for value in temporal.values())
            quality = _quality(enriched, feature)
            eligible = (
                int(row24["event_count"]) >= MIN_EVENTS
                and row24["bootstrap_ci_low"] is not None
                and float(row24["bootstrap_ci_low"]) > float(row24["unconditional_mean"])
                and random24 >= 0.80
                and positive_years >= 2
                and quality == "DATA_QUALITY_READY"
            )
            scoreboard.append(
                {
                    "feature": feature,
                    "state_or_transformation": state,
                    "history_length": _history_length(enriched, feature),
                    "event_count": row24["event_count"],
                    "6h_excess": _horizon_value(feature_rows, state, 6),
                    "12h_excess": _horizon_value(feature_rows, state, 12),
                    "24h_excess": row24["excess_vs_unconditional"],
                    "bootstrap_ci": f"[{row24['bootstrap_ci_low']},{row24['bootstrap_ci_high']}]",
                    "random_percentile": random24,
                    "temporal_stability": f"positive_years={positive_years}/{len(temporal)}",
                    "regime_stability": _regime_stability(enriched, events),
                    "data_quality": quality,
                    "incremental_value": bool(eligible),
                    "phase_b_status": "SAMPLE_TOO_SMALL"
                    if int(row24["event_count"]) < MIN_EVENTS
                    else "PHASE_B_HYPOTHESIS_CANDIDATE"
                    if eligible
                    else "REJECTED",
                }
            )
        analyses[feature] = feature_rows
    correlations = _correlations(enriched)
    coefficients = _information_coefficients(enriched)
    candidates = _hypotheses(scoreboard)[:3]
    return {
        "analyses": analyses,
        "scoreboard": scoreboard,
        "correlations": correlations,
        "information_coefficients": coefficients,
        "phase_b_hypotheses": candidates,
        "multiple_testing_risk": True,
        "strategy_generated": False,
    }


def _episodes(rows: list[dict[str, Any]], field: str, state: str) -> list[int]:
    result: list[int] = []
    previous = False
    for index, row in enumerate(rows):
        active = row.get(field) == state
        if active and not previous:
            result.append(index)
        previous = active
    return result


def _forward(
    rows: list[dict[str, Any]], events: Sequence[int], horizon: int
) -> tuple[list[float], list[float], list[float]]:
    values: list[float] = []
    mfe: list[float] = []
    mae: list[float] = []
    for index in events:
        if index + horizon >= len(rows):
            continue
        entry = float(rows[index]["spot_close"])
        window = rows[index + 1 : index + horizon + 1]
        values.append(float(window[-1]["spot_close"]) / entry - 1)
        prices = [float(item["spot_close"]) for item in window]
        mfe.append(max(prices) / entry - 1)
        mae.append(min(prices) / entry - 1)
    return values, mfe, mae


def _unconditional(rows: list[dict[str, Any]]) -> dict[int, float]:
    return {
        horizon: mean(
            float(rows[index + horizon]["spot_close"]) / float(rows[index]["spot_close"]) - 1
            for index in range(len(rows) - horizon)
        )
        for horizon in HORIZONS
    }


def _bootstrap(values: Sequence[float], identity: str) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = np.random.default_rng(
        RANDOM_SEED + int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16)
    )
    array = np.asarray(values)
    samples = rng.choice(array, size=(500, len(array)), replace=True).mean(axis=1)
    return float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))


def _random_percentile(
    rows: list[dict[str, Any]], events: Sequence[int], horizon: int, identity: str
) -> float:
    forward_returns = [
        float(rows[index + horizon]["spot_close"]) / float(rows[index]["spot_close"]) - 1
        for index in range(len(rows) - horizon)
    ]
    actual = [forward_returns[index] for index in events if index < len(forward_returns)]
    if not actual:
        return 0.0
    eligible = list(range(1, len(forward_returns)))
    rng = random.Random(RANDOM_SEED + int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16))
    samples = [
        mean(
            forward_returns[index]
            for index in rng.sample(eligible, min(len(actual), len(eligible)))
        )
        for _ in range(RANDOM_SAMPLES)
    ]
    return sum(value <= mean(actual) for value in samples) / len(samples)


def _temporal_means(
    rows: list[dict[str, Any]], events: Sequence[int], horizon: int
) -> dict[str, float]:
    groups: dict[str, list[float]] = defaultdict(list)
    for index in events:
        values, _, _ = _forward(rows, (index,), horizon)
        if values:
            groups[str(rows[index]["timestamp"])[:4]].extend(values)
    return {key: mean(values) for key, values in groups.items()}


def _regime_stability(rows: list[dict[str, Any]], events: Sequence[int]) -> str:
    groups: dict[str, list[float]] = defaultdict(list)
    for index in events:
        values, _, _ = _forward(rows, (index,), 24)
        if values:
            groups[
                f"vol={rows[index]['volatility_context']}|market={rows[index]['market_context']}"
            ].extend(values)
    positive = sum(mean(values) > 0 for values in groups.values() if len(values) >= 20)
    return f"positive_regimes={positive}/{sum(len(values) >= 20 for values in groups.values())}"


def _correlations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("funding_rate", "basis_pct", "oi_pct_change_1h", "volume", "spot_return_1h")
    result: list[dict[str, Any]] = []
    for left_index, left in enumerate(fields):
        for right in fields[left_index + 1 :]:
            pairs = [
                (float(row[left]), float(row[right]))
                for row in rows
                if row.get(left) is not None and row.get(right) is not None
            ]
            result.append(
                {
                    "feature_left": left,
                    "feature_right": right,
                    "count": len(pairs),
                    "pearson": float(np.corrcoef(np.asarray(pairs).T)[0, 1])
                    if len(pairs) >= 3
                    else None,
                }
            )
    return result


def _information_coefficients(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for feature in ("funding_rate", "basis_pct", "oi_pct_change_1h"):
        for horizon in (6, 12, 24):
            periods = (
                "all",
                *sorted({str(row["timestamp"])[:4] for row in rows}),
                *sorted({str(row["timestamp"])[:7] for row in rows}),
            )
            for period in periods:
                pairs = []
                for index, row in enumerate(rows[:-horizon]):
                    if row.get(feature) is None or (
                        period != "all" and not str(row["timestamp"]).startswith(period)
                    ):
                        continue
                    forward = (
                        float(rows[index + horizon]["spot_close"]) / float(row["spot_close"]) - 1
                    )
                    pairs.append((float(row[feature]), forward))
                result.append(
                    {
                        "feature": feature,
                        "horizon_hours": horizon,
                        "period": period,
                        "count": len(pairs),
                        "spearman_ic": _spearman(pairs),
                    }
                )
    return result


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    left = _ranks([item[0] for item in pairs])
    right = _ranks([item[1] for item in pairs])
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    denominator = sqrt(
        sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else 0.0


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank = (index + end - 1) / 2 + 1
        for position in order[index:end]:
            ranks[position] = rank
        index = end
    return ranks


def _quality(rows: list[dict[str, Any]], feature: str) -> str:
    field = {
        "funding": "funding_rate",
        "basis": "basis_pct",
        "oi": "open_interest_btc",
        "price_only": "price_direction",
        "price_oi_quadrants": "price_oi_quadrant",
    }[feature]
    coverage = sum(row.get(field) is not None for row in rows) / max(len(rows), 1)
    return (
        "DATA_QUALITY_READY"
        if coverage >= 0.5
        else "DATA_QUALITY_LIMITED"
        if coverage >= 0.05
        else "DATA_QUALITY_REJECTED"
    )


def _history_length(rows: list[dict[str, Any]], feature: str) -> int:
    field = {
        "funding": "funding_rate",
        "basis": "basis_pct",
        "oi": "open_interest_btc",
        "price_only": "price_direction",
        "price_oi_quadrants": "price_oi_quadrant",
    }[feature]
    indexes = [index for index, row in enumerate(rows) if row.get(field) is not None]
    return indexes[-1] - indexes[0] + 1 if indexes else 0


def _horizon_value(rows: list[dict[str, Any]], state: str, horizon: int) -> Any:
    return next(
        row["excess_vs_unconditional"]
        for row in rows
        if row["state"] == state and row["horizon_hours"] == horizon
    )


def _hypotheses(scoreboard: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "feature": str(row["feature"]),
            "state": str(row["state_or_transformation"]),
            "status": "PHASE_B_HYPOTHESIS_CANDIDATE",
            "hypothesis": f"The {row['feature']} state {row['state_or_transformation']} may contain incremental forward information.",
        }
        for row in scoreboard
        if row["phase_b_status"] == "PHASE_B_HYPOTHESIS_CANDIDATE"
    ]
