from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import app
from app.market.historical_data import MarketDataError
from app.services.runtime_observation import read_runtime_status
from app.services.vwap_continuous_shadow import ContinuousVWAPShadowRunner
from scripts.demo_runtime_recovery import demonstrate, local_fixture, semantic_snapshot


def _ready(tmp_path: Path):
    database, runner, history, candles, clock = local_fixture(tmp_path)
    session = runner.start_session()
    runner.handle_ws_connected(session, 1)
    runner.handle_live_candle(session, candles[29], generation=1)
    return database, runner, session, history, candles, clock


@pytest.mark.parametrize("fault", ["market_stale", "progress_stalled"])
def test_heartbeat_is_not_market_or_business_health(tmp_path: Path, fault: str) -> None:
    database, runner, session, _, candles, clock = _ready(tmp_path)
    clock.advance(121_000 if fault == "market_stale" else 7_321_000)
    if fault == "progress_stalled":
        runner.handle_live_candle(session, replace(candles[30], confirmed=False), generation=1)
    runner.observer.persist()
    status = read_runtime_status(database.path, session.run_id, now=clock.now())
    assert status["process_heartbeat_fresh"] is True
    assert fault in status["blocking_reasons"]
    with pytest.raises(MarketDataError, match=fault):
        runner.pulse(session)
    assert (
        read_runtime_status(database.path, session.run_id, now=clock.now())["health"] == "blocked"
    )
    runner.stop_session(session, final_status="failed", reason=fault)


def test_duplicate_old_generation_and_gap_reconnect_are_audited(tmp_path: Path) -> None:
    import asyncio

    database, runner, session, history, candles, clock = _ready(tmp_path)
    before = semantic_snapshot(database, session.run_id)
    assert not runner.handle_live_candle(session, candles[29], generation=1)
    assert not runner.handle_live_candle(session, candles[28], generation=1)
    assert before == semantic_snapshot(database, session.run_id)
    runner.handle_ws_disconnected(session, 1)
    assert (
        read_runtime_status(database.path, session.run_id, now=clock.now())["health"] == "blocked"
    )
    history.candles = candles[:33]
    runner.handle_ws_reconnected(session, 2)
    asyncio.run(runner.reconcile_after_reconnect(session))
    assert session.processed == 4
    assert not runner.handle_live_candle(session, candles[33], generation=1)
    status = read_runtime_status(database.path, session.run_id, now=clock.now())
    assert status["observation"]["public_reconnects"] == 1
    alert = next(a for a in status["alerts"] if a["code"] == "public_disconnected")
    assert alert["active"] is False and alert["recoveries"] == 1
    runner.stop_session(session)


@pytest.mark.parametrize("corruption", ["hash", "version", "missing_signal", "history", "revision"])
def test_restore_rejects_inconsistent_evidence_without_business_writes(
    tmp_path: Path, corruption: str
) -> None:
    import hashlib

    database, runner, session, history, candles, clock = _ready(tmp_path)
    history.candles = candles[:30]
    runner.stop_session(session)
    with database.connect() as connection:
        if corruption == "hash":
            connection.execute("UPDATE strategy_runtime_states SET state_hash='wrong'")
        elif corruption == "version":
            connection.execute(
                "UPDATE strategy_runtime_states SET strategy_version='unknown_version'"
            )
        elif corruption == "missing_signal":
            connection.execute("DELETE FROM strategy_signal_events")
        elif corruption == "revision":
            state = json.loads(
                connection.execute("SELECT state_json FROM strategy_runtime_states").fetchone()[0]
            )
            state["revision"] = 900
            payload = json.dumps(state, sort_keys=True)
            connection.execute(
                "UPDATE strategy_runtime_states SET state_json=?,state_hash=?",
                (payload, hashlib.sha256(payload.encode()).hexdigest()),
            )
    if corruption == "history":
        history.candles = [*candles[:29], replace(candles[29], volume=candles[29].volume + 1)]
    before = semantic_snapshot(database, session.run_id)
    resumed = ContinuousVWAPShadowRunner(
        database, runner.config, runner.instrument, history, observation_clock=clock
    )
    with pytest.raises((RuntimeError, ValueError, MarketDataError)):
        resumed.start_session(resume_run_id=session.run_id)
    assert semantic_snapshot(database, session.run_id) == before


def test_same_run_cannot_steal_live_lease(tmp_path: Path) -> None:
    database, runner, session, history, _, _ = _ready(tmp_path)
    another = ContinuousVWAPShadowRunner(database, runner.config, runner.instrument, history)
    with pytest.raises(RuntimeError, match="lock is held"):
        another.start_session(resume_run_id=session.run_id)
    runner.stop_session(session)


def test_unknown_order_requires_query_reconciliation_and_blocks_shadow_restore(
    tmp_path: Path,
) -> None:
    from decimal import Decimal

    from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType
    from app.storage.repositories import TradingRepository

    database, runner, session, history, _, clock = _ready(tmp_path)
    runner.stop_session(session)
    request = OrderRequest(
        "unknown-original",
        "BTC-USDT",
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("1"),
        Decimal("100"),
        "test",
        clock.now(),
        run_id=session.run_id,
        mode="demo",
    )
    TradingRepository(database).save_order(Order(request, state=OrderState.UNKNOWN))
    status = read_runtime_status(database.path, session.run_id, now=clock.now())
    assert status["health"] == "blocked"
    assert "unknown_order" in status["blocking_reasons"]
    assert any("never_resubmit" in action for action in status["recovery_actions"])
    another = ContinuousVWAPShadowRunner(database, runner.config, runner.instrument, history)
    with pytest.raises(ValueError, match="order or fill"):
        another.start_session(resume_run_id=session.run_id)
    assert TradingRepository(database).load_order("unknown-original").state is OrderState.UNKNOWN


def test_alert_storm_is_coalesced_and_recovery_recorded_once(tmp_path: Path) -> None:
    database, runner, session, _, _, clock = _ready(tmp_path)
    for _ in range(300):
        runner.observer.alert("public_disconnected")
    runner.observer.alert("public_disconnected", active=False)
    runner.observer.alert("public_disconnected", active=False)
    status = read_runtime_status(database.path, session.run_id, now=clock.now())
    assert len(status["alerts"]) == 1
    assert status["alerts"][0]["occurrences"] == 300
    assert status["alerts"][0]["recoveries"] == 1
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM continuous_demo_run_events WHERE event_type='runtime_recovered_v1'"
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError, match="unsupported"):
        runner.observer.alert("arbitrary_unbounded_category")
    runner.stop_session(session)


def test_runtime_cli_is_readonly_and_legacy_state_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runner, session, _, _, _ = _ready(tmp_path)
    runner.stop_session(session)
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM continuous_demo_run_events WHERE event_type='runtime_observation_v1'"
        )
    before = database.path.read_bytes()
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setattr(
        "app.cli.Settings", lambda **_: pytest.fail("state query must not load credentials")
    )
    result = CliRunner().invoke(
        app, ["runtime-status", "--database", str(database.path), "--run-id", session.run_id]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["health"] == "unknown"
    assert database.path.read_bytes() == before
    missing = tmp_path / "missing.db"
    result = CliRunner().invoke(
        app, ["runtime-status", "--database", str(missing), "--run-id", "none"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["health"] == "unknown"
    assert not missing.exists()


@pytest.mark.parametrize("version", [True, "1", 2])
def test_unknown_observation_format_is_not_reinterpreted_as_healthy(
    tmp_path: Path, version: object
) -> None:
    database, runner, session, _, _, _ = _ready(tmp_path)
    runner.stop_session(session)
    with database.connect() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT details_json FROM continuous_demo_run_events WHERE event_type='runtime_observation_v1'"
            ).fetchone()[0]
        )
        payload["format_version"] = version
        connection.execute(
            "UPDATE continuous_demo_run_events SET details_json=? WHERE event_type='runtime_observation_v1'",
            (json.dumps(payload),),
        )
    result = CliRunner().invoke(
        app, ["runtime-status", "--database", str(database.path), "--run-id", session.run_id]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["health"] == "unknown"


def test_offline_fault_recovery_matches_uninterrupted_results(tmp_path: Path) -> None:
    report = demonstrate(tmp_path / "demo")
    assert report["normal"]["health"] == "healthy"
    assert report["fault"]["health"] == "blocked"
    assert report["recovered"]["health"] == "healthy"
    assert report["business_results_equal_uninterrupted"]
    assert report["processed_candles"] == 31
    assert report["broker_write_calls"] == report["network_calls"] == 0


@pytest.mark.parametrize("cause", ["history_empty", "history_timeout"])
def test_bootstrap_failure_persists_reason_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    database, runner, history, _, clock = local_fixture(tmp_path)
    if cause == "history_empty":
        history.candles = []
    else:

        def failure(*_, **__):
            raise TimeoutError("local history timeout")

        monkeypatch.setattr(history, "get_historical_bars", failure)
    with pytest.raises((MarketDataError, TimeoutError)):
        runner.start_session()
    with database.connect() as connection:
        run_id = connection.execute("SELECT run_id FROM continuous_demo_runs").fetchone()[0]
        assert connection.execute("SELECT released_at FROM continuous_run_locks").fetchone()[0]
    status = read_runtime_status(database.path, run_id, now=clock.now())
    assert status["health"] == "blocked"
    assert status["stop_reason"].startswith("startup_failed:")
    with pytest.raises(RuntimeError, match="new instance"):
        runner.start_session()
