from __future__ import annotations

import ast
import hashlib
import socket
from pathlib import Path
from typing import cast

import pytest

from app.config.run_config import load_run_config
from app.market.historical_data import MarketDataError
from app.market.synthetic_candles import (
    SyntheticCandleRequest,
    generate_synthetic_candles,
)
from app.services.vwap_shadow_soak import (
    ShadowSoakStore,
    build_synthetic_soak_source,
    load_csv_soak_source,
    read_soak_snapshot,
    run_vwap_shadow_soak,
    validate_soak_candles,
)
from app.vwap_shadow_soak import build_parser

_CONFIG = Path("configs/btc_vwap_shadow.yaml")
_REAL_CSV = Path("tests/fixtures/vwap/btc_usdt_1h_live.csv")
_REAL_CSV_HASH = "d21d9d6cf43ab88cf29efb9ead08363bfdf5c0ba5eb2626102e275f3f2442253"
_PRODUCTION_DATABASE = Path("data/trading.db")


def test_synthetic_candles_are_deterministic_valid_and_injectable() -> None:
    request = SyntheticCandleRequest(
        count=10_000,
        seed=20260731,
        bar_interval="5m",
        zero_volume_at=frozenset({17}),
        unconfirmed_at=frozenset({23}),
    )
    first = generate_synthetic_candles(request)
    second = generate_synthetic_candles(request)

    assert first == second
    assert len(first) == 10_000
    assert first[17].volume == 0
    assert not first[23].confirmed
    assert all(
        candle.low
        <= min(candle.open, candle.close)
        <= max(candle.open, candle.close)
        <= candle.high
        for candle in first
    )
    assert all(candle.volume >= 0 for candle in first)
    assert validate_soak_candles(first, bar_interval="5m") == tuple(first)

    duplicate = generate_synthetic_candles(
        SyntheticCandleRequest(
            count=100,
            seed=1,
            bar_interval="5m",
            duplicate_at=frozenset({50}),
        )
    )
    with pytest.raises(MarketDataError, match="重复 K 线时间戳"):
        validate_soak_candles(duplicate, bar_interval="5m")

    missing = generate_synthetic_candles(
        SyntheticCandleRequest(
            count=100,
            seed=1,
            bar_interval="5m",
            missing_at=frozenset({50}),
        )
    )
    with pytest.raises(MarketDataError, match="预期周期: 5m"):
        validate_soak_candles(missing, bar_interval="5m")


def test_10000_bar_soak_is_deterministic_idempotent_and_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokerSentinel:
        objects_created = 0
        write_calls = 0

        def __init__(self, *_: object, **__: object) -> None:
            type(self).objects_created += 1
            raise AssertionError("VWAP Shadow soak must not construct a Broker")

        def submit_order(self, *_: object, **__: object) -> None:
            type(self).write_calls += 1
            raise AssertionError("VWAP Shadow soak must not call a Broker")

    import app.execution.backtest_broker as backtest_broker
    import app.execution.demo_broker as demo_broker
    import app.execution.read_only_broker as read_only_broker

    monkeypatch.setattr(backtest_broker, "BacktestBroker", BrokerSentinel)
    monkeypatch.setattr(demo_broker, "OKXDemoBroker", BrokerSentinel)
    monkeypatch.setattr(read_only_broker, "ReadOnlyBroker", BrokerSentinel)

    network_calls = 0

    def reject_network(*_: object, **__: object) -> None:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("VWAP Shadow soak must not access the network")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    before_bytes = _PRODUCTION_DATABASE.read_bytes() if _PRODUCTION_DATABASE.exists() else None
    before_stat = _PRODUCTION_DATABASE.stat() if _PRODUCTION_DATABASE.exists() else None

    config = load_run_config(_CONFIG, environ={})
    source = build_synthetic_soak_source(
        SyntheticCandleRequest(
            count=10_000,
            seed=20260731,
            bar_interval="1h",
        )
    )
    database_path = tmp_path / "soak.db"
    output_dir = tmp_path / "output"
    first = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=1_000,
    )
    first_snapshot = read_soak_snapshot(database_path, str(first["run_id"]))
    repeated = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=1_000,
        resume_run_id=str(first["run_id"]),
    )
    repeated_snapshot = read_soak_snapshot(database_path, str(first["run_id"]))

    assert first["status"] == "completed"
    assert first["bars_received"] == 10_000
    assert first["bars_confirmed"] == 10_000
    assert first["bars_processed"] == 10_000
    assert first["signals_persisted"] == 10_000
    assert first["buy_signals"] == first["proposals_persisted"]
    assert first["checkpoint_count"] == 10
    assert first["submission_performed"] == 0
    assert repeated["bars_processed"] == 10_000
    assert repeated["signals_persisted"] == 10_000
    assert repeated["proposals_persisted"] == first["proposals_persisted"]
    assert repeated["resume_count"] == 1
    assert repeated_snapshot == first_snapshot
    signals = cast(list[dict[str, object]], first_snapshot["signals"])
    proposals = cast(list[dict[str, object]], first_snapshot["proposals"])
    assert len({row["signal_id"] for row in signals}) == 10_000
    assert len({row["proposal_id"] for row in proposals}) == first["proposals_persisted"]
    assert BrokerSentinel.objects_created == 0
    assert BrokerSentinel.write_calls == 0
    assert network_calls == 0
    assert (
        _PRODUCTION_DATABASE.read_bytes() if _PRODUCTION_DATABASE.exists() else None
    ) == before_bytes
    if before_stat is not None:
        after_stat = _PRODUCTION_DATABASE.stat()
        assert after_stat.st_size == before_stat.st_size
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_zero_volume_and_unconfirmed_candles_reset_the_durable_window(
    tmp_path: Path,
) -> None:
    config = load_run_config(_CONFIG, environ={})
    source = build_synthetic_soak_source(
        SyntheticCandleRequest(
            count=100,
            seed=9,
            bar_interval="1h",
            zero_volume_at=frozenset({50}),
            unconfirmed_at=frozenset({75}),
        )
    )
    database_path = tmp_path / "state.db"
    result = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=tmp_path / "output",
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=10,
    )
    snapshot = read_soak_snapshot(database_path, str(result["run_id"]))
    signals = cast(list[dict[str, object]], snapshot["signals"])
    strategy_state = cast(dict[str, object], snapshot["strategy_state"])

    assert "非正成交量" in str(signals[50]["explanation_json"])
    assert "未确认 K 线" in str(signals[75]["explanation_json"])
    assert strategy_state["window_length"] == 24


def test_real_csv_remains_compatible_and_non_executable(tmp_path: Path) -> None:
    config = load_run_config(_CONFIG, environ={})
    source = load_csv_soak_source(_REAL_CSV, bar_interval="1h")
    assert source.identity_hash == _REAL_CSV_HASH
    assert source.parameters["file_sha256"] == hashlib.sha256(_REAL_CSV.read_bytes()).hexdigest()
    result = run_vwap_shadow_soak(
        database_path=tmp_path / "real.db",
        output_dir=tmp_path / "output",
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=24,
    )
    snapshot = read_soak_snapshot(tmp_path / "real.db", str(result["run_id"]))
    signals = cast(list[dict[str, object]], snapshot["signals"])
    proposals = cast(list[dict[str, object]], snapshot["proposals"])

    assert result["data_source_hash"] == _REAL_CSV_HASH
    assert result["bars_received"] == 119
    assert result["bars_confirmed"] == 119
    assert result["buy_signals"] == 1
    assert result["proposals_persisted"] == 1
    buy_signals = [signal for signal in signals if signal["action"] == "buy"]
    assert len(buy_signals) == 1
    assert buy_signals[0]["bar_timestamp"] == "2026-07-22T12:00:00+00:00"
    proposal = proposals[0]
    assert proposal["quantity"] == "0"
    assert proposal["notional"] == "0"
    assert proposal["submission_performed"] == 0
    assert proposal["exchange_order_id"] is None
    assert proposal["capability_status"] == "read_only"
    assert proposal["risk_status"] == "blocked"


def test_soak_modules_and_cli_have_no_execution_surface() -> None:
    paths = (
        Path("app/services/vwap_shadow_soak.py"),
        Path("app/vwap_shadow_soak.py"),
        Path("app/market/synthetic_candles.py"),
    )
    forbidden_imports = {
        "app.exchange",
        "app.execution",
        "app.trading_engine",
        "app.services.bounded_demo",
        "app.services.continuous_demo",
    }
    forbidden_calls = {
        "submit_order",
        "place_order",
        "cancel_order",
        "create_connection",
    }
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
        assert not {
            name
            for name in imported
            if any(name.startswith(prefix) for prefix in forbidden_imports)
        }
        assert not calls.intersection(forbidden_calls)

    help_text = build_parser().format_help()
    for allowed in (
        "--config",
        "--input-csv",
        "--synthetic-bars",
        "--seed",
        "--bar-interval",
        "--checkpoint-every",
        "--stop-after-bars",
        "--resume-run-id",
        "--output-dir",
    ):
        assert allowed in help_text
    for forbidden in (
        "api-key",
        "api-secret",
        "passphrase",
        "broker",
        "confirm-demo",
        "budget",
        "quantity",
        "leverage",
        "account-mode",
    ):
        assert forbidden not in help_text.lower()

    production_store_error = "production database is forbidden"
    with pytest.raises(RuntimeError, match=production_store_error):
        ShadowSoakStore(_PRODUCTION_DATABASE)
