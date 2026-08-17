from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Protocol

from app.domain.market import Instrument
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.services.reconciliation import ReconciliationResult


class PrivateEventStream(Protocol):
    def stream_events(self) -> AsyncIterator[PrivateEvent]: ...

    async def stop(self) -> None: ...


class PrivateStateCoordinatorProtocol(Protocol):
    def reconcile_private_state(
        self, instrument: Instrument, *, source: str
    ) -> ReconciliationResult: ...

    def handle_private_ws_event(self, event: PrivateEvent) -> bool: ...

    def handle_private_stream_failure(self, reason: str) -> None: ...


class PrivateStateMonitor:
    """Applies private pushes while REST reconciliation remains authoritative."""

    def __init__(
        self,
        stream: PrivateEventStream,
        coordinator: PrivateStateCoordinatorProtocol,
        *,
        reconciliation_interval_seconds: float = 30,
    ) -> None:
        if reconciliation_interval_seconds <= 0:
            raise ValueError("对账周期必须大于 0")
        self.stream = stream
        self.coordinator = coordinator
        self.interval = reconciliation_interval_seconds
        self._account_snapshot_received = threading.Event()
        self._position_snapshot_received = threading.Event()
        self._private_state_received = threading.Event()

    @property
    def account_snapshot_received(self) -> bool:
        return self._account_snapshot_received.is_set()

    @property
    def position_snapshot_received(self) -> bool:
        return self._position_snapshot_received.is_set()

    @property
    def private_state_received(self) -> bool:
        return self._private_state_received.is_set()

    def wait_until_private_state_received(self, timeout_seconds: float) -> bool:
        """Wait for the initial account and position snapshots without polling."""
        return self._private_state_received.wait(timeout_seconds)

    def _record_private_state_event(self, event: PrivateEvent) -> None:
        if event.kind is PrivateEventKind.ACCOUNT:
            self._account_snapshot_received.set()
        elif event.kind is PrivateEventKind.POSITION:
            self._position_snapshot_received.set()
        if self.account_snapshot_received and self.position_snapshot_received:
            self._private_state_received.set()

    async def run(self, instrument: Instrument, *, max_events: int | None = None) -> int:
        reconciliation_error: list[str] = []

        async def reconcile_periodically() -> None:
            while True:
                await asyncio.sleep(self.interval)
                result = await asyncio.to_thread(
                    self.coordinator.reconcile_private_state,
                    instrument,
                    source="private_state_monitor",
                )
                if not result.order_submission_allowed:
                    reconciliation_error.append(result.message)
                    await self.stream.stop()
                    return

        task = asyncio.create_task(reconcile_periodically())
        processed = 0
        try:
            async for event in self.stream.stream_events():
                if not isinstance(event, PrivateEvent):
                    raise TypeError("私有事件流返回了无效事件")
                if event.kind is PrivateEventKind.CONNECTION:
                    accepted = await asyncio.to_thread(
                        self.coordinator.handle_private_ws_event,
                        event,
                    )
                    if not accepted:
                        raise RuntimeError("私有 WebSocket 重连 epoch 未被接受")
                    result = await asyncio.to_thread(
                        self.coordinator.reconcile_private_state,
                        instrument,
                        source="private_stream_reconnected",
                    )
                    if not result.order_submission_allowed:
                        raise RuntimeError(f"私有 WebSocket 重连对账失败: {result.message}")
                    continue
                accepted = await asyncio.to_thread(
                    self.coordinator.handle_private_ws_event,
                    event,
                )
                if accepted:
                    self._record_private_state_event(event)
                processed += 1
                if max_events is not None and processed >= max_events:
                    break
                if reconciliation_error:
                    raise RuntimeError(f"周期 REST 对账失败: {reconciliation_error[0]}")
        except Exception as exc:
            await asyncio.to_thread(
                self.coordinator.handle_private_stream_failure,
                f"private_stream_failure:{type(exc).__name__}",
            )
            raise
        finally:
            task.cancel()
            await self.stream.stop()
            await asyncio.gather(task, return_exceptions=True)
        if reconciliation_error:
            raise RuntimeError(f"周期 REST 对账失败: {reconciliation_error[0]}")
        return processed
