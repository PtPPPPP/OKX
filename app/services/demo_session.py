from __future__ import annotations

import asyncio
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import Settings, TradingMode
from app.domain.market import Instrument
from app.exchange.okx_client import OkxClient
from app.market.private_websocket import OKXPrivateWebSocketProvider
from app.runtime.clock import SystemClock
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.private_state_monitor import PrivateStateMonitor
from app.services.reconciliation import (
    AccountSnapshot,
    ReconciliationResult,
    ReconciliationStatus,
)
from app.storage.repositories import TradingRepository


def _new_monitor_event_loop() -> asyncio.AbstractEventLoop:
    """Avoid Windows Proactor shutdown races for the single private WS connection."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


class DemoSubmissionGate(Protocol):
    @property
    def order_submission_ready(self) -> bool: ...

    def reconcile_result(self) -> ReconciliationResult: ...

    def synchronize_private_account(self, *, run_id: str) -> AccountSnapshot: ...


@dataclass(frozen=True, slots=True)
class DemoSessionStart:
    instrument: Instrument
    account: AccountSnapshot
    reconciliation_status: ReconciliationStatus


class PrivateReadinessStage(StrEnum):
    DISCONNECTED = "disconnected"
    REST_SYNCED = "rest_synced"
    AUTHENTICATED = "authenticated"
    SUBSCRIBED = "subscribed"
    PRIVATE_STATE_RECEIVED = "private_state_received"
    RECONCILED = "reconciled"
    READY = "ready"
    STATE_PENDING_RECONCILIATION = "state_pending_reconciliation"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class PrivateReadinessSnapshot:
    stage: PrivateReadinessStage
    account_snapshot_ready: bool
    position_snapshot_ready: bool
    order_snapshot_ready: bool
    private_state_received: bool
    private_state_reconciled: bool
    stream_ready: bool
    monitor_thread_alive: bool
    monitor_error_type: str | None


class DemoTradingSession:
    """One controlled demo session. It never runs a strategy loop."""

    def __init__(
        self,
        config: RunConfig,
        settings: Settings,
        client: OkxClient,
        repository: TradingRepository,
        *,
        stream: OKXPrivateWebSocketProvider | None = None,
        reconciliation_interval_seconds: float = 5,
    ) -> None:
        if config.mode is not TradingMode.DEMO or not config.exchange.simulated:
            raise ValueError("受控会话只允许 OKX 模拟现货配置")
        if settings.allow_live_trading:
            raise ValueError("实盘开关必须关闭")
        settings.require_demo_credentials()
        self.config = config
        self.client = client
        self.repository = repository
        self.stream = stream or OKXPrivateWebSocketProvider(settings, network=client.network)
        self.private_state_coordinator: PrivateStateCoordinator | None = None
        self.monitor: PrivateStateMonitor | None = None
        self.instrument: Instrument | None = None
        self.start_snapshot: AccountSnapshot | None = None
        self.reconciliation_interval_seconds = reconciliation_interval_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._monitor_error: BaseException | None = None
        self._stage = PrivateReadinessStage.DISCONNECTED
        self._readiness_id: str | None = None
        self._last_reconciliation: ReconciliationResult | None = None
        self._closed = False

    @property
    def order_submission_ready(self) -> bool:
        return bool(
            self.instrument is not None
            and self.start_snapshot is not None
            and self.stream.is_ready
            and self._thread is not None
            and self._thread.is_alive()
            and self._monitor_error is None
            and not self.repository.has_unreconciled_private_state()
        )

    @property
    def readiness_snapshot(self) -> PrivateReadinessSnapshot:
        monitor = self.monitor
        private_state_reconciled = not self.repository.has_unreconciled_private_state()
        stage = self._stage
        if stage is PrivateReadinessStage.READY and not private_state_reconciled:
            stage = PrivateReadinessStage.STATE_PENDING_RECONCILIATION
        return PrivateReadinessSnapshot(
            stage=stage,
            account_snapshot_ready=self.start_snapshot is not None,
            position_snapshot_ready=bool(monitor and monitor.position_snapshot_received),
            order_snapshot_ready=self._last_reconciliation is not None,
            private_state_received=bool(monitor and monitor.private_state_received),
            private_state_reconciled=private_state_reconciled,
            stream_ready=self.stream.is_ready,
            monitor_thread_alive=bool(self._thread and self._thread.is_alive()),
            monitor_error_type=(
                type(self._monitor_error).__name__ if self._monitor_error is not None else None
            ),
        )

    @property
    def readiness_id(self) -> str | None:
        return self._readiness_id

    def start(self, *, timeout_seconds: float = 15) -> DemoSessionStart:
        if self._thread is not None:
            raise RuntimeError("受控模拟会话不能重复启动")
        try:
            self._readiness_id = uuid4().hex
            self._record_readiness_event("private_readiness_started")
            instrument = self.client.get_instrument(self.config.market.instrument_id)
            coordinator = PrivateStateCoordinator.for_private_account(
                self.client, self.repository, SystemClock()
            )
            account = coordinator.synchronize_private_account(
                instrument,
                self.config.market.bar,
                run_id=self._readiness_id,
                mode=self.config.mode.value,
                strategy_name=self.config.strategy.name,
                source="demo_session_startup",
            )
            first = coordinator.reconcile_private_state(instrument, source="demo_session_startup")
            self._last_reconciliation = first
            if not first.order_submission_allowed:
                raise ValueError(f"受控会话首次对账未通过: {first.message}")
            self._stage = PrivateReadinessStage.REST_SYNCED
            monitor = PrivateStateMonitor(
                self.stream,
                coordinator,
                reconciliation_interval_seconds=self.reconciliation_interval_seconds,
            )
            self.instrument = instrument
            self.start_snapshot = account
            self.private_state_coordinator = coordinator
            self.monitor = monitor
            self._thread = threading.Thread(
                target=self._run_monitor,
                name="okx-demo-private-state",
                daemon=True,
            )
            self._thread.start()
            if not self.stream.wait_until_ready(timeout_seconds):
                raise RuntimeError("私有 WebSocket 未在时限内就绪")
            if not self.stream.health.authenticated:
                raise RuntimeError("私有 WebSocket 未完成认证")
            self._stage = PrivateReadinessStage.AUTHENTICATED
            if not self.stream.health.subscriptions_ready:
                raise RuntimeError("私有 WebSocket 未完成状态订阅")
            self._stage = PrivateReadinessStage.SUBSCRIBED
            if not monitor.wait_until_private_state_received(timeout_seconds):
                raise RuntimeError("私有 WebSocket 未在时限内收到账户和持仓快照")
            self._stage = PrivateReadinessStage.PRIVATE_STATE_RECEIVED
            self._reconcile_private_state(timeout_seconds=timeout_seconds)
            self._stage = PrivateReadinessStage.RECONCILED
            if not self.order_submission_ready:
                raise RuntimeError("私有状态在最终对账后不健康，禁止提交订单")
            self._stage = PrivateReadinessStage.READY
            self._record_readiness_event("private_readiness_ready")
            return DemoSessionStart(instrument, account, self._last_reconciliation.status)
        except Exception as exc:
            failed_stage = self._stage.value
            self._stage = PrivateReadinessStage.FAILED
            self._record_readiness_event(
                "private_readiness_failed",
                failure_stage=failed_stage,
                failure_type=type(exc).__name__,
            )
            self.close()
            raise

    def reconcile(self) -> ReconciliationStatus:
        return self.reconcile_result().status

    def reconcile_result(self) -> ReconciliationResult:
        if self.instrument is None:
            raise RuntimeError("受控模拟会话尚未启动")
        coordinator = self.private_state_coordinator
        if coordinator is None:
            raise RuntimeError("受控模拟会话缺少私有状态协调器")
        result = coordinator.reconcile_private_state(self.instrument, source="demo_session")
        self._last_reconciliation = result
        if not result.order_submission_allowed:
            raise ValueError(f"受控会话对账失败: {result.message}")
        return result

    def _reconcile_private_state(self, *, timeout_seconds: float) -> ReconciliationStatus:
        """Perform one authoritative reconciliation after explicit WS state receipt."""
        if self.instrument is None:
            raise RuntimeError("controlled demo session has not started")
        if timeout_seconds <= 0:
            raise ValueError("private reconciliation timeout must be positive")
        result = self.reconcile_result()
        if self.repository.has_unreconciled_private_state():
            raise RuntimeError("private state changed during final reconciliation")
        return result.status

    def synchronize_private_account(self, *, run_id: str) -> AccountSnapshot:
        if self.instrument is None or self.private_state_coordinator is None:
            raise RuntimeError("受控模拟会话尚未启动")
        return self.private_state_coordinator.synchronize_private_account(
            self.instrument,
            self.config.market.bar,
            run_id=run_id,
            mode=self.config.mode.value,
            strategy_name=self.config.strategy.name,
            source="demo_order_service",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.stream.stop(), loop)
            with suppress(TimeoutError, RuntimeError):
                future.result(timeout=5)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._stage = PrivateReadinessStage.STOPPED
        self._record_readiness_event("private_readiness_shutdown")

    def _run_monitor(self) -> None:
        monitor = self.monitor
        instrument = self.instrument
        if monitor is None or instrument is None:
            self._monitor_error = RuntimeError("受控会话监视器未初始化")
            return
        loop = _new_monitor_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        def capture_unhandled_exception(
            _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
        ) -> None:
            exception = context.get("exception")
            self._monitor_error = (
                exception
                if isinstance(exception, BaseException)
                else RuntimeError(str(context.get("message", "event loop failure")))
            )

        loop.set_exception_handler(capture_unhandled_exception)
        try:
            loop.run_until_complete(monitor.run(instrument))
        except BaseException as exc:
            self._monitor_error = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            except BaseException as exc:
                self._monitor_error = exc
            finally:
                loop.close()
                self._loop = None

    def _record_readiness_event(
        self,
        event_type: str,
        *,
        failure_stage: str | None = None,
        failure_type: str | None = None,
    ) -> None:
        health = self.stream.health
        private_state = self.repository.private_state_snapshot()
        readiness = self.readiness_snapshot
        details: dict[str, Any] = {
            "readiness_id": self._readiness_id,
            "readiness_stage": readiness.stage.value,
            "network_mode": self.stream.network.mode.value,
            "proxy_configured": self.stream.network.proxy_url is not None,
            "proxy_url": self.stream.network.redacted_proxy_url,
            "private_ws_connect_attempts": health.connect_attempts,
            "private_ws_connections": health.connections,
            "private_ws_tls_ready": health.tls_ready,
            "private_ws_handshake_ready": health.handshake_ready,
            "private_ws_login_sent": health.login_sent,
            "private_ws_authenticated": health.authenticated,
            "private_ws_subscribe_sent": health.subscribe_sent,
            "private_ws_subscriptions_ready": health.subscriptions_ready,
            "private_ws_events_received": health.events_received,
            "private_ws_last_event_timestamp": (
                health.last_message_at.isoformat() if health.last_message_at is not None else None
            ),
            "private_ws_unsubscriptions": health.unsubscriptions,
            "private_ws_closed_cleanly": health.closed_cleanly,
            "private_ws_failure_stage": failure_stage or health.failure_stage,
            "private_ws_failure_type": failure_type or health.failure_type,
            "account_snapshot_ready": readiness.account_snapshot_ready,
            "position_snapshot_ready": readiness.position_snapshot_ready,
            "order_snapshot_ready": readiness.order_snapshot_ready,
            "snapshot_source": "private_rest_reconciliation_with_private_ws_replay",
            "snapshot_timestamp": (
                private_state.last_consistent_at.isoformat()
                if private_state.last_consistent_at is not None
                else None
            ),
            "snapshot_freshness_valid": private_state.submission_allowed,
            "derivative_positions_count": None,
            "non_terminal_orders_count": (
                len(self.start_snapshot.open_orders) if self.start_snapshot is not None else None
            ),
            "private_state_reconciled": readiness.private_state_reconciled,
            "reconciliation_failure_reason": (
                None
                if readiness.private_state_reconciled
                else ",".join(private_state.dirty_reasons)
            ),
        }
        self.repository.save_system_event(
            event_type,
            "private readiness lifecycle telemetry",
            details,
        )
