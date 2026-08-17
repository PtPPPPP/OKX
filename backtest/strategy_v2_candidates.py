"""Frozen, causal entry candidates for offline Strategy Research V2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from app.domain.market import Candle
from backtest.vwap_signal_edge import _market_regime, _volatility_regime

ONE_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class CandidateVariant:
    candidate_id: str
    variant_id: str
    economic_rationale: str
    entry_rule: str
    expected_behavior: str
    parameters: dict[str, int | float]
    primary: bool


@dataclass(frozen=True, slots=True)
class EntryEpisode:
    episode_id: str
    candidate_id: str
    variant_id: str
    start_index: int
    end_index: int
    signal_timestamp: str
    entry_index: int | None
    entry_timestamp: str | None
    entry_price: float | None
    duration_bars: int
    closed: bool
    closure_reason: str
    market_regime: str
    volatility_regime: str
    holdout: bool
    returns: dict[int, float | None]
    mfe: dict[int, float | None]
    mae: dict[int, float | None]

    def flat(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "episode_id": self.episode_id,
            "candidate_id": self.candidate_id,
            "variant_id": self.variant_id,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "signal_timestamp": self.signal_timestamp,
            "entry_index": self.entry_index,
            "entry_timestamp": self.entry_timestamp,
            "entry_price": self.entry_price,
            "duration_bars": self.duration_bars,
            "closed": self.closed,
            "closure_reason": self.closure_reason,
            "market_regime": self.market_regime,
            "volatility_regime": self.volatility_regime,
            "holdout": self.holdout,
        }
        for horizon, value in self.returns.items():
            row[f"return_{horizon}h"] = value
        for horizon, value in self.mfe.items():
            row[f"mfe_{horizon}h"] = value
            row[f"mae_{horizon}h"] = self.mae[horizon]
        return row


def load_candidate_specs(path: Path) -> tuple[CandidateVariant, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("frozen_before_oos") is not True or payload.get("lookahead_bias") is not False:
        raise ValueError("candidate specification must be frozen and causal")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not 3 <= len(candidates) <= 5:
        raise ValueError("Strategy V2 requires three to five candidate architectures")
    variants: list[CandidateVariant] = []
    for candidate in candidates:
        declared = candidate.get("variants", [])
        if not 1 <= len(declared) <= 3:
            raise ValueError("each candidate requires one to three frozen variants")
        for variant in declared:
            variants.append(
                CandidateVariant(
                    candidate_id=str(candidate["candidate_id"]),
                    variant_id=str(variant["variant_id"]),
                    economic_rationale=str(candidate["economic_rationale"]),
                    entry_rule=str(candidate["entry_rule"]),
                    expected_behavior=str(candidate["expected_behavior"]),
                    parameters=dict(variant["parameters"]),
                    primary=bool(variant["primary"]),
                )
            )
    if len({item.candidate_id for item in variants}) != len(candidates):
        raise ValueError("candidate identifiers must be unique")
    if any(
        sum(item.primary for item in variants if item.candidate_id == candidate["candidate_id"])
        != 1
        for candidate in candidates
    ):
        raise ValueError("each candidate requires exactly one primary variant")
    return tuple(variants)


def generate_signals(candles: list[Candle], variant: CandidateVariant) -> tuple[bool, ...]:
    return tuple(_signal_at(candles, index, variant) for index in range(len(candles)))


def build_entry_episodes(
    candles: list[Candle],
    signals: tuple[bool, ...],
    variant: CandidateVariant,
    horizons: tuple[int, ...],
) -> tuple[EntryEpisode, ...]:
    if len(candles) != len(signals):
        raise ValueError("candles and signals must align")
    if not candles:
        return ()
    cutoff = candles[int(len(candles) * 0.8)].timestamp
    groups: list[tuple[int, int, bool, str]] = []
    start: int | None = None
    for index, signal in enumerate(signals):
        if start is not None and index > start and not _adjacent(candles, index - 1, index):
            groups.append((start, index - 1, False, "data_gap"))
            start = None
        if signal and start is None:
            start = index
        elif not signal and start is not None:
            groups.append((start, index - 1, True, "signal_ended"))
            start = None
    if start is not None:
        groups.append((start, len(candles) - 1, False, "dataset_end"))
    return tuple(
        _make_episode(candles, variant, start_, end, closed, reason, cutoff, horizons)
        for start_, end, closed, reason in groups
    )


def _signal_at(candles: list[Candle], index: int, variant: CandidateVariant) -> bool:
    parameters = variant.parameters
    if variant.candidate_id == "trend_continuation":
        fast = int(parameters["fast"])
        slow = int(parameters["slow"])
        momentum = int(parameters["momentum"])
        if index < max(slow - 1, momentum):
            return False
        close = float(candles[index].close)
        return (
            _sma(candles, index, fast) > _sma(candles, index, slow)
            and close > _sma(candles, index, slow)
            and close > float(candles[index - momentum].close)
        )
    if variant.candidate_id == "price_breakout":
        lookback = int(parameters["lookback"])
        return index >= lookback and float(candles[index].close) > max(
            float(item.high) for item in candles[index - lookback : index]
        )
    if variant.candidate_id == "volatility_breakout":
        lookback = int(parameters["lookback"])
        atr_window = int(parameters["atr_window"])
        if index < max(lookback, atr_window + 1):
            return False
        resistance = max(float(item.high) for item in candles[index - lookback : index])
        prior_atr = mean(
            _true_range(candles, position) for position in range(index - atr_window, index)
        )
        return float(candles[index].close) > resistance and _true_range(
            candles, index
        ) > prior_atr * float(parameters["range_atr_multiple"])
    if variant.candidate_id == "confirmed_mean_reversion":
        window = int(parameters["mean_window"])
        if index < window:
            return False
        reference = mean(float(item.close) for item in candles[index - window : index])
        candle = candles[index]
        return (
            float(candle.close) < reference * (1 - float(parameters["deviation"]))
            and float(candle.close) > float(candle.open)
            and float(candle.close) > float(candles[index - 1].close)
        )
    if variant.candidate_id == "momentum_pullback":
        fast = int(parameters["fast"])
        slow = int(parameters["slow"])
        if index < slow:
            return False
        current_fast = _sma(candles, index, fast)
        prior_fast = _sma(candles, index - 1, fast)
        return (
            current_fast > _sma(candles, index, slow)
            and float(candles[index - 1].close) <= prior_fast
            and float(candles[index].close) > current_fast
            and float(candles[index].close) > float(candles[index - 1].close)
        )
    raise ValueError(f"unknown candidate: {variant.candidate_id}")


def _make_episode(
    candles: list[Candle],
    variant: CandidateVariant,
    start: int,
    end: int,
    closed: bool,
    reason: str,
    cutoff: Any,
    horizons: tuple[int, ...],
) -> EntryEpisode:
    entry = start + 1
    valid_entry = entry < len(candles) and _adjacent(candles, start, entry)
    price = float(candles[entry].open) if valid_entry else None
    returns: dict[int, float | None] = {}
    mfe: dict[int, float | None] = {}
    mae: dict[int, float | None] = {}
    for horizon in horizons:
        if not valid_entry or not _continuous(candles, entry, entry + horizon - 1):
            returns[horizon] = None
            mfe[horizon] = None
            mae[horizon] = None
            continue
        window = candles[entry : entry + horizon]
        if price is None:
            raise RuntimeError("valid entry must have a price")
        returns[horizon] = float(window[-1].close) / price - 1
        mfe[horizon] = max(float(item.high) for item in window) / price - 1
        mae[horizon] = min(float(item.low) for item in window) / price - 1
    stamp = candles[start].timestamp.astimezone(UTC)
    identifier = hashlib.sha256(
        f"strategy_v2|{variant.variant_id}|{stamp.isoformat()}".encode()
    ).hexdigest()[:24]
    return EntryEpisode(
        episode_id=identifier,
        candidate_id=variant.candidate_id,
        variant_id=variant.variant_id,
        start_index=start,
        end_index=end,
        signal_timestamp=stamp.isoformat(),
        entry_index=entry if valid_entry else None,
        entry_timestamp=candles[entry].timestamp.astimezone(UTC).isoformat()
        if valid_entry
        else None,
        entry_price=price,
        duration_bars=end - start + 1,
        closed=closed,
        closure_reason=reason,
        market_regime=_market_regime(candles, start),
        volatility_regime=_volatility_regime(candles, start),
        holdout=stamp >= cutoff,
        returns=returns,
        mfe=mfe,
        mae=mae,
    )


def _sma(candles: list[Candle], index: int, window: int) -> float:
    return mean(float(item.close) for item in candles[index - window + 1 : index + 1])


def _true_range(candles: list[Candle], index: int) -> float:
    high = float(candles[index].high)
    low = float(candles[index].low)
    previous = float(candles[index - 1].close)
    return max(high - low, abs(high - previous), abs(low - previous))


def _adjacent(candles: list[Candle], left: int, right: int) -> bool:
    return candles[right].timestamp - candles[left].timestamp == ONE_HOUR


def _continuous(candles: list[Candle], start: int, end: int) -> bool:
    return end < len(candles) and all(
        _adjacent(candles, index - 1, index) for index in range(start + 1, end + 1)
    )
