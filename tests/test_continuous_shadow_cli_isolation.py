from __future__ import annotations

import ast
import inspect
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app
from app.continuous_shadow_cli import run_vwap_continuous_shadow


def test_continuous_shadow_cli_help_is_read_only_and_has_no_trading_options() -> None:
    result = CliRunner().invoke(app, ["run-vwap-continuous-shadow", "--help"])
    assert result.exit_code == 0
    assert "READ-ONLY" in result.output
    for forbidden in (
        "--api-key",
        "--secret",
        "--passphrase",
        "--quantity",
        "--notional",
        "--budget",
    ):
        assert forbidden not in result.output
    assert "max_runtime_seconds" in inspect.signature(run_vwap_continuous_shadow).parameters


def test_continuous_shadow_recovery_cli_is_local_only() -> None:
    result = CliRunner().invoke(app, ["recover-vwap-continuous-shadow", "--help"])
    assert result.exit_code == 0
    assert "dead-owner" in result.output
    for forbidden in ("--api-key", "--secret", "--passphrase", "--proxy", "--network-mode"):
        assert forbidden not in result.output


def test_continuous_shadow_public_modules_have_no_execution_dependencies() -> None:
    forbidden = {
        "DemoTradingSession",
        "OKXDemoBroker",
        "ReadOnlyBroker",
        "BacktestBroker",
        "TradingEngine",
        "ControlledDemoWriteService",
        "OkxClient",
    }
    for path in (
        Path("app/continuous_shadow_cli.py"),
        Path("app/market/network.py"),
        Path("app/market/okx_public.py"),
        Path("app/market/websocket.py"),
        Path("app/services/vwap_continuous_shadow.py"),
        Path("app/services/shadow_smoke_recovery.py"),
        Path("scripts/diagnostics/public_network_preflight.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not forbidden & imported
