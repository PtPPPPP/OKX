from __future__ import annotations

import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.settings import TradingMode
from app.domain.market import InstrumentType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExchangeConfig(StrictModel):
    name: Literal["okx"] = "okx"
    simulated: bool = True


class MarketConfig(StrictModel):
    instrument_id: str = "BTC-USDT"
    instrument_type: InstrumentType = InstrumentType.SPOT
    bar: str = "5m"

    @model_validator(mode="after")
    def validate_supported_market(self) -> MarketConfig:
        if self.instrument_type is not InstrumentType.SPOT:
            raise ValueError("当前阶段只实现 spot 市场")
        if not self.instrument_id or "-" not in self.instrument_id:
            raise ValueError("instrument_id 格式无效")
        return self


class StrategyConfig(StrictModel):
    name: str = "moving_average_cross"
    acceptance_only: bool = False
    timeframe: str | None = None
    fast_window: int | None = Field(default=None, gt=0)
    slow_window: int | None = Field(default=None, gt=0)
    safety_buffer: int = Field(default=0, ge=0)
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"fast_period": 10, "slow_period": 30}
    )

    @model_validator(mode="after")
    def normalize_acceptance_windows(self) -> StrategyConfig:
        if self.fast_window is not None:
            self.parameters["fast_period"] = self.fast_window
        if self.slow_window is not None:
            self.parameters["slow_period"] = self.slow_window
        if (
            self.fast_window is not None
            and self.slow_window is not None
            and self.fast_window >= self.slow_window
        ):
            raise ValueError("fast_window must be less than slow_window")
        return self


class PositionSizingConfig(StrictModel):
    name: Literal["fixed_notional"] = "fixed_notional"
    parameters: dict[str, Any] = Field(default_factory=lambda: {"order_notional": Decimal("20")})


class RiskConfig(StrictModel):
    max_order_notional: Decimal = Field(default=Decimal("20"), gt=0)
    max_total_exposure: Decimal = Field(default=Decimal("100"), gt=0)
    max_daily_loss: Decimal = Field(default=Decimal("10"), ge=0)
    max_drawdown_pct: Decimal = Field(default=Decimal("5"), gt=0, le=100)
    max_orders_per_minute: int = Field(default=2, gt=0)
    stale_after_seconds: int = Field(default=600, gt=0)

    @model_validator(mode="after")
    def validate_limits(self) -> RiskConfig:
        if self.max_order_notional > self.max_total_exposure:
            raise ValueError("单笔金额不得超过总风险敞口")
        return self


class ProtectiveExitsConfig(StrictModel):
    enabled: bool = True
    stop_loss_pct: Decimal = Field(default=Decimal("1"), gt=0, lt=100)
    take_profit_pct: Decimal = Field(default=Decimal("2"), gt=0)


class BacktestConfig(StrictModel):
    initial_capital: Decimal = Field(default=Decimal("10000"), ge=0)
    fee_rate: Decimal = Field(default=Decimal("0.001"), ge=0, lt=1)
    slippage_rate: Decimal = Field(default=Decimal("0.0005"), ge=0, lt=1)
    seed: int = 0


class DataConfig(StrictModel):
    source: Literal["okx", "csv"] = "okx"
    path: Path | None = None
    limit: int = Field(default=300, gt=0, le=1000)
    output: Path = Path("data/market_data.csv")
    instrument_snapshot: Path | None = None

    @model_validator(mode="after")
    def validate_source(self) -> DataConfig:
        if self.source == "csv" and self.path is None:
            raise ValueError("CSV 数据源必须配置 path")
        return self


class OutputConfig(StrictModel):
    directory: Path = Path("data/results")


class RunConfig(StrictModel):
    mode: TradingMode = TradingMode.BACKTEST
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    position_sizing: PositionSizingConfig = Field(default_factory=PositionSizingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    protective_exits: ProtectiveExitsConfig = Field(default_factory=ProtectiveExitsConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def validate_safety(self) -> RunConfig:
        if self.mode is TradingMode.LIVE:
            raise ValueError("当前框架禁止 live 模式")
        if not self.exchange.simulated:
            raise ValueError("当前框架要求 exchange.simulated=true")
        if self.strategy.acceptance_only and self.mode is not TradingMode.DEMO:
            raise ValueError("acceptance_only config is restricted to demo mode")
        return self


ENVIRONMENT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "TRADING_MODE": ("mode",),
    "INSTRUMENT_ID": ("market", "instrument_id"),
    "INSTRUMENT_TYPE": ("market", "instrument_type"),
    "BAR": ("market", "bar"),
    "STRATEGY_NAME": ("strategy", "name"),
    "ORDER_NOTIONAL": ("position_sizing", "parameters", "order_notional"),
    "MAX_ORDER_NOTIONAL": ("risk", "max_order_notional"),
    "MAX_TOTAL_EXPOSURE": ("risk", "max_total_exposure"),
    "MAX_DAILY_LOSS": ("risk", "max_daily_loss"),
    "MAX_DRAWDOWN_PCT": ("risk", "max_drawdown_pct"),
    "INITIAL_CAPITAL": ("backtest", "initial_capital"),
}


def load_run_config(
    path: Path | None = None,
    *,
    cli_overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> RunConfig:
    data: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ValueError(f"配置文件不存在: {path}")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("YAML 顶层必须是对象")
        data = loaded or {}

    if environ is None:
        environment = {
            **{key: value for key, value in dotenv_values(".env").items() if value is not None},
            **os.environ,
        }
    else:
        environment = environ
    if "STRATEGY_NAME" in environment:
        strategy_data = data.get("strategy")
        configured_name = strategy_data.get("name") if isinstance(strategy_data, dict) else None
        if configured_name != environment["STRATEGY_NAME"]:
            _set_nested(data, ("strategy", "parameters"), {})
    for name, key_path in ENVIRONMENT_OVERRIDES.items():
        if name in environment:
            _set_nested(data, key_path, environment[name])
    command_line = cli_overrides or {}
    cli_strategy = command_line.get("strategy.name")
    if cli_strategy is not None:
        strategy_data = data.get("strategy")
        configured_name = strategy_data.get("name") if isinstance(strategy_data, dict) else None
        if configured_name != cli_strategy:
            _set_nested(data, ("strategy", "parameters"), {})
    for key, value in command_line.items():
        if value is not None:
            _set_nested(data, tuple(key.split(".")), value)

    config = RunConfig.model_validate(deepcopy(data))
    from app.strategies.registry import validate_strategy_parameters

    validate_strategy_parameters(config.strategy.name, config.strategy.parameters)
    validate_position_sizing(config.position_sizing)
    return config


def validate_position_sizing(config: PositionSizingConfig) -> None:
    allowed = {"order_notional"}
    unknown = set(config.parameters) - allowed
    if unknown:
        raise ValueError(f"fixed_notional 不支持参数: {', '.join(sorted(unknown))}")
    value = Decimal(str(config.parameters.get("order_notional", "0")))
    if not value.is_finite() or value <= 0:
        raise ValueError("order_notional 必须大于 0")


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"配置路径冲突: {'.'.join(path)}")
        current = child
    current[path[-1]] = value
