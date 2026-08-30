from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.domain.market import Candle
from app.market.historical_data import MarketDataError
from app.market.websocket import PublicWebSocketEvent, PublicWebSocketEventType
from app.reproducibility import InstrumentSnapshotStore
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.vwap_continuous_shadow import (
    ContinuousShadowLifecycle,
    ContinuousShadowSession,
    ContinuousVWAPShadowRunner,
)
from app.storage.database import Database
from scripts.phase_4a_soak import WriteBoundaryCounters, exchange_write_guard


def _candle(timestamp: datetime, price: str = "100") -> Candle:
    value = Decimal(price)
    return Candle(timestamp, value, value + 1, value - 1, value, Decimal("10"), True)


class _ProgrammableHistory:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.error: Exception | None = None
        self.pause = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_historical_bars(
        self, *_: object, limit: int | None = None, **__: object
    ) -> list[Candle]:
        if self.pause:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("paused REST fixture was not released")
        if self.error is not None:
            raise self.error
        return self.candles[-limit:] if limit is not None else self.candles


def _prepared_session(
    tmp_path: Path,
) -> tuple[
    ContinuousVWAPShadowRunner,
    ContinuousShadowSession,
    _ProgrammableHistory,
    list[Candle],
    Database,
]:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    candles = [_candle(start + timedelta(hours=index)) for index in range(34)]
    history = _ProgrammableHistory(candles[:29])
    database = Database(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    runner = ContinuousVWAPShadowRunner(database, config, instrument, history)
    session = runner.start_session()
    runner.handle_ws_connected(session, 1)
    assert runner.handle_live_candle(session, candles[29], generation=1)
    history.candles = candles[:30]
    return runner, session, history, candles, database


def _business_counts(database: Database, run_id: str, candle: Candle) -> tuple[int, int, int]:
    with sqlite3.connect(database.path) as connection:
        processed = connection.execute(
            "SELECT COUNT(*) FROM processed_candles WHERE run_id=? AND candle_open_time=?",
            (run_id, candle.timestamp.isoformat()),
        ).fetchone()[0]
        signals = connection.execute(
            "SELECT COUNT(*) FROM strategy_signal_events WHERE run_id=? AND candle_open_time=?",
            (run_id, candle.timestamp.isoformat()),
        ).fetchone()[0]
        proposals = connection.execute(
            "SELECT COUNT(*) FROM shadow_order_proposals WHERE run_id=? AND signal_id IN "
            "(SELECT signal_id FROM strategy_signal_events WHERE run_id=? AND candle_open_time=?)",
            (run_id, run_id, candle.timestamp.isoformat()),
        ).fetchone()[0]
    return processed, signals, proposals


def _checkpoint(database: Database, run_id: str) -> str:
    with sqlite3.connect(database.path) as connection:
        value = connection.execute(
            "SELECT last_candle_open_time FROM strategy_runtime_states WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    return str(value)


def test_initial_bootstrap_becomes_ready_only_after_initial_subscription(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    history = _ProgrammableHistory([_candle(start + timedelta(hours=index)) for index in range(29)])
    database = Database(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing("m", "d", {"test": True}, "test")
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    runner = ContinuousVWAPShadowRunner(database, config, instrument, history)
    session = runner.start_session()
    with database.connect() as connection:
        bootstrap = connection.execute(
            "SELECT details_json FROM continuous_demo_run_events WHERE run_id=? AND event_type=?",
            (session.run_id, "shadow_smoke_bootstrap_completed"),
        ).fetchone()
    assert json.loads(bootstrap[0]) == {
        "bootstrap_latest_confirmed_timestamp": history.candles[-1].timestamp.isoformat(),
        "confirmed_history_bars": 29,
    }
    assert session.lifecycle_state is ContinuousShadowLifecycle.BOOTSTRAPPING
    assert not session.can_process_live_candle
    runner.handle_ws_connected(session, 1)
    assert session.lifecycle_state.value == ContinuousShadowLifecycle.READY.value
    runner.stop_session(session, persist=False)


def test_disconnect_transitions_to_stale_and_stale_rejects_live_candle(
    tmp_path: Path,
) -> None:
    runner, session, _, candles, database = _prepared_session(tmp_path)
    checkpoint = _checkpoint(database, session.run_id)
    runner.handle_ws_disconnected(session, 1)
    assert session.lifecycle_state is ContinuousShadowLifecycle.STALE
    assert session.disconnect_count == 1
    assert not runner.handle_live_candle(session, candles[30], generation=1)
    assert _business_counts(database, session.run_id, candles[30]) == (0, 0, 0)
    assert _checkpoint(database, session.run_id) == checkpoint
    runner.stop_session(session, persist=False)


def test_reconnect_enters_reconciling_and_no_gap_returns_ready(tmp_path: Path) -> None:
    runner, session, _, candles, database = _prepared_session(tmp_path)
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)
    assert session.lifecycle_state is ContinuousShadowLifecycle.RECONCILING
    assert not runner.handle_live_candle(session, candles[30], generation=2)
    asyncio.run(runner.reconcile_after_reconnect(session))
    assert session.lifecycle_state.value == ContinuousShadowLifecycle.READY.value
    assert _business_counts(database, session.run_id, candles[30]) == (0, 0, 0)
    runner.stop_session(session, persist=False)


@pytest.mark.parametrize("missing_count", (1, 3))
def test_reconnect_backfills_every_missing_bar_in_order(tmp_path: Path, missing_count: int) -> None:
    runner, session, history, candles, database = _prepared_session(tmp_path)
    history.candles = candles[: 30 + missing_count]
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)
    asyncio.run(runner.reconcile_after_reconnect(session))
    assert session.lifecycle_state is ContinuousShadowLifecycle.READY
    assert session.last_committed_checkpoint == candles[29 + missing_count].timestamp
    for candle in candles[30 : 30 + missing_count]:
        assert _business_counts(database, session.run_id, candle)[0:2] == (1, 1)
    runner.stop_session(session, persist=False)


def test_unrecoverable_reconnect_gap_is_blocked(tmp_path: Path) -> None:
    runner, session, history, candles, database = _prepared_session(tmp_path)
    history.candles = [*candles[:30], candles[31]]
    checkpoint = _checkpoint(database, session.run_id)
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)
    with pytest.raises(MarketDataError):
        asyncio.run(runner.reconcile_after_reconnect(session))
    assert session.lifecycle_state is ContinuousShadowLifecycle.BLOCKED
    assert _business_counts(database, session.run_id, candles[31]) == (0, 0, 0)
    assert _checkpoint(database, session.run_id) == checkpoint
    runner.stop_session(session, persist=False)


def test_rest_reconciliation_failure_never_returns_ready(tmp_path: Path) -> None:
    runner, session, history, candles, database = _prepared_session(tmp_path)
    history.error = ConnectionError("fixture REST failure")
    checkpoint = _checkpoint(database, session.run_id)
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)
    with pytest.raises(ConnectionError, match="fixture REST failure"):
        asyncio.run(runner.reconcile_after_reconnect(session))
    assert session.lifecycle_state is ContinuousShadowLifecycle.BLOCKED
    assert not runner.handle_live_candle(session, candles[30], generation=2)
    assert _checkpoint(database, session.run_id) == checkpoint
    runner.stop_session(session, persist=False)


def test_disconnect_during_reconciliation_cannot_return_session_to_ready(
    tmp_path: Path,
) -> None:
    runner, session, history, candles, _ = _prepared_session(tmp_path)
    history.candles = candles[:31]
    history.pause = True
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)

    async def exercise() -> None:
        task = asyncio.create_task(runner.reconcile_after_reconnect(session))
        assert await asyncio.to_thread(history.entered.wait, 2)
        runner.handle_ws_disconnected(session, 2)
        history.release.set()
        await task

    asyncio.run(exercise())
    assert session.lifecycle_state is ContinuousShadowLifecycle.STALE
    runner.stop_session(session, persist=False)


def test_reconnect_overlap_and_delayed_old_generation_are_idempotent(tmp_path: Path) -> None:
    runner, session, history, candles, database = _prepared_session(tmp_path)
    history.candles = candles[:31]
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)
    asyncio.run(runner.reconcile_after_reconnect(session))
    before = _business_counts(database, session.run_id, candles[30])
    assert not runner.handle_live_candle(session, candles[30], generation=2)
    assert not runner.handle_live_candle(session, candles[31], generation=1)
    assert _business_counts(database, session.run_id, candles[30]) == before == (1, 1, 0)
    assert _business_counts(database, session.run_id, candles[31]) == (0, 0, 0)
    runner.stop_session(session, persist=False)


def test_stop_transitions_ready_stale_and_blocked_sessions_to_stopped(
    tmp_path: Path,
) -> None:
    for index, target in enumerate(
        (
            ContinuousShadowLifecycle.READY,
            ContinuousShadowLifecycle.STALE,
            ContinuousShadowLifecycle.BLOCKED,
        )
    ):
        runner, session, _, _, _ = _prepared_session(tmp_path / str(index))
        if target is ContinuousShadowLifecycle.STALE:
            runner.handle_ws_disconnected(session, 1)
        elif target is ContinuousShadowLifecycle.BLOCKED:
            session.lifecycle_state = ContinuousShadowLifecycle.BLOCKED
        runner.stop_session(session, persist=False)
        assert session.lifecycle_state is ContinuousShadowLifecycle.STOPPED
        assert session.stop_requested


def test_reconnected_ws_candle_cannot_enter_strategy_before_rest_reconciliation(
    tmp_path: Path,
) -> None:
    runner, session, history, candles, database = _prepared_session(tmp_path)
    checkpoint = _checkpoint(database, session.run_id)
    history.candles = candles[:32]
    history.pause = True
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)

    async def exercise() -> None:
        task = asyncio.create_task(runner.reconcile_after_reconnect(session))
        assert await asyncio.to_thread(history.entered.wait, 2)
        assert not runner.handle_live_candle(session, candles[31], generation=2)
        assert _business_counts(database, session.run_id, candles[31]) == (0, 0, 0)
        assert _checkpoint(database, session.run_id) == checkpoint
        history.release.set()
        await task

    asyncio.run(exercise())
    assert session.lifecycle_state is ContinuousShadowLifecycle.READY
    assert _business_counts(database, session.run_id, candles[30])[0:2] == (1, 1)
    assert _business_counts(database, session.run_id, candles[31])[0:2] == (1, 1)
    runner.stop_session(session, persist=False)


def test_early_ws_candle_is_not_used_when_rest_reconciliation_fails(
    tmp_path: Path,
) -> None:
    runner, session, history, candles, database = _prepared_session(tmp_path)
    checkpoint = _checkpoint(database, session.run_id)
    history.pause = True
    history.error = ConnectionError("paused REST failure")
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)

    async def exercise() -> None:
        task = asyncio.create_task(runner.reconcile_after_reconnect(session))
        assert await asyncio.to_thread(history.entered.wait, 2)
        assert not runner.handle_live_candle(session, candles[30], generation=2)
        history.release.set()
        with pytest.raises(ConnectionError, match="paused REST failure"):
            await task

    asyncio.run(exercise())
    assert session.lifecycle_state is ContinuousShadowLifecycle.BLOCKED
    assert _business_counts(database, session.run_id, candles[30]) == (0, 0, 0)
    assert _checkpoint(database, session.run_id) == checkpoint
    runner.stop_session(session, persist=False)


def test_local_event_integration_reconciles_before_accepting_new_generation(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    candles = [_candle(start + timedelta(hours=index)) for index in range(32)]
    history = _ProgrammableHistory(candles[:29])
    database = Database(f"sqlite:///{tmp_path / 'integration.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing("m", "d", {"test": True}, "test")
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    runner = ContinuousVWAPShadowRunner(database, config, instrument, history)

    async def events() -> AsyncIterator[PublicWebSocketEvent]:
        yield PublicWebSocketEvent(PublicWebSocketEventType.CONNECTED, 1)
        yield PublicWebSocketEvent(PublicWebSocketEventType.CANDLE, 1, candles[29])
        yield PublicWebSocketEvent(PublicWebSocketEventType.DISCONNECTED, 1)
        history.candles = candles
        yield PublicWebSocketEvent(PublicWebSocketEventType.RECONNECTED, 2)
        yield PublicWebSocketEvent(PublicWebSocketEventType.CANDLE, 2, candles[31])

    write_counters = WriteBoundaryCounters()
    with exchange_write_guard(write_counters):
        result = asyncio.run(runner.run_events(events()))
    assert result.confirmed_bars_processed == 3
    assert runner.session is not None
    assert runner.session.lifecycle_state is ContinuousShadowLifecycle.STOPPED
    assert runner.session.rejected_live_candles == 1
    for candle in candles[29:32]:
        assert _business_counts(database, result.run_id, candle)[0:2] == (1, 1)
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shadow_order_proposals WHERE submission_performed=1"
            ).fetchone()[0]
            == 0
        )
    assert write_counters.total == 0
