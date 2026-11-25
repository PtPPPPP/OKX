from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.domain.market import Instrument, InstrumentType
from app.strategies.base import Strategy
from app.strategies.buy_and_hold import (
    BuyAndHoldParameters,
    BuyAndHoldStrategy,
)
from app.strategies.ma_bollinger import (
    MABollingerParameters,
    MABollingerPullbackStrategy,
)
from app.strategies.moving_average import (
    MovingAverageCrossParameters,
    MovingAverageCrossStrategy,
)
from app.strategies.vwap_mean_reversion import (
    VWAPMeanReversionParameters,
    VWAPMeanReversionStrategy,
)


@dataclass(frozen=True, slots=True)
class StrategyRegistration:
    parameter_model: type[BaseModel]
    factory: Callable[[BaseModel], Strategy]
    description: str
    required_history: int | str
    supported_market_types: frozenset[InstrumentType]


def _moving_average_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, MovingAverageCrossParameters):
        raise TypeError("双均线参数类型错误")
    return MovingAverageCrossStrategy(parameters)


def _buy_and_hold_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, BuyAndHoldParameters):
        raise TypeError("买入持有参数类型错误")
    return BuyAndHoldStrategy(parameters)


def _ma_bollinger_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, MABollingerParameters):
        raise TypeError("均线布林带参数类型错误")
    return MABollingerPullbackStrategy(parameters)


def _vwap_factory(parameters: BaseModel) -> Strategy:
    if not isinstance(parameters, VWAPMeanReversionParameters):
        raise TypeError("VWAP 参数类型错误")
    return VWAPMeanReversionStrategy(parameters)


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
    "ma_bollinger_pullback": StrategyRegistration(
        parameter_model=MABollingerParameters,
        factory=_ma_bollinger_factory,
        description=MABollingerPullbackStrategy.description,
        required_history="max(trend_ma_period + 1 if require_trend_rising else trend_ma_period, bollinger_period)",
        supported_market_types=MABollingerPullbackStrategy.supported_market_types,
    ),
    "vwap_mean_reversion": StrategyRegistration(
        parameter_model=VWAPMeanReversionParameters,
        factory=_vwap_factory,
        description=VWAPMeanReversionStrategy.description,
        required_history="max(vwap_window, rsi_period + 1, atr_period + 1)",
        supported_market_types=VWAPMeanReversionStrategy.supported_market_types,
    ),
}


def create_strategy(name: str, parameters: dict[str, Any], instrument: Instrument) -> Strategy:
    registration = _registration(name)
    validated = registration.parameter_model.model_validate(parameters)
    strategy = registration.factory(validated)
    if instrument.instrument_type not in registration.supported_market_types:
        raise ValueError(f"策略 {name} 不支持市场类型 {instrument.instrument_type.value}")
    return strategy


def validate_strategy_parameters(name: str, parameters: dict[str, Any]) -> None:
    _registration(name).parameter_model.model_validate(parameters)


def strategy_descriptions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": registration.description,
            "parameter_schema": registration.parameter_model.model_json_schema(),
            "required_history": registration.required_history,
            "supported_market_types": sorted(
                item.value for item in registration.supported_market_types
            ),
        }
        for name, registration in sorted(STRATEGY_REGISTRY.items())
    ]


def _registration(name: str) -> StrategyRegistration:
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as exc:
        names = ", ".join(sorted(STRATEGY_REGISTRY))
        raise ValueError(f"未注册策略: {name}。可用策略: {names}") from exc
