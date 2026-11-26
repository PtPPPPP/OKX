from __future__ import annotations

import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.settings import Settings, TradingMode
from app.domain.market import InstrumentType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExchangeConfig(StrictModel):
    name: Literal["okx"] = "okx"
    simulated: bool = True
    post_only: bool = False


class MarketConfig(StrictModel):
    instrument_id: str = "BTC-USDT"
    instruments: list[str] = Field(default_factory=list)
    instrument_type: InstrumentType = InstrumentType.SPOT
    bar: str = "5m"

    @property
    def effective_instruments(self) -> tuple[str, ...]:
        if not self.instruments:
            return (self.instrument_id,)
        return tuple(self.instruments)

    @model_validator(mode="after")
    def validate_supported_market(self) -> MarketConfig:
        if self.instrument_type is not InstrumentType.SPOT:
            raise ValueError("当前阶段只实现 spot 市场")
        if not self.instrument_id or "-" not in self.instrument_id:
            raise ValueError("instrument_id 格式无效")
        if self.instruments:
            quotes: set[str] = set()
            seen: set[str] = set()
            for inst in self.instruments:
                if not inst or "-" not in inst:
                    raise ValueError(f"instruments 包含无效格式: {inst}")
                if inst in seen:
                    raise ValueError(f"instruments 包含重复品种: {inst}")
                seen.add(inst)
                quotes.add(inst.split("-", 1)[1])
            if len(quotes) > 1:
                raise ValueError("多品种组合要求所有品种共享同一计价货币")
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
    name: Literal["fixed_notional", "percent_equity", "risk_per_trade"] = "fixed_notional"
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
    volume_participation: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    seed: int = 0


class DataConfig(StrictModel):
    source: Literal["okx", "csv"] = "okx"
    path: Path | None = None
    paths: dict[str, Path] = Field(default_factory=dict)
    limit: int = Field(default=300, gt=0)
    output: Path = Path("data/market_data.csv")
    instrument_snapshot: Path | None = None

    @model_validator(mode="after")
    def validate_source(self) -> DataConfig:
        if self.source == "okx" and self.limit > 1000:
            raise ValueError("OKX 常规历史入口最多读取 1000 根 K 线；长历史使用研究下载器")
        if self.source == "csv" and self.path is None and not self.paths:
            raise ValueError("CSV 数据源必须配置 path 或 paths")
        return self


class OutputConfig(StrictModel):
    directory: Path = Path("data/results")


class MonitoringConfig(StrictModel):
    alerts_enabled: bool = False
    provider: Literal["wecom", "feishu", "generic"] = "wecom"
    min_level: Literal["info", "warning", "critical"] = "info"
    timeout_seconds: float = Field(default=3.0, gt=0, le=30.0)


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
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @model_validator(mode="after")
    def validate_safety(self) -> RunConfig:
        if self.mode is not TradingMode.LIVE and not self.exchange.simulated:
            raise ValueError("当前框架在非 live 模式下要求 exchange.simulated=true")
        if self.strategy.acceptance_only and self.mode is not TradingMode.DEMO:
            raise ValueError("acceptance_only config is restricted to demo mode")
        return self

    def with_instrument(self, instrument_id: str) -> RunConfig:
        new_market = self.market.model_copy(
            update={"instrument_id": instrument_id, "instruments": [instrument_id]}
        )
        new_data = self.data
        if instrument_id in self.data.paths:
            new_data = self.data.model_copy(update={"path": self.data.paths[instrument_id]})
        return self.model_copy(update={"market": new_market, "data": new_data})


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

    effective_mode = str(data.get("mode", "")).strip().lower()
    allow_live_raw = str(environment.get("ALLOW_LIVE_TRADING", "")).strip().lower()
    allow_live = allow_live_raw in {"1", "true", "yes"}
    if effective_mode == TradingMode.LIVE.value:
        if not allow_live:
            # Ensure Pydantic raises a ValidationError for callers expecting config validation failure
            Settings.model_validate({"trading_mode": "live", "allow_live_trading": False})
        exchange_dict = data.get("exchange")
        if not isinstance(exchange_dict, dict):
            data["exchange"] = {"name": "okx", "simulated": False}
        elif "simulated" not in exchange_dict:
            exchange_dict["simulated"] = False

    config = RunConfig.model_validate(deepcopy(data))
    from app.strategies.registry import validate_strategy_parameters

    validate_strategy_parameters(config.strategy.name, config.strategy.parameters)
    validate_position_sizing(config.position_sizing)
    return config


def validate_position_sizing(config: PositionSizingConfig) -> None:
    from app.position_sizing.fixed_notional import validate_position_sizing_parameters

    validate_position_sizing_parameters(config.name, config.parameters)


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"配置路径冲突: {'.'.join(path)}")
        current = child
    current[path[-1]] = value
