from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle
from app.domain.order import OrderState
from app.reproducibility import canonical_hash
from app.runtime.clock import BacktestClock
from app.services.strategy_shadow import ConfirmedStrategyShadow
from app.strategies.registry import create_strategy
from app.strategies.v3 import V3Strategy
from tests.test_v3_strategy_contract import (
    VARIANTS,
    candles,
    feedback,
    instrument,
    portfolio,
    shadow,
)


def context(
    adapter: ConfirmedStrategyShadow, at: datetime, candle: Candle | None = None
) -> StrategyContext:
    return StrategyContext(
        "direct-core-clock-test",
        TradingMode.BACKTEST,
        adapter.strategy.name,
        adapter.instrument,
        "1h",
        adapter.portfolio,
        MarketSnapshot(candle, candle.close) if candle else None,
        BacktestClock(at),
        read_only=True,
    )


def rehash(state: dict[str, Any]) -> dict[str, Any]:
    state["state_hash"] = canonical_hash(
        {key: value for key, value in state.items() if key != "state_hash"}
    )
    return state


@pytest.mark.parametrize("direct", [False, True], ids=["shadow", "core"])
@pytest.mark.parametrize("restored", [False, True], ids=["continuous", "restored"])
def test_feedback_cannot_rewind_past_a_published_candle(direct: bool, restored: bool) -> None:
    bars = candles(4)
    adapter = shadow(VARIANTS[0])
    adapter.replay(bars[:2])
    created = adapter.clock.now()
    order = feedback(adapter, "pending", OrderState.ACCEPTED, created=created)
    adapter.on_candle(bars[2])
    if restored:
        adapter = shadow(
            VARIANTS[0], resume_state=adapter.checkpoint_state(), open_order_ids=("pending",)
        )
    before = adapter.checkpoint_state()
    stale = created + timedelta(minutes=30)
    cancelled = replace(order, state=OrderState.CANCELLED, updated_at=stale)
    with pytest.raises(ValueError, match="out-of-order"):
        if direct:
            adapter.strategy.on_order_update(context(adapter, stale), cancelled)
        else:
            adapter.on_order_update(cancelled, portfolio(), at=stale)
    assert adapter.checkpoint_state() == before
    # A rejected event must not prevent subsequent valid input.
    adapter.on_candle(bars[3])


@pytest.mark.parametrize("direct", [False, True], ids=["shadow", "core"])
@pytest.mark.parametrize("restored", [False, True], ids=["continuous", "restored"])
def test_delayed_feedback_delivery_clock_blocks_earlier_candle(
    direct: bool, restored: bool
) -> None:
    bars = candles(4)
    adapter = shadow(VARIANTS[0])
    adapter.replay(bars[:2])
    created = adapter.clock.now()
    order = feedback(adapter, "pending", OrderState.ACCEPTED, created=created)
    adapter.on_candle(bars[2])
    delivery = adapter.clock.now() + timedelta(hours=1, minutes=30)
    # Exchange update time stays old. The logical delivery watermark must survive restore.
    adapter.on_order_update(order, portfolio(), at=delivery)
    saved = adapter.checkpoint_state()
    assert datetime.fromisoformat(saved["strategy_state"]["last_event_at"]) == delivery
    if restored:
        adapter = shadow(VARIANTS[0], resume_state=saved, open_order_ids=("pending",))
    before = adapter.checkpoint_state()
    candle = bars[3]
    with pytest.raises(ValueError, match="out-of-order"):
        if direct:
            adapter.strategy.on_bar(
                context(adapter, candle.timestamp + timedelta(hours=1), candle), candle
            )
        else:
            adapter.on_candle(candle)
    assert adapter.checkpoint_state() == before


@pytest.mark.parametrize("offset", [None, timezone(timedelta(hours=8))], ids=["naive", "non-UTC"])
@pytest.mark.parametrize("direct", [False, True], ids=["shadow", "core"])
def test_feedback_clock_requires_utc_without_mutating_state(
    offset: timezone | None, direct: bool
) -> None:
    adapter = shadow(VARIANTS[0])
    adapter.replay(candles(2))
    created = adapter.clock.now()
    order = feedback(adapter, "pending", OrderState.ACCEPTED, created=created)
    before = adapter.checkpoint_state()
    stamp = created.replace(tzinfo=None) if offset is None else created.astimezone(offset)
    with pytest.raises(ValueError, match=r"aware|UTC"):
        if direct:
            adapter.strategy.on_order_update(context(adapter, stamp), order)
        else:
            adapter.on_order_update(order, portfolio(), at=stamp)
    assert adapter.checkpoint_state() == before


@pytest.mark.parametrize("version", [1, 3, True, 2.0, "2"])
def test_core_rejects_other_or_coerced_state_formats_without_partial_restore(version: Any) -> None:
    adapter = shadow(VARIANTS[0])
    adapter.replay(candles(3))
    saved = adapter.strategy.checkpoint_state()
    changed = rehash({**saved, "format_version": version})
    fresh = create_strategy(VARIANTS[0].candidate_id, VARIANTS[0].parameters, instrument())
    assert isinstance(fresh, V3Strategy)
    with pytest.raises(ValueError, match="incompatible V3 checkpoint format"):
        fresh.restore_checkpoint_state(changed, context(adapter, adapter.clock.now()))
    # A failed restore leaves a fresh instance available for the correct checkpoint.
    fresh.restore_checkpoint_state(saved, context(adapter, adapter.clock.now()))
    assert fresh.checkpoint_state() == saved


@pytest.mark.parametrize(
    "field", ["outer_clock", "inner_clock", "missing_clock", "legacy_v1", "outer_version"]
)
def test_shadow_rejects_inconsistent_or_missing_delivery_watermark(field: str) -> None:
    adapter = shadow(VARIANTS[0])
    adapter.replay(candles(3))
    changed = copy.deepcopy(adapter.checkpoint_state())
    later = (adapter.clock.now() + timedelta(hours=1)).isoformat()
    if field == "outer_clock":
        changed["clock"] = later
    elif field == "inner_clock":
        changed["strategy_state"]["last_event_at"] = later
    elif field == "outer_version":
        changed["format_version"] = True
    else:
        del changed["strategy_state"]["last_event_at"]
        if field == "legacy_v1":
            changed["strategy_state"]["format_version"] = 1
            changed["strategy_state"]["implementation_version"] = "v3_step_v1"
    rehash(changed["strategy_state"])
    rehash(changed)
    with pytest.raises(ValueError, match=r"clock|last_event_at|format"):
        shadow(VARIANTS[0], resume_state=changed)


def test_same_instant_feedback_is_idempotent_and_resume_continues_identically() -> None:
    bars = candles(6)
    left = shadow(VARIANTS[0])
    left.replay(bars[:2])
    order = feedback(left, "pending", OrderState.ACCEPTED, created=left.clock.now())
    left.on_candle(bars[2])
    before = left.checkpoint_state()
    left.on_order_update(order, portfolio(), at=left.clock.now())
    assert left.checkpoint_state() == before
    right = shadow(VARIANTS[0], resume_state=before, open_order_ids=("pending",))
    assert right.replay(bars[3:]) == left.replay(bars[3:])
    assert right.checkpoint_state() == left.checkpoint_state()
