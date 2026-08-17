from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol

from app.exchange.exceptions import ExchangeError, NetworkError, RateLimitError, RequestTimeout
from app.storage.database import StorageError


class FaultAction(StrEnum):
    PASS = "pass"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    CONNECTION_RESET = "connection_reset"
    MALFORMED_JSON = "malformed_json"
    SCHEMA_INVALID = "schema_invalid"
    SEMANTIC_INVALID = "semantic_invalid"
    STORAGE_ERROR = "storage_error"


KNOWN_INJECTION_POINTS = frozenset(
    {
        "private_rest.request.before_send",
        "private_rest.response.before_parse",
        "private_rest.snapshot.before_apply",
        "private_ws.connect.before",
        "private_ws.receive.before",
        "private_ws.event.before_coordinator",
        "private_ws.event.during_reconciliation",
        "private_ws.replay.before_event",
        "shadow_soak.signal.before_insert",
        "shadow_soak.proposal.before_insert",
        "shadow_soak.checkpoint.before_insert",
        "shadow_soak.transaction.before_commit",
        "continuous_shadow.before_processed_identity",
        "continuous_shadow.after_processed_identity",
        "continuous_shadow.before_runtime",
        "continuous_shadow.after_runtime",
        "continuous_shadow.before_signal",
        "continuous_shadow.after_signal",
        "continuous_shadow.before_proposal",
        "continuous_shadow.after_proposal",
        "continuous_shadow.before_heartbeat",
        "continuous_shadow.after_heartbeat",
        "continuous_shadow.before_commit",
    }
)


@dataclass(frozen=True, slots=True)
class FaultStep:
    injection_point: str
    action: FaultAction
    occurrence: int = 1
    delay_ticks: int = 0


@dataclass(frozen=True, slots=True)
class FaultPlan:
    scenario_id: str
    seed: int
    steps: tuple[FaultStep, ...]
    max_steps: int = 128

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FaultTraceEntry:
    step: int
    injection_point: str
    action: FaultAction
    occurrence: int


@dataclass(slots=True)
class FaultTrace:
    scenario_id: str
    seed: int
    entries: list[FaultTraceEntry] = field(default_factory=list)

    def state_hash(self) -> str:
        return sha256(repr((self.scenario_id, self.seed, self.entries)).encode()).hexdigest()


class VirtualClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._ticks = 0

    def now(self) -> datetime:
        return self._now

    def monotonic_ticks(self) -> int:
        return self._ticks

    def advance(self, ticks: int = 1) -> None:
        if ticks < 0:
            raise ValueError("virtual clock cannot move backwards")
        self._ticks += ticks
        self._now += timedelta(milliseconds=ticks)


class LocalAdapter(Protocol):
    is_local_adapter: bool


class FaultInjector:
    """Deterministic, local-only boundary fault selector with no state write access."""

    is_local_adapter = True

    def __init__(self, plan: FaultPlan, adapter: LocalAdapter, clock: VirtualClock) -> None:
        if not getattr(adapter, "is_local_adapter", False):
            raise ValueError("fault injection requires an explicit local adapter")
        if plan.max_steps <= 0 or len(plan.steps) > plan.max_steps:
            raise ValueError("fault plan exceeds its execution limit")
        unknown = {step.injection_point for step in plan.steps} - KNOWN_INJECTION_POINTS
        if unknown:
            raise ValueError(f"unknown fault injection point: {sorted(unknown)!r}")
        self.plan, self.clock = plan, clock
        self.trace = FaultTrace(plan.scenario_id, plan.seed)
        self._seen: dict[str, int] = {}

    def inject(self, injection_point: str) -> None:
        if injection_point not in KNOWN_INJECTION_POINTS:
            raise ValueError(f"unknown fault injection point: {injection_point}")
        occurrence = self._seen.get(injection_point, 0) + 1
        self._seen[injection_point] = occurrence
        step = next(
            (
                item
                for item in self.plan.steps
                if item.injection_point == injection_point and item.occurrence == occurrence
            ),
            FaultStep(injection_point, FaultAction.PASS, occurrence),
        )
        self.clock.advance(step.delay_ticks)
        self.trace.entries.append(
            FaultTraceEntry(len(self.trace.entries) + 1, injection_point, step.action, occurrence)
        )
        if step.action is FaultAction.PASS:
            return
        errors: dict[FaultAction, type[Exception]] = {
            FaultAction.TIMEOUT: RequestTimeout,
            FaultAction.RATE_LIMIT: RateLimitError,
            FaultAction.SERVER_ERROR: ExchangeError,
            FaultAction.CONNECTION_RESET: NetworkError,
            FaultAction.MALFORMED_JSON: ValueError,
            FaultAction.SCHEMA_INVALID: ValueError,
            FaultAction.SEMANTIC_INVALID: ValueError,
            FaultAction.STORAGE_ERROR: StorageError,
        }
        raise errors[step.action](f"local injected {step.action.value}")

    def assert_consumed(self) -> None:
        missing = [
            step
            for step in self.plan.steps
            if self._seen.get(step.injection_point, 0) < step.occurrence
        ]
        if missing:
            raise AssertionError(f"fault plan steps were not triggered: {missing!r}")
