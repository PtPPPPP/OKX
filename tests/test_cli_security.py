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
