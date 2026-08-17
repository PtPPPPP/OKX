"""Frozen multi-timeframe and volume entry candidates for Strategy Research V3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import mean

from app.domain.market import Candle
from backtest.strategy_v3_features import (
    HigherTimeframeCandle,
    causal_htf_uptrend_flags,
    relative_volume,
)


@dataclass(frozen=True, slots=True)
class V3Variant:
    candidate_id: str
    variant_id: str
    hypothesis: str
    economic_rationale: str
    entry_rule: str
    expected_failure_mode: str
    timeframes: tuple[str, ...]
    parameters: dict[str, int | float]
    primary: bool


@dataclass(frozen=True, slots=True)
class SignalSet:
    candidate: tuple[bool, ...]
    without_htf: tuple[bool, ...] | None
    without_volume: tuple[bool, ...] | None


def load_v3_specs(path: Path) -> tuple[V3Variant, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("frozen_before_oos") is not True or payload.get("lookahead_bias") is not False:
        raise ValueError("V3 specifications must be frozen and causal")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5:
        raise ValueError("V3 supports one to five candidate architectures")
    variants: list[V3Variant] = []
    for candidate in candidates:
        declared = candidate.get("variants")
        if not isinstance(declared, list) or not 1 <= len(declared) <= 2:
            raise ValueError("each V3 candidate requires one or two variants")
        for item in declared:
            variants.append(
                V3Variant(
                    candidate_id=str(candidate["candidate_id"]),
                    variant_id=str(item["variant_id"]),
                    hypothesis=str(candidate["hypothesis"]),
                    economic_rationale=str(candidate["economic_rationale"]),
                    entry_rule=str(candidate["entry_rule"]),
                    expected_failure_mode=str(candidate["expected_failure_mode"]),
                    timeframes=tuple(candidate["timeframes"]),
                    parameters=dict(item["parameters"]),
                    primary=bool(item["primary"]),
                )
            )
    if len(variants) > 10:
        raise ValueError("V3 variant count exceeds ten")
    return tuple(variants)


def generate_v3_signals(
    candles: list[Candle], bars4h: tuple[HigherTimeframeCandle, ...], variant: V3Variant
) -> SignalSet:
    candidate: list[bool] = []
    no_htf: list[bool] = []
    no_volume: list[bool] = []
    uses_htf = variant.candidate_id in {
        "htf_pullback_recovery",
        "htf_breakout",
        "htf_volume_momentum",
    }
    uses_volume = variant.candidate_id in {
        "relative_volume_breakout",
        "htf_volume_momentum",
        "volume_exhaustion_reversal",
    }
    volumes = [item.volume for item in candles]
    htf_flags = (
        causal_htf_uptrend_flags(
            candles,
            bars4h,
            fast=int(variant.parameters["htf_fast"]),
            slow=int(variant.parameters["htf_slow"]),
        )
        if uses_htf
        else None
    )
    for index, _candle in enumerate(candles):
        htf_ok = htf_flags[index] if htf_flags is not None else True
        base, volume_ok = _components(candles, volumes, index, variant)
        candidate.append(
            base and (htf_ok if uses_htf else True) and (volume_ok if uses_volume else True)
        )
        no_htf.append(base and (volume_ok if uses_volume else True))
        no_volume.append(base and (htf_ok if uses_htf else True))
    return SignalSet(
        tuple(candidate),
        tuple(no_htf) if uses_htf else None,
        tuple(no_volume) if uses_volume else None,
    )


def _components(
    candles: list[Candle],
    volumes: list[Decimal],
    index: int,
    variant: V3Variant,
) -> tuple[bool, bool]:
    parameters = variant.parameters
    volume = (
        relative_volume(volumes, index, window=int(parameters.get("volume_window", 1)))
        if "volume_window" in parameters
        else None
    )
    volume_ok = volume is not None and volume > float(parameters.get("volume_multiple", 1))
    if variant.candidate_id == "htf_pullback_recovery":
        fast = int(parameters["one_hour_fast"])
        slow = int(parameters["one_hour_slow"])
        if index < slow:
            return False, volume_ok
        current_fast = _sma(candles, index, fast)
        prior_fast = _sma(candles, index - 1, fast)
        base = (
            _sma(candles, index, fast) > _sma(candles, index, slow)
            and float(candles[index - 1].close) <= prior_fast
            and float(candles[index].close) > current_fast
            and float(candles[index].close) > float(candles[index - 1].close)
        )
        return base, volume_ok
    if variant.candidate_id in {"htf_breakout", "relative_volume_breakout"}:
        lookback = int(parameters["breakout"])
        base = index >= lookback and float(candles[index].close) > max(
            float(item.high) for item in candles[index - lookback : index]
        )
        return base, volume_ok
    if variant.candidate_id == "htf_volume_momentum":
        momentum = int(parameters["momentum"])
        base = index >= momentum and float(candles[index].close) > float(
            candles[index - momentum].close
        )
        return base, volume_ok
    if variant.candidate_id == "volume_exhaustion_reversal":
        window = int(parameters["mean_window"])
        if index < window:
            return False, volume_ok
        reference = mean(float(item.close) for item in candles[index - window : index])
        candle = candles[index]
        base = (
            float(candle.close) < reference * (1 - float(parameters["deviation"]))
            and float(candle.close) > float(candle.open)
            and float(candle.close) > float(candles[index - 1].close)
        )
        return base, volume_ok
    raise ValueError(f"unknown V3 candidate: {variant.candidate_id}")


def _sma(candles: list[Candle], index: int, window: int) -> float:
    return mean(float(item.close) for item in candles[index - window + 1 : index + 1])
