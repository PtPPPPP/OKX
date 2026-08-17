"""Strict staged elimination for frozen multi-timeframe and volume candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import prod
from typing import Any

from app.domain.market import Candle
from backtest.strategy_v2_candidates import (
    CandidateVariant,
    EntryEpisode,
    build_entry_episodes,
)
from backtest.strategy_v2_research import (
    _cost_rows,
    _fixed_exit_analysis,
    _fixed_exit_gate,
    _forward_analysis,
    _random_analysis,
    _regime_analysis,
    _select_fixed_horizon,
    _temporal_analysis,
    _walk_forward,
    _yearly,
)
from backtest.strategy_v3_candidates import SignalSet, V3Variant, generate_v3_signals
from backtest.strategy_v3_features import HigherTimeframeCandle
from backtest.vwap_signal_edge import CORE_HORIZONS

MIN_EPISODES = 100


@dataclass(frozen=True, slots=True)
class V3CandidateResult:
    candidate_id: str
    primary_variant_id: str
    status: str
    stage_reached: str
    episodes: tuple[dict[str, Any], ...]
    forward_rows: tuple[dict[str, Any], ...]
    control_rows: tuple[dict[str, Any], ...]
    random_rows: tuple[dict[str, Any], ...]
    regime_rows: tuple[dict[str, Any], ...]
    temporal_rows: tuple[dict[str, Any], ...]
    fixed_rows: tuple[dict[str, Any], ...]
    cost_rows: tuple[dict[str, Any], ...]
    concentration_rows: tuple[dict[str, Any], ...]
    walk_forward_rows: tuple[dict[str, Any], ...]
    yearly_rows: tuple[dict[str, Any], ...]
    scoreboard: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StrategyV3Study:
    candidate_specs: tuple[dict[str, Any], ...]
    variant_count: int
    variant_rows: tuple[dict[str, Any], ...]
    results: tuple[V3CandidateResult, ...]
    scoreboard: tuple[dict[str, Any], ...]
    surviving_candidate: str | None
    final_state: str
    historical_final_holdout_pristine: bool


def run_strategy_v3_study(
    candles: list[Candle],
    bars4h: tuple[HigherTimeframeCandle, ...],
    variants: tuple[V3Variant, ...],
    *,
    vwap_forward_means: dict[int, float],
    v2_forward_means: dict[str, dict[int, float]],
) -> StrategyV3Study:
    holdout_index = int(len(candles) * 0.8)
    research_index = int(len(candles) * 0.6)
    episode_map: dict[str, tuple[EntryEpisode, ...]] = {}
    signals_map: dict[str, SignalSet] = {}
    variant_rows: list[dict[str, Any]] = []
    for variant in variants:
        signals = generate_v3_signals(candles, bars4h, variant)
        signals_map[variant.variant_id] = signals
        episodes = _episodes(candles, signals.candidate, variant)
        episode_map[variant.variant_id] = episodes
        rows = _forward_analysis(
            candles,
            episodes,
            _adapter(variant),
            holdout_index,
            vwap_forward_means,
        )
        relevant = v2_forward_means[_relevant_v2_candidate(variant.candidate_id)]
        for row in rows:
            row["excess_vs_relevant_v2"] = (
                float(row["mean"]) - relevant[int(row["horizon_hours"])]
                if row["mean"] is not None
                else None
            )
        variant_rows.extend(rows)

    results: list[V3CandidateResult] = []
    candidate_ids = tuple(dict.fromkeys(item.candidate_id for item in variants))
    for candidate_id in candidate_ids:
        candidate_variants = tuple(item for item in variants if item.candidate_id == candidate_id)
        primary = next(item for item in candidate_variants if item.primary)
        episodes = episode_map[primary.variant_id]
        forward = [row for row in variant_rows if row["variant_id"] == primary.variant_id]
        controls = _control_analysis(
            candles,
            primary,
            signals_map[primary.variant_id],
            holdout_index,
            vwap_forward_means,
        )
        random_rows = _random_analysis(candles, episodes, _adapter(primary))
        regimes = _regime_analysis(episodes)
        temporal = _temporal_analysis(episodes)
        gate = _early_gate(
            primary,
            forward,
            controls,
            random_rows,
            temporal,
            candidate_variants,
            variant_rows,
        )
        fixed: list[dict[str, Any]] = []
        costs: list[dict[str, Any]] = []
        concentration: list[dict[str, Any]] = []
        walk_forward: list[dict[str, Any]] = []
        best_horizon: int | None = None
        stage = "forward_edge"
        status = str(gate["status"])
        if bool(gate["passed"]):
            stage = "fixed_exit"
            fixed, concentration = _fixed_exit_analysis(
                candles, episodes, research_index, holdout_index
            )
            costs = _cost_rows(fixed)
            best_horizon = _select_fixed_horizon(fixed)
            fixed_gate = _fixed_exit_gate(fixed, concentration, best_horizon)
            status = _map_v2_status(str(fixed_gate["status"]))
            if bool(fixed_gate["passed"]):
                stage = "walk_forward"
                walk_forward = _walk_forward(candles, episodes, best_horizon, holdout_index)
                status = _walk_forward_status(walk_forward, fixed, concentration, best_horizon)
        years = _yearly(episodes)
        scoreboard = _scoreboard(
            primary,
            forward,
            controls,
            random_rows,
            fixed,
            concentration,
            walk_forward,
            temporal,
            status,
            stage,
            best_horizon,
        )
        results.append(
            V3CandidateResult(
                candidate_id,
                primary.variant_id,
                status,
                stage,
                tuple(item.flat() for item in episodes),
                tuple(forward),
                tuple(controls),
                tuple(random_rows),
                tuple(regimes),
                tuple(temporal),
                tuple(fixed),
                tuple(costs),
                tuple(concentration),
                tuple(walk_forward),
                tuple(years),
                scoreboard,
            )
        )

    qualified = [item for item in results if item.status == "RESEARCH_CANDIDATE_V3"]
    if len(qualified) > 1:
        retained = max(qualified, key=_rank)
        results = [
            item
            if item.status != "RESEARCH_CANDIDATE_V3" or item is retained
            else replace(
                item,
                status="RESEARCH_PROMISING",
                scoreboard={**item.scoreboard, "final_status": "RESEARCH_PROMISING"},
            )
            for item in results
        ]
    survivor = next(
        (item.candidate_id for item in results if item.status == "RESEARCH_CANDIDATE_V3"),
        None,
    )
    if survivor:
        final_state = "RESEARCH_CANDIDATE_V3_READY_FOR_HUMAN_REVIEW"
    elif any(item.status == "RESEARCH_PROMISING" for item in results):
        final_state = "STRATEGY_V3_ONE_CANDIDATE_PROMISING"
    else:
        final_state = "NO_STRATEGY_CANDIDATE_FOUND_V3"
    return StrategyV3Study(
        tuple(asdict(item) for item in variants),
        len(variants),
        tuple(variant_rows),
        tuple(results),
        tuple(item.scoreboard for item in results),
        survivor,
        final_state,
        False,
    )


def _episodes(
    candles: list[Candle], signals: tuple[bool, ...], variant: V3Variant
) -> tuple[EntryEpisode, ...]:
    return build_entry_episodes(candles, signals, _adapter(variant), CORE_HORIZONS)


def _adapter(variant: V3Variant, *, suffix: str = "") -> CandidateVariant:
    return CandidateVariant(
        variant.candidate_id,
        variant.variant_id + suffix,
        variant.economic_rationale,
        variant.entry_rule,
        variant.expected_failure_mode,
        variant.parameters,
        variant.primary,
    )


def _control_analysis(
    candles: list[Candle],
    variant: V3Variant,
    signals: SignalSet,
    holdout_index: int,
    vwap_forward_means: dict[int, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    controls = (
        ("without_htf", signals.without_htf),
        ("without_volume", signals.without_volume),
    )
    for control_id, values in controls:
        if values is None:
            continue
        episodes = _episodes(candles, values, replace(variant, variant_id=control_id))
        control_rows = _forward_analysis(
            candles,
            episodes,
            _adapter(variant, suffix=f"__{control_id}"),
            holdout_index,
            vwap_forward_means,
        )
        for row in control_rows:
            row["control_id"] = control_id
        rows.extend(control_rows)
    return rows


def _early_gate(
    primary: V3Variant,
    forward: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    random_rows: list[dict[str, Any]],
    temporal: list[dict[str, Any]],
    candidate_variants: tuple[V3Variant, ...],
    variant_rows: list[dict[str, Any]],
) -> dict[str, object]:
    development = {
        int(row["horizon_hours"]): row for row in forward if row["scope"] == "development"
    }
    recent = next(
        row
        for row in forward
        if row["scope"] == "reference_recent_20pct" and int(row["horizon_hours"]) == 24
    )
    random24 = next(row for row in random_rows if int(row["horizon_hours"]) == 24)
    candidate_count = int(development[24]["count"] or 0)
    control24 = [
        row for row in controls if row["scope"] == "development" and int(row["horizon_hours"]) == 24
    ]
    control_count = max((int(row["count"] or 0) for row in control24), default=candidate_count)
    if candidate_count < MIN_EPISODES or candidate_count / max(control_count, 1) < 0.20:
        return {"passed": False, "status": "REJECTED_SAMPLE_TOO_SMALL"}
    incremental = all(
        float(development[24]["mean"] or 0) > float(row["mean"] or 0)
        and float(development[24]["mean_ci_low"] or 0) > float(row["mean"] or 0)
        for row in control24
    )
    if control24 and not incremental:
        return {"passed": False, "status": "REJECTED_NO_INCREMENTAL_VALUE"}
    positive_horizons = sum(
        float(development[h]["excess_vs_unconditional"] or 0) > 0 for h in (6, 12, 24, 48)
    )
    if (
        float(development[24]["mean_ci_low"] or 0)
        <= float(development[24]["unconditional_mean"] or 0)
        or positive_horizons < 3
    ):
        return {"passed": False, "status": "REJECTED_NO_EDGE"}
    if float(random24["random_percentile"]) < 0.80:
        return {"passed": False, "status": "REJECTED_RANDOM_LIKE"}
    variant_ids = {item.variant_id for item in candidate_variants}
    variant24 = [
        row
        for row in variant_rows
        if row["variant_id"] in variant_ids
        and row["scope"] == "development"
        and int(row["horizon_hours"]) == 24
    ]
    variant_stable = all(float(row["excess_vs_unconditional"] or 0) > 0 for row in variant24)
    years = [
        row
        for row in temporal
        if row["dimension"] == "year" and row["holdout"] is False and row["mean"] is not None
    ]
    if (
        float(recent["mean"] or 0) <= 0
        or sum(float(row["mean"]) > 0 for row in years) < 3
        or not variant_stable
    ):
        return {"passed": False, "status": "REJECTED_TEMPORAL_INSTABILITY"}
    del primary
    return {"passed": True, "status": "RESEARCH_PROMISING"}


def _walk_forward_status(
    rows: list[dict[str, Any]],
    fixed: list[dict[str, Any]],
    concentration: list[dict[str, Any]],
    horizon: int,
) -> str:
    base = [row for row in rows if int(row["round_trip_cost_bps"]) == 10]
    stress = [row for row in rows if int(row["round_trip_cost_bps"]) == 20]
    stitched = prod(1 + float(row["total_return"]) for row in base) - 1
    stitched_stress = prod(1 + float(row["total_return"]) for row in stress) - 1
    recent = next(
        row
        for row in fixed
        if row["segment"] == "reference_recent_20pct"
        and int(row["horizon_hours"]) == horizon
        and int(row["round_trip_cost_bps"]) == 10
    )
    top10 = next(
        row
        for row in concentration
        if row["segment"] == "validation_60_to_80pct" and int(row["removed_top_winners"]) == 10
    )
    if (
        stitched <= 0
        or sum(float(row["total_return"]) > 0 for row in base) * 3 < len(base) * 2
        or float(recent["total_return"]) <= 0
    ):
        return "REJECTED_OOS_WEAK"
    if stitched_stress <= 0:
        return "REJECTED_COST_FRAGILE"
    if float(top10["return_after_removal"]) <= 0:
        return "REJECTED_PROFIT_CONCENTRATION"
    return "RESEARCH_CANDIDATE_V3"


def _scoreboard(
    variant: V3Variant,
    forward: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    random_rows: list[dict[str, Any]],
    fixed: list[dict[str, Any]],
    concentration: list[dict[str, Any]],
    walk_forward: list[dict[str, Any]],
    temporal: list[dict[str, Any]],
    status: str,
    stage: str,
    horizon: int | None,
) -> dict[str, Any]:
    development = {
        int(row["horizon_hours"]): row for row in forward if row["scope"] == "development"
    }
    control24 = {
        str(row["control_id"]): row
        for row in controls
        if row["scope"] == "development" and int(row["horizon_hours"]) == 24
    }
    htf_delta = (
        float(development[24]["mean"]) - float(control24["without_htf"]["mean"])
        if "without_htf" in control24
        else None
    )
    volume_delta = (
        float(development[24]["mean"]) - float(control24["without_volume"]["mean"])
        if "without_volume" in control24
        else None
    )
    baseline_count = max(
        (int(row["count"] or 0) for row in control24.values()),
        default=int(development[24]["count"] or 0),
    )
    fixed10 = next(
        (
            row
            for row in fixed
            if row["segment"] == "validation_60_to_80pct"
            and row["horizon_hours"] == horizon
            and int(row["round_trip_cost_bps"]) == 10
        ),
        None,
    )
    fixed20 = next(
        (
            row
            for row in fixed
            if row["segment"] == "validation_60_to_80pct"
            and row["horizon_hours"] == horizon
            and int(row["round_trip_cost_bps"]) == 20
        ),
        None,
    )
    remove5 = next(
        (
            row
            for row in concentration
            if row["segment"] == "validation_60_to_80pct" and int(row["removed_top_winners"]) == 5
        ),
        None,
    )
    remove10 = next(
        (
            row
            for row in concentration
            if row["segment"] == "validation_60_to_80pct" and int(row["removed_top_winners"]) == 10
        ),
        None,
    )
    wf10 = [row for row in walk_forward if int(row["round_trip_cost_bps"]) == 10]
    years = [
        row
        for row in temporal
        if row["dimension"] == "year" and row["holdout"] is False and row["mean"] is not None
    ]
    return {
        "candidate_id": variant.candidate_id,
        "hypothesis": variant.hypothesis,
        "episodes": development[24]["count"],
        "control_episodes": baseline_count,
        "sample_size_reduction": 1 - int(development[24]["count"] or 0) / max(baseline_count, 1),
        "htf_incremental_value": _incremental_pass(development[24], control24.get("without_htf")),
        "htf_incremental_delta": htf_delta,
        "volume_incremental_value": _incremental_pass(
            development[24], control24.get("without_volume")
        ),
        "volume_incremental_delta": volume_delta,
        "6h_excess": development[6]["excess_vs_unconditional"],
        "12h_excess": development[12]["excess_vs_unconditional"],
        "24h_excess": development[24]["excess_vs_unconditional"],
        "48h_excess": development[48]["excess_vs_unconditional"],
        "24h_excess_vs_vwap_v1": development[24]["excess_vs_vwap_v1"],
        "24h_excess_vs_relevant_v2": development[24]["excess_vs_relevant_v2"],
        "random_percentile": next(
            row["random_percentile"] for row in random_rows if int(row["horizon_hours"]) == 24
        ),
        "fixed_exit_best_horizon": horizon,
        "return_10bps": fixed10["total_return"] if fixed10 else None,
        "return_20bps": fixed20["total_return"] if fixed20 else None,
        "max_drawdown": fixed10["max_drawdown"] if fixed10 else None,
        "Sharpe": fixed10["Sharpe"] if fixed10 else None,
        "profit_factor": fixed10["profit_factor"] if fixed10 else None,
        "remove_top5_result": remove5["return_after_removal"] if remove5 else None,
        "remove_top10_result": remove10["return_after_removal"] if remove10 else None,
        "walk_forward_return": prod(1 + float(row["total_return"]) for row in wf10) - 1
        if wf10
        else None,
        "positive_oos_windows": sum(float(row["total_return"]) > 0 for row in wf10)
        if wf10
        else None,
        "cost_fragility": bool(fixed20 and float(fixed20["total_return"]) <= 0),
        "temporal_fragility": sum(float(row["mean"]) > 0 for row in years) < 3,
        "profit_concentration": bool(remove5 and float(remove5["return_after_removal"]) <= 0),
        "stage_reached": stage,
        "final_status": status,
    }


def _map_v2_status(status: str) -> str:
    return {
        "REJECTED_OOS_WEAK": "REJECTED_OOS_WEAK",
        "REJECTED_DRAWDOWN": "REJECTED_OOS_WEAK",
        "REJECTED_COST_FRAGILE": "REJECTED_COST_FRAGILE",
        "REJECTED_TEMPORALLY_UNSTABLE": "REJECTED_PROFIT_CONCENTRATION",
        "RESEARCH_PROMISING": "RESEARCH_PROMISING",
    }[status]


def _incremental_pass(candidate: dict[str, Any], control: dict[str, Any] | None) -> bool | None:
    if control is None:
        return None
    control_mean = float(control["mean"] or 0)
    return (
        float(candidate["mean"] or 0) > control_mean
        and float(candidate["mean_ci_low"] or 0) > control_mean
    )


def _relevant_v2_candidate(candidate_id: str) -> str:
    return {
        "htf_pullback_recovery": "momentum_pullback",
        "htf_breakout": "price_breakout",
        "relative_volume_breakout": "price_breakout",
        "htf_volume_momentum": "momentum_pullback",
        "volume_exhaustion_reversal": "confirmed_mean_reversion",
    }[candidate_id]


def _rank(result: V3CandidateResult) -> tuple[float, float, float]:
    row = result.scoreboard
    return (
        float(row["random_percentile"] or 0),
        float(row["walk_forward_return"] or 0),
        float(row["return_20bps"] or 0),
    )
