from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order, OrderSide, OrderState
from app.domain.signal import Signal, SignalAction
from app.strategies.base import ProtectiveExitLevels


class MABollingerParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trend_ma_period: int = Field(default=50, gt=1)
    trend_ma_type: Literal["sma", "ema"] = "sma"
    trend_tolerance_pct: Decimal = Field(default=Decimal("0"), ge=0, lt=Decimal("0.5"))
    bollinger_period: int = Field(default=20, gt=1)
    bollinger_std_dev: Decimal = Field(default=Decimal("2"), gt=0)
    min_bandwidth_pct: Decimal = Field(default=Decimal("0.005"), ge=0, lt=1)
    require_trend_rising: bool = True
    confirm_rebound: bool = False
    exit_band: Literal["middle", "upper"] = "middle"
    exit_on_trend_break: bool = True
    stop_loss_pct: Decimal | None = Field(default=Decimal("0.02"), gt=0, lt=1)
    atr_period: int = Field(default=14, gt=1)
    atr_stop_multiplier: Decimal | None = Field(default=None, gt=0)
    max_hold_bars: int | None = Field(default=48, gt=0)

    @field_validator(
        "trend_tolerance_pct",
        "bollinger_std_dev",
        "min_bandwidth_pct",
        "stop_loss_pct",
        "atr_stop_multiplier",
        mode="after",
    )
    @classmethod
    def finite_decimal(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("均线布林带策略参数必须为有限数值")
        return value

    @model_validator(mode="after")
    def validate_periods(self) -> MABollingerParameters:
        if self.trend_ma_period < self.bollinger_period:
            raise ValueError("trend_ma_period 必须大于或等于 bollinger_period")
        return self


@dataclass(frozen=True, slots=True)
class _MABollingerIndicators:
    trend_ma: Decimal
    prev_trend_ma: Decimal | None
    trend_floor: Decimal
    bb_upper: Decimal
    bb_middle: Decimal
    bb_lower: Decimal
    bb_bandwidth: Decimal
    atr: Decimal | None
    stop_distance_pct: Decimal | None


@dataclass(slots=True)
class MABollingerPositionState:
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
    def restore(cls, data: dict[str, Any]) -> MABollingerPositionState:
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


class MABollingerPullbackStrategy:
    name = "ma_bollinger_pullback"
    description = "均线趋势过滤与布林带回调多头策略"
    supported_market_types = frozenset({InstrumentType.SPOT})

    def __init__(self, parameters: MABollingerParameters) -> None:
        self.parameters = parameters
        self._bars: deque[Candle] = deque(maxlen=self.required_history + 4)
        self._closes: deque[Decimal] = deque(maxlen=self.required_history + 4)
        self._seen: set[str] = set()
        self._bar_index = 0
        self._active_order = False
        self._ema_current: Decimal | None = None
        self._ema_previous: Decimal | None = None
        self.state = MABollingerPositionState()

    @property
    def required_history(self) -> int:
        trend_bars = (
            self.parameters.trend_ma_period + 1
            if self.parameters.require_trend_rising
            else self.parameters.trend_ma_period
        )
        bb_bars = (
            self.parameters.bollinger_period + 1
            if self.parameters.confirm_rebound
            else self.parameters.bollinger_period
        )
        atr_bars = (
            self.parameters.atr_period + 1 if self.parameters.atr_stop_multiplier is not None else 0
        )
        return max(trend_bars, bb_bars, atr_bars)

    def on_start(self, context: StrategyContext) -> None:
        self._bars.clear()
        self._closes.clear()
        self._seen.clear()
        self._bar_index = 0
        self._active_order = False
        self._ema_current = None
        self._ema_previous = None
        self.state = MABollingerPositionState()

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
        self._closes.append(bar.close)
        self._bar_index += 1
        self._update_ema(bar.close)

        position = context.portfolio_snapshot.position(context.instrument.instrument_id)
        if position <= 0 and self.state.in_position:
            self.state = MABollingerPositionState()
        if position > 0 and not self.state.in_position:
            entry = context.portfolio_snapshot.position_cost(
                context.instrument.instrument_id
            ).average_entry_price
            if entry is not None and entry.is_finite():
                self.state.entry_price = entry
                self.state.entry_candle_index = self._bar_index
                self.state.stop_price = self._compute_stop_price(entry, None)

        if len(self._closes) < self.required_history:
            return [self._signal(context, bar, SignalAction.HOLD, "指标 warm-up 未完成")]

        closes = list(self._closes)
        trend_ma, prev_trend_ma = self._trend_ma_values(closes)
        bands = _bollinger_bands(
            closes,
            self.parameters.bollinger_period,
            self.parameters.bollinger_std_dev,
        )
        if trend_ma is None or bands is None:
            return [self._signal(context, bar, SignalAction.HOLD, "指标数据无效")]
        if self.parameters.require_trend_rising and prev_trend_ma is None:
            return [self._signal(context, bar, SignalAction.HOLD, "趋势均线历史不足")]

        atr = (
            _atr(self._bars, self.parameters.atr_period)
            if self.parameters.atr_stop_multiplier is not None
            else None
        )
        if self.parameters.atr_stop_multiplier is not None and atr is None:
            return [self._signal(context, bar, SignalAction.HOLD, "ATR 指标数据无效")]

        bb_upper, bb_middle, bb_lower, bb_bandwidth = bands
        trend_floor = trend_ma * (Decimal("1") - self.parameters.trend_tolerance_pct)
        prospective_stop = self._compute_stop_price(bar.close, atr)
        stop_distance_pct = (
            (bar.close - prospective_stop) / bar.close
            if prospective_stop is not None and bar.close > prospective_stop > 0
            else self.parameters.stop_loss_pct
        )
        indicators = _MABollingerIndicators(
            trend_ma=trend_ma,
            prev_trend_ma=prev_trend_ma,
            trend_floor=trend_floor,
            bb_upper=bb_upper,
            bb_middle=bb_middle,
            bb_lower=bb_lower,
            bb_bandwidth=bb_bandwidth,
            atr=atr,
            stop_distance_pct=stop_distance_pct,
        )

        if position > 0:
            self.state.holding_bars += 1
            if self.state.entry_price is not None:
                self.state.stop_price = self._compute_stop_price(self.state.entry_price, atr)
            if self.state.stop_price is not None and bar.close <= self.state.stop_price:
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        "触发保护止损",
                        indicators=indicators,
                        protective_exit=True,
                        exit_reason="stop_loss",
                    )
                ]
            exit_target = bb_middle if self.parameters.exit_band == "middle" else bb_upper
            if bar.close >= exit_target:
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        f"价格回归布林带{'中轨' if self.parameters.exit_band == 'middle' else '上轨'}止盈",
                        indicators=indicators,
                        protective_exit=True,
                        exit_reason="bollinger_take_profit",
                    )
                ]
            if self.parameters.exit_on_trend_break and bar.close < trend_floor:
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        "收盘跌破趋势均线容忍带离场",
                        indicators=indicators,
                        protective_exit=True,
                        exit_reason="trend_break",
                    )
                ]
            if (
                self.parameters.max_hold_bars is not None
                and self.state.holding_bars >= self.parameters.max_hold_bars
            ):
                return [
                    self._signal(
                        context,
                        bar,
                        SignalAction.CLOSE,
                        "达到最大持仓周期时间止损",
                        indicators=indicators,
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
                    indicators=indicators,
                )
            ]

        if self._active_order:
            return [
                self._signal(
                    context,
                    bar,
                    SignalAction.HOLD,
                    "存在活动订单",
                    indicators=indicators,
                )
            ]

        trend_ok = bar.close >= trend_floor and (
            not self.parameters.require_trend_rising
            or (prev_trend_ma is not None and trend_ma >= prev_trend_ma)
        )
        bandwidth_ok = bb_bandwidth >= self.parameters.min_bandwidth_pct
        pullback_ok = self._check_pullback_entry(bar, closes, bb_lower, bb_middle)

        if trend_ok and bandwidth_ok and pullback_ok:
            reason = (
                "上升趋势中布林带下轨回踩企稳确认"
                if self.parameters.confirm_rebound
                else "上升趋势中价格回调至布林带下轨"
            )
            return [
                self._signal(
                    context,
                    bar,
                    SignalAction.BUY,
                    reason,
                    indicators=indicators,
                )
            ]

        return [
            self._signal(
                context,
                bar,
                SignalAction.HOLD,
                "未满足均线趋势与布林带回调入场条件",
                indicators=indicators,
            )
        ]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        self._active_order = order.is_open
        if order.state is OrderState.FILLED and order.filled_quantity > 0:
            if order.request.side is OrderSide.BUY and order.average_price is not None:
                atr = (
                    _atr(self._bars, self.parameters.atr_period)
                    if self.parameters.atr_stop_multiplier is not None
                    else None
                )
                self.state.entry_price = order.average_price
                self.state.entry_candle_index = self._bar_index
                self.state.stop_price = self._compute_stop_price(order.average_price, atr)
                self.state.holding_bars = 0
            elif order.request.side is OrderSide.SELL:
                self.state = MABollingerPositionState()

    def on_stop(self, context: StrategyContext) -> None:
        return None

    def protective_exit_levels(self, entry_price: Decimal) -> ProtectiveExitLevels:
        """Pure levels derived from confirmed bars only, consumed by the offline runner."""
        atr = (
            _atr(self._bars, self.parameters.atr_period)
            if self.parameters.atr_stop_multiplier is not None
            else None
        )
        return ProtectiveExitLevels(
            stop_price=self._compute_stop_price(entry_price, atr),
            take_profit_price=None,
            take_profit_reason="bollinger_take_profit",
        )

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "bar_index": self._bar_index,
            "state": self.state.snapshot(),
            "active_order": self._active_order,
            "ema_current": str(self._ema_current) if self._ema_current is not None else None,
            "ema_previous": str(self._ema_previous) if self._ema_previous is not None else None,
        }

    def _check_pullback_entry(
        self,
        bar: Candle,
        closes: list[Decimal],
        bb_lower: Decimal,
        bb_middle: Decimal,
    ) -> bool:
        if not self.parameters.confirm_rebound:
            return bar.close <= bb_lower
        if len(self._bars) < 2 or len(closes) < self.parameters.bollinger_period + 1:
            return False
        prev_bands = _bollinger_bands(
            closes[:-1],
            self.parameters.bollinger_period,
            self.parameters.bollinger_std_dev,
        )
        if prev_bands is None:
            return False
        _, _, prev_lower, _ = prev_bands
        prev_bar = self._bars[-2]
        prev_touched_lower = prev_bar.low <= prev_lower or prev_bar.close <= prev_lower
        rebound_confirmed = bar.close > bar.open and bar.close >= bb_lower and bar.close < bb_middle
        return prev_touched_lower and rebound_confirmed

    def _compute_stop_price(
        self,
        entry_price: Decimal,
        atr: Decimal | None,
    ) -> Decimal | None:
        fixed = (
            entry_price * (Decimal("1") - self.parameters.stop_loss_pct)
            if self.parameters.stop_loss_pct is not None
            else None
        )
        atr_stop = (
            entry_price - atr * self.parameters.atr_stop_multiplier
            if atr is not None and self.parameters.atr_stop_multiplier is not None
            else None
        )
        if fixed is not None and atr_stop is not None:
            return max(fixed, atr_stop)
        return fixed if fixed is not None else atr_stop

    def _update_ema(self, close: Decimal) -> None:
        if self.parameters.trend_ma_type != "ema":
            return
        period = self.parameters.trend_ma_period
        if self._ema_current is None:
            if len(self._closes) >= period:
                closes = list(self._closes)
                self._ema_current = _mean(closes[-period:])
            return
        alpha = Decimal("2") / Decimal(period + 1)
        self._ema_previous = self._ema_current
        self._ema_current = close * alpha + self._ema_current * (Decimal("1") - alpha)

    def _trend_ma_values(self, closes: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
        period = self.parameters.trend_ma_period
        if self.parameters.trend_ma_type == "ema":
            return self._ema_current, self._ema_previous
        if len(closes) < period:
            return None, None
        current = _mean(closes[-period:])
        previous = _mean(closes[-period - 1 : -1]) if len(closes) >= period + 1 else None
        return current, previous

    def _signal(
        self,
        context: StrategyContext,
        bar: Candle,
        action: SignalAction,
        reason: str,
        *,
        indicators: _MABollingerIndicators | None = None,
        protective_exit: bool = False,
        exit_reason: str | None = None,
    ) -> Signal:
        metadata: dict[str, Any] = {
            "candle_timestamp": bar.timestamp.isoformat(),
            "candle_confirmed": bar.confirmed,
            "holding_bars": self.state.holding_bars,
            "protective_exit": protective_exit,
        }
        if indicators is not None:
            metadata["trend_ma"] = str(indicators.trend_ma)
            metadata["trend_floor"] = str(indicators.trend_floor)
            if indicators.prev_trend_ma is not None:
                metadata["prev_trend_ma"] = str(indicators.prev_trend_ma)
            metadata["bb_upper"] = str(indicators.bb_upper)
            metadata["bb_middle"] = str(indicators.bb_middle)
            metadata["bb_lower"] = str(indicators.bb_lower)
            metadata["bb_bandwidth"] = str(indicators.bb_bandwidth)
            if indicators.atr is not None:
                metadata["atr"] = str(indicators.atr)
            if indicators.stop_distance_pct is not None:
                metadata["stop_distance_pct"] = str(indicators.stop_distance_pct)
        if exit_reason is not None:
            metadata["exit_reason"] = exit_reason
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


def _valid_bar(bar: Candle) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    return (
        all(value.is_finite() for value in values)
        and bar.open > 0
        and bar.high > 0
        and bar.low > 0
        and bar.close > 0
        and bar.volume >= 0
        and bar.high >= bar.low
    )


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, start=Decimal("0")) / Decimal(len(values))


def _bollinger_bands(
    closes: list[Decimal],
    period: int,
    std_multiplier: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = _mean(window)
    if middle <= 0:
        return None
    variance = sum(((price - middle) ** 2 for price in window), start=Decimal("0")) / Decimal(
        period
    )
    std_dev = variance.sqrt()
    upper = middle + std_multiplier * std_dev
    lower = middle - std_multiplier * std_dev
    bandwidth = (upper - lower) / middle
    return upper, middle, lower, bandwidth


def _atr(bars: deque[Candle], period: int) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    recent = list(bars)[-(period + 1) :]
    true_ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(recent)
    ]
    value = sum(true_ranges, Decimal("0")) / Decimal(period)
    return value if value.is_finite() and value >= 0 else None
