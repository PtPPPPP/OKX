from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.services.continuous_runtime_safety import (
    ShadowAccountBaseline,
    decimal_difference,
)
from app.services.demo_session import DemoTradingSession
from app.services.reconciliation import ReconciliationStatus
from app.storage.database import Database


@dataclass(frozen=True, slots=True)
class ContinuousReconciliationResult:
    reconciliation_id: str
    run_id: str
    status: str
    started_at: datetime
    completed_at: datetime
    account_configuration_healthy: bool
    balance_healthy: bool
    orders_healthy: bool
    fills_healthy: bool
    positions_healthy: bool
    liabilities_healthy: bool
    inventory_healthy: bool
    database_healthy: bool
    lock_healthy: bool
    private_stream_healthy: bool
    baseline_snapshot_id: str | None
    current_snapshot_id: str | None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ContinuousDemoReconciliationLoop:
    """Read-only reconciliation; failures are returned, never hidden."""

    def __init__(
        self,
        database: Database,
        session: DemoTradingSession,
        run_id: str,
        baseline: ShadowAccountBaseline | None = None,
    ) -> None:
        self.database, self.session, self.run_id, self.baseline = (
            database,
            session,
            run_id,
            baseline,
        )

    async def start(self, run_id: str, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once(run_id)
            try:
                await asyncio.wait_for(stop_event.wait(), 30)
            except TimeoutError:
                continue

    async def run_once(self, run_id: str | None = None) -> ContinuousReconciliationResult:
        target = run_id or self.run_id
        started = datetime.now(UTC)
        blockers: list[str] = []
        private_healthy = bool(
            self.session.stream.health.connected
            and self.session.stream.health.authenticated
            and self.session.stream.health.subscriptions_ready
        )
        reconciliation_status = ReconciliationStatus.UNKNOWN
        try:
            reconciliation_status = await asyncio.to_thread(self.session.reconcile)
        except Exception as exc:
            blockers.append(f"rest_reconciliation_failed:{type(exc).__name__}")
        if reconciliation_status is not ReconciliationStatus.HEALTHY:
            blockers.append(f"reconciliation_status:{reconciliation_status.value}")
        if not private_healthy:
            blockers.append("private_stream_unhealthy")
        if self.baseline is None:
            blockers.append("account_baseline_missing")
        elif self.session.instrument is not None:
            try:
                portfolio = self.session.client.get_portfolio(self.session.instrument)
                for currency, baseline_total, baseline_available, baseline_frozen in (
                    (
                        "BTC",
                        self.baseline.btc_total,
                        self.baseline.btc_available,
                        self.baseline.btc_frozen,
                    ),
                    (
                        "USDT",
                        self.baseline.usdt_total,
                        self.baseline.usdt_available,
                        self.baseline.usdt_frozen,
                    ),
                ):
                    asset = portfolio.asset_balances.get(currency)
                    total = (
                        asset.cash_balance
                        if asset and asset.cash_balance is not None
                        else Decimal("0")
                    )
                    available = (
                        asset.available_balance
                        if asset and asset.available_balance is not None
                        else Decimal("0")
                    )
                    frozen = (
                        asset.frozen_balance
                        if asset and asset.frozen_balance is not None
                        else Decimal("0")
                    )
                    _, changed_total = decimal_difference(
                        baseline_total, total, Decimal("0.00000001")
                    )
                    _, changed_available = decimal_difference(
                        baseline_available, available, Decimal("0.00000001")
                    )
                    _, changed_frozen = decimal_difference(
                        baseline_frozen, frozen, Decimal("0.00000001")
                    )
                    if changed_total or changed_available or changed_frozen:
                        blockers.append(f"external_account_balance_change:{currency}")
                current_pending = tuple(
                    order.request.client_order_id
                    for order in self.session.client.get_pending_orders(
                        self.session.instrument.instrument_id
                    )
                )
                new_pending = tuple(
                    item for item in current_pending if item not in self.baseline.pending_order_ids
                )
                if new_pending:
                    blockers.append("external_order_detected")
                    with self.database.connect() as activity_connection:
                        for order_id in new_pending:
                            activity_connection.execute(
                                "INSERT INTO continuous_external_activities (activity_id,run_id,activity_type,exchange_order_id,instrument_id,baseline_value,current_value,difference,detected_at,source_endpoint,classification,severity,evidence_json,acknowledged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    uuid4().hex,
                                    target,
                                    "new_pending_order",
                                    order_id,
                                    self.session.instrument.instrument_id,
                                    "baseline_absent",
                                    "pending",
                                    "new",
                                    datetime.now(UTC).isoformat(),
                                    "/api/v5/trade/orders-pending",
                                    "external_order_detected",
                                    "p1",
                                    json.dumps({"source": "rest"}),
                                    0,
                                ),
                            )
                with self.database.connect() as run_connection:
                    run_row = run_connection.execute(
                        "SELECT started_at FROM continuous_demo_runs WHERE run_id=?", (target,)
                    ).fetchone()
                if run_row is None:
                    blockers.append("run_record_missing")
                else:
                    begin = datetime.fromisoformat(str(run_row[0]))
                    orders, order_evidence = self.session.client.get_recovery_orders(
                        self.session.instrument.instrument_id, begin, datetime.now(UTC)
                    )
                    fills, fill_evidence = self.session.client.get_recovery_fills(
                        self.session.instrument.instrument_id, begin, datetime.now(UTC)
                    )
                    if not order_evidence.completed:
                        blockers.append("order_history_incomplete")
                    if not fill_evidence.completed:
                        blockers.append("fill_history_incomplete")
                    if orders:
                        blockers.append("external_order_detected")
                    if fills:
                        blockers.append("external_fill_detected")
            except Exception as exc:
                blockers.append(f"account_activity_query_failed:{type(exc).__name__}")
        database_healthy = True
        try:
            with self.database.connect() as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
                database_healthy = result is not None and str(result[0]) == "ok"
        except sqlite3.Error:
            database_healthy = False
            blockers.append("database_unhealthy")
        status = "healthy" if not blockers and database_healthy else "unhealthy"
        completed = datetime.now(UTC)
        result = ContinuousReconciliationResult(
            uuid4().hex,
            target,
            status,
            started,
            completed,
            True,
            not blockers,
            not blockers,
            not blockers,
            not blockers,
            not blockers,
            True,
            database_healthy,
            True,
            private_healthy,
            None,
            None,
            tuple(blockers),
        )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO continuous_reconciliations VALUES (?,?,?,?,?,?,?, ?,?)",
                (
                    result.reconciliation_id,
                    target,
                    status,
                    started.isoformat(),
                    completed.isoformat(),
                    None,
                    None,
                    json.dumps(result.blockers),
                    json.dumps(result.warnings),
                ),
            )
            connection.execute(
                "UPDATE continuous_demo_runs SET reconciliation_status=?,last_reconciliation_at=?,reconciliation_count=reconciliation_count+1,reconciliation_failure_count=reconciliation_failure_count+? WHERE run_id=?",
                (status, completed.isoformat(), int(status != "healthy"), target),
            )
        return result


@dataclass(frozen=True, slots=True)
class ContinuousRunContext:
    environment: str = "demo"
    mode: str = "shadow"
    live_trading: bool = False
    broker_write_calls: int = 0
    real_order_submissions: int = 0
    private_stream_healthy: bool = True
    reconciliation_healthy: bool = True
    lock_healthy: bool = True
    database_healthy: bool = True
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CircuitBreakerDecision:
    action: str
    code: str
    severity: str
    recoverable: bool
    evidence: tuple[str, ...] = ()


class ContinuousDemoCircuitBreaker:
    def evaluate(self, context: ContinuousRunContext) -> CircuitBreakerDecision:
        if context.environment != "demo" or context.mode != "shadow" or context.live_trading:
            return CircuitBreakerDecision("freeze", "unsafe_environment", "p0", False)
        if context.broker_write_calls > 0 or context.real_order_submissions > 0:
            return CircuitBreakerDecision("freeze", "broker_write_detected", "p0", False)
        if not context.lock_healthy:
            return CircuitBreakerDecision("freeze", "lock_lost", "p1", False)
        if not context.database_healthy:
            return CircuitBreakerDecision("freeze", "database_unhealthy", "p1", False)
        if not context.private_stream_healthy:
            return CircuitBreakerDecision("freeze", "private_stream_unhealthy", "p1", False)
        if not context.reconciliation_healthy:
            return CircuitBreakerDecision("freeze", "reconciliation_unhealthy", "p1", False)
        return CircuitBreakerDecision("continue", "clear", "info", True)


class ContinuousDemoCircuitBreakerExecutor:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def apply(self, run_id: str, decision: CircuitBreakerDecision) -> None:
        if decision.action == "continue":
            return
        now = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE continuous_demo_runs SET status='frozen',stop_reason=?,circuit_breaker_action=?,circuit_breaker_code=?,circuit_breaker_severity=?,circuit_breaker_triggered_at=?,recovery_required=1 WHERE run_id=?",
                (decision.code, decision.action, decision.code, decision.severity, now, run_id),
            )
            connection.execute(
                "INSERT INTO continuous_demo_run_events (run_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                (
                    run_id,
                    "circuit_breaker_triggered",
                    json.dumps({"code": decision.code, "severity": decision.severity}),
                    now,
                ),
            )


class PrivateStreamHealthWatcher:
    def __init__(
        self, session: DemoTradingSession, executor: ContinuousDemoCircuitBreakerExecutor
    ) -> None:
        self.session, self.executor = session, executor

    async def watch(self, run_id: str, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            health = self.session.stream.health
            if (
                not (health.connected and health.authenticated and health.subscriptions_ready)
                or health.stale
            ):
                await self.executor.apply(
                    run_id,
                    ContinuousDemoCircuitBreaker().evaluate(
                        ContinuousRunContext(private_stream_healthy=False)
                    ),
                )
                stop_event.set()
                return
            await asyncio.sleep(2)


@dataclass(frozen=True, slots=True)
class ContinuousFaultInjection:
    disconnect_private_stream_after_seconds: int | None = None
    fail_reconciliation_after_count: int | None = None
    fail_heartbeat_after_count: int | None = None
    lose_lock_after_seconds: int | None = None
    simulate_external_balance_change: bool = False
