from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from threading import Event

from app.domain.market import Instrument
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.services.private_state_monitor import PrivateStateMonitor
from app.services.reconciliation import ReconciliationResult, ReconciliationStatus


class ReconciliationTriggeredEventStream:
    def __init__(self, reconciliation_completed: Event) -> None:
        self.stopped = False
        self.reconciliation_completed = reconciliation_completed

    async def stream_events(self) -> AsyncIterator[PrivateEvent]:
        assert await asyncio.to_thread(self.reconciliation_completed.wait, 1)
        yield PrivateEvent(PrivateEventKind.ACCOUNT, "account:key", {"uTime": "1"})

    async def stop(self) -> None:
        self.stopped = True


class HealthyCoordinator:
    def __init__(self) -> None:
        self.event_count = 0
        self.reconciliation_count = 0
        self.completed = Event()
        self.stream_failures: list[str] = []

    def handle_private_ws_event(self, event: PrivateEvent) -> bool:
        self.event_count += 1
        return True

    def reconcile_private_state(
        self, instrument: Instrument, *, source: str
    ) -> ReconciliationResult:
        self.reconciliation_count += 1
        self.completed.set()
        return ReconciliationResult(
            ReconciliationStatus.HEALTHY,
            "ok",
            remote_order_count=0,
            recovered_order_count=0,
        )

    def handle_private_stream_failure(self, reason: str) -> None:
        self.stream_failures.append(reason)


def test_private_monitor_runs_rest_reconciliation_periodically(
    btc_instrument: Instrument,
) -> None:
    coordinator = HealthyCoordinator()
    stream = ReconciliationTriggeredEventStream(coordinator.completed)
    monitor = PrivateStateMonitor(
        stream,
        coordinator,
        reconciliation_interval_seconds=0.005,
    )
    processed = asyncio.run(monitor.run(btc_instrument, max_events=1))
    assert processed == 1
    assert coordinator.event_count == 1
    assert coordinator.reconciliation_count >= 1
    assert stream.stopped


def test_private_event_callback_runs_off_the_event_loop(
    btc_instrument: Instrument,
) -> None:
    coordinator = HealthyCoordinator()
    callback_threads: list[int] = []
    loop_thread = threading.get_ident()

    def record_thread(event: PrivateEvent) -> bool:
        callback_threads.append(threading.get_ident())
        return HealthyCoordinator.handle_private_ws_event(coordinator, event)

    coordinator.handle_private_ws_event = record_thread  # type: ignore[method-assign]
    stream = ReconciliationTriggeredEventStream(coordinator.completed)
    monitor = PrivateStateMonitor(stream, coordinator, reconciliation_interval_seconds=0.005)

    assert asyncio.run(monitor.run(btc_instrument, max_events=1)) == 1
    assert callback_threads and callback_threads[0] != loop_thread
