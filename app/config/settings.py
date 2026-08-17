from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.exchange.auth_diagnostics import CredentialFieldStatus


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    DEMO = "demo"
    LIVE = "live"


class Settings(BaseSettings):
    """Infrastructure and secret settings. Strategy settings belong to RunConfig."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    trading_mode: TradingMode = TradingMode.BACKTEST
    allow_live_trading: bool = False
    okx_api_key: SecretStr = SecretStr("")
    okx_secret_key: SecretStr = SecretStr("")
    okx_passphrase: SecretStr = SecretStr("")
    database_url: str = "sqlite:///data/trading.db"
    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_okx_environment_names(cls, values: Any) -> Any:
        allowed = {
            "OKX_API_KEY",
            "OKX_NETWORK_MODE",
            "OKX_PASSPHRASE",
            "OKX_PROXY_URL",
            "OKX_SECRET_KEY",
        }
        names = {name.upper() for name in os.environ if name.upper().startswith("OKX_")}
        env_file = Path(".env")
        if env_file.is_file():
            names.update(
                str(name).upper()
                for name in dotenv_values(env_file)
                if str(name).upper().startswith("OKX_")
            )
        unknown = sorted(names - allowed)
        if isinstance(values, Mapping):
            known_fields = set(cls.model_fields)
            unknown.extend(
                sorted(
                    str(name)
                    for name in values
                    if str(name).lower().startswith("okx_")
                    and str(name).lower() not in known_fields
                )
            )
        if unknown:
            raise ValueError(
                f"unknown OKX configuration name(s): {', '.join(sorted(set(unknown)))}"
            )
        return values

    @model_validator(mode="after")
    def validate_safety(self) -> Settings:
        if self.trading_mode is TradingMode.LIVE:
            raise ValueError("当前框架禁止 live 模式")
        if self.allow_live_trading:
            raise ValueError("当前框架要求 ALLOW_LIVE_TRADING=false")
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("当前框架仅支持 SQLite")
        return self

    def require_demo_credentials(self) -> None:
        if self.trading_mode is not TradingMode.DEMO:
            raise ValueError("该命令只能在 mode=demo 下运行")
        missing = [
            name
            for name, value in (
                ("OKX_API_KEY", self.okx_api_key),
                ("OKX_SECRET_KEY", self.okx_secret_key),
                ("OKX_PASSPHRASE", self.okx_passphrase),
            )
            if not value.get_secret_value().strip()
        ]
        if missing:
            raise ValueError(f"模拟盘缺少凭证配置: {', '.join(missing)}")

    @property
    def demo_credentials_configured(self) -> bool:
        return all(
            value.get_secret_value().strip()
            for value in (
                self.okx_api_key,
                self.okx_secret_key,
                self.okx_passphrase,
            )
        )

    def safe_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"okx_api_key", "okx_secret_key", "okx_passphrase"})
        data.update(
            {
                "okx_api_key": self._masked(self.okx_api_key),
                "okx_secret_key": self._masked(self.okx_secret_key),
                "okx_passphrase": self._masked(self.okx_passphrase),
            }
        )
        return data

    def credential_diagnostics(self) -> dict[str, CredentialFieldStatus]:
        env_file = Path(".env")
        env_names = {
            "OKX_API_KEY": self.okx_api_key,
            "OKX_SECRET_KEY": self.okx_secret_key,
            "OKX_PASSPHRASE": self.okx_passphrase,
        }
        result: dict[str, CredentialFieldStatus] = {}
        for name, secret in env_names.items():
            value = secret.get_secret_value()
            source = (
                "environment"
                if name in os.environ
                else "env_file"
                if env_file.exists()
                else "default"
            )
            result[name] = CredentialFieldStatus(
                configured=bool(value.strip()),
                source=source,
                has_leading_or_trailing_whitespace=value != value.strip(),
                contains_linebreak="\n" in value or "\r" in value,
            )
        return result

    @staticmethod
    def _masked(value: SecretStr) -> str:
        return "***configured***" if value.get_secret_value() else ""
