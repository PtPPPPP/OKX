from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SwapMarketConfig(_Strict):
    instrument_id: str
    execution_timeframe: str = "5m"
    confirmation_timeframes: tuple[str, str] = ("15m", "1h")

    @model_validator(mode="after")
    def supported_contract(self) -> SwapMarketConfig:
        if self.instrument_id not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}:
            raise ValueError("only BTC-USDT-SWAP and ETH-USDT-SWAP are supported")
        if self.execution_timeframe != "5m" or self.confirmation_timeframes != ("15m", "1h"):
            raise ValueError("phase B requires 5m execution with 15m and 1h confirmation")
        return self


class SwapRiskConfig(_Strict):
    leverage: Decimal = Field(default=Decimal("1"), ge=1, le=1)
    risk_fraction_per_trade: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.005"))
    minimum_risk_reward: Decimal = Field(default=Decimal("1.5"), ge=Decimal("1.5"))
    maximum_consecutive_losses: int = Field(default=3, ge=1)


class SwapBacktestConfig(_Strict):
    environment: str = "backtest"
    live_trading: bool = False
    swap_demo_enabled: bool = False
    strategy_profitability_validated: bool = False
    strategy_name: str = "anchored_vwap_multifactor_swap"
    market: SwapMarketConfig
    risk: SwapRiskConfig = Field(default_factory=SwapRiskConfig)
    vwap_session_timezone: str = "UTC"
    vwap_anchor_hour: int = Field(default=0, ge=0, le=23)
    vwap_anchor_minute: int = Field(default=0, ge=0, le=59)
    macd_fast: int = Field(default=12, gt=0)
    macd_slow: int = Field(default=26, gt=0)
    macd_signal: int = Field(default=9, gt=0)
    maximum_oi_staleness_minutes: int = Field(default=15, gt=0)

    @model_validator(mode="after")
    def safety(self) -> SwapBacktestConfig:
        if self.environment != "backtest" or self.live_trading or self.swap_demo_enabled:
            raise ValueError("swap phase B is fixture/backtest only")
        if self.strategy_name != "anchored_vwap_multifactor_swap":
            raise ValueError("unsupported swap strategy")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be lower than macd_slow")
        return self


def load_swap_backtest_config(path: Path) -> SwapBacktestConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("swap config must be a mapping")
    return SwapBacktestConfig.model_validate(payload)
