from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config.run_config import load_run_config
from app.config.settings import Settings, TradingMode


def test_btc_and_eth_examples_validate() -> None:
    btc = load_run_config(Path("configs/btc_ma_backtest.yaml"), environ={})
    eth = load_run_config(Path("configs/eth_buy_hold_backtest.yaml"), environ={})
    assert btc.market.instrument_id == "BTC-USDT"
    assert eth.market.instrument_id == "ETH-USDT"
    assert eth.strategy.name == "buy_and_hold"


def test_precedence_cli_over_environment_over_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "market:\n  instrument_id: BTC-USDT\n  bar: 5m\n"
        "strategy:\n  name: buy_and_hold\n  parameters: {}\n",
        encoding="utf-8",
    )
    config = load_run_config(
        path,
        environ={"INSTRUMENT_ID": "ETH-USDT"},
        cli_overrides={"market.instrument_id": "SOL-USDT"},
    )
    assert config.market.instrument_id == "SOL-USDT"


def test_environment_overrides_defaults() -> None:
    config = load_run_config(None, environ={"INSTRUMENT_ID": "ETH-USDT", "BAR": "1h"})
    assert config.market.instrument_id == "ETH-USDT"
    assert config.market.bar == "1h"


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="未注册策略"):
        load_run_config(None, environ={}, cli_overrides={"strategy.name": "unknown"})


def test_strategy_parameters_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "strategy:\n  name: buy_and_hold\n  parameters:\n    fast_period: 10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="fast_period"):
        load_run_config(path, environ={})


def test_cli_strategy_override_does_not_reuse_yaml_parameters() -> None:
    config = load_run_config(
        Path("configs/btc_ma_backtest.yaml"),
        environ={},
        cli_overrides={"strategy.name": "buy_and_hold"},
    )
    assert config.strategy.name == "buy_and_hold"
    assert config.strategy.parameters == {}


def test_moving_average_parameter_error_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "strategy:\n  name: moving_average_cross\n"
        "  parameters:\n    fast_period: 30\n    slow_period: 10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="fast_period"):
        load_run_config(path, environ={})


def test_live_mode_is_always_rejected() -> None:
    with pytest.raises(ValidationError, match="禁止 live"):
        load_run_config(None, environ={"TRADING_MODE": "live"})


def test_application_live_mode_is_always_rejected() -> None:
    with pytest.raises(ValidationError, match="禁止 live"):
        Settings(trading_mode=TradingMode.LIVE)


def test_secrets_are_not_exposed() -> None:
    settings = Settings(
        okx_api_key=SecretStr("real-key"),
        okx_secret_key=SecretStr("real-secret"),
        okx_passphrase=SecretStr("real-passphrase"),
    )
    output = str(settings.safe_dict())
    assert "real-key" not in output
    assert "real-secret" not in output
    assert "real-passphrase" not in output


def test_unknown_okx_environment_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_API_KEYS", "typo")

    with pytest.raises(ValidationError, match="OKX_API_KEYS"):
        Settings()


def test_unknown_okx_constructor_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="okx_api_keys"):
        Settings(okx_api_keys="typo")  # type: ignore[call-arg]


def test_unrelated_environment_name_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THIRD_PARTY_SETTING", "allowed")

    Settings()
    assert "THIRD_PARTY_SETTING" not in Settings.model_fields
