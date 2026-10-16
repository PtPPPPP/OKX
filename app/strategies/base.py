from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.domain.context import StrategyContext
from app.domain.market import Candle, InstrumentType
from app.domain.order import Order
from app.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class ProtectiveExitLevels:
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    take_profit_reason: str = "take_profit"


@runtime_checkable
class ProtectiveExitProvider(Protocol):
    def protective_exit_levels(self, entry_price: Decimal) -> ProtectiveExitLevels: ...


class Strategy(Protocol):
    """Candle timestamps are opening instants; runners deliver confirmed bars at close.

    Legacy strategies retain their behavior. StatefulStrategy additionally requires
    strict continuity, explicit warmup, version-bound restoration and feedback.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def required_history(self) -> int: ...

    @property
    def supported_market_types(self) -> frozenset[InstrumentType]: ...

    def on_start(self, context: StrategyContext) -> None: ...

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]: ...

    def on_order_update(self, context: StrategyContext, order: Order) -> None: ...

    def on_stop(self, context: StrategyContext) -> None: ...


@runtime_checkable
class StatefulStrategy(Strategy, Protocol):
    @property
    def implementation_version(self) -> str: ...

    @property
    def parameter_values(self) -> dict[str, Any]: ...

    def checkpoint_state(self) -> dict[str, Any]: ...

    def restore_checkpoint_state(
        self,
        state: dict[str, Any],
        context: StrategyContext,
        *,
        open_order_ids: tuple[str, ...] = (),
    ) -> None: ...


def strategy_identity(strategy: Strategy) -> dict[str, Any]:
    if isinstance(strategy, StatefulStrategy):
        return {
            "strategy_id": strategy.name,
            "implementation_version": strategy.implementation_version,
            "parameters": strategy.parameter_values,
            "state_contract_version": 1,
        }
    from app.strategies.registry import STRATEGY_REGISTRY

    registration = STRATEGY_REGISTRY.get(strategy.name)
    parameters = getattr(strategy, "parameters", None)
    return {
        "strategy_id": strategy.name,
        "implementation_version": registration.implementation_version
        if registration
        else "legacy_unversioned",
        "parameters": parameters.model_dump(mode="json")
        if isinstance(parameters, BaseModel)
        else {},
        "state_contract_version": None,
    }
