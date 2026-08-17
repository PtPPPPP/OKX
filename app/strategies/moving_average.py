from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order
from app.domain.signal import Signal, SignalAction


class MovingAverageCrossParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fast_period: int = Field(default=10, gt=0)
    slow_period: int = Field(default=30, gt=0)
    moving_average_type: Literal["sma"] = "sma"

    @model_validator(mode="after")
    def validate_periods(self) -> MovingAverageCrossParameters:
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period 必须小于 slow_period")
        return self


class MovingAverageCrossStrategy:
    name = "moving_average_cross"
    description = "简单移动平均线交叉策略"
    supported_market_types = frozenset({InstrumentType.SPOT})

    def __init__(self, parameters: MovingAverageCrossParameters) -> None:
        self.parameters = parameters
        self._closes: deque[Decimal] = deque(maxlen=parameters.slow_period + 1)
        self._last_entry_timestamp: datetime | None = None

    @property
    def required_history(self) -> int:
        return self.parameters.slow_period + 1

    def on_start(self, context: StrategyContext) -> None:
        self._closes.clear()
        self._last_entry_timestamp = None

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        if not bar.confirmed:
            return [self._signal(context, bar, SignalAction.HOLD, "最新 K 线尚未收盘")]
        self._closes.append(bar.close)
        if len(self._closes) < self.required_history:
            return [self._signal(context, bar, SignalAction.HOLD, "历史数据不足")]

        closes = list(self._closes)
        fast = self.parameters.fast_period
        slow = self.parameters.slow_period
        previous_fast = _mean(closes[-fast - 1 : -1])
        current_fast = _mean(closes[-fast:])
        previous_slow = _mean(closes[-slow - 1 : -1])
        current_slow = _mean(closes[-slow:])
        action = SignalAction.HOLD
        reason = "均线未发生交叉"
        if previous_fast <= previous_slow and current_fast > current_slow:
            action = SignalAction.BUY
            reason = "快均线上穿慢均线"
        elif previous_fast >= previous_slow and current_fast < current_slow:
            action = SignalAction.SELL
            reason = "快均线下穿慢均线"

        if action is SignalAction.BUY and self._last_entry_timestamp == bar.timestamp:
            action = SignalAction.HOLD
            reason = "同一入场信号已生成"
        if action is SignalAction.BUY:
            self._last_entry_timestamp = bar.timestamp
        return [
            self._signal(
                context,
                bar,
                action,
                reason,
                fast_ma=current_fast,
                slow_ma=current_slow,
            )
        ]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        return None

    def on_stop(self, context: StrategyContext) -> None:
        return None

    def _signal(
        self,
        context: StrategyContext,
        bar: Candle,
        action: SignalAction,
        reason: str,
        *,
        fast_ma: Decimal | None = None,
        slow_ma: Decimal | None = None,
    ) -> Signal:
        metadata = {
            "candle_timestamp": bar.timestamp.isoformat(),
            "candle_confirmed": bar.confirmed,
        }
        if fast_ma is not None:
            metadata["fast_ma"] = str(fast_ma)
        if slow_ma is not None:
            metadata["slow_ma"] = str(slow_ma)
        return Signal(
            signal_id=uuid4().hex,
            strategy_name=self.name,
            instrument_id=context.instrument.instrument_id,
            action=action,
            timestamp=context.now,
            reason=reason,
            confidence=Decimal("1") if action is not SignalAction.HOLD else Decimal("0"),
            metadata=metadata,
        )


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, start=Decimal("0")) / Decimal(len(values))
