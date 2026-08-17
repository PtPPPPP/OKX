"""Deterministic signal-edge statistics for the formal VWAP Shadow strategy."""

from __future__ import annotations

import hashlib
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from math import log, sqrt
from statistics import mean, median, stdev

import numpy as np

from app.domain.market import Candle, Instrument
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_shadow_research import ShadowSignalRecord, replay_shadow

RETURN_HORIZONS = (1, 3, 6, 12, 24, 48, 72, 96, 168)
CORE_HORIZONS = (1, 3, 6, 12, 24, 48, 72)
EXCURSION_HORIZONS = (6, 12, 24, 48, 72)
COST_HORIZONS = (6, 12, 24, 48)
COST_BPS = (0, 5, 10, 20)
BOOTSTRAP_SEED = 20260812
BOOTSTRAP_SAMPLES = 2000
RANDOM_BENCHMARK_SAMPLES = 1000


@dataclass(frozen=True, slots=True)
class SignalObservation:
    signal_id: str
    signal_timestamp: str
    entry_reference_timestamp: str
    entry_reference_price: float
    vwap: float
    deviation_bps: float
    market_regime: str
    volatility_regime: str
    strength_bucket: str
    episode_id: str
    temporal_slice: str
    holdout: bool
    returns: dict[int, float | None]
    mfe: dict[int, float | None]
    mae: dict[int, float | None]
    time_to_mfe: dict[int, int | None]
    time_to_mae: dict[int, int | None]
    path_type: dict[int, str | None]

    def flat(self) -> dict[str, object]:
        row: dict[str, object] = {
            "signal_id": self.signal_id,
            "signal_timestamp": self.signal_timestamp,
            "entry_reference_timestamp": self.entry_reference_timestamp,
            "entry_reference_price": self.entry_reference_price,
            "vwap": self.vwap,
            "deviation_bps": self.deviation_bps,
            "market_regime": self.market_regime,
            "volatility_regime": self.volatility_regime,
            "strength_bucket": self.strength_bucket,
            "episode_id": self.episode_id,
            "temporal_slice": self.temporal_slice,
            "holdout": self.holdout,
        }
        for horizon in RETURN_HORIZONS:
            row[f"return_{horizon}h"] = self.returns[horizon]
        for horizon in EXCURSION_HORIZONS:
            row[f"mfe_{horizon}h"] = self.mfe[horizon]
            row[f"mae_{horizon}h"] = self.mae[horizon]
            row[f"time_to_mfe_{horizon}h"] = self.time_to_mfe[horizon]
            row[f"time_to_mae_{horizon}h"] = self.time_to_mae[horizon]
            row[f"path_type_{horizon}h"] = self.path_type[horizon]
        return row


@dataclass(frozen=True, slots=True)
class SignalEdgeStudy:
    observations: tuple[SignalObservation, ...]
    signal_records: tuple[ShadowSignalRecord, ...]
    forward_statistics: tuple[dict[str, object], ...]
    mfe_mae_statistics: tuple[dict[str, object], ...]
    benchmark_statistics: tuple[dict[str, object], ...]
    regime_statistics: tuple[dict[str, object], ...]
    monthly_statistics: tuple[dict[str, object], ...]
    quarterly_statistics: tuple[dict[str, object], ...]
    temporal_statistics: tuple[dict[str, object], ...]
    cost_statistics: tuple[dict[str, object], ...]
    clustering_statistics: dict[str, object]
    random_benchmark: tuple[dict[str, object], ...]
    market_context: dict[str, object]


def run_signal_edge_study(
    candles: list[Candle],
    instrument: Instrument,
    parameters: VWAPShadowParameters,
) -> SignalEdgeStudy:
    records = replay_shadow(candles, instrument, parameters)
    buy_indices = [index for index, record in enumerate(records) if record.proposal_eligible]
    episode_by_index = _episode_ids(buy_indices, candles)
    strength_by_index = _strength_buckets(buy_indices, records)
    cutoff = candles[int(len(candles) * 0.8)].timestamp
    observations: list[SignalObservation] = []
    for index in buy_indices:
        if index + 1 >= len(candles):
            continue
        record = records[index]
        entry_index = index + 1
        entry_price = float(candles[entry_index].open)
        returns = {
            horizon: _forward_return(candles, entry_index, entry_price, horizon)
            for horizon in RETURN_HORIZONS
        }
        mfe: dict[int, float | None] = {}
        mae: dict[int, float | None] = {}
        time_mfe: dict[int, int | None] = {}
        time_mae: dict[int, int | None] = {}
        path_type: dict[int, str | None] = {}
        for horizon in EXCURSION_HORIZONS:
            excursion = _excursion(candles, entry_index, entry_price, horizon)
            mfe[horizon], mae[horizon], time_mfe[horizon], time_mae[horizon], path_type[horizon] = (
                excursion
            )
        timestamp = candles[index].timestamp.astimezone(UTC)
        observations.append(
            SignalObservation(
                signal_id=hashlib.sha256(
                    f"vwap_shadow|{timestamp.isoformat()}|{parameters.vwap_window}|"
                    f"{parameters.buy_deviation_bps}".encode()
                ).hexdigest()[:24],
                signal_timestamp=timestamp.isoformat(),
                entry_reference_timestamp=candles[entry_index]
                .timestamp.astimezone(UTC)
                .isoformat(),
                entry_reference_price=entry_price,
                vwap=float(Decimal(record.vwap or "0")),
                deviation_bps=float(Decimal(record.deviation_bps or "0")),
                market_regime=_market_regime(candles, index),
                volatility_regime=_volatility_regime(candles, index),
                strength_bucket=strength_by_index[index],
                episode_id=episode_by_index[index],
                temporal_slice=_temporal_slice(index, len(candles)),
                holdout=timestamp >= cutoff,
                returns=returns,
                mfe=mfe,
                mae=mae,
                time_to_mfe=time_mfe,
                time_to_mae=time_mae,
                path_type=path_type,
            )
        )
    observations_tuple = tuple(observations)
    return SignalEdgeStudy(
        observations=observations_tuple,
        signal_records=tuple(records),
        forward_statistics=tuple(_forward_statistics(observations_tuple)),
        mfe_mae_statistics=tuple(_mfe_mae_statistics(observations_tuple)),
        benchmark_statistics=tuple(_benchmark(candles, observations_tuple)),
        regime_statistics=tuple(_regime_statistics(observations_tuple)),
        monthly_statistics=tuple(_calendar_statistics(observations_tuple, quarterly=False)),
        quarterly_statistics=tuple(_calendar_statistics(observations_tuple, quarterly=True)),
        temporal_statistics=tuple(_temporal_statistics(observations_tuple)),
        cost_statistics=tuple(_cost_statistics(observations_tuple)),
        clustering_statistics=_clustering(observations_tuple),
        random_benchmark=tuple(_random_benchmark(candles, observations_tuple)),
        market_context=_market_context(candles),
    )


def _forward_return(
    candles: list[Candle], entry_index: int, entry_price: float, horizon: int
) -> float | None:
    end_index = entry_index + horizon - 1
    return float(candles[end_index].close) / entry_price - 1 if end_index < len(candles) else None


def _excursion(
    candles: list[Candle], entry_index: int, entry_price: float, horizon: int
) -> tuple[float | None, float | None, int | None, int | None, str | None]:
    end_index = entry_index + horizon
    if end_index > len(candles):
        return None, None, None, None, None
    window = candles[entry_index:end_index]
    highs = [float(candle.high) / entry_price - 1 for candle in window]
    lows = [float(candle.low) / entry_price - 1 for candle in window]
    max_index = int(np.argmax(highs))
    min_index = int(np.argmin(lows))
    ending = float(window[-1].close) / entry_price - 1
    if min_index < max_index:
        path = "down_then_up"
    elif max_index < min_index:
        path = "up_then_down"
    elif ending > 0:
        path = "positive_trend"
    elif ending < 0:
        path = "negative_trend"
    else:
        path = "flat_or_intrabar_ambiguous"
    return max(highs), min(lows), max_index + 1, min_index + 1, path


def _episode_ids(indices: list[int], candles: list[Candle]) -> dict[int, str]:
    result: dict[int, str] = {}
    episode = 0
    episode_id = ""
    previous: int | None = None
    for index in indices:
        if previous is None or index != previous + 1:
            episode += 1
            episode_id = f"episode_{episode:05d}_{candles[index].timestamp:%Y%m%dT%H%M%SZ}"
        result[index] = episode_id
        previous = index
    return result


def _strength_buckets(indices: list[int], records: list[ShadowSignalRecord]) -> dict[int, str]:
    values = np.array([float(Decimal(records[index].deviation_bps or "0")) for index in indices])
    if len(values) == 0:
        return {}
    boundaries = np.quantile(values, [0.25, 0.5, 0.75])
    return {
        index: f"Q{int(np.searchsorted(boundaries, value, side='right')) + 1}"
        for index, value in zip(indices, values, strict=True)
    }


def _market_regime(candles: list[Candle], index: int, window: int = 168) -> str:
    start = max(0, index - window + 1)
    closes = [float(candle.close) for candle in candles[start : index + 1]]
    if len(closes) < window:
        return "insufficient_history"
    ratio = closes[-1] / mean(closes) - 1
    if ratio > 0.03:
        return "bull"
    if ratio < -0.03:
        return "bear"
    return "sideways"


def _volatility_regime(candles: list[Candle], index: int, window: int = 168) -> str:
    start = max(1, index - window + 1)
    returns = [
        log(float(candles[position].close) / float(candles[position - 1].close))
        for position in range(start, index + 1)
    ]
    if len(returns) < window - 1:
        return "insufficient_history"
    annualized = stdev(returns) * sqrt(24 * 365)
    if annualized < 0.40:
        return "low"
    if annualized > 0.80:
        return "high"
    return "normal"


def _temporal_slice(index: int, count: int) -> str:
    fraction = index / max(count, 1)
    return (
        "first_third" if fraction < 1 / 3 else "middle_third" if fraction < 2 / 3 else "last_third"
    )


def descriptive(values: list[float]) -> dict[str, object]:
    array = np.array(values, dtype=float)
    if len(array) == 0:
        return {
            key: None
            for key in (
                "count",
                "mean",
                "median",
                "std",
                "positive_rate",
                "p10",
                "p25",
                "p50",
                "p75",
                "p90",
            )
        }
    return {
        "count": len(values),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "std": float(np.std(array, ddof=1)) if len(values) > 1 else 0.0,
        "positive_rate": float(np.mean(array > 0)),
        "p10": float(np.quantile(array, 0.10)),
        "p25": float(np.quantile(array, 0.25)),
        "p50": float(np.quantile(array, 0.50)),
        "p75": float(np.quantile(array, 0.75)),
        "p90": float(np.quantile(array, 0.90)),
    }


def bootstrap_intervals(values: list[float], *, seed_offset: int = 0) -> dict[str, object]:
    if not values:
        return {
            "mean_ci_low": None,
            "mean_ci_high": None,
            "median_ci_low": None,
            "median_ci_high": None,
            "positive_rate_ci_low": None,
            "positive_rate_ci_high": None,
        }
    generator = np.random.default_rng(BOOTSTRAP_SEED + seed_offset)
    array = np.array(values, dtype=float)
    draws = generator.choice(array, size=(BOOTSTRAP_SAMPLES, len(array)), replace=True)
    metrics = (np.mean(draws, axis=1), np.median(draws, axis=1), np.mean(draws > 0, axis=1))
    names = ("mean", "median", "positive_rate")
    result: dict[str, object] = {}
    for name, metric in zip(names, metrics, strict=True):
        result[f"{name}_ci_low"] = float(np.quantile(metric, 0.025))
        result[f"{name}_ci_high"] = float(np.quantile(metric, 0.975))
    return result


def _values(observations: tuple[SignalObservation, ...], horizon: int) -> list[float]:
    return [value for item in observations if (value := item.returns[horizon]) is not None]


def _observation_scopes(
    observations: tuple[SignalObservation, ...],
) -> tuple[tuple[str, tuple[SignalObservation, ...]], ...]:
    """Return the two declared inference populations in a stable order."""
    return (
        ("signal", observations),
        ("episode", tuple(_episode_representatives(observations))),
    )


def _forward_statistics(observations: tuple[SignalObservation, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for scope, subset in _observation_scopes(observations):
        for horizon in RETURN_HORIZONS:
            values = _values(subset, horizon)
            rows.append(
                {
                    "scope": scope,
                    "horizon_hours": horizon,
                    **descriptive(values),
                    **bootstrap_intervals(
                        values, seed_offset=horizon + (0 if scope == "signal" else 1000)
                    ),
                }
            )
    return rows


def _episode_representatives(
    observations: tuple[SignalObservation, ...],
) -> list[SignalObservation]:
    seen: set[str] = set()
    result: list[SignalObservation] = []
    for item in observations:
        if item.episode_id not in seen:
            seen.add(item.episode_id)
            result.append(item)
    return result


def _mfe_mae_statistics(observations: tuple[SignalObservation, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for horizon in EXCURSION_HORIZONS:
        for metric in ("mfe", "mae"):
            values = [
                value
                for item in observations
                if (value := getattr(item, metric)[horizon]) is not None
            ]
            times = [
                value
                for item in observations
                if (value := getattr(item, f"time_to_{metric}")[horizon]) is not None
            ]
            stats = descriptive(values)
            rows.append(
                {
                    "horizon_hours": horizon,
                    "metric": metric,
                    **stats,
                    "mean_hours_to_extreme": mean(times) if times else None,
                    "median_hours_to_extreme": median(times) if times else None,
                }
            )
    return rows


def _benchmark(
    candles: list[Candle], observations: tuple[SignalObservation, ...]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    market_by_horizon = {
        horizon: descriptive(
            [
                value
                for entry_index in range(1, len(candles))
                if (
                    value := _forward_return(
                        candles, entry_index, float(candles[entry_index].open), horizon
                    )
                )
                is not None
            ]
        )
        for horizon in RETURN_HORIZONS
    }
    for scope, subset in _observation_scopes(observations):
        for horizon in RETURN_HORIZONS:
            signal = _values(subset, horizon)
            signal_stats = descriptive(signal)
            market_stats = market_by_horizon[horizon]
            rows.append(
                {
                    "scope": scope,
                    "horizon_hours": horizon,
                    "signal_count": signal_stats["count"],
                    "signal_forward_mean": signal_stats["mean"],
                    "signal_forward_median": signal_stats["median"],
                    "signal_positive_rate": signal_stats["positive_rate"],
                    "unconditional_count": market_stats["count"],
                    "unconditional_forward_mean": market_stats["mean"],
                    "unconditional_forward_median": market_stats["median"],
                    "unconditional_positive_rate": market_stats["positive_rate"],
                    "signal_excess_return": _difference(signal_stats["mean"], market_stats["mean"]),
                    "positive_rate_excess": _difference(
                        signal_stats["positive_rate"], market_stats["positive_rate"]
                    ),
                }
            )
    return rows


def _group_statistics(
    observations: tuple[SignalObservation, ...], key: str, *, scope: str = "signal"
) -> list[dict[str, object]]:
    grouped: dict[str, list[SignalObservation]] = defaultdict(list)
    for item in observations:
        grouped[str(getattr(item, key))].append(item)
    rows: list[dict[str, object]] = []
    for group, items in sorted(grouped.items()):
        subset = tuple(items)
        for horizon in (6, 12, 24, 48, 72):
            values = _values(subset, horizon)
            mfe = [x for item in subset if (x := item.mfe[horizon]) is not None]
            mae = [x for item in subset if (x := item.mae[horizon]) is not None]
            stats = descriptive(values)
            rows.append(
                {
                    "scope": scope,
                    "dimension": key,
                    "group": group,
                    "horizon_hours": horizon,
                    "count": stats["count"],
                    "median_return": stats["median"],
                    "mean_return": stats["mean"],
                    "positive_rate": stats["positive_rate"],
                    "median_mfe": median(mfe) if mfe else None,
                    "median_mae": median(mae) if mae else None,
                }
            )
    return rows


def _regime_statistics(observations: tuple[SignalObservation, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for scope, subset in _observation_scopes(observations):
        rows.extend(_group_statistics(subset, "market_regime", scope=scope))
        rows.extend(_group_statistics(subset, "volatility_regime", scope=scope))
        rows.extend(_group_statistics(subset, "strength_bucket", scope=scope))
    return rows


def _calendar_statistics(
    observations: tuple[SignalObservation, ...], *, quarterly: bool
) -> list[dict[str, object]]:
    grouped: dict[str, list[SignalObservation]] = defaultdict(list)
    for item in observations:
        timestamp = datetime.fromisoformat(item.signal_timestamp)
        key = (
            f"{timestamp.year}-Q{(timestamp.month - 1) // 3 + 1}"
            if quarterly
            else timestamp.strftime("%Y-%m")
        )
        grouped[key].append(item)
    rows: list[dict[str, object]] = []
    for period, items in sorted(grouped.items()):
        row: dict[str, object] = {"period": period, "buy_count": len(items)}
        for horizon in (6, 12, 24):
            stats = descriptive(_values(tuple(items), horizon))
            row[f"median_return_{horizon}h"] = stats["median"]
            row[f"positive_rate_{horizon}h"] = stats["positive_rate"]
        rows.append(row)
    return rows


def _temporal_statistics(observations: tuple[SignalObservation, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for scope, subset in _observation_scopes(observations):
        rows.extend(_group_statistics(subset, "temporal_slice", scope=scope))
        train = tuple(item for item in subset if not item.holdout)
        holdout = tuple(item for item in subset if item.holdout)
        for group, period in (
            ("first_80_percent", train),
            ("holdout_last_20_percent", holdout),
        ):
            for horizon in (6, 12, 24, 48, 72):
                stats = descriptive(_values(period, horizon))
                rows.append(
                    {
                        "scope": scope,
                        "dimension": "holdout",
                        "group": group,
                        "horizon_hours": horizon,
                        "count": stats["count"],
                        "median_return": stats["median"],
                        "mean_return": stats["mean"],
                        "positive_rate": stats["positive_rate"],
                        "median_mfe": None,
                        "median_mae": None,
                    }
                )
    return rows


def _cost_statistics(observations: tuple[SignalObservation, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for scope, subset in _observation_scopes(observations):
        for horizon in COST_HORIZONS:
            gross = _values(subset, horizon)
            gross_median = float(np.median(gross)) if gross else None
            for cost in COST_BPS:
                net = [value - cost / 10000 for value in gross]
                stats = descriptive(net)
                rows.append(
                    {
                        "scope": scope,
                        "horizon_hours": horizon,
                        "assumed_round_trip_cost_bps": cost,
                        "count": stats["count"],
                        "mean_net_forward_return": stats["mean"],
                        "median_net_forward_return": stats["median"],
                        "positive_rate_net": stats["positive_rate"],
                        "break_even_cost_bps": gross_median * 10000
                        if gross_median is not None
                        else None,
                        "cost_fragility": gross_median is not None and gross_median * 10000 <= 10,
                    }
                )
    return rows


def _clustering(observations: tuple[SignalObservation, ...]) -> dict[str, object]:
    timestamps = [datetime.fromisoformat(item.signal_timestamp) for item in observations]
    gaps = [(later - earlier).total_seconds() / 3600 for earlier, later in pairwise(timestamps)]
    episodes = {item.episode_id for item in observations}
    per_day = Counter(timestamp.date().isoformat() for timestamp in timestamps)
    per_week = Counter(
        f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"
        for timestamp in timestamps
    )
    consecutive = Counter(item.episode_id for item in observations)
    result: dict[str, object] = {
        "raw_signal_count": len(observations),
        "signal_episode_count": len(episodes),
        "median_gap_hours": median(gaps) if gaps else None,
        "p25_gap_hours": float(np.quantile(gaps, 0.25)) if gaps else None,
        "p75_gap_hours": float(np.quantile(gaps, 0.75)) if gaps else None,
        "max_consecutive_buy_bars": max(consecutive.values(), default=0),
        "mean_signals_per_active_day": mean(per_day.values()) if per_day else 0,
        "mean_signals_per_active_week": mean(per_week.values()) if per_week else 0,
    }
    for horizon in (6, 12, 24, 48):
        overlaps = sum(gap < horizon for gap in gaps)
        result[f"overlap_rate_{horizon}h"] = overlaps / len(gaps) if gaps else 0.0
    return result


def _random_benchmark(
    candles: list[Candle], observations: tuple[SignalObservation, ...]
) -> list[dict[str, object]]:
    candidates: dict[str, list[int]] = defaultdict(list)
    for index in range(1, len(candles) - max(RETURN_HORIZONS) + 1):
        candidates[candles[index - 1].timestamp.strftime("%Y-%m")].append(index)
    rows: list[dict[str, object]] = []
    for scope_index, (scope, subset) in enumerate(_observation_scopes(observations)):
        signal_months = Counter(item.signal_timestamp[:7] for item in subset)
        for horizon in CORE_HORIZONS:
            randomizer = random.Random(BOOTSTRAP_SEED + scope_index * 1000 + horizon)
            actual = _values(subset, horizon)
            actual_mean = mean(actual) if actual else 0.0
            random_means: list[float] = []
            for _ in range(RANDOM_BENCHMARK_SAMPLES):
                sample: list[float] = []
                for month, count in signal_months.items():
                    pool = candidates.get(month, [])
                    chosen = randomizer.choices(pool, k=count) if pool else []
                    sample.extend(
                        value
                        for index in chosen
                        if (
                            value := _forward_return(
                                candles, index, float(candles[index].open), horizon
                            )
                        )
                        is not None
                    )
                if sample:
                    random_means.append(mean(sample))
            rows.append(
                {
                    "scope": scope,
                    "horizon_hours": horizon,
                    "signal_mean": actual_mean,
                    "random_mean_median": median(random_means) if random_means else None,
                    "random_mean_p05": float(np.quantile(random_means, 0.05))
                    if random_means
                    else None,
                    "random_mean_p95": float(np.quantile(random_means, 0.95))
                    if random_means
                    else None,
                    "signal_minus_random_median": actual_mean - median(random_means)
                    if random_means
                    else None,
                    "one_sided_random_p_value": sum(value >= actual_mean for value in random_means)
                    / len(random_means)
                    if random_means
                    else None,
                    "seed": BOOTSTRAP_SEED + scope_index * 1000 + horizon,
                    "samples": RANDOM_BENCHMARK_SAMPLES,
                }
            )
    return rows


def _market_context(candles: list[Candle]) -> dict[str, object]:
    closes = [float(candle.close) for candle in candles]
    returns = [log(later / earlier) for earlier, later in pairwise(closes)]
    peak = closes[0]
    drawdowns: list[float] = []
    for close in closes:
        peak = max(peak, close)
        drawdowns.append(close / peak - 1)
    return {
        "total_asset_return": closes[-1] / closes[0] - 1,
        "annualized_volatility": stdev(returns) * sqrt(24 * 365),
        "max_drawdown": min(drawdowns),
    }


def parameter_sensitivity(candles: list[Candle], instrument: Instrument) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for window in (20, 24, 28):
        for deviation in (80, 100, 120):
            parameters = VWAPShadowParameters(
                vwap_window=window, buy_deviation_bps=Decimal(deviation)
            )
            records = replay_shadow(candles, instrument, parameters)
            indices = [index for index, record in enumerate(records) if record.proposal_eligible]
            for horizon in (6, 12, 24, 48):
                values = [
                    value
                    for index in indices
                    if index + 1 < len(candles)
                    and (
                        value := _forward_return(
                            candles, index + 1, float(candles[index + 1].open), horizon
                        )
                    )
                    is not None
                ]
                stats = descriptive(values)
                rows.append(
                    {
                        "vwap_window": window,
                        "buy_deviation_bps": deviation,
                        "horizon_hours": horizon,
                        "is_frozen_baseline": window == 24 and deviation == 100,
                        "signal_count": len(indices),
                        "mean_return": stats["mean"],
                        "median_return": stats["median"],
                        "positive_rate": stats["positive_rate"],
                    }
                )
    return rows


def _difference(left: object, right: object) -> float | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return float(left) - float(right)
