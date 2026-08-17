from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from app.config.run_config import load_run_config
from app.continuous_shadow_cli import _bounded_feed, _BoundedFeedState
from app.market.websocket import (
    ConnectionState,
    OKXPublicWebSocketProvider,
    PublicWebSocketEvent,
    PublicWebSocketEventType,
    WebSocketLike,
)
from app.reproducibility import InstrumentSnapshotStore
from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.shadow_smoke_recovery import ShadowSmokeRecoveryService
from app.services.vwap_continuous_shadow import ContinuousVWAPShadowRunner
from app.storage.database import Database
from tests.test_vwap_continuous_shadow_lifecycle import _candle, _ProgrammableHistory


def _database_with_orphan(tmp_path: Path) -> tuple[Database, str, datetime]:
    database = Database(f"sqlite:///{tmp_path / 'smoke-recovery.db'}")
    database.initialize()
    now = datetime(2026, 8, 10, tzinfo=UTC)
    run_id = "orphan-shadow-smoke"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO continuous_demo_runs
            (run_id,strategy_name,instrument_id,timeframe,status,mode,configuration_hash,started_at,
             reconciliation_status,private_stream_status,public_stream_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "vwap_shadow",
                "BTC-USDT",
                "1h",
                "warming_up",
                "shadow",
                "configuration",
                (now - timedelta(minutes=2)).isoformat(),
                "unknown",
                "unknown",
                "starting",
            ),
        )
        connection.execute(
            """INSERT INTO continuous_run_locks
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "continuous-vwap-shadow",
                run_id,
                socket.gethostname(),
                999_999,
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=2)).isoformat(),
                (now - timedelta(minutes=1)).isoformat(),
                None,
                None,
            ),
        )
    return database, run_id, now


def test_dead_owner_orphan_recovery_preserves_history_and_releases_lock(tmp_path: Path) -> None:
    database, run_id, now = _database_with_orphan(tmp_path)

    result = ShadowSmokeRecoveryService(
        database, now=now, process_exists=lambda _process_id: False
    ).recover(run_id, "external_process_termination_recovered")

    assert result.recovered
    assert result.final_status == "interrupted"
    assert result.recovery_state == "INTERRUPTED_RECOVERED"
    with database.connect() as connection:
        run = connection.execute(
            "SELECT status,stop_reason FROM continuous_demo_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        lock = connection.execute(
            "SELECT released_at,release_reason FROM continuous_run_locks WHERE run_id=?", (run_id,)
        ).fetchone()
        recovery = connection.execute(
            "SELECT status,closure_type FROM continuous_run_recoveries WHERE run_id=?", (run_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT event_type FROM continuous_demo_run_events WHERE run_id=?", (run_id,)
        ).fetchall()
    assert tuple(run) == ("interrupted", "external_process_termination_recovered")
    assert lock[0] is not None and lock[1] == "stale_owner_absent"
    assert tuple(recovery) == ("interrupted_recovered", "shadow_smoke_stale_recovery")
    assert [row[0] for row in events] == ["shadow_smoke_interrupted_recovered"]


def test_live_owner_blocks_recovery_even_when_lease_is_expired(tmp_path: Path) -> None:
    database, run_id, now = _database_with_orphan(tmp_path)

    result = ShadowSmokeRecoveryService(
        database, now=now, process_exists=lambda _process_id: True
    ).recover(run_id, "external_process_termination_recovered")

    assert not result.recovered
    assert result.recovery_state == "ACTIVE_RUN_LOCKED"
    assert result.blockers == ("owner_process_exists",)
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM continuous_demo_runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == "warming_up"
        )
        assert (
            connection.execute(
                "SELECT released_at FROM continuous_run_locks WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            is None
        )


def test_shadow_smoke_transport_evidence_is_persisted(tmp_path: Path) -> None:
    database, run_id, _ = _database_with_orphan(tmp_path)
    details = {
        "public_ws_connections": 1,
        "subscriptions": 1,
        "live_events_received": 0,
        "unsubscriptions": 1,
        "closed_cleanly": True,
    }
    ContinuousShadowRepository(database).record_run_event(
        run_id, "shadow_smoke_observation_completed", details
    )
    with database.connect() as connection:
        event = connection.execute(
            "SELECT details_json FROM continuous_demo_run_events WHERE run_id=? AND event_type=?",
            (run_id, "shadow_smoke_observation_completed"),
        ).fetchone()
    assert json.loads(event[0]) == details


def test_windows_missing_process_id_is_not_treated_as_a_live_lock_owner() -> None:
    error = OSError(22, "invalid parameter", None, 87)
    with patch("app.services.shadow_smoke_recovery.os.kill", side_effect=error):
        assert not ShadowSmokeRecoveryService._process_exists(999_999)


def test_interrupted_runner_finalizes_run_and_releases_lock_without_pending_tasks(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        start = datetime(2026, 8, 1, tzinfo=UTC)
        candles = [_candle(start + timedelta(hours=index)) for index in range(29)]
        history = _ProgrammableHistory(candles)
        database = Database(f"sqlite:///{tmp_path / 'cancelled-run.db'}")
        database.initialize()
        generation = RuntimeGenerationService(database, start)
        generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
        generation.activate(generation_id)
        config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
        assert config.data.instrument_snapshot is not None
        instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
        runner = ContinuousVWAPShadowRunner(database, config, instrument, history)
        entered_wait = asyncio.Event()

        async def events() -> AsyncIterator[PublicWebSocketEvent]:
            yield PublicWebSocketEvent(PublicWebSocketEventType.CONNECTED, 1)
            entered_wait.set()
            await asyncio.Future()

        task = asyncio.create_task(runner.run_events(events()))
        await entered_wait.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.session is not None
        run_id = runner.session.run_id
        with database.connect() as connection:
            run = connection.execute(
                "SELECT status,stop_reason FROM continuous_demo_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            lock = connection.execute(
                "SELECT released_at FROM continuous_run_locks WHERE run_id=?",
                (run_id,),
            ).fetchone()
        assert tuple(run) == ("interrupted", "continuous_vwap_shadow_task_cancelled")
        assert lock[0] is not None
        assert all(item is asyncio.current_task() or item.done() for item in asyncio.all_tasks())

    asyncio.run(exercise())


def test_websocket_shutdown_timeout_is_bounded_and_leaves_no_active_socket() -> None:
    class _HangingSocket(WebSocketLike):
        async def send(self, _message: str) -> None:
            await asyncio.Future()

        async def recv(self) -> str | bytes:
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            await asyncio.Future()

    async def stop() -> None:
        provider = OKXPublicWebSocketProvider(shutdown_timeout_seconds=0)
        provider._active = _HangingSocket()
        provider._active_subscription = ("candle1H", "BTC-USDT")
        with pytest.raises(Exception, match="WebSocket unsubscribe timed out"):
            await provider.stop()
        assert provider.state is ConnectionState.STOPPED
        assert provider._active is None
        assert provider._active_subscription is None

    asyncio.run(stop())


def test_bounded_feed_deadline_is_a_normal_closed_event_and_stops_provider() -> None:
    class _WaitingProvider:
        connection_count = 1

        def __init__(self) -> None:
            self.stopped = False

        async def stream_events(
            self, _instrument: str, _bar: str
        ) -> AsyncIterator[PublicWebSocketEvent]:
            if False:
                yield PublicWebSocketEvent(PublicWebSocketEventType.CLOSED, 1)
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def stop(self) -> None:
            self.stopped = True

    async def exercise() -> None:
        provider = _WaitingProvider()
        state = _BoundedFeedState()
        events = [
            event
            async for event in _bounded_feed(cast(OKXPublicWebSocketProvider, provider), 0, state)
        ]
        assert [event.event_type for event in events] == [PublicWebSocketEventType.CLOSED]
        assert provider.stopped
        assert state.runtime_deadline_reached

    asyncio.run(exercise())


def test_zero_confirmed_events_finish_normally_and_release_lock(tmp_path: Path) -> None:
    async def exercise() -> None:
        start = datetime(2026, 8, 1, tzinfo=UTC)
        database = Database(f"sqlite:///{tmp_path / 'no-event-run.db'}")
        database.initialize()
        generation = RuntimeGenerationService(database, start)
        generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
        generation.activate(generation_id)
        config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
        assert config.data.instrument_snapshot is not None
        instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
        runner = ContinuousVWAPShadowRunner(
            database,
            config,
            instrument,
            _ProgrammableHistory([_candle(start + timedelta(hours=index)) for index in range(29)]),
        )

        async def events() -> AsyncIterator[PublicWebSocketEvent]:
            yield PublicWebSocketEvent(PublicWebSocketEventType.CONNECTED, 1)
            yield PublicWebSocketEvent(PublicWebSocketEventType.CLOSED, 1)

        result = await runner.run_events(events())
        assert result.confirmed_bars_processed == 0
        assert runner.session is not None
        with database.connect() as connection:
            run = connection.execute(
                "SELECT status,stop_reason FROM continuous_demo_runs WHERE run_id=?",
                (runner.session.run_id,),
            ).fetchone()
            lock = connection.execute(
                "SELECT released_at FROM continuous_run_locks WHERE run_id=?",
                (runner.session.run_id,),
            ).fetchone()
        assert tuple(run) == ("stopped", "bounded_observation_complete")
        assert lock[0] is not None

    asyncio.run(exercise())


def test_recovery_blocks_private_or_order_activity(tmp_path: Path) -> None:
    database, run_id, now = _database_with_orphan(tmp_path)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            """INSERT INTO orders
            (client_order_id,instrument_id,side,order_type,quantity,price,signal_id,state,
             filled_quantity,created_at,updated_at,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "order",
                "BTC-USDT",
                "buy",
                "limit",
                "0",
                "0",
                "signal",
                "created",
                "0",
                now.isoformat(),
                now.isoformat(),
                run_id,
            ),
        )
    result = ShadowSmokeRecoveryService(
        database, now=now, process_exists=lambda _process_id: False
    ).recover(run_id, "external_process_termination_recovered")
    assert not result.recovered
    assert result.blockers == ("local_order_present",)
