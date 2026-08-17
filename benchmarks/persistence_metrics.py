"""Zero-semantic-effect SQLite instrumentation for the Phase 2A harness.

``instrumented_sqlite`` swaps ``sqlite3.connect`` for a counting/timing
factory inside the benchmark process only. Production modules keep calling
``sqlite3.connect`` exactly as before; they simply receive a subclassed
connection whose ``execute``/``executemany``/``commit``/``rollback`` record
counts and wall-clock durations before delegating to sqlite3.

Disabled by default: nothing in ``app/`` imports this module.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast

_FIRST_WORD = re.compile(r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*([A-Za-z]+)", re.S)
_TABLE_INSERT = re.compile(r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"'`\[]?(\w+)", re.I)
_TABLE_UPDATE = re.compile(r"^\s*UPDATE\s+[\"'`\[]?(\w+)", re.I)
_TABLE_DELETE = re.compile(r"^\s*DELETE\s+FROM\s+[\"'`\[]?(\w+)", re.I)

_WRITE_KINDS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


def classify(sql: str) -> str:
    match = _FIRST_WORD.match(sql or "")
    if match is None:
        return "OTHER"
    word = match.group(1).upper()
    if word == "INSERT":
        return "INSERT"
    if word in _WRITE_KINDS:
        return word
    return word if word.isalpha() else "OTHER"


def target_table(sql: str) -> str | None:
    for pattern in (_TABLE_INSERT, _TABLE_UPDATE, _TABLE_DELETE):
        match = pattern.match(sql or "")
        if match is not None:
            return match.group(1).lower()
    return None


@dataclass
class PersistenceMetrics:
    connections_opened: int = 0
    statements: Counter[str] = field(default_factory=Counter)
    table_writes: Counter[str] = field(default_factory=Counter)
    commit_calls: int = 0
    rollback_calls: int = 0
    begin_statements: int = 0
    statement_durations_ns: list[int] = field(default_factory=list)
    commit_durations_ns: list[int] = field(default_factory=list)
    connect_durations_ns: list[int] = field(default_factory=list)
    close_durations_ns: list[int] = field(default_factory=list)
    kind_durations_ns: dict[str, int] = field(default_factory=dict)
    db_time_ns: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def sql_reads(self) -> int:
        return self.statements.get("SELECT", 0)

    @property
    def sql_writes(self) -> int:
        return (
            self.statements.get("INSERT", 0)
            + self.statements.get("UPDATE", 0)
            + self.statements.get("DELETE", 0)
            + self.statements.get("REPLACE", 0)
        )

    @property
    def transactions_committed(self) -> int:
        return self.commit_calls

    def record_statement(self, sql: str, duration_ns: int) -> None:
        kind = classify(sql)
        self.statements[kind] += 1
        self.kind_durations_ns[kind] = self.kind_durations_ns.get(kind, 0) + duration_ns
        if kind == "BEGIN":
            self.begin_statements += 1
        if kind in _WRITE_KINDS:
            table = target_table(sql)
            if table is not None:
                self.table_writes[table] += 1
        self.statement_durations_ns.append(duration_ns)
        self.db_time_ns += duration_ns

    def snapshot_counts(self) -> dict[str, Any]:
        return {
            "connections_opened": self.connections_opened,
            "sql_reads": self.sql_reads,
            "sql_writes": self.sql_writes,
            "insert_count": self.statements.get("INSERT", 0),
            "update_count": self.statements.get("UPDATE", 0),
            "delete_count": self.statements.get("DELETE", 0),
            "begin_statements": self.begin_statements,
            "commit_calls": self.commit_calls,
            "rollback_calls": self.rollback_calls,
            "table_writes": dict(self.table_writes),
        }

    def latency_summary(self) -> dict[str, float]:
        return {
            "mean_db_operation_ms": _mean_ms(self.statement_durations_ns),
            "p50_db_operation_ms": _percentile_ms(self.statement_durations_ns, 50),
            "p95_db_operation_ms": _percentile_ms(self.statement_durations_ns, 95),
            "p99_db_operation_ms": _percentile_ms(self.statement_durations_ns, 99),
            "mean_commit_ms": _mean_ms(self.commit_durations_ns),
            "db_time_ms": self.db_time_ns / 1_000_000,
        }

    def kind_cost_summary_ms(self) -> dict[str, float]:
        """Sum of wall time per statement kind, plus connect/close/commit pools."""
        per_kind: dict[str, int] = {}
        for kind, count in self.statements.items():
            per_kind[kind] = count
        sums_ns: dict[str, int] = {kind: 0 for kind in per_kind}
        # statement_durations_ns aligns with record order; re-derive per kind by
        # replaying classification is not possible here, so aggregate via the
        # dedicated per-kind pool filled by record_statement.
        for kind, total in self.kind_durations_ns.items():
            sums_ns[kind] = total
        result = {kind: total / 1_000_000 for kind, total in sums_ns.items()}
        result["__connect__"] = sum(self.connect_durations_ns) / 1_000_000
        result["__commit__"] = sum(self.commit_durations_ns) / 1_000_000
        result["__close__"] = sum(self.close_durations_ns) / 1_000_000
        return result


def _mean_ms(durations_ns: list[int]) -> float:
    if not durations_ns:
        return 0.0
    return sum(durations_ns) / len(durations_ns) / 1_000_000


def _percentile_ms(durations_ns: list[int], percentile: float) -> float:
    if not durations_ns:
        return 0.0
    ordered = sorted(durations_ns)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return ordered[index] / 1_000_000


class _CountingConnection(sqlite3.Connection):
    """Subclass-only instrumentation: delegates every call to sqlite3."""

    metrics: PersistenceMetrics

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        started = time.perf_counter_ns()
        try:
            return super().execute(sql, parameters)
        finally:
            self.metrics.record_statement(str(sql), time.perf_counter_ns() - started)

    def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
        started = time.perf_counter_ns()
        try:
            return super().executemany(sql, parameters)
        finally:
            self.metrics.record_statement(str(sql), time.perf_counter_ns() - started)

    def commit(self) -> None:
        started = time.perf_counter_ns()
        try:
            super().commit()
        finally:
            duration = time.perf_counter_ns() - started
            self.metrics.commit_calls += 1
            self.metrics.commit_durations_ns.append(duration)
            self.metrics.db_time_ns += duration

    def rollback(self) -> None:
        started = time.perf_counter_ns()
        try:
            super().rollback()
        finally:
            duration = time.perf_counter_ns() - started
            self.metrics.rollback_calls += 1
            self.metrics.db_time_ns += duration

    def close(self) -> None:
        started = time.perf_counter_ns()
        try:
            super().close()
        finally:
            duration = time.perf_counter_ns() - started
            self.metrics.close_durations_ns.append(duration)
            self.metrics.db_time_ns += duration


@contextmanager
def instrumented_sqlite(metrics: PersistenceMetrics) -> Iterator[PersistenceMetrics]:
    """Patch sqlite3.connect for the duration of one benchmark workload."""
    original_connect = sqlite3.connect

    def counting_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        started = time.perf_counter_ns()
        kwargs["factory"] = _CountingConnection
        connection = cast(_CountingConnection, original_connect(*args, **kwargs))
        duration = time.perf_counter_ns() - started
        metrics.connections_opened += 1
        metrics.connect_durations_ns.append(duration)
        metrics.db_time_ns += duration
        connection.metrics = metrics
        return connection

    sqlite3.connect = counting_connect  # type: ignore[assignment]
    try:
        yield metrics
    finally:
        sqlite3.connect = original_connect
