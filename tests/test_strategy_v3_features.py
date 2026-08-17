from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.market import Candle
from backtest.strategy_v3_features import (
    aggregate_completed,
    completed_htf_index,
    htf_uptrend,
    relative_volume,
    validate_research_partition,
)


def _candles(count: int, *, start: datetime | None = None) -> list[Candle]:
    origin = start or datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=origin + timedelta(hours=index),
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=Decimal(index + 1),
            confirmed=True,
        )
        for index in range(count)
    ]


def test_one_hour_to_four_hour_aggregation() -> None:
    bars = aggregate_completed(_candles(8), hours=4)
    assert len(bars) == 2
    assert bars[0].open == Decimal("100")
    assert bars[0].close == Decimal("104")
    assert bars[0].high == Decimal("105")
    assert bars[0].low == Decimal("99")
    assert bars[0].volume == Decimal("10")
    assert bars[0].close_time == datetime(2026, 1, 1, 4, tzinfo=UTC)


def test_one_hour_to_one_day_aggregation_uses_utc_boundary() -> None:
    bars = aggregate_completed(_candles(48), hours=24)
    assert len(bars) == 2
    assert bars[0].open_time == datetime(2026, 1, 1, tzinfo=UTC)
    assert bars[0].close_time == datetime(2026, 1, 2, tzinfo=UTC)
    assert bars[0].source_bar_count == 24


@pytest.mark.parametrize("hours,count", [(4, 7), (24, 47)])
def test_incomplete_higher_timeframe_is_excluded(hours: int, count: int) -> None:
    bars = aggregate_completed(_candles(count), hours=hours)
    assert len(bars) == 1


def test_partial_leading_bucket_is_excluded() -> None:
    bars = aggregate_completed(_candles(7, start=datetime(2026, 1, 1, 1, tzinfo=UTC)), hours=4)
    assert len(bars) == 1
    assert bars[0].open_time == datetime(2026, 1, 1, 4, tzinfo=UTC)


def test_higher_timeframe_feature_does_not_use_unclosed_bar() -> None:
    bars = aggregate_completed(_candles(64), hours=4)
    decision = datetime(2026, 1, 3, 10, tzinfo=UTC)
    index = completed_htf_index(bars, decision)
    assert index is not None
    assert bars[index].close_time <= decision
    assert index + 1 == len([bar for bar in bars if bar.close_time <= decision])
    baseline = htf_uptrend(bars, decision, fast=3, slow=6)
    altered = list(bars)
    altered[index + 1] = replace(altered[index + 1], close=Decimal("1"))
    assert htf_uptrend(tuple(altered), decision, fast=3, slow=6) == baseline


def test_aggregation_is_deterministic() -> None:
    candles = _candles(48)
    assert aggregate_completed(candles, hours=4) == aggregate_completed(candles, hours=4)
    assert aggregate_completed(candles, hours=24) == aggregate_completed(candles, hours=24)


def test_relative_volume_uses_only_prior_reference_window() -> None:
    volumes = [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("100")]
    assert relative_volume(volumes, 3, window=3) == 5.0
    altered_future = [*volumes, Decimal("1000000")]
    assert relative_volume(altered_future, 3, window=3) == 5.0


def test_relative_volume_zero_missing_and_malformed() -> None:
    assert relative_volume([Decimal("0"), Decimal("0"), Decimal("10")], 2, window=2) is None
    with pytest.raises(ValueError, match="missing volume"):
        relative_volume([Decimal("1"), None], 1, window=1)
    with pytest.raises(ValueError, match="malformed volume"):
        relative_volume([Decimal("1"), "invalid"], 1, window=1)


def test_prospective_data_is_rejected_from_research_partition() -> None:
    candles = _candles(2, start=datetime(2026, 8, 13, tzinfo=UTC))
    with pytest.raises(ValueError, match="prospective data"):
        validate_research_partition(
            candles,
            research_cutoff=datetime(2026, 8, 12, 23, 59, 59, tzinfo=UTC),
            prospective_start=datetime(2026, 8, 13, tzinfo=UTC),
        )
