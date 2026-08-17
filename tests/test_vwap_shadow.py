from __future__ import annotations

import ast
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from app.config.run_config import RunConfig, load_run_config
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.signal import SignalAction
from app.market.historical_data import MarketDataError, save_candles_csv
from app.runtime.clock import BacktestClock
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.shadow_replay import _load_shadow_candles, run_shadow_replay
from app.storage.database import Database
from app.strategies.registry import STRATEGY_REGISTRY, create_strategy
from app.strategies.vwap_shadow import (
    VWAPShadowParameters,
    VWAPShadowStrategy,
    rolling_vwap,
)
from tests.conftest import make_candles

_EXPECTED_DATA_HASH = "d21d9d6cf43ab88cf29efb9ead08363bfdf5c0ba5eb2626102e275f3f2442253"
_EXPECTED_BUY_TIME = "2026-07-22T12:00:00+00:00"


def _context(instrument: Instrument, candle: Candle, *, bar: str = "5m") -> StrategyContext:
    interval = timedelta(minutes=5) if bar == "5m" else timedelta(hours=1)
    clock = BacktestClock(candle.timestamp + interval)
    return StrategyContext(
        run_id="vwap-shadow-test",
        mode=TradingMode.DEMO,
        strategy_name="vwap_shadow",
        instrument=instrument,
        bar=bar,
        portfolio_snapshot=PortfolioSnapshot({}, {}, {}, trusted_for_trading=False),
        market_snapshot=MarketSnapshot(candle, candle.close),
        clock=clock,
    )


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'vwap-shadow.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, datetime(2026, 1, 1, tzinfo=UTC))
    generation_id = generation.create_preparing(
        "manifest",
        "database",
        {"test": True},
        "pure VWAP shadow test",
    )
    generation.activate(generation_id)
    return database


def _config(
    *,
    bar: str = "1h",
    window: int = 24,
    deviation_bps: str = "100",
) -> RunConfig:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    return config.model_copy(
        update={
            "market": config.market.model_copy(update={"bar": bar}),
            "strategy": config.strategy.model_copy(
                update={
                    "parameters": {
                        "vwap_window": window,
                        "buy_deviation_bps": deviation_bps,
                    }
                }
            ),
        }
    )


def test_vwap_formula_uses_typical_price_and_volume() -> None:
    candles = make_candles(["10", "20"], interval_minutes=5)
    candles[0] = replace(candles[0], volume=Decimal("1"))
    candles[1] = replace(candles[1], volume=Decimal("3"))
    assert rolling_vwap(candles, 2) == Decimal("17.5")


def test_window_shortfall_and_threshold_hold_then_buy(
    btc_instrument: Instrument,
) -> None:
    strategy = VWAPShadowStrategy(
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("100"))
    )
    candles = make_candles(["100", "100", "97"], interval_minutes=5)
    strategy.on_start(_context(btc_instrument, candles[0]))
    signals = [strategy.on_bar(_context(btc_instrument, candle), candle)[0] for candle in candles]
    assert [signal.action for signal in signals] == [
        SignalAction.HOLD,
        SignalAction.HOLD,
        SignalAction.BUY,
    ]
    assert signals[-1].metadata["close"] == Decimal("97")
    assert signals[-1].metadata["vwap"] == Decimal("98.5")
    assert signals[-1].metadata["deviation_bps"] > Decimal("100")


def test_unconfirmed_candle_does_not_enter_window(
    btc_instrument: Instrument,
) -> None:
    strategy = VWAPShadowStrategy(
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("100"))
    )
    candles = make_candles(["100", "99", "97"], interval_minutes=5)
    candles[1] = replace(candles[1], confirmed=False)
    strategy.on_start(_context(btc_instrument, candles[0]))
    signals = [strategy.on_bar(_context(btc_instrument, candle), candle)[0] for candle in candles]
    assert all(signal.action is SignalAction.HOLD for signal in signals)
    assert signals[1].metadata["window_length"] == 0
    assert signals[2].metadata["window_length"] == 1


def test_zero_volume_resets_window_and_zero_total_vwap_is_none(
    btc_instrument: Instrument,
) -> None:
    strategy = VWAPShadowStrategy(
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("100"))
    )
    candles = make_candles(["100", "99", "97"], interval_minutes=5)
    candles[1] = replace(candles[1], volume=Decimal("0"))
    strategy.on_start(_context(btc_instrument, candles[0]))
    signals = [strategy.on_bar(_context(btc_instrument, candle), candle)[0] for candle in candles]
    assert all(signal.action is SignalAction.HOLD for signal in signals)
    assert signals[1].metadata["window_length"] == 0
    zero_volume = [replace(candle, volume=Decimal("0")) for candle in candles[:2]]
    assert rolling_vwap(zero_volume, 2) is None


def test_duplicate_timestamp_is_rejected(tmp_path: Path) -> None:
    candles = make_candles(["100", "101"], interval_minutes=5)
    path = tmp_path / "duplicate.csv"
    save_candles_csv([candles[0], candles[0], candles[1]], path)
    with pytest.raises(MarketDataError, match="重复"):
        _load_shadow_candles(path, bar="5m")


def test_missing_candle_reports_period_and_gap(tmp_path: Path) -> None:
    candles = make_candles(["100", "101"], interval_minutes=5)
    candles[1] = replace(
        candles[1],
        timestamp=candles[1].timestamp + timedelta(minutes=5),
    )
    path = tmp_path / "missing.csv"
    save_candles_csv(candles, path)
    with pytest.raises(MarketDataError) as error:
        _load_shadow_candles(path, bar="5m")
    assert "预期周期: 5m" in str(error.value)
    assert "2026-01-01" in str(error.value)


def test_cross_utc_date_keeps_rolling_window(btc_instrument: Instrument) -> None:
    start = datetime(2026, 1, 1, 23, 55, tzinfo=UTC)
    candles = [
        Candle(
            start,
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            Decimal("10"),
            True,
        ),
        Candle(
            start + timedelta(minutes=5),
            Decimal("97"),
            Decimal("98"),
            Decimal("96"),
            Decimal("97"),
            Decimal("10"),
            True,
        ),
    ]
    strategy = VWAPShadowStrategy(
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("100"))
    )
    strategy.on_start(_context(btc_instrument, candles[0]))
    first = strategy.on_bar(_context(btc_instrument, candles[0]), candles[0])[0]
    second = strategy.on_bar(_context(btc_instrument, candles[1]), candles[1])[0]
    assert first.action is SignalAction.HOLD
    assert second.action is SignalAction.BUY
    assert second.metadata["window_length"] == 2


def test_non_hour_replay_uses_configured_close_interval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "five-minute.csv"
    candles = make_candles(["100", "100", "97"], interval_minutes=5)
    save_candles_csv(candles, path)
    database = _database(tmp_path)
    result = run_shadow_replay(
        database,
        _config(bar="5m", window=2),
        path,
        maximum=3,
    )
    with database.connect() as connection:
        close_time = connection.execute(
            """SELECT candle_close_time FROM processed_candles
            WHERE run_id=? ORDER BY candle_open_time LIMIT 1""",
            (result["run_id"],),
        ).fetchone()[0]
    assert datetime.fromisoformat(close_time) - candles[0].timestamp == timedelta(minutes=5)


def test_registry_and_config_contain_only_pure_vwap_parameters(
    btc_instrument: Instrument,
) -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert "vwap_shadow" in STRATEGY_REGISTRY
    assert set(config.strategy.parameters) == {"vwap_window", "buy_deviation_bps"}
    assert config.data.source == "csv"
    assert isinstance(
        create_strategy("vwap_shadow", config.strategy.parameters, btc_instrument),
        VWAPShadowStrategy,
    )


def test_shadow_replay_has_static_broker_and_exchange_isolation() -> None:
    path = Path("app/services/shadow_replay.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    strategy_source = Path("app/strategies/vwap_shadow.py").read_text(encoding="utf-8").lower()
    forbidden_imports = {
        "app.exchange",
        "app.execution",
        "app.trading_engine",
        "backtest",
    }
    imported: set[str] = set()
    forbidden_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"submit_order", "place_order", "cancel_order"}
        ):
            forbidden_calls.add(node.func.attr)
    assert not {
        name for name in imported if any(name.startswith(prefix) for prefix in forbidden_imports)
    }
    assert not forbidden_calls
    assert "rsi" not in strategy_source
    assert "atr" not in strategy_source
    assert "macd" not in strategy_source


def test_real_csv_is_deterministic_unsized_and_broker_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokerSentinel:
        objects_created = 0
        write_calls = 0

        def __init__(self, *_: object, **__: object) -> None:
            type(self).objects_created += 1
            raise AssertionError("Shadow replay must not construct a Broker")

        def submit_order(self, *_: object, **__: object) -> None:
            type(self).write_calls += 1
            raise AssertionError("Shadow replay must not call a Broker")

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
        raise AssertionError("Shadow replay must not access the network")

    monkeypatch.setattr(socket, "create_connection", reject_network)

    production_database = Path("data/trading.db")
    before = production_database.read_bytes() if production_database.exists() else None
    database = _database(tmp_path)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    result = run_shadow_replay(
        database,
        config,
        Path("tests/fixtures/vwap/btc_usdt_1h_live.csv"),
        maximum=119,
    )
    after = production_database.read_bytes() if production_database.exists() else None

    assert result["data_hash"] == _EXPECTED_DATA_HASH
    assert result["confirmed_candles"] == 119
    assert result["entry_signals"] == 1
    assert result["buy_signal_times"] == [_EXPECTED_BUY_TIME]
    assert result["shadow_proposals"] == 1
    assert BrokerSentinel.objects_created == 0
    assert BrokerSentinel.write_calls == 0
    assert network_calls == 0
    assert before == after

    with database.connect() as connection:
        proposal = connection.execute(
            """SELECT signal_id,reference_price,planned_price,quantity,notional,
                      blockers_json,submission_performed,exchange_order_id,
                      capability_status,risk_status
            FROM shadow_order_proposals WHERE run_id=?""",
            (result["run_id"],),
        ).fetchone()
        persisted_signal = connection.execute(
            "SELECT signal_id,signal_value FROM strategy_signal_events WHERE signal_id=?",
            (proposal["signal_id"],),
        ).fetchone()

    buy_signal_ids = cast(list[str], result["buy_signal_ids"])
    assert proposal["signal_id"] == buy_signal_ids[0]
    assert persisted_signal["signal_id"] == proposal["signal_id"]
    explanation = json.loads(persisted_signal["signal_value"])
    assert set(explanation) == {
        "close",
        "deviation_bps",
        "reason",
        "vwap",
        "vwap_window",
        "window_length",
    }
    assert explanation["close"] == proposal["reference_price"]
    assert explanation["reason"] == "收盘价低于 VWAP 买入偏离阈值"
    assert explanation["vwap_window"] == 24
    assert explanation["window_length"] == 24
    assert Decimal(explanation["deviation_bps"]) >= Decimal("100")
    assert Decimal(explanation["vwap"]) > Decimal(explanation["close"])
    assert proposal["reference_price"] == proposal["planned_price"]
    assert proposal["quantity"] == "0"
    assert Decimal(proposal["notional"]) == 0
    assert proposal["blockers_json"] == '["shadow_only", "not_sized"]'
    assert proposal["submission_performed"] == 0
    assert proposal["exchange_order_id"] is None
    assert proposal["capability_status"] == "read_only"
    assert proposal["risk_status"] == "blocked"
