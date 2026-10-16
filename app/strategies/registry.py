from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from pydantic import BaseModel

from app.config.settings import TradingMode
from app.domain.market import Instrument, InstrumentType
from app.strategies.base import Strategy
from app.strategies.buy_and_hold import (
    BuyAndHoldParameters,
    BuyAndHoldStrategy,
)
from app.strategies.moving_average import (
    MovingAverageCrossParameters,
    MovingAverageCrossStrategy,
)
from app.strategies.v3 import V3Strategy
from app.strategies.v3_parameters import V3_PARAMETER_MODELS, V3Parameters
from app.strategies.vwap_mean_reversion import (
    VWAPMeanReversionParameters,
    VWAPMeanReversionStrategy,
)
from app.strategies.vwap_shadow import (
    VWAPShadowParameters,
    VWAPShadowStrategy,
)


@dataclass(frozen=True, slots=True)
class StrategyRegistration:
    parameter_model: type[BaseModel]
    factory: Callable[[BaseModel], Strategy]
    description: str
    required_history: int | str
    supported_market_types: frozenset[InstrumentType]
    shadow_only: bool = False
    implementation_version: str = "legacy_unversioned"
    research_only: bool = False


def _moving_average_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, MovingAverageCrossParameters):
        raise TypeError("双均线参数类型错误")
    return MovingAverageCrossStrategy(parameters)


def _buy_and_hold_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, BuyAndHoldParameters):
        raise TypeError("买入持有参数类型错误")
    return BuyAndHoldStrategy(parameters)


def _vwap_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, VWAPMeanReversionParameters):
        raise TypeError("VWAP 参数类型错误")
    return VWAPMeanReversionStrategy(parameters)


def _vwap_shadow_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, VWAPShadowParameters):
        raise TypeError("纯 VWAP Shadow 参数类型错误")
    return VWAPShadowStrategy(parameters)


STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    "moving_average_cross": StrategyRegistration(
        parameter_model=MovingAverageCrossParameters,
        factory=_moving_average_factory,
        description=MovingAverageCrossStrategy.description,
        required_history="slow_period + 1",
        supported_market_types=MovingAverageCrossStrategy.supported_market_types,
    ),
    "buy_and_hold": StrategyRegistration(
        parameter_model=BuyAndHoldParameters,
        factory=_buy_and_hold_factory,
        description=BuyAndHoldStrategy.description,
        required_history=BuyAndHoldStrategy.required_history,
        supported_market_types=BuyAndHoldStrategy.supported_market_types,
    ),
    "vwap_mean_reversion": StrategyRegistration(
        parameter_model=VWAPMeanReversionParameters,
        factory=_vwap_factory,
        description=VWAPMeanReversionStrategy.description,
        required_history="max(vwap_window, rsi_period + 1, atr_period + 1)",
        supported_market_types=VWAPMeanReversionStrategy.supported_market_types,
    ),
    "vwap_shadow": StrategyRegistration(
        parameter_model=VWAPShadowParameters,
        factory=_vwap_shadow_factory,
        description=VWAPShadowStrategy.description,
        required_history="vwap_window",
        supported_market_types=VWAPShadowStrategy.supported_market_types,
        shadow_only=True,
        implementation_version="vwap_shadow_v1",
    ),
}


def _v3_factory(name: str, parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, V3Parameters):
        raise TypeError("V3 parameter model mismatch")
    return V3Strategy(name, parameters)


for _name, _model in V3_PARAMETER_MODELS.items():
    STRATEGY_REGISTRY[_name] = StrategyRegistration(
        parameter_model=_model,
        factory=partial(_v3_factory, _name),
        description=V3Strategy.description,
        required_history="parameter-bound 1H window and completed 4H bars",
        supported_market_types=V3Strategy.supported_market_types,
        implementation_version=V3Strategy.implementation_version,
        research_only=True,
    )


def validate_strategy_mode(name: str, mode: TradingMode) -> None:
    if _registration(name).research_only and mode is not TradingMode.BACKTEST:
        raise ValueError(
            "research strategy requires backtest mode; use the explicit read-only Shadow adapter"
        )


def validate_strategy_version(name: str, version: str | None) -> None:
    registration = _registration(name)
    if version is None and registration.research_only:
        raise ValueError("V3 configuration requires an explicit implementation_version")
    if version is not None and version != registration.implementation_version:
        raise ValueError("configured strategy implementation_version is unavailable")


def configuration_identity(
    name: str, parameters: dict[str, Any], version: str | None
) -> dict[str, Any]:
    validate_strategy_version(name, version)
    registration = _registration(name)
    validated = registration.parameter_model.model_validate(parameters)
    return {
        "strategy_id": name,
        "implementation_version": registration.implementation_version,
        "parameters": validated.model_dump(mode="json"),
        "state_contract_version": 1 if registration.research_only else None,
    }


def create_strategy(
    name: str,
    parameters: dict[str, Any],
    instrument: Instrument,
    *,
    implementation_version: str | None = None,
) -> Strategy:
    registration = _registration(name)
    if implementation_version is not None:
        validate_strategy_version(name, implementation_version)
    validated = registration.parameter_model.model_validate(parameters)
    strategy = registration.factory(validated)
    if instrument.instrument_type not in registration.supported_market_types:
        raise ValueError(f"策略 {name} 不支持市场类型 {instrument.instrument_type.value}")
    return strategy


def validate_strategy_parameters(name: str, parameters: dict[str, Any]) -> None:
    _registration(name).parameter_model.model_validate(parameters)


def strategy_descriptions() -> list[dict[str, Any]]:
    descriptions = []
    for name, registration in sorted(STRATEGY_REGISTRY.items()):
        if registration.shadow_only:
            continue
        descriptions.append(
            {
                "name": name,
                "description": registration.description,
                "parameter_schema": registration.parameter_model.model_json_schema(),
                "required_history": registration.required_history,
                "supported_market_types": sorted(
                    item.value for item in registration.supported_market_types
                ),
                "implementation_version": registration.implementation_version,
                "research_only": registration.research_only,
            }
        )
    return descriptions


def _registration(name: str) -> StrategyRegistration:
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as exc:
        names = ", ".join(sorted(STRATEGY_REGISTRY))
        raise ValueError(f"未注册策略: {name}。可用策略: {names}") from exc
