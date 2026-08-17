"""Unified staged elimination pipeline for frozen Strategy Research V2 entries."""

from __future__ import annotations

import hashlib
import random
from bisect import bisect_left
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from math import prod
from statistics import mean, median
from typing import Any

import numpy as np

from app.domain.market import Candle
from backtest.strategy_v2_candidates import (
    CandidateVariant,
    EntryEpisode,
    build_entry_episodes,
    generate_signals,
)
from backtest.vwap_fixed_exit_research import (
    COST_SCENARIOS_BPS,
    FIXED_HORIZONS,
    CostModel,
    TradeCandidate,
    performance_metrics,
    simulate_portfolio,
)
from backtest.vwap_signal_edge import CORE_HORIZONS, bootstrap_intervals, descriptive
from backtest.vwap_walk_forward_research import add_months

RANDOM_SEED = 20260814
RANDOM_SAMPLES = 500
MIN_EPISODES = 100
HOLDOUT_FRACTION = 0.20


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidate_id: str
    primary_variant_id: str
    status: str
    stage_reached: str
    episodes: tuple[dict[str, Any], ...]
    forward_rows: tuple[dict[str, Any], ...]
    regime_rows: tuple[dict[str, Any], ...]
    temporal_rows: tuple[dict[str, Any], ...]
    random_rows: tuple[dict[str, Any], ...]
    fixed_rows: tuple[dict[str, Any], ...]
    cost_rows: tuple[dict[str, Any], ...]
    concentration_rows: tuple[dict[str, Any], ...]
    walk_forward_rows: tuple[dict[str, Any], ...]
    yearly_rows: tuple[dict[str, Any], ...]
    scoreboard: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StrategyV2Study:
    candidate_specs: tuple[dict[str, Any], ...]
    variant_count: int
    variant_rows: tuple[dict[str, Any], ...]
    results: tuple[CandidateResult, ...]
    scoreboard: tuple[dict[str, Any], ...]
    surviving_candidates: tuple[str, ...]
    final_state: str
    final_holdout_not_pristine: bool


def run_strategy_v2_study(
    candles: list[Candle],
    variants: tuple[CandidateVariant, ...],
    vwap_forward_means: dict[int, float],
) -> StrategyV2Study:
    if len(candles) < 24 * 365:
        raise ValueError("Strategy V2 requires at least one year of hourly candles")
    holdout_index = int(len(candles) * (1 - HOLDOUT_FRACTION))
    research_index = int(len(candles) * 0.60)
    episode_map: dict[str, tuple[EntryEpisode, ...]] = {}
    variant_rows: list[dict[str, Any]] = []
    for variant in variants:
        episodes = build_entry_episodes(
            candles, generate_signals(candles, variant), variant, CORE_HORIZONS
        )
        episode_map[variant.variant_id] = episodes
        variant_rows.extend(
            _forward_analysis(candles, episodes, variant, holdout_index, vwap_forward_means)
        )

    results: list[CandidateResult] = []
    candidates = tuple(dict.fromkeys(item.candidate_id for item in variants))
    for candidate_id in candidates:
        candidate_variants = tuple(item for item in variants if item.candidate_id == candidate_id)
        primary = next(item for item in candidate_variants if item.primary)
        episodes = episode_map[primary.variant_id]
        forward = [row for row in variant_rows if row["variant_id"] == primary.variant_id]
        regimes = _regime_analysis(episodes)
        temporal = _temporal_analysis(episodes)
        random_rows = _random_analysis(candles, episodes, primary)
        gate = _early_gate(forward, temporal, random_rows, candidate_variants, variant_rows)
        fixed_rows: list[dict[str, Any]] = []
        costs: list[dict[str, Any]] = []
        concentration: list[dict[str, Any]] = []
        walk_forward: list[dict[str, Any]] = []
        status = gate["status"]
        stage = "forward_edge"
        best_horizon: int | None = None
        if gate["passed"]:
            stage = "fixed_exit"
            fixed_rows, concentration = _fixed_exit_analysis(
                candles, episodes, research_index, holdout_index
            )
            costs = _cost_rows(fixed_rows)
            best_horizon = _select_fixed_horizon(fixed_rows)
            fixed_gate = _fixed_exit_gate(fixed_rows, concentration, best_horizon)
            status = fixed_gate["status"]
            if fixed_gate["passed"]:
                stage = "walk_forward"
                walk_forward = _walk_forward(candles, episodes, best_horizon, holdout_index)
                status = _final_candidate_status(
                    walk_forward,
                    fixed_rows,
                    concentration,
                    regimes,
                    best_horizon,
                )
        years = _yearly(episodes)
        scoreboard = _scoreboard(
            candidate_id,
            primary.variant_id,
            forward,
            temporal,
            regimes,
            random_rows,
            fixed_rows,
            concentration,
            walk_forward,
            years,
            status,
            stage,
            best_horizon,
        )
        results.append(
            CandidateResult(
                candidate_id,
                primary.variant_id,
                status,
                stage,
                tuple(item.flat() for item in episodes),
                tuple(forward),
                tuple(regimes),
                tuple(temporal),
                tuple(random_rows),
                tuple(fixed_rows),
                tuple(costs),
                tuple(concentration),
                tuple(walk_forward),
                tuple(years),
                scoreboard,
            )
        )
    qualified = [item for item in results if item.status == "RESEARCH_CANDIDATE_V2"]
    if len(qualified) > 2:
        ranked = sorted(qualified, key=_survivor_rank, reverse=True)
        retained = {item.candidate_id for item in ranked[:2]}
        results = [
            item
            if item.candidate_id in retained or item.status != "RESEARCH_CANDIDATE_V2"
            else replace(
                item,
                status="RESEARCH_PROMISING",
                scoreboard={**item.scoreboard, "final_status": "RESEARCH_PROMISING"},
            )
            for item in results
        ]
    survivors = tuple(
        item.candidate_id for item in results if item.status == "RESEARCH_CANDIDATE_V2"
    )
    promising = sum(
        item.status in {"RESEARCH_PROMISING", "RESEARCH_CANDIDATE_V2"} for item in results
    )
    if survivors:
        final_state = "RESEARCH_CANDIDATE_V2_READY_FOR_HUMAN_REVIEW"
    elif promising == 1:
        final_state = "STRATEGY_V2_ONE_CANDIDATE_PROMISING"
    elif promising > 1:
        final_state = "STRATEGY_V2_MULTIPLE_CANDIDATES_PROMISING"
    elif all(item.stage_reached == "forward_edge" for item in results):
        final_state = "NO_STRATEGY_CANDIDATE_FOUND"
    else:
        final_state = "STRATEGY_V2_CANDIDATES_WEAK"
    return StrategyV2Study(
        tuple(asdict(item) for item in variants),
        len(variants),
        tuple(variant_rows),
        tuple(results),
        tuple(item.scoreboard for item in results),
        survivors,
        final_state,
        True,
    )


def _forward_analysis(
    candles: list[Candle],
    episodes: tuple[EntryEpisode, ...],
    variant: CandidateVariant,
    holdout_index: int,
    vwap_forward_means: dict[int, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scopes = (
        ("development", tuple(item for item in episodes if item.start_index < holdout_index)),
        (
            "reference_recent_20pct",
            tuple(item for item in episodes if item.start_index >= holdout_index),
        ),
    )
    for scope, subset in scopes:
        for horizon in CORE_HORIZONS:
            values = [value for item in subset if (value := item.returns[horizon]) is not None]
            unconditional = _unconditional(
                candles,
                horizon,
                0,
                holdout_index if scope == "development" else len(candles),
                holdout_index if scope != "development" else 1,
            )
            rows.append(
                {
                    "candidate_id": variant.candidate_id,
                    "variant_id": variant.variant_id,
                    "primary_variant": variant.primary,
                    "scope": scope,
                    "horizon_hours": horizon,
                    **descriptive(values),
                    **bootstrap_intervals(
                        values,
                        seed_offset=int(
                            hashlib.sha256(
                                f"{variant.variant_id}|{scope}|{horizon}".encode()
                            ).hexdigest()[:6],
                            16,
                        ),
                    ),
                    "unconditional_mean": mean(unconditional) if unconditional else None,
                    "excess_vs_unconditional": mean(values) - mean(unconditional)
                    if values and unconditional
                    else None,
                    "excess_vs_vwap_v1": mean(values) - vwap_forward_means[horizon]
                    if values
                    else None,
                }
            )
    return rows


def _unconditional(
    candles: list[Candle], horizon: int, _unused: int, end: int, start: int
) -> list[float]:
    return [
        float(candles[index + horizon - 1].close) / float(candles[index].open) - 1
        for index in range(max(start, 1), end - horizon + 1)
    ]


def _random_analysis(
    candles: list[Candle], episodes: tuple[EntryEpisode, ...], variant: CandidateVariant
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cutoff = int(len(candles) * 0.8)
    subset = [
        item for item in episodes if item.start_index < cutoff and item.entry_index is not None
    ]
    months: dict[str, list[int]] = {}
    for index in range(1, cutoff - max(CORE_HORIZONS)):
        months.setdefault(candles[index].timestamp.strftime("%Y-%m"), []).append(index)
    counts: dict[str, int] = {}
    for item in subset:
        counts[item.signal_timestamp[:7]] = counts.get(item.signal_timestamp[:7], 0) + 1
    for horizon in CORE_HORIZONS:
        actual = [value for item in subset if (value := item.returns[horizon]) is not None]
        actual_mean = mean(actual) if actual else 0.0
        sample_means: list[float] = []
        seed = RANDOM_SEED + int(
            hashlib.sha256(f"{variant.variant_id}|{horizon}".encode()).hexdigest()[:6], 16
        )
        rng = random.Random(seed)
        for _ in range(RANDOM_SAMPLES):
            values: list[float] = []
            for month, count in counts.items():
                for index in rng.choices(months[month], k=count):
                    values.append(
                        float(candles[index + horizon - 1].close) / float(candles[index].open) - 1
                    )
            sample_means.append(mean(values))
        rows.append(
            {
                "candidate_id": variant.candidate_id,
                "variant_id": variant.variant_id,
                "horizon_hours": horizon,
                "episode_count": len(actual),
                "actual_mean": actual_mean,
                "random_median": median(sample_means),
                "random_percentile": sum(value <= actual_mean for value in sample_means)
                / len(sample_means),
                "seed": seed,
                "samples": RANDOM_SAMPLES,
            }
        )
    return rows


def _regime_analysis(episodes: tuple[EntryEpisode, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    development = [item for item in episodes if not item.holdout]
    for dimension in ("market_regime", "volatility_regime"):
        groups = sorted({str(getattr(item, dimension)) for item in development})
        for group in groups:
            selected = [item for item in development if getattr(item, dimension) == group]
            for horizon in (6, 12, 24, 48, 72):
                values = [
                    value for item in selected if (value := item.returns[horizon]) is not None
                ]
                mfe = [value for item in selected if (value := item.mfe[horizon]) is not None]
                mae = [value for item in selected if (value := item.mae[horizon]) is not None]
                stats = descriptive(values)
                rows.append(
                    {
                        "dimension": dimension,
                        "group": group,
                        "horizon_hours": horizon,
                        **stats,
                        "median_mfe": median(mfe) if mfe else None,
                        "median_mae": median(mae) if mae else None,
                    }
                )
    return rows


def _temporal_analysis(episodes: tuple[EntryEpisode, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in episodes:
        timestamp = datetime.fromisoformat(item.signal_timestamp)
        periods = {
            "year": str(timestamp.year),
            "quarter": f"{timestamp.year}-Q{(timestamp.month - 1) // 3 + 1}",
        }
        for dimension, period in periods.items():
            rows.append(
                {
                    "dimension": dimension,
                    "period": period,
                    "holdout": item.holdout,
                    "return_24h": item.returns[24],
                    "mfe_24h": item.mfe[24],
                    "mae_24h": item.mae[24],
                }
            )
    grouped: list[dict[str, Any]] = []
    keys = sorted({(row["dimension"], row["period"], row["holdout"]) for row in rows})
    for dimension, period, holdout in keys:
        selected = [
            row
            for row in rows
            if (row["dimension"], row["period"], row["holdout"]) == (dimension, period, holdout)
            and row["return_24h"] is not None
        ]
        values = [float(row["return_24h"]) for row in selected]
        grouped.append(
            {
                "dimension": dimension,
                "period": period,
                "holdout": holdout,
                **descriptive(values),
                "median_mfe": median(float(row["mfe_24h"]) for row in selected)
                if selected
                else None,
                "median_mae": median(float(row["mae_24h"]) for row in selected)
                if selected
                else None,
            }
        )
    return grouped


def _early_gate(
    forward: list[dict[str, Any]],
    temporal: list[dict[str, Any]],
    random_rows: list[dict[str, Any]],
    candidate_variants: tuple[CandidateVariant, ...],
    variant_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    development = {
        int(row["horizon_hours"]): row for row in forward if row["scope"] == "development"
    }
    recent = next(
        row
        for row in forward
        if row["scope"] == "reference_recent_20pct" and row["horizon_hours"] == 24
    )
    random24 = next(row for row in random_rows if row["horizon_hours"] == 24)
    positive_horizons = sum(
        float(development[h]["excess_vs_unconditional"] or 0) > 0 for h in (6, 12, 24, 48, 72)
    )
    years = [
        row
        for row in temporal
        if row["dimension"] == "year" and row["holdout"] is False and row["mean"] is not None
    ]
    positive_years = sum(float(row["mean"]) > 0 for row in years)
    variant24 = [
        row
        for row in variant_rows
        if row["variant_id"] in {item.variant_id for item in candidate_variants}
        and row["scope"] == "development"
        and row["horizon_hours"] == 24
    ]
    robustness = sum(float(row["excess_vs_unconditional"] or 0) > 0 for row in variant24) == len(
        variant24
    )
    if int(development[24]["count"] or 0) < MIN_EPISODES:
        return {"passed": False, "status": "REJECTED_NO_EDGE"}
    confidence_excess = float(development[24]["mean_ci_low"] or 0) - float(
        development[24]["unconditional_mean"] or 0
    )
    if (
        float(development[24]["excess_vs_unconditional"] or 0) <= 0
        or confidence_excess <= 0
        or positive_horizons < 4
    ):
        return {"passed": False, "status": "REJECTED_NO_EDGE"}
    if float(random24["random_percentile"]) < 0.80:
        return {"passed": False, "status": "REJECTED_RANDOM_LIKE"}
    if float(recent["mean"] or 0) <= 0 or positive_years < 3 or not robustness:
        return {"passed": False, "status": "REJECTED_TEMPORALLY_UNSTABLE"}
    return {"passed": True, "status": "RESEARCH_PROMISING"}


def _fixed_exit_analysis(
    candles: list[Candle],
    episodes: tuple[EntryEpisode, ...],
    research_index: int,
    validation_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    trades_by_key: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    segments = (
        ("research_first_60pct", 0, research_index),
        ("validation_60_to_80pct", research_index, validation_index),
        ("reference_recent_20pct", validation_index, len(candles)),
    )
    for segment_id, start, end in segments:
        for horizon in FIXED_HORIZONS:
            candidates = _trade_candidates(candles, episodes, horizon, start, end)
            for cost_bps in COST_SCENARIOS_BPS:
                local = tuple(
                    TradeCandidate(
                        item.episode_id,
                        item.signal_timestamp,
                        item.entry_index - start,
                        item.exit_index - start,
                        item.market_regime,
                        item.volatility_regime,
                        segment_id == "reference_recent_20pct",
                    )
                    for item in candidates
                )
                trades, equity = simulate_portfolio(
                    candles[start:end], local, horizon, CostModel.equal_split(cost_bps)
                )
                metrics = performance_metrics(
                    equity, trades, candles[start:end], horizon=horizon, cost_bps=cost_bps
                )
                metrics["segment"] = segment_id
                rows.append(metrics)
                trades_by_key[(segment_id, horizon, cost_bps)] = trades
    best = max(
        (
            row
            for row in rows
            if row["segment"] == "research_first_60pct" and row["round_trip_cost_bps"] == 10
        ),
        key=lambda row: float(row["total_return"]),
    )
    concentration: list[dict[str, Any]] = []
    for segment_id in ("research_first_60pct", "validation_60_to_80pct", "reference_recent_20pct"):
        trades = trades_by_key[(segment_id, int(best["horizon_hours"]), 10)]
        ranked = sorted(trades, key=lambda row: float(row["net_pnl"]), reverse=True)
        for remove in (0, 5, 10):
            removed = {str(row["trade_id"]) for row in ranked[:remove]}
            retained = [row for row in trades if str(row["trade_id"]) not in removed]
            concentration.append(
                {
                    "segment": segment_id,
                    "horizon_hours": best["horizon_hours"],
                    "removed_top_winners": remove,
                    "remaining_trade_count": len(retained),
                    "return_after_removal": prod(1 + float(row["net_return"]) for row in retained)
                    - 1,
                }
            )
    return rows, concentration


def _trade_candidates(
    candles: list[Candle], episodes: tuple[EntryEpisode, ...], horizon: int, start: int, end: int
) -> tuple[TradeCandidate, ...]:
    selected: list[TradeCandidate] = []
    next_flat = start
    for item in episodes:
        if item.entry_index is None or not start <= item.start_index < end:
            continue
        exit_index = item.entry_index + horizon
        if item.start_index < next_flat or exit_index >= end:
            continue
        selected.append(
            TradeCandidate(
                item.episode_id,
                item.signal_timestamp,
                item.entry_index,
                exit_index,
                item.market_regime,
                item.volatility_regime,
                item.holdout,
            )
        )
        next_flat = exit_index
    return tuple(selected)


def _select_fixed_horizon(rows: list[dict[str, Any]]) -> int:
    return int(
        max(
            (
                row
                for row in rows
                if row["segment"] == "research_first_60pct" and row["round_trip_cost_bps"] == 10
            ),
            key=lambda row: float(row["total_return"]),
        )["horizon_hours"]
    )


def _fixed_exit_gate(
    rows: list[dict[str, Any]], concentration: list[dict[str, Any]], horizon: int
) -> dict[str, Any]:
    validation = next(
        row
        for row in rows
        if row["segment"] == "validation_60_to_80pct"
        and row["horizon_hours"] == horizon
        and row["round_trip_cost_bps"] == 10
    )
    stress = next(
        row
        for row in rows
        if row["segment"] == "validation_60_to_80pct"
        and row["horizon_hours"] == horizon
        and row["round_trip_cost_bps"] == 20
    )
    top5 = next(
        row
        for row in concentration
        if row["segment"] == "validation_60_to_80pct" and row["removed_top_winners"] == 5
    )
    if (
        int(validation["trade_count"]) < 30
        or float(validation["total_return"]) <= 0
        or float(validation["profit_factor"] or 0) <= 1.10
        or float(validation["Sharpe"] or 0) <= 0
    ):
        return {"passed": False, "status": "REJECTED_OOS_WEAK"}
    if float(validation["max_drawdown"]) <= -0.30:
        return {"passed": False, "status": "REJECTED_DRAWDOWN"}
    if float(stress["total_return"]) <= 0:
        return {"passed": False, "status": "REJECTED_COST_FRAGILE"}
    if float(top5["return_after_removal"]) <= 0:
        return {"passed": False, "status": "REJECTED_TEMPORALLY_UNSTABLE"}
    return {"passed": True, "status": "RESEARCH_PROMISING"}


def _walk_forward(
    candles: list[Candle],
    episodes: tuple[EntryEpisode, ...],
    horizon: int,
    development_end_index: int,
) -> list[dict[str, Any]]:
    timestamps = [item.timestamp for item in candles]
    train_start = timestamps[0]
    rows: list[dict[str, Any]] = []
    number = 1
    while True:
        train_end = add_months(train_start, 12)
        test_end = add_months(train_end, 3)
        if test_end > timestamps[development_end_index]:
            break
        test_start_index = bisect_left(timestamps, train_end)
        test_end_index = bisect_left(timestamps, test_end)
        for cost in (10, 20):
            candidates = _trade_candidates(
                candles, episodes, horizon, test_start_index, test_end_index
            )
            local = tuple(
                TradeCandidate(
                    item.episode_id,
                    item.signal_timestamp,
                    item.entry_index - test_start_index,
                    item.exit_index - test_start_index,
                    item.market_regime,
                    item.volatility_regime,
                    False,
                )
                for item in candidates
            )
            trades, equity = simulate_portfolio(
                candles[test_start_index:test_end_index],
                local,
                horizon,
                CostModel.equal_split(cost),
            )
            metrics = performance_metrics(
                equity,
                trades,
                candles[test_start_index:test_end_index],
                horizon=horizon,
                cost_bps=cost,
            )
            metrics.update(
                {
                    "window_id": f"WF{number:02d}",
                    "train_start": train_start.astimezone(UTC).isoformat(),
                    "train_end": train_end.astimezone(UTC).isoformat(),
                    "test_start": timestamps[test_start_index].astimezone(UTC).isoformat(),
                    "test_end": timestamps[test_end_index].astimezone(UTC).isoformat(),
                }
            )
            rows.append(metrics)
        train_start = add_months(train_start, 3)
        number += 1
    return rows


def _final_candidate_status(
    walk_forward: list[dict[str, Any]],
    fixed: list[dict[str, Any]],
    concentration: list[dict[str, Any]],
    regimes: list[dict[str, Any]],
    horizon: int,
) -> str:
    base = [row for row in walk_forward if row["round_trip_cost_bps"] == 10]
    stress = [row for row in walk_forward if row["round_trip_cost_bps"] == 20]
    positive = sum(float(row["total_return"]) > 0 for row in base)
    stitched = prod(1 + float(row["total_return"]) for row in base) - 1
    stitched_stress = prod(1 + float(row["total_return"]) for row in stress) - 1
    holdout = next(
        row
        for row in fixed
        if row["segment"] == "reference_recent_20pct"
        and row["horizon_hours"] == horizon
        and row["round_trip_cost_bps"] == 10
    )
    top10 = next(
        row
        for row in concentration
        if row["segment"] == "validation_60_to_80pct" and row["removed_top_winners"] == 10
    )
    market_regimes = [
        row
        for row in regimes
        if row["dimension"] == "market_regime"
        and row["horizon_hours"] == 24
        and int(row["count"] or 0) >= 20
    ]
    total_regime_episodes = sum(int(row["count"]) for row in market_regimes)
    regime_positive = sum(float(row["mean"] or 0) > 0 for row in market_regimes)
    regime_concentration = (
        max(int(row["count"]) for row in market_regimes) / total_regime_episodes
        if total_regime_episodes
        else 1.0
    )
    if (
        stitched <= 0
        or positive < max(1, int(np.ceil(len(base) * 2 / 3)))
        or float(holdout["total_return"]) <= 0
    ):
        return "REJECTED_OOS_WEAK"
    if stitched_stress <= 0:
        return "REJECTED_COST_FRAGILE"
    if float(top10["return_after_removal"]) <= 0:
        return "REJECTED_TEMPORALLY_UNSTABLE"
    if regime_positive < 2 or regime_concentration > 0.80:
        return "REJECTED_TEMPORALLY_UNSTABLE"
    return "RESEARCH_CANDIDATE_V2"


def _yearly(episodes: tuple[EntryEpisode, ...]) -> list[dict[str, Any]]:
    grouped: dict[int, list[float]] = {}
    for item in episodes:
        value = item.returns[24]
        if value is not None:
            grouped.setdefault(datetime.fromisoformat(item.signal_timestamp).year, []).append(value)
    return [
        {
            "year": year,
            "episode_count": len(values),
            "mean_24h_return": mean(values),
            "median_24h_return": median(values),
            "positive_rate": sum(value > 0 for value in values) / len(values),
        }
        for year, values in sorted(grouped.items())
    ]


def _cost_rows(fixed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "segment": row["segment"],
            "horizon_hours": row["horizon_hours"],
            "round_trip_cost_bps": row["round_trip_cost_bps"],
            "total_return": row["total_return"],
            "Sharpe": row["Sharpe"],
            "max_drawdown": row["max_drawdown"],
        }
        for row in fixed
    ]


def _scoreboard(
    candidate_id: str,
    variant_id: str,
    forward: list[dict[str, Any]],
    temporal: list[dict[str, Any]],
    regimes: list[dict[str, Any]],
    random_rows: list[dict[str, Any]],
    fixed: list[dict[str, Any]],
    concentration: list[dict[str, Any]],
    walk_forward: list[dict[str, Any]],
    years: list[dict[str, Any]],
    status: str,
    stage: str,
    best_horizon: int | None,
) -> dict[str, Any]:
    f24 = next(
        row for row in forward if row["scope"] == "development" and row["horizon_hours"] == 24
    )
    recent = next(
        row
        for row in forward
        if row["scope"] == "reference_recent_20pct" and row["horizon_hours"] == 24
    )
    random24 = next(row for row in random_rows if row["horizon_hours"] == 24)
    fixed10 = next(
        (
            row
            for row in fixed
            if row["segment"] == "validation_60_to_80pct"
            and row["horizon_hours"] == best_horizon
            and row["round_trip_cost_bps"] == 10
        ),
        None,
    )
    fixed20 = next(
        (
            row
            for row in fixed
            if row["segment"] == "validation_60_to_80pct"
            and row["horizon_hours"] == best_horizon
            and row["round_trip_cost_bps"] == 20
        ),
        None,
    )
    wf10 = [row for row in walk_forward if row["round_trip_cost_bps"] == 10]
    top5 = next(
        (
            row
            for row in concentration
            if row["segment"] == "validation_60_to_80pct" and row["removed_top_winners"] == 5
        ),
        None,
    )
    positive_years = sum(float(row["mean_24h_return"]) > 0 for row in years)
    regime_groups = [
        row
        for row in regimes
        if row["dimension"] == "market_regime"
        and row["horizon_hours"] == 24
        and int(row["count"] or 0) >= 20
    ]
    return {
        "candidate_id": candidate_id,
        "primary_variant_id": variant_id,
        "episodes": f24["count"],
        "forward_edge": f24["excess_vs_unconditional"],
        "forward_edge_vs_vwap_v1": f24["excess_vs_vwap_v1"],
        "recent_edge": recent["mean"],
        "random_percentile": random24["random_percentile"],
        "best_fixed_exit": best_horizon,
        "10bps_return": fixed10["total_return"] if fixed10 else None,
        "20bps_return": fixed20["total_return"] if fixed20 else None,
        "max_drawdown": fixed10["max_drawdown"] if fixed10 else None,
        "Sharpe": fixed10["Sharpe"] if fixed10 else None,
        "profit_factor": fixed10["profit_factor"] if fixed10 else None,
        "top5_concentration": (float(fixed10["total_return"]) - float(top5["return_after_removal"]))
        if fixed10 and top5
        else None,
        "walk_forward_return": prod(1 + float(row["total_return"]) for row in wf10) - 1
        if wf10
        else None,
        "walk_forward_sharpe": median(
            float(row["Sharpe"]) for row in wf10 if row["Sharpe"] is not None
        )
        if wf10 and any(row["Sharpe"] is not None for row in wf10)
        else None,
        "positive_oos_windows": sum(float(row["total_return"]) > 0 for row in wf10)
        if wf10
        else None,
        "temporal_fragility": positive_years < max(2, len(years) - 1),
        "regime_fragility": bool(
            regime_groups
            and (
                sum(float(row["mean"] or 0) > 0 for row in regime_groups) < 2
                or max(int(row["count"]) for row in regime_groups)
                / sum(int(row["count"]) for row in regime_groups)
                > 0.80
            )
        ),
        "cost_fragility": bool(fixed20 and float(fixed20["total_return"]) <= 0),
        "stage_reached": stage,
        "final_status": status,
    }


def _survivor_rank(result: CandidateResult) -> tuple[float, float, float, float]:
    row = result.scoreboard
    windows = len(result.walk_forward_rows) // 2
    positive_ratio = float(row["positive_oos_windows"] or 0) / max(windows, 1)
    return (
        float(row["random_percentile"] or 0),
        positive_ratio,
        float(row["20bps_return"] or 0),
        float(row["max_drawdown"] or -1),
    )
