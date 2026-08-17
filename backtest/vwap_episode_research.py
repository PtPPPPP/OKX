"""Research-only VWAP BUY episode semantics and deterministic statistics."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import mean, median

import numpy as np

from app.domain.market import Candle, Instrument
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_shadow_research import ShadowSignalRecord, replay_shadow
from backtest.vwap_signal_edge import (
    CORE_HORIZONS,
    EXCURSION_HORIZONS,
    _excursion,
    _market_regime,
    _temporal_slice,
    _volatility_regime,
    bootstrap_intervals,
    descriptive,
)

EPISODE_INTERVAL = timedelta(hours=1)
TEMPORAL_HORIZONS = (6, 12, 24, 48, 72)


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    start_index: int
    end_index: int
    start_signal_timestamp: str
    end_signal_timestamp: str
    first_entry_reference_timestamp: str | None
    first_entry_reference_price: float | None
    duration_bars: int
    duration_hours: float
    buy_signal_count: int
    max_deviation: float
    min_deviation: float
    start_deviation: float
    end_deviation: float
    start_vwap: float
    start_close: float
    next_bar_open: float | None
    closed: bool
    closure_reason: str
    market_regime: str
    volatility_regime: str
    temporal_slice: str
    holdout: bool
    returns: dict[int, float | None]
    mfe: dict[int, float | None]
    mae: dict[int, float | None]

    def flat(self) -> dict[str, object]:
        row: dict[str, object] = {
            "episode_id": self.episode_id,
            "start_signal_timestamp": self.start_signal_timestamp,
            "end_signal_timestamp": self.end_signal_timestamp,
            "first_entry_reference_timestamp": self.first_entry_reference_timestamp,
            "first_entry_reference_price": self.first_entry_reference_price,
            "duration_bars": self.duration_bars,
            "duration_hours": self.duration_hours,
            "buy_signal_count": self.buy_signal_count,
            "max_deviation": self.max_deviation,
            "min_deviation": self.min_deviation,
            "start_deviation": self.start_deviation,
            "end_deviation": self.end_deviation,
            "start_vwap": self.start_vwap,
            "start_close": self.start_close,
            "next_bar_open": self.next_bar_open,
            "closed": self.closed,
            "closure_reason": self.closure_reason,
            "market_regime": self.market_regime,
            "volatility_regime": self.volatility_regime,
            "temporal_slice": self.temporal_slice,
            "holdout": self.holdout,
        }
        for horizon in CORE_HORIZONS:
            row[f"return_{horizon}h"] = self.returns[horizon]
        for horizon in EXCURSION_HORIZONS:
            row[f"mfe_{horizon}h"] = self.mfe[horizon]
            row[f"mae_{horizon}h"] = self.mae[horizon]
        return row


@dataclass(frozen=True, slots=True)
class EpisodeStudy:
    records: tuple[ShadowSignalRecord, ...]
    episodes: tuple[Episode, ...]
    summary_statistics: dict[str, object]
    forward_statistics: tuple[dict[str, object], ...]
    mfe_mae_statistics: tuple[dict[str, object], ...]
    overlap_statistics: tuple[dict[str, object], ...]
    regime_statistics: tuple[dict[str, object], ...]
    temporal_statistics: tuple[dict[str, object], ...]
    deviation_statistics: dict[str, object]


def run_episode_study(
    candles: list[Candle],
    instrument: Instrument,
    parameters: VWAPShadowParameters,
) -> EpisodeStudy:
    records = replay_shadow(candles, instrument, parameters)
    episodes = build_episodes(candles, records)
    return EpisodeStudy(
        records=tuple(records),
        episodes=episodes,
        summary_statistics=_episode_summary(records, episodes),
        forward_statistics=tuple(_forward_statistics(candles, records, episodes)),
        mfe_mae_statistics=tuple(_mfe_mae_statistics(episodes)),
        overlap_statistics=tuple(_overlap_statistics(episodes)),
        regime_statistics=tuple(_regime_statistics(episodes)),
        temporal_statistics=tuple(_temporal_statistics(episodes)),
        deviation_statistics=descriptive([item.start_deviation for item in episodes]),
    )


def build_episodes(candles: list[Candle], records: list[ShadowSignalRecord]) -> tuple[Episode, ...]:
    """Collapse contiguous BUY records without crossing a candle gap."""
    if len(candles) != len(records):
        raise ValueError("candles and signal records must be aligned")
    if not candles:
        return ()
    cutoff = candles[int(len(candles) * 0.8)].timestamp
    groups: list[tuple[int, int, bool, str]] = []
    start: int | None = None
    for index, record in enumerate(records):
        is_buy = record.proposal_eligible
        if start is not None and index > start:
            contiguous = candles[index].timestamp - candles[index - 1].timestamp == EPISODE_INTERVAL
            if not contiguous:
                groups.append((start, index - 1, False, "data_gap"))
                start = None
        if is_buy and start is None:
            start = index
        elif not is_buy and start is not None:
            groups.append((start, index - 1, True, "buy_condition_ended"))
            start = None
    if start is not None:
        groups.append((start, len(records) - 1, False, "dataset_end"))
    return tuple(
        _build_episode(candles, records, start_index, end_index, closed, reason, cutoff)
        for start_index, end_index, closed, reason in groups
    )


def _build_episode(
    candles: list[Candle],
    records: list[ShadowSignalRecord],
    start_index: int,
    end_index: int,
    closed: bool,
    closure_reason: str,
    cutoff: datetime,
) -> Episode:
    start_candle = candles[start_index]
    end_candle = candles[end_index]
    episode_records = records[start_index : end_index + 1]
    deviations = [_decimal(record.deviation_bps) for record in episode_records]
    entry_index = start_index + 1
    has_entry = entry_index < len(candles) and _is_contiguous(candles, start_index, entry_index)
    entry_price = float(candles[entry_index].open) if has_entry else None
    returns = {
        horizon: _forward_return(candles, entry_index, entry_price, horizon)
        if entry_price is not None
        else None
        for horizon in CORE_HORIZONS
    }
    mfe: dict[int, float | None] = {}
    mae: dict[int, float | None] = {}
    for horizon in EXCURSION_HORIZONS:
        if entry_price is None or not _window_is_contiguous(candles, entry_index, horizon):
            mfe[horizon] = None
            mae[horizon] = None
            continue
        excursion = _excursion(candles, entry_index, entry_price, horizon)
        mfe[horizon], mae[horizon] = excursion[0], excursion[1]
    start_timestamp = start_candle.timestamp.astimezone(UTC)
    identifier = hashlib.sha256(
        f"vwap_episode_v1|{start_timestamp.isoformat()}".encode()
    ).hexdigest()[:24]
    duration_bars = end_index - start_index + 1
    return Episode(
        episode_id=identifier,
        start_index=start_index,
        end_index=end_index,
        start_signal_timestamp=start_timestamp.isoformat(),
        end_signal_timestamp=end_candle.timestamp.astimezone(UTC).isoformat(),
        first_entry_reference_timestamp=(
            candles[entry_index].timestamp.astimezone(UTC).isoformat() if has_entry else None
        ),
        first_entry_reference_price=entry_price,
        duration_bars=duration_bars,
        duration_hours=duration_bars * EPISODE_INTERVAL.total_seconds() / 3600,
        buy_signal_count=len(episode_records),
        max_deviation=max(deviations),
        min_deviation=min(deviations),
        start_deviation=deviations[0],
        end_deviation=deviations[-1],
        start_vwap=_decimal(records[start_index].vwap),
        start_close=float(start_candle.close),
        next_bar_open=entry_price,
        closed=closed,
        closure_reason=closure_reason,
        market_regime=_market_regime(candles, start_index),
        volatility_regime=_volatility_regime(candles, start_index),
        temporal_slice=_temporal_slice(start_index, len(candles)),
        holdout=start_timestamp >= cutoff,
        returns=returns,
        mfe=mfe,
        mae=mae,
    )


def _forward_statistics(
    candles: list[Candle],
    records: list[ShadowSignalRecord],
    episodes: tuple[Episode, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    raw_entries = [index + 1 for index, record in enumerate(records) if record.proposal_eligible]
    for scope in ("raw_signal", "episode"):
        for horizon in CORE_HORIZONS:
            values = (
                [
                    value
                    for entry_index in raw_entries
                    if entry_index < len(candles)
                    and _is_contiguous(candles, entry_index - 1, entry_index)
                    and (
                        value := _forward_return(
                            candles, entry_index, float(candles[entry_index].open), horizon
                        )
                    )
                    is not None
                ]
                if scope == "raw_signal"
                else [value for item in episodes if (value := item.returns[horizon]) is not None]
            )
            rows.append(
                {
                    "scope": scope,
                    "horizon_hours": horizon,
                    **descriptive(values),
                    **bootstrap_intervals(
                        values, seed_offset=horizon + (0 if scope == "raw_signal" else 1000)
                    ),
                }
            )
    return rows


def _mfe_mae_statistics(episodes: tuple[Episode, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for horizon in EXCURSION_HORIZONS:
        for metric in ("mfe", "mae"):
            values = [
                value for item in episodes if (value := getattr(item, metric)[horizon]) is not None
            ]
            rows.append({"horizon_hours": horizon, "metric": metric, **descriptive(values)})
    return rows


def _overlap_statistics(episodes: tuple[Episode, ...]) -> list[dict[str, object]]:
    entries = [
        datetime.fromisoformat(item.first_entry_reference_timestamp)
        for item in episodes
        if item.first_entry_reference_timestamp is not None
    ]
    rows: list[dict[str, object]] = []
    for horizon in EXCURSION_HORIZONS:
        adjacent_overlap = sum(
            current < previous + timedelta(hours=horizon) for previous, current in pairwise(entries)
        )
        accepted_until: datetime | None = None
        tradable = 0
        blocked = 0
        for entry in entries:
            if accepted_until is not None and entry < accepted_until:
                blocked += 1
                continue
            tradable += 1
            accepted_until = entry + timedelta(hours=horizon)
        rows.append(
            {
                "horizon_hours": horizon,
                "total_episodes": len(entries),
                "episode_overlap_count": adjacent_overlap,
                "episode_overlap_rate": adjacent_overlap / len(entries) if entries else 0.0,
                "tradable_if_one_position_only": tradable,
                "blocked_by_existing_position": blocked,
                "blocked_rate": blocked / len(entries) if entries else 0.0,
            }
        )
    return rows


def _regime_statistics(episodes: tuple[Episode, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for dimension in ("market_regime", "volatility_regime"):
        grouped: dict[str, list[Episode]] = defaultdict(list)
        for episode in episodes:
            grouped[str(getattr(episode, dimension))].append(episode)
        rows.extend(_group_returns(grouped, dimension))
    return rows


def _temporal_statistics(episodes: tuple[Episode, ...]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Episode]] = defaultdict(list)
    for episode in episodes:
        timestamp = datetime.fromisoformat(episode.start_signal_timestamp)
        groups[("year", str(timestamp.year))].append(episode)
        groups[("quarter", f"{timestamp.year}-Q{(timestamp.month - 1) // 3 + 1}")].append(episode)
        groups[("third", episode.temporal_slice)].append(episode)
        groups[("holdout", "recent_20_percent" if episode.holdout else "first_80_percent")].append(
            episode
        )
    rows: list[dict[str, object]] = []
    for (dimension, group), items in sorted(groups.items()):
        rows.extend(_return_rows(dimension, group, items))
    return rows


def _group_returns(groups: dict[str, list[Episode]], dimension: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for group, items in sorted(groups.items()):
        rows.extend(_return_rows(dimension, group, items))
    return rows


def _return_rows(dimension: str, group: str, episodes: list[Episode]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for horizon in TEMPORAL_HORIZONS:
        values = [value for item in episodes if (value := item.returns[horizon]) is not None]
        stats = descriptive(values)
        rows.append(
            {
                "dimension": dimension,
                "group": group,
                "horizon_hours": horizon,
                "count": stats["count"],
                "mean_return": stats["mean"],
                "median_return": stats["median"],
                "positive_rate": stats["positive_rate"],
            }
        )
    return rows


def _episode_summary(
    records: list[ShadowSignalRecord], episodes: tuple[Episode, ...]
) -> dict[str, object]:
    signal_counts = [item.buy_signal_count for item in episodes]
    durations = [item.duration_hours for item in episodes]
    gaps = [
        (
            datetime.fromisoformat(later.start_signal_timestamp)
            - datetime.fromisoformat(earlier.end_signal_timestamp)
        ).total_seconds()
        / 3600
        for earlier, later in pairwise(episodes)
    ]
    raw_count = sum(record.proposal_eligible for record in records)
    return {
        "raw_buy_signals": raw_count,
        "episode_count": len(episodes),
        "signal_inflation_ratio": raw_count / len(episodes) if episodes else None,
        "signals_per_episode_mean": mean(signal_counts) if signal_counts else None,
        "signals_per_episode_median": median(signal_counts) if signal_counts else None,
        "signals_per_episode_p90": _quantile(signal_counts, 0.90),
        "episode_duration_mean": mean(durations) if durations else None,
        "episode_duration_median": median(durations) if durations else None,
        "episode_duration_p90": _quantile(durations, 0.90),
        "median_gap_between_episodes": median(gaps) if gaps else None,
        "max_episode_duration": max(durations, default=None),
        "max_signals_per_episode": max(signal_counts, default=None),
        "open_episode_count": sum(not item.closed for item in episodes),
    }


def _forward_return(
    candles: list[Candle], entry_index: int, entry_price: float, horizon: int
) -> float | None:
    if not _window_is_contiguous(candles, entry_index, horizon):
        return None
    end_index = entry_index + horizon - 1
    return float(candles[end_index].close) / entry_price - 1


def _window_is_contiguous(candles: list[Candle], entry_index: int, horizon: int) -> bool:
    end_index = entry_index + horizon
    if entry_index < 0 or end_index > len(candles):
        return False
    return all(
        later.timestamp - earlier.timestamp == EPISODE_INTERVAL
        for earlier, later in pairwise(candles[entry_index:end_index])
    )


def _is_contiguous(candles: list[Candle], earlier: int, later: int) -> bool:
    return candles[later].timestamp - candles[earlier].timestamp == EPISODE_INTERVAL


def _decimal(value: str | None) -> float:
    if value is None:
        raise ValueError("BUY episode requires VWAP and deviation metadata")
    return float(Decimal(value))


def _quantile(values: list[int] | list[float], quantile: float) -> float | None:
    return float(np.quantile(values, quantile)) if values else None
