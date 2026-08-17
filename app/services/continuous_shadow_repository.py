from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from app.domain.shadow_proposal import validate_shadow_proposal
from app.market.historical_data import BAR_INTERVALS
from app.services.legacy_quarantine import RuntimeGenerationService
from app.storage.database import Database, StorageError
from app.testing.fault_injection import FaultInjector


class _Configuration(Protocol):
    @property
    def strategy_name(self) -> str: ...
    @property
    def instrument_id(self) -> str: ...
    @property
    def timeframe(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ContinuousShadowResumeContext:
    """Persisted facts required to resume one VWAP Shadow run safely."""

    run_id: str
    strategy_name: str
    instrument_id: str
    timeframe: str
    configuration_hash: str
    status: str
    checkpoint: datetime
    runtime_state: dict[str, object]
    processed_count: int
    signal_count: int
    proposal_count: int


class _Candle(Protocol):
    @property
    def timestamp(self) -> datetime: ...
    @property
    def close(self) -> Decimal: ...
    @property
    def confirmed(self) -> bool: ...


def _now() -> datetime:
    return datetime.now(UTC)


def configuration_fingerprint(config: _Configuration) -> str:
    """Fingerprint the complete persisted coordinator configuration deterministically."""
    payload = asdict(config) if is_dataclass(config) else getattr(config, "__dict__", {})
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class ContinuousRunLock:
    def __init__(
        self, database: Database, lock_name: str = "continuous-demo", lease_seconds: int = 30
    ) -> None:
        self.database, self.lock_name, self.lease_seconds = database, lock_name, lease_seconds

    def acquire(self, run_id: str) -> None:
        now = _now()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT run_id,released_at,lease_expires_at FROM continuous_run_locks WHERE lock_name=?",
                (self.lock_name,),
            ).fetchone()
            if row and row[0] != run_id and row[1] is None:
                if datetime.fromisoformat(str(row[2])) > now:
                    raise RuntimeError("continuous demo lease lock is held")
                raise RuntimeError("continuous demo lease expired; recovery required")
            c.execute(
                "INSERT OR REPLACE INTO continuous_run_locks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.lock_name,
                    run_id,
                    socket.gethostname(),
                    os.getpid(),
                    now.isoformat(),
                    now.isoformat(),
                    expires.isoformat(),
                    None,
                    None,
                ),
            )

    def renew(self, run_id: str) -> None:
        now = _now()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self.database.connect() as c:
            updated = c.execute(
                "UPDATE continuous_run_locks SET last_renewed_at=?,lease_expires_at=? WHERE lock_name=? AND run_id=? AND released_at IS NULL",
                (now.isoformat(), expires.isoformat(), self.lock_name, run_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("continuous demo lease lock lost")

    def release(self, run_id: str, reason: str) -> None:
        with self.database.connect() as c:
            c.execute(
                "UPDATE continuous_run_locks SET released_at=?,release_reason=? WHERE lock_name=? AND run_id=? AND released_at IS NULL",
                (_now().isoformat(), reason, self.lock_name, run_id),
            )


class ShadowReplaySession:
    """Owns one dedicated connection for a shadow-replay execution scope.

    Ownership and durability model (Phase 2B4):
    - the session creates, configures (once) and closes the connection;
    - replay persistence is reconstructible and alone uses WAL + NORMAL;
    - NORMAL does not provide FULL's power-loss guarantee, so system-level
      durability loss can require deterministic replay from a candle boundary;
    - this relaxed connection must never persist orders, authorization,
      migration, reconciliation or any other funds-affecting state;
    - the repository's canonical candle transaction runs on this borrowed
      connection and never closes it;
    - transaction lifetime stays strictly one candle: BEGIN IMMEDIATE ... COMMIT
      per candle, no cross-candle or nested transactions;
    - any sqlite failure is surfaced as StorageError (fail closed), never
      silently retried or reconnected.
    """

    def __init__(self, repository: ContinuousShadowRepository) -> None:
        self._repository = repository
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> ShadowReplaySession:
        if self._connection is not None:
            raise RuntimeError("shadow replay session is already open")
        self._connection = self._repository.database.open_reconstructible_replay_connection()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.ProgrammingError:
            # The connection was already closed underneath us; cleanup only.
            pass
        finally:
            with contextlib.suppress(sqlite3.ProgrammingError):
                connection.close()

    @property
    def transaction_open(self) -> bool:
        """True while a candle transaction is active on the scoped connection."""
        return self._connection is not None and self._connection.in_transaction

    def commit_vwap_shadow_candle(self, **kwargs: Any) -> bool:
        """Run the canonical candle transaction on the scoped connection."""
        connection = self._require_open()
        try:
            return self._repository._commit_vwap_shadow_candle_tx(connection, **kwargs)
        except sqlite3.Error as exc:
            raise StorageError(f"shadow replay session candle commit failed: {exc}") from exc

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("shadow replay session is not open")
        return self._connection


class ContinuousShadowRepository:
    def __init__(self, database: Database, *, fault_injector: FaultInjector | None = None) -> None:
        self.database = database
        self.fault_injector = fault_injector

    def _inject(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector.inject(point)

    def replay_session(self) -> ShadowReplaySession:
        """Open a path-scoped connection owner for one replay execution."""
        return ShadowReplaySession(self)

    def create_run(self, run_id: str, config: _Configuration, now: datetime) -> None:
        generation_id = RuntimeGenerationService(self.database, now).require_active_generation()
        with self.database.connect() as c:
            c.execute(
                "INSERT INTO continuous_demo_runs (run_id,strategy_name,instrument_id,timeframe,status,mode,configuration_hash,started_at,reconciliation_status,private_stream_status,public_stream_status,circuit_breaker_status,generation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    config.strategy_name,
                    config.instrument_id,
                    config.timeframe,
                    "warming_up",
                    "fault_injection" if getattr(config, "fault_injection", None) else "shadow",
                    configuration_fingerprint(config),
                    now.isoformat(),
                    "unknown",
                    "unknown",
                    "starting",
                    "continue",
                    generation_id,
                ),
            )
            c.execute(
                "INSERT INTO continuous_demo_run_events (run_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                (run_id, "shadow_started", "{}", now.isoformat()),
            )

    def record_run_event(self, run_id: str, event_type: str, details: Mapping[str, object]) -> None:
        """Persist lifecycle evidence without changing the run's trading state."""
        with self.database.connect() as c:
            c.execute(
                "INSERT INTO continuous_demo_run_events (run_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                (run_id, event_type, json.dumps(details, sort_keys=True), _now().isoformat()),
            )

    def claim_candle(
        self,
        run_id: str,
        config: _Configuration,
        candle: _Candle,
        strategy_version: str,
        *,
        market_data_source: str = "okx_public_websocket",
    ) -> bool:
        interval = BAR_INTERVALS.get(config.timeframe.lower())
        if interval is None:
            raise ValueError(f"unsupported candle timeframe: {config.timeframe}")
        with self.database.connect() as c:
            return (
                c.execute(
                    "INSERT OR IGNORE INTO processed_candles VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        config.instrument_id,
                        config.timeframe,
                        candle.timestamp.isoformat(),
                        (candle.timestamp + interval).isoformat(),
                        int(candle.confirmed),
                        market_data_source,
                        _now().isoformat(),
                        strategy_version,
                    ),
                ).rowcount
                == 1
            )

    def commit_vwap_shadow_candle(
        self,
        *,
        run_id: str,
        config: _Configuration,
        candle: _Candle,
        strategy_version: str,
        signal_id: str,
        signal_type: str,
        signal_value: str,
        runtime_state: str,
        warmup_count: int,
        warmup_completed: bool,
        proposal_price: Decimal | None,
        processed_count: int,
        signal_count: int,
        proposal_count: int,
        market_data_source: str = "okx_public_market",
        private_stream_status: str = "not_created",
        public_stream_status: str = "ready",
    ) -> bool:
        """Atomically commit one confirmed VWAP candle on a self-managed connection."""
        with self.database.connect() as connection:
            return self._commit_vwap_shadow_candle_tx(
                connection,
                run_id=run_id,
                config=config,
                candle=candle,
                strategy_version=strategy_version,
                signal_id=signal_id,
                signal_type=signal_type,
                signal_value=signal_value,
                runtime_state=runtime_state,
                warmup_count=warmup_count,
                warmup_completed=warmup_completed,
                proposal_price=proposal_price,
                processed_count=processed_count,
                signal_count=signal_count,
                proposal_count=proposal_count,
                market_data_source=market_data_source,
                private_stream_status=private_stream_status,
                public_stream_status=public_stream_status,
            )

    def _commit_vwap_shadow_candle_tx(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        config: _Configuration,
        candle: _Candle,
        strategy_version: str,
        signal_id: str,
        signal_type: str,
        signal_value: str,
        runtime_state: str,
        warmup_count: int,
        warmup_completed: bool,
        proposal_price: Decimal | None,
        processed_count: int,
        signal_count: int,
        proposal_count: int,
        market_data_source: str = "okx_public_market",
        private_stream_status: str = "not_created",
        public_stream_status: str = "ready",
    ) -> bool:
        """One canonical candle transaction on a borrowed connection.

        The single persistence implementation for both the live engine
        (per-call connection) and the replay session (scoped connection).
        Owns the transaction explicitly: BEGIN IMMEDIATE ... COMMIT, rollback on
        any failure; the caller keeps owning the connection itself.
        """
        interval = BAR_INTERVALS.get(config.timeframe.lower())
        if interval is None or not candle.confirmed:
            raise ValueError("confirmed candle with a supported timeframe is required")
        if connection.in_transaction:
            raise RuntimeError("candle transaction must not nest inside another transaction")
        proposal_id = uuid4().hex if proposal_price is not None else None
        state_hash = hashlib.sha256(runtime_state.encode()).hexdigest()
        now = _now()
        try:
            c = connection
            c.execute("BEGIN IMMEDIATE")
            self._inject("continuous_shadow.before_processed_identity")
            claimed = (
                c.execute(
                    "INSERT OR IGNORE INTO processed_candles VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        config.instrument_id,
                        config.timeframe,
                        candle.timestamp.isoformat(),
                        (candle.timestamp + interval).isoformat(),
                        1,
                        market_data_source,
                        now.isoformat(),
                        strategy_version,
                    ),
                ).rowcount
                == 1
            )
            self._inject("continuous_shadow.after_processed_identity")
            if not claimed:
                c.rollback()
                return False
            self._inject("continuous_shadow.before_runtime")
            c.execute(
                "INSERT INTO strategy_runtime_states (state_id,run_id,strategy_name,strategy_version,instrument_id,timeframe,last_candle_open_time,previous_fast_value,previous_slow_value,previous_relation,last_signal_type,last_signal_candle_time,warmup_completed,warmup_candle_count,state_hash,state_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,strategy_name,instrument_id,timeframe) DO UPDATE SET last_candle_open_time=excluded.last_candle_open_time,last_signal_type=excluded.last_signal_type,last_signal_candle_time=excluded.last_signal_candle_time,warmup_completed=excluded.warmup_completed,warmup_candle_count=excluded.warmup_candle_count,state_hash=excluded.state_hash,state_json=excluded.state_json,updated_at=excluded.updated_at",
                (
                    uuid4().hex,
                    run_id,
                    config.strategy_name,
                    strategy_version,
                    config.instrument_id,
                    config.timeframe,
                    candle.timestamp.isoformat(),
                    None,
                    None,
                    "vwap",
                    signal_type,
                    candle.timestamp.isoformat(),
                    int(warmup_completed),
                    warmup_count,
                    state_hash,
                    runtime_state,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._inject("continuous_shadow.after_runtime")
            self._inject("continuous_shadow.before_signal")
            c.execute(
                "INSERT INTO strategy_signal_events (signal_id,run_id,instrument_id,candle_open_time,signal_type,signal_value,strategy_state_hash,position_state_hash,decision,blockers_json,created_at,strategy_name,strategy_version,timeframe,previous_relation,current_relation,warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal_id,
                    run_id,
                    config.instrument_id,
                    candle.timestamp.isoformat(),
                    signal_type,
                    signal_value,
                    state_hash,
                    "shadow_only",
                    "shadow_candidate" if proposal_id else "no_signal",
                    "[]",
                    now.isoformat(),
                    config.strategy_name,
                    strategy_version,
                    config.timeframe,
                    "vwap",
                    "vwap",
                    "[]",
                ),
            )
            self._inject("continuous_shadow.after_signal")
            if proposal_id is not None:
                self._inject("continuous_shadow.before_proposal")
                validate_shadow_proposal(
                    quantity=Decimal("0"),
                    notional=Decimal("0"),
                    submission_performed=0,
                    exchange_order_id=None,
                    capability_status="read_only",
                    risk_status="blocked",
                    decision="blocked",
                    blockers=["shadow_only", "not_sized"],
                )
                c.execute(
                    "INSERT INTO shadow_order_proposals (shadow_proposal_id,run_id,signal_id,instrument_id,side,order_type,reference_price,planned_price,quantity,notional,estimated_fee,inventory_scope,blockers_json,warnings_json,created_at,expires_at,submission_performed,exchange_order_id,instrument_type,trade_mode,decision,is_shadow,capability_status,risk_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal_id,
                        run_id,
                        signal_id,
                        config.instrument_id,
                        "buy",
                        "limit",
                        str(proposal_price),
                        str(proposal_price),
                        "0",
                        "0",
                        "0",
                        "strategy_managed",
                        '["shadow_only", "not_sized"]',
                        "[]",
                        now.isoformat(),
                        (now + interval).isoformat(),
                        0,
                        None,
                        "SPOT",
                        "cash",
                        "blocked",
                        1,
                        "read_only",
                        "blocked",
                    ),
                )
                c.execute(
                    "INSERT INTO shadow_order_proposal_events (shadow_proposal_id,event_type,reason,created_at) VALUES (?,?,?,?)",
                    (proposal_id, "blocked", "shadow_only;not_sized", now.isoformat()),
                )
                self._inject("continuous_shadow.after_proposal")
            self._inject("continuous_shadow.before_heartbeat")
            c.execute(
                "UPDATE continuous_demo_runs SET status='shadow_running',processed_candle_count=?,generated_signal_count=?,shadow_proposal_count=?,last_heartbeat_at=?,private_stream_status=?,public_stream_status=? WHERE run_id=?",
                (
                    processed_count,
                    signal_count,
                    proposal_count,
                    now.isoformat(),
                    private_stream_status,
                    public_stream_status,
                    run_id,
                ),
            )
            self._inject("continuous_shadow.after_heartbeat")
            self._inject("continuous_shadow.before_commit")
            c.commit()
            return True
        except Exception:
            c.rollback()
            raise

    def save_runtime(
        self,
        run_id: str,
        config: _Configuration,
        *,
        candle_time: datetime | None,
        fast: Decimal | None,
        slow: Decimal | None,
        relation: str | None,
        signal_type: str | None,
        warmup_count: int,
        warmup_completed: bool,
        state_json: str = "{}",
        strategy_version: str | None = None,
    ) -> str:
        state_hash = hashlib.sha256(
            json.dumps(
                [str(candle_time), str(fast), str(slow), relation, signal_type, warmup_count],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        now = _now()
        with self.database.connect() as c:
            c.execute(
                "INSERT INTO strategy_runtime_states (state_id,run_id,strategy_name,strategy_version,instrument_id,timeframe,last_candle_open_time,previous_fast_value,previous_slow_value,previous_relation,last_signal_type,last_signal_candle_time,warmup_completed,warmup_candle_count,state_hash,state_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,strategy_name,instrument_id,timeframe) DO UPDATE SET last_candle_open_time=excluded.last_candle_open_time,previous_fast_value=excluded.previous_fast_value,previous_slow_value=excluded.previous_slow_value,previous_relation=excluded.previous_relation,last_signal_type=excluded.last_signal_type,last_signal_candle_time=excluded.last_signal_candle_time,warmup_completed=excluded.warmup_completed,warmup_candle_count=excluded.warmup_candle_count,state_hash=excluded.state_hash,state_json=excluded.state_json,updated_at=excluded.updated_at",
                (
                    uuid4().hex,
                    run_id,
                    config.strategy_name,
                    strategy_version or f"{config.strategy_name}_v1",
                    config.instrument_id,
                    config.timeframe,
                    candle_time.isoformat() if candle_time else None,
                    str(fast) if fast is not None else None,
                    str(slow) if slow is not None else None,
                    relation,
                    signal_type,
                    candle_time.isoformat() if signal_type and candle_time else None,
                    int(warmup_completed),
                    warmup_count,
                    state_hash,
                    state_json,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return state_hash

    def load_vwap_shadow_runtime(self, run_id: str) -> dict[str, object] | None:
        """Read the last committed VWAP runtime/checkpoint without mutating persistent state."""
        with self.database.connect() as c:
            row = c.execute(
                "SELECT * FROM strategy_runtime_states WHERE run_id=? AND strategy_name='vwap_shadow'",
                (run_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def load_vwap_shadow_resume_context(self, run_id: str) -> ContinuousShadowResumeContext | None:
        """Aggregate only committed v23 state; this method never changes the run."""
        with self.database.connect() as c:
            row = c.execute(
                """SELECT run.run_id,run.strategy_name,run.instrument_id,run.timeframe,
                          run.configuration_hash,run.status,run.processed_candle_count,
                          run.generated_signal_count,run.shadow_proposal_count,
                          runtime.last_candle_open_time,runtime.state_json
                   FROM continuous_demo_runs AS run
                   JOIN strategy_runtime_states AS runtime
                     ON runtime.run_id=run.run_id
                   WHERE run.run_id=? AND run.strategy_name='vwap_shadow'
                     AND runtime.strategy_name='vwap_shadow'""",
                (run_id,),
            ).fetchone()
        if row is None or row["last_candle_open_time"] is None:
            return None
        try:
            checkpoint = datetime.fromisoformat(str(row["last_candle_open_time"]))
            if checkpoint.tzinfo is None:
                raise ValueError("checkpoint timezone is missing")
            state = json.loads(str(row["state_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("persisted VWAP Shadow resume state is invalid") from exc
        if not isinstance(state, dict):
            raise ValueError("persisted VWAP Shadow checkpoint is invalid")
        return ContinuousShadowResumeContext(
            run_id=str(row["run_id"]),
            strategy_name=str(row["strategy_name"]),
            instrument_id=str(row["instrument_id"]),
            timeframe=str(row["timeframe"]),
            configuration_hash=str(row["configuration_hash"]),
            status=str(row["status"]),
            checkpoint=checkpoint.astimezone(UTC),
            runtime_state=state,
            processed_count=int(row["processed_candle_count"]),
            signal_count=int(row["generated_signal_count"]),
            proposal_count=int(row["shadow_proposal_count"]),
        )

    def heartbeat(
        self,
        run_id: str,
        processed: int,
        signals: int,
        proposals: int,
        private_status: str = "ready",
        public_status: str = "ready",
        submitted: int | None = None,
    ) -> None:
        now = _now()
        with self.database.connect() as c:
            if submitted is None:
                c.execute(
                    "UPDATE continuous_demo_runs SET status='shadow_running',processed_candle_count=?,generated_signal_count=?,shadow_proposal_count=?,last_heartbeat_at=?,private_stream_status=?,public_stream_status=? WHERE run_id=?",
                    (
                        processed,
                        signals,
                        proposals,
                        now.isoformat(),
                        private_status,
                        public_status,
                        run_id,
                    ),
                )
            else:
                c.execute(
                    "UPDATE continuous_demo_runs SET status='bounded_running',processed_candle_count=?,generated_signal_count=?,shadow_proposal_count=?,submitted_order_count=?,last_heartbeat_at=?,private_stream_status=?,public_stream_status=? WHERE run_id=?",
                    (
                        processed,
                        signals,
                        proposals,
                        submitted,
                        now.isoformat(),
                        private_status,
                        public_status,
                        run_id,
                    ),
                )

    def save_signal(
        self,
        run_id: str,
        config: _Configuration,
        candle: _Candle,
        previous: str,
        current: str,
        signal_type: str | None,
        state_hash: str,
        decision: str,
        blockers: list[str],
        strategy_version: str | None = None,
        source_signal_id: str | None = None,
        signal_value: str | None = None,
    ) -> str:
        signal_id = (
            source_signal_id
            or hashlib.sha256(
                f"{run_id}:{candle.timestamp.isoformat()}:{signal_type}".encode()
            ).hexdigest()[:32]
        )
        with self.database.connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO strategy_signal_events (signal_id,run_id,instrument_id,candle_open_time,signal_type,signal_value,strategy_state_hash,position_state_hash,decision,blockers_json,created_at,strategy_name,strategy_version,timeframe,previous_relation,current_relation,warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal_id,
                    run_id,
                    config.instrument_id,
                    candle.timestamp.isoformat(),
                    signal_type or "none",
                    signal_value if signal_value is not None else current,
                    state_hash,
                    "shadow_only",
                    decision,
                    json.dumps(blockers),
                    _now().isoformat(),
                    config.strategy_name,
                    strategy_version or f"{config.strategy_name}_v1",
                    config.timeframe,
                    previous,
                    current,
                    "[]",
                ),
            )
        return signal_id

    def save_proposal(
        self,
        run_id: str,
        signal_id: str,
        config: _Configuration,
        side: str,
        price: Decimal,
        quantity: Decimal,
        decision: str,
        blockers: list[str],
    ) -> str:
        notional = Decimal("0")
        validate_shadow_proposal(
            quantity=quantity,
            notional=notional,
            submission_performed=0,
            exchange_order_id=None,
            capability_status="read_only",
            risk_status="blocked",
            decision=decision,
            blockers=blockers,
        )
        proposal_id = uuid4().hex
        now = _now()
        interval = BAR_INTERVALS.get(config.timeframe.lower())
        if interval is None:
            raise ValueError(f"unsupported candle timeframe: {config.timeframe}")
        with self.database.connect() as c:
            c.execute(
                "INSERT INTO shadow_order_proposals (shadow_proposal_id,run_id,signal_id,instrument_id,side,order_type,reference_price,planned_price,quantity,notional,estimated_fee,inventory_scope,blockers_json,warnings_json,created_at,expires_at,submission_performed,exchange_order_id,instrument_type,trade_mode,decision,is_shadow,capability_status,risk_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    run_id,
                    signal_id,
                    config.instrument_id,
                    side,
                    "limit",
                    str(price),
                    str(price),
                    str(notional),
                    str(notional),
                    "0",
                    "strategy_managed",
                    json.dumps(blockers),
                    "[]",
                    now.isoformat(),
                    (now + interval).isoformat(),
                    0,
                    None,
                    "SPOT",
                    "cash",
                    decision,
                    1,
                    "read_only",
                    "blocked",
                ),
            )
            c.execute(
                "INSERT INTO shadow_order_proposal_events (shadow_proposal_id,event_type,reason,created_at) VALUES (?,?,?,?)",
                (
                    proposal_id,
                    "blocked",
                    ";".join(blockers),
                    now.isoformat(),
                ),
            )
        return proposal_id

    def finish(
        self, run_id: str, status: str, reason: str | None, *, touch_heartbeat: bool = True
    ) -> None:
        with self.database.connect() as c:
            now = _now().isoformat()
            if touch_heartbeat:
                c.execute(
                    "UPDATE continuous_demo_runs SET status=?,stop_reason=?,stopped_at=?,last_heartbeat_at=? WHERE run_id=?",
                    (status, reason, now, now, run_id),
                )
            else:
                c.execute(
                    "UPDATE continuous_demo_runs SET status=?,stop_reason=?,stopped_at=? WHERE run_id=?",
                    (status, reason, now, run_id),
                )

    def get_status(self, run_id: str) -> dict[str, object] | None:
        with self.database.connect() as c:
            row = c.execute(
                "SELECT * FROM continuous_demo_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def request_stop(self, run_id: str) -> bool:
        with self.database.connect() as c:
            return (
                c.execute(
                    "UPDATE continuous_demo_runs SET stop_requested=1,stop_reason='manual_stop_requested' WHERE run_id=? AND status IN ('starting','warming_up','shadow_running','bounded_running')",
                    (run_id,),
                ).rowcount
                == 1
            )
