from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol
from uuid import uuid4

from app.domain.market import Instrument
from app.domain.private_state import PrivateStateSnapshot, ReconciliationToken
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.runtime.clock import Clock
from app.services.private_events import PrivateEventProcessor
from app.services.reconciliation import (
    AccountSnapshot,
    AccountSync,
    ReconciliationClient,
    ReconciliationResult,
    ReconciliationService,
    ReconciliationStatus,
)
from app.storage.repositories import TradingRepository


@dataclass(frozen=True, slots=True)
class BufferedPrivateEvent:
    watermark: int
    event: PrivateEvent


class PrivateStateControl(Protocol):
    def private_state_snapshot(self) -> PrivateStateSnapshot: ...

    def begin_private_connection_epoch(self, connection_epoch: int) -> None: ...

    def record_private_ws_watermark(
        self,
        *,
        connection_epoch: int,
        watermark: int,
        event_kind: str,
        event_at: datetime,
    ) -> None: ...

    def begin_private_reconciliation(self, reconciliation_id: str) -> None: ...

    def confirm_private_state_snapshots(self, confirmed_at: datetime) -> int: ...

    def freeze_private_state(self, reason: str) -> None: ...

    def save_system_event(
        self, event_type: str, message: str, details: dict[str, object]
    ) -> None: ...


class PrivateStateReconciler(Protocol):
    def reconcile(
        self, instrument: Instrument, *, persist_remote_state: bool = False
    ) -> ReconciliationResult: ...


class PrivateStateFaultInjector(Protocol):
    is_local_adapter: bool

    def inject(self, injection_point: str) -> None: ...


class PrivateStateCoordinator:
    """Serializes private WebSocket events and REST reconciliation writes."""

    def __init__(
        self,
        processor: PrivateEventProcessor,
        reconciler: PrivateStateReconciler,
        private_state: PrivateStateControl,
        account_sync: AccountSync | None = None,
        max_buffered_events: int = 256,
        fault_injector: PrivateStateFaultInjector | None = None,
    ) -> None:
        if max_buffered_events <= 0:
            raise ValueError("private WS buffer limit must be positive")
        if fault_injector is not None and not getattr(fault_injector, "is_local_adapter", False):
            raise ValueError("private-state fault injection requires a local adapter")
        self._processor = processor
        self._reconciler = reconciler
        self._private_state = private_state
        self._account_sync = account_sync
        self._max_buffered_events = max_buffered_events
        self._fault_injector = fault_injector
        self._lock = RLock()
        state = private_state.private_state_snapshot()
        self._connection_epoch = state.epoch
        self._ws_watermark = state.ws_watermark
        self._seen: dict[tuple[int, str], int] = {}
        self._active_reconciliation: ReconciliationToken | None = None
        self._buffer: list[BufferedPrivateEvent] = []
        self._reconciliation_failure: str | None = None

    @classmethod
    def for_private_account(
        cls,
        client: ReconciliationClient,
        repository: TradingRepository,
        clock: Clock,
    ) -> PrivateStateCoordinator:
        """Build the only production owner of private REST and WS state writes."""
        return cls(
            PrivateEventProcessor(repository),
            ReconciliationService(client, repository),
            repository,
            AccountSync(client, repository, clock),
        )

    @property
    def active_reconciliation(self) -> ReconciliationToken | None:
        with self._lock:
            return self._active_reconciliation

    def handle_private_ws_event(self, event: PrivateEvent) -> bool:
        with self._lock:
            if not self._inject_or_freeze(
                "private_ws.event.before_coordinator",
                "private_ws_event_fault_before_coordinator",
            ):
                return False
            event_epoch = event.connection_epoch or self._connection_epoch
            if event_epoch < self._connection_epoch:
                self._fail_private_input("stale_private_connection_epoch")
                return False
            if event_epoch > self._connection_epoch:
                if self._active_reconciliation is not None:
                    self._fail_private_input("connection_epoch_changed_during_reconciliation")
                    return False
                self._private_state.begin_private_connection_epoch(event_epoch)
                self._connection_epoch = event_epoch
                self._ws_watermark = 0
                self._seen.clear()
            if event.kind is PrivateEventKind.CONNECTION:
                return True

            key = (event_epoch, event.idempotency_key)
            if key in self._seen:
                return False
            watermark = event.sequence if event.sequence is not None else self._ws_watermark + 1
            if watermark != self._ws_watermark + 1:
                self._fail_private_input("private_websocket_sequence_gap")
                return False
            self._seen[key] = watermark
            self._ws_watermark = watermark
            if self._active_reconciliation is not None:
                if not self._inject_or_freeze(
                    "private_ws.event.during_reconciliation",
                    "private_ws_event_fault_during_reconciliation",
                ):
                    return False
                if len(self._buffer) >= self._max_buffered_events:
                    self._fail_private_input("private_websocket_buffer_overflow")
                    return False
                self._buffer.append(BufferedPrivateEvent(watermark, event))
                return True
            return self._apply(event, watermark)

    def reconcile_private_state(
        self, instrument: Instrument, *, source: str
    ) -> ReconciliationResult:
        with self._lock:
            if self._active_reconciliation is not None:
                self._fail_private_input("overlapping_private_reconciliation")
                return ReconciliationResult(
                    ReconciliationStatus.BLOCKED,
                    "private reconciliation already active",
                    0,
                    0,
                )
            state = self._private_state.private_state_snapshot()
            token = ReconciliationToken(
                uuid4().hex,
                self._connection_epoch,
                self._ws_watermark,
                state.version,
                datetime.now(UTC),
            )
            self._private_state.begin_private_reconciliation(token.reconciliation_id)
            self._private_state.save_system_event(
                "private_reconciliation_requested",
                "private reconciliation is owned by coordinator",
                {"source": source, "reconciliation_id": token.reconciliation_id},
            )
            self._active_reconciliation = token
            self._buffer = []
            self._reconciliation_failure = None
        try:
            result = self._reconciler.reconcile(instrument, persist_remote_state=True)
        except Exception:
            with self._lock:
                self._private_state.freeze_private_state("private_reconciliation_exception")
                self._active_reconciliation = None
                self._buffer = []
                self._reconciliation_failure = None
            raise
        with self._lock:
            try:
                if not result.order_submission_allowed:
                    self._private_state.freeze_private_state("private_reconciliation_failed")
                    return result
                if self._reconciliation_failure is not None:
                    return self._frozen_result(self._reconciliation_failure)
                if token.connection_epoch != self._connection_epoch:
                    return self._frozen_result("connection_epoch_changed_during_reconciliation")
                for buffered in sorted(self._buffer, key=lambda item: item.watermark):
                    if buffered.watermark <= token.starting_ws_watermark:
                        return self._frozen_result("invalid_buffered_private_watermark")
                    try:
                        injector = self._fault_injector
                        if injector is not None:
                            injector.inject("private_ws.replay.before_event")
                        applied = self._apply(buffered.event, buffered.watermark)
                    except Exception:
                        return self._frozen_result("buffered_private_event_replay_exception")
                    if not applied:
                        return self._frozen_result("buffered_private_event_not_applied")
                self._private_state.confirm_private_state_snapshots(datetime.now(UTC))
                snapshot = self._private_state.private_state_snapshot()
                if not snapshot.submission_allowed:
                    return self._frozen_result("private_state_not_healthy_after_replay")
                return result
            finally:
                self._active_reconciliation = None
                self._buffer = []
                self._reconciliation_failure = None

    def synchronize_private_account(
        self,
        instrument: Instrument,
        bar: str,
        *,
        run_id: str,
        mode: str,
        strategy_name: str,
        source: str,
    ) -> AccountSnapshot:
        """Persist a private REST account snapshot through the single writer."""
        synchronizer = self._account_sync
        if synchronizer is None:
            raise RuntimeError("private account synchronizer is unavailable")
        with self._lock:
            if self._active_reconciliation is not None:
                self._fail_private_input("account_sync_during_private_reconciliation")
                raise RuntimeError("private reconciliation is already active")
            self._private_state.save_system_event(
                "private_account_sync_requested",
                "private account synchronization is owned by coordinator",
                {"source": source, "run_id": run_id},
            )
            return synchronizer.sync(
                instrument,
                bar,
                run_id=run_id,
                mode=mode,
                strategy_name=strategy_name,
            )

    def get_private_state_health(self) -> PrivateStateSnapshot:
        with self._lock:
            return self._private_state.private_state_snapshot()

    def handle_private_stream_failure(self, reason: str) -> None:
        with self._lock:
            self._fail_private_input(reason)

    def _apply(self, event: PrivateEvent, watermark: int) -> bool:
        applied = self._processor.process(event)
        if not applied:
            return False
        self._private_state.record_private_ws_watermark(
            connection_epoch=self._connection_epoch,
            watermark=watermark,
            event_kind=event.kind.value,
            event_at=datetime.now(UTC),
        )
        return True

    def _frozen_result(self, reason: str) -> ReconciliationResult:
        self._private_state.freeze_private_state(reason)
        return ReconciliationResult(ReconciliationStatus.BLOCKED, reason, 0, 0)

    def _fail_private_input(self, reason: str) -> None:
        if self._active_reconciliation is not None:
            self._reconciliation_failure = reason
        self._private_state.freeze_private_state(reason)

    def _inject_or_freeze(self, injection_point: str, reason: str) -> bool:
        injector = self._fault_injector
        if injector is None:
            return True
        try:
            injector.inject(injection_point)
        except Exception:
            self._fail_private_input(reason)
            return False
        return True
