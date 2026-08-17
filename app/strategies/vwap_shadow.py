from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order
from app.domain.signal import Signal, SignalAction

_BASIS_POINTS = Decimal("10000")


class VWAPShadowParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vwap_window: int = Field(default=24, gt=1)
    buy_deviation_bps: Decimal = Field(default=Decimal("100"), ge=0, lt=_BASIS_POINTS)

    @field_validator("buy_deviation_bps", mode="after")
    @classmethod
    def finite_deviation(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("buy_deviation_bps must be finite")
        return value


class VWAPShadowStrategy:
    name = "vwap_shadow"
    description = "仅使用滚动 VWAP 偏离产生 BUY/HOLD 的 Shadow 策略"
    supported_market_types = frozenset({InstrumentType.SPOT})

    def __init__(self, parameters: VWAPShadowParameters) -> None:
        self.parameters = parameters
        self._bars: deque[Candle] = deque(maxlen=parameters.vwap_window)

    @property
    def required_history(self) -> int:
        return self.parameters.vwap_window

    def on_start(self, context: StrategyContext) -> None:
        self._bars.clear()

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        if not bar.confirmed:
            self._bars.clear()
            return [self._signal(context, bar, SignalAction.HOLD, "未确认 K 线，VWAP 窗口已重置")]

        if not bar.volume.is_finite() or bar.volume <= 0:
            self._bars.clear()
            return [self._signal(context, bar, SignalAction.HOLD, "非正成交量，VWAP 窗口已重置")]

        self._bars.append(bar)
        vwap = rolling_vwap(self._bars, self.parameters.vwap_window)
        if vwap is None:
            return [self._signal(context, bar, SignalAction.HOLD, "VWAP 窗口不足")]

        deviation_bps = (vwap - bar.close) / vwap * _BASIS_POINTS
        threshold = vwap * (Decimal("1") - self.parameters.buy_deviation_bps / _BASIS_POINTS)
        if bar.close <= threshold:
            return [
                self._signal(
                    context,
                    bar,
                    SignalAction.BUY,
                    "收盘价低于 VWAP 买入偏离阈值",
                    vwap=vwap,
                    deviation_bps=deviation_bps,
                )
            ]
        return [
            self._signal(
                context,
                bar,
                SignalAction.HOLD,
                "收盘价未达到 VWAP 买入偏离阈值",
                vwap=vwap,
                deviation_bps=deviation_bps,
            )
        ]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        return None

    def on_stop(self, context: StrategyContext) -> None:
        self._bars.clear()

    def state_snapshot(self) -> dict[str, int]:
        return {"window_length": len(self._bars)}

    def checkpoint_state(self) -> dict[str, object]:
        total_volume = sum((bar.volume for bar in self._bars), Decimal("0"))
        weighted_total = sum(
            ((bar.high + bar.low + bar.close) / Decimal("3") * bar.volume for bar in self._bars),
            Decimal("0"),
        )
        return {
            "revision": 1,
            "vwap_window": self.parameters.vwap_window,
            "window_length": len(self._bars),
            "total_volume": str(total_volume),
            "weighted_total": str(weighted_total),
            "bars": [
                {
                    "timestamp": bar.timestamp.astimezone(UTC).isoformat(),
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.volume),
                    "confirmed": bar.confirmed,
                }
                for bar in self._bars
            ],
        }

    def restore_checkpoint_state(self, state: dict[str, Any]) -> None:
        try:
            revision = int(state["revision"])
            configured_window = int(state["vwap_window"])
            bars_payload = state["bars"]
            expected_length = int(state["window_length"])
            expected_volume = Decimal(str(state["total_volume"]))
            expected_weighted = Decimal(str(state["weighted_total"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("VWAP checkpoint state is invalid") from exc
        if revision != 1:
            raise ValueError(f"unsupported VWAP checkpoint revision: {revision}")
        if configured_window != self.parameters.vwap_window:
            raise ValueError("VWAP checkpoint window does not match strategy configuration")
        if not isinstance(bars_payload, list) or len(bars_payload) != expected_length:
            raise ValueError("VWAP checkpoint window length is invalid")
        if expected_length > self.parameters.vwap_window:
            raise ValueError("VWAP checkpoint window exceeds configured length")

        restored: deque[Candle] = deque(maxlen=self.parameters.vwap_window)
        try:
            for payload in bars_payload:
                if not isinstance(payload, dict):
                    raise ValueError("VWAP checkpoint candle is invalid")
                timestamp = datetime.fromisoformat(str(payload["timestamp"]))
                if timestamp.tzinfo is None:
                    raise ValueError("VWAP checkpoint candle timestamp must include timezone")
                candle = Candle(
                    timestamp=timestamp.astimezone(UTC),
                    open=Decimal(str(payload["open"])),
                    high=Decimal(str(payload["high"])),
                    low=Decimal(str(payload["low"])),
                    close=Decimal(str(payload["close"])),
                    volume=Decimal(str(payload["volume"])),
                    confirmed=bool(payload["confirmed"]),
                )
                if not candle.confirmed or candle.volume <= 0:
                    raise ValueError("VWAP checkpoint contains an ineligible candle")
                if (
                    any(
                        not value.is_finite()
                        for value in (
                            candle.open,
                            candle.high,
                            candle.low,
                            candle.close,
                            candle.volume,
                        )
                    )
                    or candle.low <= 0
                    or candle.high < max(candle.open, candle.close)
                    or candle.low > min(candle.open, candle.close)
                ):
                    raise ValueError("VWAP checkpoint candle values are invalid")
                restored.append(candle)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("VWAP checkpoint candle is invalid") from exc

        actual_volume = sum((bar.volume for bar in restored), Decimal("0"))
        actual_weighted = sum(
            ((bar.high + bar.low + bar.close) / Decimal("3") * bar.volume for bar in restored),
            Decimal("0"),
        )
        if actual_volume != expected_volume or actual_weighted != expected_weighted:
            raise ValueError("VWAP checkpoint aggregates do not match its window")
        self._bars = restored

    def _signal(
        self,
        context: StrategyContext,
        bar: Candle,
        action: SignalAction,
        reason: str,
        *,
        vwap: Decimal | None = None,
        deviation_bps: Decimal | None = None,
    ) -> Signal:
        identity = (
            f"{context.run_id}:{self.name}:{context.instrument.instrument_id}:"
            f"{bar.timestamp.isoformat()}:{action.value}"
        )
        metadata: dict[str, Any] = {
            "close": bar.close,
            "vwap": vwap,
            "deviation_bps": deviation_bps,
            "vwap_window": self.parameters.vwap_window,
            "window_length": len(self._bars),
            "candle_timestamp": bar.timestamp.isoformat(),
            "candle_confirmed": bar.confirmed,
        }
        return Signal(
            signal_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
            strategy_name=self.name,
            instrument_id=context.instrument.instrument_id,
            action=action,
            timestamp=context.now,
            reason=reason,
            confidence=Decimal("1") if action is SignalAction.BUY else Decimal("0"),
            metadata=metadata,
        )


def rolling_vwap(bars: Iterable[Candle], window: int) -> Decimal | None:
    sample = list(bars)[-window:]
    if len(sample) < window:
        return None
    total_volume = sum((bar.volume for bar in sample), Decimal("0"))
    if total_volume <= 0:
        return None
    weighted_price = sum(
        ((bar.high + bar.low + bar.close) / Decimal("3") * bar.volume for bar in sample),
        Decimal("0"),
    )
    return weighted_price / total_volume
