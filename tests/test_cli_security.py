from __future__ import annotations

import pytest
from typer.testing import CliRunner

from app.cli import app


def test_live_validation_never_echoes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("OKX_API_KEY", "sensitive-api-key")
    monkeypatch.setenv("OKX_SECRET_KEY", "sensitive-secret")
    monkeypatch.setenv("OKX_PASSPHRASE", "sensitive-passphrase")

    result = CliRunner().invoke(app, ["show-config"])

    assert result.exit_code == 1
    assert "禁止 live" in result.output
    assert "sensitive-api-key" not in result.output
    assert "sensitive-secret" not in result.output
    assert "sensitive-passphrase" not in result.output


def test_demo_cancellation_requires_explicit_confirmation() -> None:
    result = CliRunner().invoke(
        app,
        [
            "cancel-demo-order",
            "client-order-id",
            "--config",
            "configs/btc_ma_demo.yaml",
        ],
    )
    assert result.exit_code == 1
    assert "--confirm-demo-cancellation" in result.output


def test_run_demo_execute_orders_requires_explicit_confirmation() -> None:
    result = CliRunner().invoke(
        app,
        [
            "run-demo",
            "--config",
            "configs/btc_ma_demo.yaml",
            "--execute-demo-orders",
        ],
    )
    assert result.exit_code == 1
    assert "--confirm-demo-order" in result.output


def test_run_live_requires_explicit_confirmation_and_dual_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Missing --confirm-live-trading when --execute-live-orders is set
    res1 = CliRunner().invoke(
        app,
        [
            "run-live",
            "--config",
            "configs/btc_ma_bollinger_live.yaml",
            "--execute-live-orders",
        ],
    )
    assert res1.exit_code == 1
    assert "--confirm-live-trading" in res1.output

    # 2. Missing ALLOW_LIVE_TRADING=true in environment
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    res2 = CliRunner().invoke(
        app,
        [
            "run-live",
            "--config",
            "configs/btc_ma_bollinger_live.yaml",
        ],
    )
    assert res2.exit_code == 1
    assert "禁止 live" in res2.output
