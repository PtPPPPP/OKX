"""Batch research consumes the same causal updates as registered V3 strategies."""

from app.domain.market import Candle
from app.strategies.v3_features import (
    SignalSet as SignalSet,
)
from app.strategies.v3_features import V3FeatureStream
from app.strategies.v3_features import (
    V3Variant as V3Variant,
)
from app.strategies.v3_features import (
    generate_v3_signals_reference as generate_v3_signals_reference,
)
from app.strategies.v3_features import (
    load_v3_specs as load_v3_specs,
)
from app.strategies.v3_timeframes import HigherTimeframeCandle, aggregate_completed


def generate_v3_signals(
    candles: list[Candle], bars4h: tuple[HigherTimeframeCandle, ...], variant: V3Variant
) -> SignalSet:
    if bars4h != aggregate_completed(candles, hours=4):
        raise ValueError("supplied HTF bars differ from the confirmed 1H input")
    stream = V3FeatureStream(variant)
    values = [stream.update(bar) for bar in candles]
    return SignalSet(
        tuple(row[0] for row in values),
        tuple(bool(row[1]) for row in values) if "htf_slow" in variant.parameters else None,
        tuple(bool(row[2]) for row in values) if "volume_window" in variant.parameters else None,
    )
