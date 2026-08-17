from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order, OrderSide, OrderState
from app.domain.signal import Signal, SignalAction


class VWAPMeanReversionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vwap_window: int = Field(default=24, gt=1)
    rsi_period: int = Field(default=14, gt=1)
    entry_deviation_pct: Decimal = Field(default=Decimal("0.008"), gt=0, lt=1)
    rsi_entry_threshold: Decimal = Field(default=Decimal("35"), gt=0, lt=100)
    fixed_stop_pct: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    atr_period: int = Field(default=14, gt=1)
    atr_multiplier: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_hold_bars: int = Field(default=12, gt=0)

    @field_validator(
        "entry_deviation_pct",
        "rsi_entry_threshold",
        "fixed_stop_pct",
        "atr_multiplier",
        mode="after",
    )
    @classmethod
    def finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("VWAP parameters must be finite")
        return value


@dataclass(slots=True)
class VWAPPositionState:
    entry_price: Decimal | None = None
    entry_candle_index: int | None = None
    stop_price: Decimal | None = None
    holding_bars: int = 0

    @property
    def in_position(self) -> bool:
        return self.entry_price is not None

    def snapshot(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> VWAPPositionState:
        return cls(
            entry_price=Decimal(str(data["entry_price"]))
            if data.get("entry_price") is not None
            else None,
            entry_candle_index=int(data["entry_candle_index"])
            if data.get("entry_candle_index") is not None
            else None,
            stop_price=Decimal(str(data["stop_price"]))
            if data.get("stop_price") is not None
            else None,
            holding_bars=int(data.get("holding_bars", 0)),
        )


class VWAPMeanReversionStrategy:
    name = "vwap_mean_reversion"
    description = "24 根 1H VWAP 与 RSI 均值回归多头策略"
    supported_market_types = frozenset({InstrumentType.SPOT})

    def __init__(self, parameters: VWAPMeanReversionParameters) -> None:
        self.parameters = parameters
        self._bars: deque[Candle] = deque(
            maxlen=max(parameters.vwap_window, parameters.rsi_period, parameters.atr_period) + 2
        )
        self._seen: set[str] = set()
        self._bar_index = 0
        self._active_order = False
        self.state = VWAPPositionState()

    @property
    def required_history(self) -> int:
        return max(
            self.parameters.vwap_window,
            self.parameters.rsi_period + 1,
            self.parameters.atr_period + 1,
        )

    def on_start(self, context: StrategyContext) -> None:
        self._bars.clear()
        self._seen.clear()
        self._bar_index = 0
        self._active_order = False

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        if not bar.confirmed:
            return [self._signal(context, bar, SignalAction.HOLD, "未确认 K 线")]
        key = bar.timestamp.isoformat()
        if key in self._seen:
            return [self._signal(context, bar, SignalAction.HOLD, "重复 K 线已忽略")]
        self._seen.add(key)
        if not _valid_bar(bar):
            return [self._signal(context, bar, SignalAction.HOLD, "K 线包含缺失或非有限值")]
        self._bars.append(bar)
        self._bar_index += 1
        position = context.portfolio_snapshot.position(context.instrument.instrument_id)
        if position <= 0 and self.state.in_position:
            self.state = VWAPPositionState()
        if position > 0 and not self.state.in_position:
            entry = context.portfolio_snapshot.position_cost(
                context.instrument.instrument_id
            ).average_entry_price
            if entry is not None and entry.is_finite():
                self.state.entry_price = entry
                self.state.entry_candle_index = self._bar_index
                self.state.stop_price = entry * (Decimal("1") - self.parameters.fixed_stop_pct)
        if len(self._bars) < self.required_history:
            return [self._signal(context, bar, SignalAction.HOLD, "指标 warm-up 未完成")]
        vwap = _vwap(self._bars, self.parameters.vwap_window)
        rsi = _rsi(self._bars, self.parameters.rsi_period)
        atr = _atr(self._bars, self.parameters.atr_period)
        if vwap is None or rsi is None or atr is None:
            return [self._signal(context, bar, SignalAction.HOLD, "指标数据无效")]
        if position > 0:
            self.state.holding_bars += 1
            if self.state.entry_price is not None:
                fixed = self.state.entry_price * (Decimal("1") - self.parameters.fixed_stop_pct)
                atr_stop = self.state.entry_price - atr * self.parameters.atr_multiplier
                self.state.stop_price = max(fixed, atr_stop)
            if bar.close >= vwap:
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        "VWAP 回归止盈",
                        vwap=vwap,
                        rsi=rsi,
                        atr=atr,
                        protective_exit=True,
                        exit_reason="vwap_take_profit",
                    )
                ]
            if self.state.stop_price is not None and bar.close <= self.state.stop_price:
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        "保护止损",
                        vwap=vwap,
                        rsi=rsi,
                        atr=atr,
                        protective_exit=True,
                        exit_reason="stop_loss",
                    )
                ]
            if self.state.holding_bars > self.parameters.max_hold_bars:
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        "时间止损",
                        vwap=vwap,
                        rsi=rsi,
                        atr=atr,
                        protective_exit=True,
                        exit_reason="time_stop",
                    )
                ]
            return [
                self._signal(
                    context,
                    bar,
                    SignalAction.HOLD,
                    "持仓期间不重复开仓",
                    vwap=vwap,
                    rsi=rsi,
                    atr=atr,
                )
            ]
        if self._active_order:
            return [
                self._signal(
                    context, bar, SignalAction.HOLD, "存在活动订单", vwap=vwap, rsi=rsi, atr=atr
                )
            ]
        if (
            bar.close <= vwap * (Decimal("1") - self.parameters.entry_deviation_pct)
            and rsi < self.parameters.rsi_entry_threshold
        ):
            return [
                self._signal(
                    context,
                    bar,
                    SignalAction.BUY,
                    "VWAP 下方超跌且 RSI 超卖",
                    vwap=vwap,
                    rsi=rsi,
                    atr=atr,
                )
            ]
        return [
            self._signal(
                context, bar, SignalAction.HOLD, "未满足入场条件", vwap=vwap, rsi=rsi, atr=atr
            )
        ]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        self._active_order = order.is_open
        if order.state is OrderState.FILLED and order.filled_quantity > 0:
            if order.request.side is OrderSide.BUY and order.average_price is not None:
                self.state.entry_price = order.average_price
                self.state.entry_candle_index = self._bar_index
                self.state.holding_bars = 0
            elif order.request.side is OrderSide.SELL:
                self.state = VWAPPositionState()

    def on_stop(self, context: StrategyContext) -> None:
        return None

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "bar_index": self._bar_index,
            "state": self.state.snapshot(),
            "active_order": self._active_order,
        }

    def restore_state(self, snapshot: dict[str, Any]) -> None:
        self._bar_index = int(snapshot.get("bar_index", 0))
        self.state = VWAPPositionState.restore(snapshot.get("state", {}))
        self._active_order = bool(snapshot.get("active_order", False))

    def _signal(
        self,
        context: StrategyContext,
        bar: Candle,
        action: SignalAction,
        reason: str,
        **metadata: Any,
    ) -> Signal:
        metadata.update(
            {"candle_timestamp": bar.timestamp.isoformat(), "candle_confirmed": bar.confirmed}
        )
        return Signal(
            uuid4().hex,
            self.name,
            context.instrument.instrument_id,
            action,
            context.now,
            reason,
            Decimal("1") if action is not SignalAction.HOLD else Decimal("0"),
            metadata=metadata,
        )


def _valid_bar(bar: Candle) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    return all(value.is_finite() for value in values) and bar.volume > 0 and bar.high >= bar.low


def _vwap(bars: deque[Candle], window: int) -> Decimal | None:
    sample = list(bars)[-window:]
    volume = sum((bar.volume for bar in sample), Decimal("0"))
    if len(sample) < window or volume <= 0:
        return None
    return (
        sum(
            ((bar.high + bar.low + bar.close) / Decimal("3") * bar.volume for bar in sample),
            Decimal("0"),
        )
        / volume
    )


def _rsi(bars: deque[Candle], period: int) -> Decimal | None:
    closes = [bar.close for bar in list(bars)[-(period + 1) :]]
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], Decimal("0")) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], Decimal("0")) for i in range(1, len(closes))]
    avg_gain = sum(gains, Decimal("0")) / Decimal(period)
    avg_loss = sum(losses, Decimal("0")) / Decimal(period)
    if avg_loss == 0:
        return Decimal("100") if avg_gain else Decimal("50")
    return Decimal("100") - Decimal("100") / (Decimal("1") + avg_gain / avg_loss)


def _atr(bars: deque[Candle], period: int) -> Decimal | None:
    sample = list(bars)[-(period + 1) :]
    if len(sample) < period + 1:
        return None
    true_ranges = []
    for previous, current in pairwise(sample):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges, Decimal("0")) / Decimal(period)
