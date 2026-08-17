import asyncio
import sqlite3
from decimal import Decimal

from app.services.continuous_runtime_safety import (
    ContinuousTaskSupervisor,
    RepositorySchemaContractError,
    SupervisedTaskDefinition,
    decimal_difference,
    detect_external_fills,
    detect_external_orders,
    execute_insert,
)


def test_repository_named_insert_validates_real_sqlite_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("create table sample (id text primary key, value text not null)")
    execute_insert(connection, "sample", {"id": "a", "value": "ok"})
    assert connection.execute("select value from sample").fetchone() == ("ok",)
    try:
        execute_insert(connection, "sample", {"id": "b", "missing": "x"})
    except RepositorySchemaContractError:
        pass
    else:
        raise AssertionError("schema contract did not reject an unknown column")


def test_external_activity_and_decimal_tolerance() -> None:
    order = detect_external_orders(["baseline", "new"], ["baseline"], [])
    fill = detect_external_fills(["trade-1"], [], ["order-1"])
    difference, changed = decimal_difference(Decimal("1.000"), Decimal("1.001"), Decimal("0.0001"))
    assert order.external_activity_detected and order.new_order_ids == ("new",)
    assert fill.external_activity_detected and fill.new_trade_ids == ("trade-1",)
    assert changed and difference == Decimal("0.001")


def test_critical_supervisor_cancels_workers_and_reports_failure() -> None:
    async def scenario() -> None:
        failures: list[tuple[str, str]] = []

        async def fail(run_id: str, task: str) -> None:
            failures.append((run_id, task))

        async def broken() -> None:
            raise RuntimeError("injected")

        supervisor = ContinuousTaskSupervisor(None, fail)  # type: ignore[arg-type]
        result = await supervisor.run(
            "run",
            [SupervisedTaskDefinition("reconciliation_loop", True, "never", 0)],
            {"reconciliation_loop": broken()},
        )
        assert result.status == "failed"
        assert failures == [("run", "reconciliation_loop")]

    asyncio.run(scenario())
