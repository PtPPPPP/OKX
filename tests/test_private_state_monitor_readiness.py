from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.domain.market import Instrument
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.services.private_state_monitor import PrivateStateMonitor
from app.services.reconciliation import ReconciliationResult, ReconciliationStatus


class _Stream:
    def __init__(self, events: tuple[PrivateEvent, ...]) -> None:
        self.events = events
        self.stopped = False

    async def stream_events(self) -> AsyncIterator[PrivateEvent]:
        for event in self.events:
            yield event

    async def stop(self) -> None:
        self.stopped = True


class _Coordinator:
    def __init__(self) -> None:
        self.events: list[PrivateEvent] = []

    def reconcile_private_state(
        self, _instrument: Instrument, *, source: str
    ) -> ReconciliationResult:
        return ReconciliationResult(ReconciliationStatus.HEALTHY, source, 0, 0)

    def handle_private_ws_event(self, event: PrivateEvent) -> bool:
        self.events.append(event)
        return True

    def handle_private_stream_failure(self, _reason: str) -> None:
        raise AssertionError("the deterministic stream must not fail")


def test_monitor_requires_account_and_position_before_private_state_is_ready(
    btc_instrument: Instrument,
) -> None:
    stream = _Stream(
        (
            PrivateEvent(PrivateEventKind.ACCOUNT, "account:initial", {}),
            PrivateEvent(PrivateEventKind.POSITION, "position:initial", {}),
        )
    )
    coordinator = _Coordinator()
    monitor = PrivateStateMonitor(stream, coordinator, reconciliation_interval_seconds=60)

    assert asyncio.run(monitor.run(btc_instrument, max_events=2)) == 2
    assert monitor.account_snapshot_received
    assert monitor.position_snapshot_received
    assert monitor.private_state_received
    assert monitor.wait_until_private_state_received(timeout_seconds=0)
    assert stream.stopped
    assert [event.kind for event in coordinator.events] == [
        PrivateEventKind.ACCOUNT,
        PrivateEventKind.POSITION,
    ]
