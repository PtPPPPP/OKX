from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.continuous_demo import ContinuousDemoConfiguration, ContinuousDemoEngine
from app.services.continuous_runtime_safety import (
    ContinuousTaskSupervisor,
    SupervisedTaskDefinition,
)
from app.services.runtime_observation import read_runtime_status
from scripts.demo_runtime_recovery import local_fixture


@pytest.mark.parametrize("outcome", ["exception", "timeout", "normal_exit", "cancelled"])
def test_supervisor_propagates_child_exit_and_awaits_siblings(outcome: str) -> None:
    async def scenario() -> None:
        ready = asyncio.Event()
        closed = asyncio.Event()
        failures: list[str] = []

        async def sibling() -> None:
            try:
                ready.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def child() -> None:
            await ready.wait()
            if outcome == "exception":
                raise RuntimeError("injected child failure")
            if outcome == "timeout":
                raise TimeoutError("injected timeout")
            if outcome == "cancelled":
                raise asyncio.CancelledError

        async def failure(_run: str, task: str) -> None:
            failures.append(task)

        supervisor = ContinuousTaskSupervisor(None, failure)
        result = await supervisor.run(
            "run",
            [
                SupervisedTaskDefinition("child", True, "never", 0),
                SupervisedTaskDefinition("sibling", True, "never", 0),
            ],
            {"child": child(), "sibling": sibling()},
        )
        assert result.status == "failed" and result.failed_task == "child"
        assert closed.is_set()
        assert failures == ["child"]

    asyncio.run(scenario())


def test_supervisor_parent_cancellation_awaits_workers() -> None:
    async def scenario() -> None:
        started, closed = asyncio.Event(), asyncio.Event()

        async def worker() -> None:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def failure(*_) -> None:
            pytest.fail("normal supervisor cancellation is not a worker failure")

        task = asyncio.create_task(
            ContinuousTaskSupervisor(None, failure).run(
                "run", [SupervisedTaskDefinition("worker", True, "never", 0)], {"worker": worker()}
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    asyncio.run(scenario())


def test_identical_circuit_breaker_fault_is_persisted_once(tmp_path: Path) -> None:
    from app.services.continuous_safety import (
        CircuitBreakerDecision,
        ContinuousDemoCircuitBreakerExecutor,
    )

    database, runner, _, _, _ = local_fixture(tmp_path)
    session = runner.start_session()

    async def scenario() -> None:
        executor = ContinuousDemoCircuitBreakerExecutor(database)
        for _ in range(100):
            await executor.apply(
                session.run_id, CircuitBreakerDecision("freeze", "same_failure", "p1", False)
            )

    asyncio.run(scenario())
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM continuous_demo_run_events WHERE event_type='circuit_breaker_triggered'"
            ).fetchone()[0]
            == 1
        )
    runner.stop_session(session, final_status="frozen")


def test_shadow_cancellation_closes_feed_and_releases_lease(tmp_path: Path) -> None:
    from app.market.websocket import PublicWebSocketEvent, PublicWebSocketEventType

    database, runner, _, _, _ = local_fixture(tmp_path)

    async def scenario() -> None:
        waiting, closed = asyncio.Event(), asyncio.Event()

        async def events():
            try:
                yield PublicWebSocketEvent(PublicWebSocketEventType.CONNECTED, 1)
                waiting.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        task = asyncio.create_task(runner.run_events(events()))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert runner.session.lock_released
        status = read_runtime_status(database.path, runner.session.run_id)
        assert status["lifecycle"] == "interrupted"

    asyncio.run(scenario())


def test_cancelled_reconciliation_joins_writer_before_lease_release(tmp_path: Path) -> None:
    from tests.test_vwap_continuous_shadow_lifecycle import _prepared_session

    runner, session, history, candles, database = _prepared_session(tmp_path)
    history.candles = candles[:31]
    history.pause = True
    runner.handle_ws_disconnected(session, 1)
    runner.handle_ws_reconnected(session, 2)

    async def scenario() -> None:
        task = asyncio.create_task(runner.reconcile_after_reconnect(session))
        assert await asyncio.to_thread(history.entered.wait, 2)
        task.cancel()
        # One deterministic event-loop turn lets cancellation reach shield's
        # handler; no real-time sleep guesses when the I/O thread has stopped.
        barrier = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(barrier.set_result, None)
        await barrier
        assert not task.done()
        assert not session.lock_released
        history.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        runner.stop_session(session, final_status="interrupted", reason="cancelled")
        assert session.lock_released

    asyncio.run(scenario())
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM strategy_signal_events WHERE run_id=?", (session.run_id,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("fault", ["market_stale", "progress_stalled"])
def test_watchdog_stops_waiting_feed_and_persists_fault(tmp_path: Path, fault: str) -> None:
    from dataclasses import replace

    from app.market.historical_data import MarketDataError
    from app.market.websocket import PublicWebSocketEvent, PublicWebSocketEventType

    database, runner, _, candles, clock = local_fixture(tmp_path)
    runner.heartbeat_seconds = 0.001
    closed: list[bool] = []

    async def feed():
        try:
            yield PublicWebSocketEvent(PublicWebSocketEventType.CONNECTED, 1)
            yield PublicWebSocketEvent(PublicWebSocketEventType.CANDLE, 1, candles[29])
            clock.advance(121_000 if fault == "market_stale" else 7_321_000)
            if fault == "progress_stalled":
                yield PublicWebSocketEvent(
                    PublicWebSocketEventType.CANDLE, 1, replace(candles[30], confirmed=False)
                )
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    async def scenario() -> None:
        with pytest.raises(MarketDataError, match=fault):
            await asyncio.wait_for(runner.run_events(feed()), timeout=2)

    asyncio.run(scenario())
    assert closed == [True]
    assert runner.session.lock_released
    status = read_runtime_status(database.path, runner.session.run_id, now=clock.now())
    assert status["health"] == "blocked"
    assert status["stop_reason"] == fault
    assert status["process_heartbeat_fresh"] is True


@pytest.mark.parametrize("failure_at", ["startup", "worker"])
def test_demo_failures_close_resources_even_without_new_market_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    database, _, _, candles, _ = local_fixture(tmp_path)
    events: list[str] = []

    async def scenario() -> None:
        market_waiting = asyncio.Event()

        class Stream:
            state = "connected"

            async def stream_confirmed_candles(self, *_):
                try:
                    market_waiting.set()
                    await asyncio.Event().wait()
                    yield candles[-1]
                finally:
                    events.append("feed.closed")

            async def stop(self) -> None:
                events.append("stream.closed")

        class Session:
            client = SimpleNamespace(get_history_candles=lambda *_: candles[:35])
            stream = SimpleNamespace(
                health=SimpleNamespace(
                    connected=True, authenticated=True, subscriptions_ready=True, stale=False
                )
            )
            order_submission_ready = True

            def start(self) -> None:
                if failure_at == "startup":
                    raise RuntimeError("startup failed")

            def close(self) -> None:
                events.append("session.closed")

        class BaselineRepository:
            def __init__(self, *_):
                pass

            def save_once(self, *_):
                pass

        class Reconciler:
            def __init__(self, *_):
                pass

            async def run_once(self, *_):
                return SimpleNamespace(status="healthy")

            async def start(self, *_):
                await market_waiting.wait()
                raise RuntimeError("worker failed")

        monkeypatch.setattr("app.services.continuous_demo.baseline_from_session", lambda *_: None)
        monkeypatch.setattr(
            "app.services.continuous_demo.ShadowAccountBaselineRepository", BaselineRepository
        )
        monkeypatch.setattr(
            "app.services.continuous_demo.ContinuousDemoReconciliationLoop", Reconciler
        )
        engine = ContinuousDemoEngine(database, Session(), Stream())
        config = ContinuousDemoConfiguration("BTC-USDT", "moving_average_cross", "1h")
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(engine.run(config), timeout=3)
        assert events.count("session.closed") == events.count("stream.closed") == 1
        if failure_at == "worker":
            assert events.count("feed.closed") == 1
        assert not engine._lease_owned
        with database.connect() as connection:
            run_id = connection.execute("SELECT run_id FROM continuous_demo_runs").fetchone()[0]
            assert (
                connection.execute("SELECT released_at FROM continuous_run_locks").fetchone()[0]
                is not None
            )
        assert read_runtime_status(database.path, run_id)["health"] == "blocked"

    asyncio.run(scenario())
