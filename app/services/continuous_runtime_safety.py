from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.storage.database import Database


class RepositorySchemaContractError(RuntimeError):
    pass


def execute_insert(
    connection: sqlite3.Connection, table: str, values: Mapping[str, object]
) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    missing = set(values) - columns
    if missing:
        raise RepositorySchemaContractError(
            f"{table}: unknown columns: {', '.join(sorted(missing))}"
        )
    if not values:
        raise RepositorySchemaContractError(f"{table}: empty insert")
    names = tuple(values)
    placeholders = ",".join(f":{name}" for name in names)
    if len(names) != placeholders.count(":"):
        raise RepositorySchemaContractError(f"{table}: column/value mismatch")
    connection.execute(
        f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})", dict(values)
    )


@dataclass(frozen=True, slots=True)
class ShadowAccountBaseline:
    baseline_id: str
    run_id: str
    account_fingerprint: str
    account_mode: str
    position_mode: str
    environment: str
    btc_total: Decimal
    btc_available: Decimal
    btc_frozen: Decimal
    usdt_total: Decimal
    usdt_available: Decimal
    usdt_frozen: Decimal
    pending_order_ids: tuple[str, ...]
    recent_order_ids: tuple[str, ...]
    recent_trade_ids: tuple[str, ...]
    latest_order_time: datetime | None
    latest_fill_time: datetime | None
    derivative_position_count: int
    liability_count: int
    captured_at: datetime


class ShadowAccountBaselineRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_once(self, baseline: ShadowAccountBaseline) -> None:
        values = {
            "baseline_id": baseline.baseline_id,
            "run_id": baseline.run_id,
            "account_fingerprint": baseline.account_fingerprint,
            "account_mode": baseline.account_mode,
            "position_mode": baseline.position_mode,
            "environment": baseline.environment,
            "btc_total": str(baseline.btc_total),
            "btc_available": str(baseline.btc_available),
            "btc_frozen": str(baseline.btc_frozen),
            "usdt_total": str(baseline.usdt_total),
            "usdt_available": str(baseline.usdt_available),
            "usdt_frozen": str(baseline.usdt_frozen),
            "pending_order_ids_json": json.dumps(baseline.pending_order_ids),
            "recent_order_ids_json": json.dumps(baseline.recent_order_ids),
            "recent_trade_ids_json": json.dumps(baseline.recent_trade_ids),
            "latest_order_time": baseline.latest_order_time.isoformat()
            if baseline.latest_order_time
            else None,
            "latest_fill_time": baseline.latest_fill_time.isoformat()
            if baseline.latest_fill_time
            else None,
            "derivative_position_count": baseline.derivative_position_count,
            "liability_count": baseline.liability_count,
            "captured_at": baseline.captured_at.isoformat(),
        }
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT baseline_id FROM shadow_account_baselines WHERE run_id=?",
                (baseline.run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != baseline.baseline_id:
                    raise RepositorySchemaContractError("shadow baseline is immutable")
                return
            execute_insert(connection, "shadow_account_baselines", values)

    def load(self, run_id: str) -> ShadowAccountBaseline | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_account_baselines WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return ShadowAccountBaseline(
            str(row["baseline_id"]),
            run_id,
            str(row["account_fingerprint"]),
            str(row["account_mode"]),
            str(row["position_mode"]),
            str(row["environment"]),
            Decimal(str(row["btc_total"])),
            Decimal(str(row["btc_available"])),
            Decimal(str(row["btc_frozen"])),
            Decimal(str(row["usdt_total"])),
            Decimal(str(row["usdt_available"])),
            Decimal(str(row["usdt_frozen"])),
            tuple(json.loads(str(row["pending_order_ids_json"]))),
            tuple(json.loads(str(row["recent_order_ids_json"]))),
            tuple(json.loads(str(row["recent_trade_ids_json"]))),
            datetime.fromisoformat(str(row["latest_order_time"]))
            if row["latest_order_time"]
            else None,
            datetime.fromisoformat(str(row["latest_fill_time"]))
            if row["latest_fill_time"]
            else None,
            int(row["derivative_position_count"]),
            int(row["liability_count"]),
            datetime.fromisoformat(str(row["captured_at"])),
        )


def baseline_from_session(run_id: str, session: Any) -> ShadowAccountBaseline:
    snapshot = session.start_snapshot
    if snapshot is None:
        raise RepositorySchemaContractError(
            "shadow account baseline requires initial REST snapshot"
        )
    portfolio = snapshot.portfolio
    btc = portfolio.asset_balances.get("BTC")
    usdt = portfolio.asset_balances.get("USDT")
    account_mode = (
        portfolio.account_configuration.account_mode.value
        if portfolio.account_configuration
        else "unknown"
    )
    position_mode = (
        portfolio.account_configuration.position_mode
        if portfolio.account_configuration and portfolio.account_configuration.position_mode
        else "unknown"
    )
    captured = snapshot.captured_at
    fingerprint = hashlib.sha256(
        f"{account_mode}:{position_mode}:{captured.isoformat()}".encode()
    ).hexdigest()
    return ShadowAccountBaseline(
        uuid4().hex,
        run_id,
        fingerprint,
        account_mode,
        position_mode,
        "demo",
        btc.cash_balance if btc and btc.cash_balance is not None else Decimal("0"),
        btc.available_balance if btc and btc.available_balance is not None else Decimal("0"),
        btc.frozen_balance if btc and btc.frozen_balance is not None else Decimal("0"),
        usdt.cash_balance if usdt and usdt.cash_balance is not None else Decimal("0"),
        usdt.available_balance if usdt and usdt.available_balance is not None else Decimal("0"),
        usdt.frozen_balance if usdt and usdt.frozen_balance is not None else Decimal("0"),
        tuple(order.request.client_order_id for order in snapshot.open_orders),
        (),
        (),
        None,
        None,
        0,
        0,
        captured,
    )


@dataclass(frozen=True, slots=True)
class SupervisedTaskDefinition:
    name: str
    critical: bool
    restart_policy: str
    maximum_restart_count: int


@dataclass(frozen=True, slots=True)
class TaskSupervisorResult:
    status: str
    failed_task: str | None = None
    exception_class: str | None = None
    restart_count: int = 0


class ContinuousTaskSupervisor:
    def __init__(
        self, database: Database, apply_failure: Callable[[str, str], Awaitable[None]]
    ) -> None:
        self.database, self.apply_failure = database, apply_failure

    async def run(
        self,
        run_id: str,
        tasks: Sequence[SupervisedTaskDefinition],
        workers: Mapping[str, Awaitable[None]] | None = None,
    ) -> TaskSupervisorResult:
        if not tasks:
            return TaskSupervisorResult("completed")
        worker_map = workers or {}
        pending: dict[str, asyncio.Task[None]] = {
            name: asyncio.create_task(worker_map[name], name=name)  # type: ignore[arg-type]
            for name in worker_map
        }
        try:
            done, _ = await asyncio.wait(pending.values(), return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                error = task.exception()
                if error is not None:
                    name = task.get_name()
                    definition = next((item for item in tasks if item.name == name), None)
                    if definition is not None and definition.critical:
                        await self.apply_failure(run_id, name)
                        return TaskSupervisorResult("failed", name, type(error).__name__)
            return TaskSupervisorResult("completed")
        finally:
            for task in pending.values():
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending.values(), return_exceptions=True)


@dataclass(frozen=True, slots=True)
class ExternalOrderDetectionResult:
    new_order_ids: tuple[str, ...]
    existing_baseline_order_ids: tuple[str, ...]
    known_project_order_ids: tuple[str, ...]
    ambiguous_order_ids: tuple[str, ...]
    external_activity_detected: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalFillDetectionResult:
    new_trade_ids: tuple[str, ...]
    related_order_ids: tuple[str, ...]
    baseline_trade_ids: tuple[str, ...]
    external_activity_detected: bool
    blockers: tuple[str, ...] = ()


def detect_external_orders(
    current_ids: Sequence[str], baseline_ids: Sequence[str], project_ids: Sequence[str]
) -> ExternalOrderDetectionResult:
    baseline, project = set(baseline_ids), set(project_ids)
    new = tuple(item for item in current_ids if item not in baseline and item not in project)
    return ExternalOrderDetectionResult(
        new,
        tuple(item for item in current_ids if item in baseline),
        tuple(item for item in current_ids if item in project),
        (),
        bool(new),
        ("external_order_detected",) if new else (),
    )


def detect_external_fills(
    current_ids: Sequence[str], baseline_ids: Sequence[str], order_ids: Sequence[str]
) -> ExternalFillDetectionResult:
    baseline = set(baseline_ids)
    new = tuple(item for item in current_ids if item not in baseline)
    return ExternalFillDetectionResult(
        new,
        tuple(order_ids),
        tuple(item for item in current_ids if item in baseline),
        bool(new),
        ("external_fill_detected",) if new else (),
    )


def decimal_difference(
    baseline: Decimal, current: Decimal, tolerance: Decimal
) -> tuple[Decimal, bool]:
    difference = current - baseline
    return difference, abs(difference) > tolerance
