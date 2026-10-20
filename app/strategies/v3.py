"""Registered V3 strategy: one causal update path, explicit warmup and restorable state."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.config.settings import TradingMode
from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order, OrderSide, OrderState
from app.domain.signal import Signal, SignalAction
from app.reproducibility import canonical_hash
from app.strategies.v3_features import V3FeatureStream, V3Variant
from app.strategies.v3_parameters import V3Parameters, validated_parameters

_OPEN = {
    OrderState.CREATED,
    OrderState.SUBMITTED,
    OrderState.ACCEPTED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_PENDING,
    OrderState.UNKNOWN,
}


class CandleCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    confirmed: StrictBool

    def candle(self) -> Candle:
        return Candle(**self.model_dump())


class OrderCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_order_id: str = Field(min_length=1)
    state: OrderState
    side: OrderSide
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    filled_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    created_at: datetime
    updated_at: datetime


class V3Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[2]
    strategy_id: str
    implementation_version: str
    parameters: dict[str, Any]
    instrument_id: str
    bar: Literal["1h"]
    bars: list[CandleCheckpoint]
    bar_count: StrictInt = Field(ge=0)
    previous: StrictBool
    live_started: StrictBool
    position: Decimal = Field(ge=0, allow_inf_nan=False)
    entered_at: datetime | None
    orders: list[OrderCheckpoint]
    last_event_at: datetime


class V3Strategy:
    implementation_version = "v3_step_v2"
    state_contract_version = 2
    description = (
        "V3 causal entry episode with confirmed 1H/4H inputs and a fixed 24H holding limit"
    )
    supported_market_types = frozenset({InstrumentType.SPOT})
    maximum_hold_hours = 24

    def __init__(self, name: str, parameters: V3Parameters) -> None:
        self._name = name
        self._parameters = validated_parameters(name, parameters.model_dump())
        self._features = V3FeatureStream(
            V3Variant(name, name, "", "", "", "", ("1H",), self.parameter_values, True)
        )
        self._started = False
        self._stopped = False
        self._live_started = False
        self._previous = False
        self._entered_at: datetime | None = None
        self._position = Decimal("0")
        self._orders: dict[str, OrderCheckpoint] = {}
        self._instrument_id: str | None = None
        self._last_event_at: datetime | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def parameters(self) -> V3Parameters:
        return self._parameters

    @property
    def parameter_values(self) -> dict[str, Any]:
        return self.parameters.model_dump(mode="json")

    @property
    def required_history(self) -> int:
        return self._features.required_history

    @property
    def open_order_ids(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, order in self._orders.items() if order.state in _OPEN))

    def _validate_context(self, context: StrategyContext) -> None:
        now = context.now
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("V3 event clock must be aware UTC")
        if self._last_event_at is not None and now < self._last_event_at:
            raise ValueError("out-of-order V3 event clock; state unchanged")
        if context.strategy_name != self.name or context.bar.lower() != "1h":
            raise ValueError("V3 strategy identity/timeframe mismatch")
        if context.instrument.instrument_type not in self.supported_market_types:
            raise ValueError("V3 requires SPOT")
        if context.mode is not TradingMode.BACKTEST and not context.read_only:
            raise ValueError("V3 is restricted to research/backtest and read-only Shadow")
        if (
            self._instrument_id is not None
            and context.instrument.instrument_id != self._instrument_id
        ):
            raise ValueError("V3 instrument changed during a run")
        position = context.portfolio_snapshot.position(context.instrument.instrument_id)
        if not position.is_finite() or position < 0:
            raise ValueError("V3 position must be finite and long-only")

    def on_start(self, context: StrategyContext) -> None:
        self._validate_context(context)
        if self._started:
            raise ValueError(
                "V3 already initialized; create a fresh instance or explicitly restore"
            )
        if context.portfolio_snapshot.position(context.instrument.instrument_id) != 0:
            raise ValueError("nonflat V3 start requires a compatible checkpoint")
        self._instrument_id = context.instrument.instrument_id
        self._started = True
        self._last_event_at = context.now

    def on_stop(self, context: StrategyContext) -> None:
        self._validate_context(context)
        self._stopped = True  # Preserve business state for checkpointing; never silently clear it.

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        self._validate_context(context)
        if not self._started or self._stopped:
            raise ValueError("V3 must be initialized and running")
        if context.now != bar.timestamp + timedelta(hours=1):
            raise ValueError("V3 decision time must equal the confirmed candle close")
        if (
            context.market_snapshot is None
            or context.market_snapshot.candle != bar
            or context.market_snapshot.price != bar.close
        ):
            raise ValueError("V3 bar and context market snapshot differ")
        if context.portfolio_snapshot.position(context.instrument.instrument_id) != self._position:
            raise ValueError("V3 position changed without order feedback")
        if context.is_warmup and self._live_started:
            raise ValueError("cannot return to warmup after live signal production")
        flag, _, _ = self._features.update(bar)
        entry = flag and not self._previous
        self._previous = flag
        self._last_event_at = context.now
        if context.is_warmup:
            return []
        self._live_started = True
        action, reason = SignalAction.HOLD, "no_frozen_episode"
        if not self.open_order_ids:
            if (
                self._position > 0
                and self._entered_at is not None
                and (context.now - self._entered_at >= timedelta(hours=self.maximum_hold_hours))
            ):
                action, reason = SignalAction.CLOSE, "time_exit"
            elif self._position == 0 and entry:
                action, reason = SignalAction.BUY, "frozen_episode"
        identity = [
            self.name,
            self.implementation_version,
            self.parameter_values,
            context.instrument.instrument_id,
            bar.timestamp.isoformat(),
            action.value,
        ]
        return [
            Signal(
                canonical_hash(identity)[:32],
                self.name,
                context.instrument.instrument_id,
                action,
                context.now,
                reason,
                Decimal("1") if action is not SignalAction.HOLD else Decimal("0"),
                metadata={
                    "candle_confirmed": True,
                    "exit_reason": reason,
                    "implementation_version": self.implementation_version,
                    "parameter_hash": canonical_hash(self.parameter_values),
                    "candle_open_time": bar.timestamp.isoformat(),
                },
            )
        ]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        self._validate_context(context)
        if not self._started or self._stopped:
            raise ValueError("V3 feedback requires an initialized running strategy")
        if (
            order.request.instrument_id != self._instrument_id
            or order.request.strategy_name != self.name
        ):
            raise ValueError("V3 order feedback identity mismatch")
        update = OrderCheckpoint(
            client_order_id=order.request.client_order_id,
            state=order.state,
            side=order.request.side,
            quantity=order.request.quantity,
            filled_quantity=order.filled_quantity,
            created_at=order.request.created_at,
            updated_at=order.updated_at or context.now,
        )
        if (
            update.updated_at.tzinfo is None
            or update.created_at.tzinfo is None
            or (update.updated_at > context.now or update.updated_at < update.created_at)
            or update.filled_quantity > update.quantity
        ):
            raise ValueError("invalid V3 feedback time/quantity")
        previous = self._orders.get(update.client_order_id)
        if (
            previous is None
            and self._features.bars
            and update.created_at < (self._features.bars[-1].timestamp + timedelta(hours=1))
        ):
            raise ValueError("unknown historical V3 feedback requires reconciliation")
        if previous:
            if (
                previous.side != update.side
                or previous.quantity != update.quantity
                or previous.created_at != update.created_at
            ):
                raise ValueError("order identity changed during feedback")
            if (
                update.updated_at < previous.updated_at
                or update.filled_quantity < previous.filled_quantity
            ):
                raise ValueError("stale V3 order feedback")
            if previous.state not in _OPEN and update.state != previous.state:
                raise ValueError("terminal V3 order cannot reopen")
        position = context.portfolio_snapshot.position(context.instrument.instrument_id)
        entered = self._entered_at
        if order.request.side is OrderSide.BUY and order.filled_quantity > 0 and entered is None:
            entered = update.updated_at
        if position == 0:
            entered = None
        if position > 0 and entered is None:
            raise ValueError("nonflat V3 feedback lacks entry-time evidence")
        self._orders[update.client_order_id] = update
        # Keep all active orders plus a bounded terminal-feedback audit tail.
        terminal = [key for key, value in self._orders.items() if value.state not in _OPEN]
        for key in terminal[:-128]:
            del self._orders[key]
        self._position, self._entered_at = position, entered
        self._last_event_at = context.now

    def checkpoint_state(self) -> dict[str, Any]:
        if not self._started or self._last_event_at is None:
            raise ValueError("cannot checkpoint an uninitialized V3 strategy")
        state = V3Checkpoint(
            format_version=2,
            strategy_id=self.name,
            implementation_version=self.implementation_version,
            parameters=self.parameter_values,
            instrument_id=str(self._instrument_id),
            bar="1h",
            bars=[
                CandleCheckpoint(
                    **{name: getattr(bar, name) for name in Candle.__dataclass_fields__}
                )
                for bar in self._features.bars
            ],
            bar_count=self._features.count,
            previous=self._previous,
            live_started=self._live_started,
            position=self._position,
            entered_at=self._entered_at,
            orders=list(self._orders.values()),
            last_event_at=self._last_event_at,
        ).model_dump(mode="json")
        return {**state, "state_hash": canonical_hash(state)}

    def restore_checkpoint_state(
        self,
        state: dict[str, Any],
        context: StrategyContext,
        *,
        open_order_ids: tuple[str, ...] = (),
    ) -> None:
        self._validate_context(context)
        if self._started:
            raise ValueError("restore requires a fresh V3 instance; state is never silently reset")
        content = {key: value for key, value in state.items() if key != "state_hash"}
        if canonical_hash(content) != state.get("state_hash"):
            raise ValueError("V3 checkpoint hash mismatch")
        if (
            type(content.get("format_version")) is not int
            or content["format_version"] != self.state_contract_version
        ):
            raise ValueError("incompatible V3 checkpoint format; no implicit migration")
        parsed = V3Checkpoint.model_validate(content)
        if (
            parsed.last_event_at.tzinfo is None
            or parsed.last_event_at.utcoffset() != timedelta(0)
            or parsed.last_event_at != context.now
        ):
            raise ValueError("V3 checkpoint event clock differs from the resume clock")
        if (
            parsed.strategy_id,
            parsed.implementation_version,
            parsed.parameters,
            parsed.instrument_id,
        ) != (
            self.name,
            self.implementation_version,
            self.parameter_values,
            context.instrument.instrument_id,
        ):
            raise ValueError("incompatible V3 checkpoint identity/version/parameters")
        if parsed.position != context.portfolio_snapshot.position(context.instrument.instrument_id):
            raise ValueError("V3 checkpoint position requires reconciliation")
        if (parsed.position > 0) != (parsed.entered_at is not None):
            raise ValueError("V3 checkpoint entry-time/position mismatch")
        if parsed.entered_at is not None and (
            parsed.entered_at.tzinfo is None or parsed.entered_at > context.now
        ):
            raise ValueError("V3 checkpoint entry time invalid")
        stream = V3FeatureStream(self._features.variant)
        if len(parsed.bars) != min(parsed.bar_count, stream.required_history):
            raise ValueError("V3 checkpoint history length mismatch")
        last_flag = False
        for item in parsed.bars:
            last_flag = stream.update(item.candle())[0]
        if last_flag != parsed.previous:
            raise ValueError("V3 checkpoint feature/episode state mismatch")
        if stream.bars and context.now < stream.bars[-1].timestamp + timedelta(hours=1):
            raise ValueError("V3 checkpoint is ahead of the resume clock")
        orders = {item.client_order_id: item for item in parsed.orders}
        if len(orders) != len(parsed.orders) or any(
            item.updated_at.tzinfo is None
            or item.created_at.tzinfo is None
            or item.updated_at > context.now
            or item.updated_at < item.created_at
            or item.filled_quantity > item.quantity
            for item in orders.values()
        ):
            raise ValueError("invalid V3 checkpoint order feedback")
        active = tuple(sorted(key for key, item in orders.items() if item.state in _OPEN))
        if active != tuple(sorted(open_order_ids)):
            raise ValueError("V3 pending orders require explicit reconciliation")
        stream.count = parsed.bar_count
        self._features = stream
        self._position, self._entered_at, self._previous = (
            parsed.position,
            parsed.entered_at,
            parsed.previous,
        )
        self._orders, self._instrument_id = orders, parsed.instrument_id
        self._live_started, self._started = parsed.live_started, True
        self._last_event_at = parsed.last_event_at
