from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.shadow_proposal import validate_shadow_proposal
from app.domain.signal import Signal, SignalAction
from app.market.historical_data import (
    BAR_INTERVALS,
    MarketDataError,
    load_candles_csv,
    normalize_candles,
)
from app.market.synthetic_candles import (
    SyntheticCandleRequest,
    generate_synthetic_candles,
)
from app.reproducibility import (
    InstrumentSnapshotStore,
    candles_hash,
    canonical_hash,
)
from app.runtime.clock import BacktestClock
from app.strategies.registry import create_strategy
from app.strategies.vwap_shadow import VWAPShadowStrategy
from app.testing.fault_injection import FaultInjector

CHECKPOINT_VERSION = 1
_STRATEGY_NAME = "vwap_shadow"
_MODE = "shadow_only"
_CAPABILITY = "read_only"
_EXECUTION_STATUS = "non_executable"


class ShadowSoakError(RuntimeError):
    pass


class InterruptionPoint(StrEnum):
    BEFORE_STRATEGY = "before_strategy"
    AFTER_STRATEGY_BEFORE_SIGNAL_SAVE = "after_strategy_before_signal_save"
    AFTER_SIGNAL_SAVE_BEFORE_PROPOSAL_SAVE = "after_signal_save_before_proposal_save"
    AFTER_PROPOSAL_SAVE_BEFORE_CHECKPOINT = "after_proposal_save_before_checkpoint"
    AFTER_CHECKPOINT = "after_checkpoint"


class ShadowSoakInterruption(ShadowSoakError):
    def __init__(self, point: InterruptionPoint, bar_number: int) -> None:
        self.point = point
        self.bar_number = bar_number
        super().__init__(f"local interruption at {point.value} for bar {bar_number}")


@dataclass(frozen=True, slots=True)
class InterruptionPlan:
    point: InterruptionPoint
    bar_number: int

    def __post_init__(self) -> None:
        if self.bar_number <= 0:
            raise ValueError("interruption bar number must be greater than zero")


@dataclass(frozen=True, slots=True)
class SoakDataSource:
    candles: tuple[Candle, ...]
    identity_hash: str
    candle_hash: str
    description: str
    parameters: dict[str, object]


def load_csv_soak_source(path: Path, *, bar_interval: str) -> SoakDataSource:
    _reject_csv_duplicate_timestamps(path)
    candles = tuple(load_candles_csv(path, bar=bar_interval))
    validated = validate_soak_candles(candles, bar_interval=bar_interval)
    raw_file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    normalized_hash = candles_hash(list(validated))
    return SoakDataSource(
        candles=validated,
        identity_hash=normalized_hash,
        candle_hash=normalized_hash,
        description=f"local_csv:{path.as_posix()}",
        parameters={
            "kind": "local_csv",
            "path": path.as_posix(),
            "normalized_sha256": normalized_hash,
            "file_sha256": raw_file_hash,
        },
    )


def build_synthetic_soak_source(request: SyntheticCandleRequest) -> SoakDataSource:
    candles = tuple(generate_synthetic_candles(request))
    validated = validate_soak_candles(candles, bar_interval=request.bar_interval)
    identity = request.identity()
    return SoakDataSource(
        candles=validated,
        identity_hash=canonical_hash(identity),
        candle_hash=candles_hash(list(validated)),
        description=f"synthetic:seed={request.seed}:bars={request.count}",
        parameters=identity,
    )


def validate_soak_candles(
    candles: tuple[Candle, ...] | list[Candle],
    *,
    bar_interval: str,
) -> tuple[Candle, ...]:
    raw = list(candles)
    if not raw:
        raise MarketDataError("Shadow soak requires at least one candle")
    timestamps: set[datetime] = set()
    for candle in raw:
        if candle.timestamp.tzinfo is None:
            raise MarketDataError("K 线时间必须包含时区")
        normalized_timestamp = candle.timestamp.astimezone(UTC)
        if normalized_timestamp in timestamps:
            raise MarketDataError(f"重复 K 线时间戳: {normalized_timestamp.isoformat()}")
        timestamps.add(normalized_timestamp)
    try:
        return tuple(normalize_candles(raw, bar=bar_interval))
    except MarketDataError as exc:
        if "K 线缺失" in str(exc):
            raise MarketDataError(f"{exc}; 预期周期: {bar_interval}") from exc
        raise


class ShadowSoakStore:
    is_local_adapter = True

    def __init__(self, database_path: Path, *, fault_injector: FaultInjector | None = None) -> None:
        self.database_path = database_path.resolve()
        self._fault_injector = fault_injector
        self._runtime_connection: sqlite3.Connection | None = None
        production_path = Path("data/trading.db").resolve()
        if self.database_path == production_path:
            raise ShadowSoakError("production database is forbidden for VWAP Shadow soak")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def open_runtime(self) -> None:
        if self._runtime_connection is not None:
            raise ShadowSoakError("Shadow soak runtime connection is already open")
        self._runtime_connection = self._new_connection()

    def close_runtime(self) -> None:
        if self._runtime_connection is not None:
            self._runtime_connection.close()
            self._runtime_connection = None

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _bar_transaction(self) -> Iterator[sqlite3.Connection]:
        owned = self._runtime_connection is None
        connection = self._runtime_connection or self._new_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            self._inject("shadow_soak.transaction.before_commit")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if owned:
                connection.close()

    def _inject(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector.inject(point)

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_soak_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    bar_interval TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode = 'shadow_only'),
                    capability_status TEXT NOT NULL CHECK(capability_status = 'read_only'),
                    execution_status TEXT NOT NULL CHECK(execution_status = 'non_executable'),
                    strategy_config_hash TEXT NOT NULL,
                    data_source_hash TEXT NOT NULL,
                    candle_hash TEXT NOT NULL,
                    data_source_json TEXT NOT NULL,
                    checkpoint_version INTEGER NOT NULL,
                    bars_received INTEGER NOT NULL,
                    bars_confirmed INTEGER NOT NULL,
                    bars_processed INTEGER NOT NULL DEFAULT 0,
                    bars_held INTEGER NOT NULL DEFAULT 0,
                    buy_signals INTEGER NOT NULL DEFAULT 0,
                    signals_persisted INTEGER NOT NULL DEFAULT 0,
                    proposals_persisted INTEGER NOT NULL DEFAULT 0,
                    duplicate_bars_rejected INTEGER NOT NULL DEFAULT 0,
                    resume_count INTEGER NOT NULL DEFAULT 0,
                    checkpoint_count INTEGER NOT NULL DEFAULT 0,
                    last_processed_timestamp TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    interrupted_at TEXT,
                    failure_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_soak_signals (
                    run_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    processing_key TEXT NOT NULL,
                    bar_timestamp TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('buy', 'hold')),
                    explanation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, signal_id),
                    UNIQUE(run_id, processing_key),
                    FOREIGN KEY(run_id) REFERENCES shadow_soak_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_soak_run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES shadow_soak_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_soak_proposals (
                    run_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side = 'buy'),
                    reference_price TEXT NOT NULL,
                    planned_price TEXT NOT NULL,
                    quantity TEXT NOT NULL CHECK(quantity = '0'),
                    notional TEXT NOT NULL CHECK(notional = '0'),
                    blockers_json TEXT NOT NULL CHECK(blockers_json = '["shadow_only"]'),
                    submission_performed INTEGER NOT NULL CHECK(submission_performed = 0),
                    exchange_order_id TEXT CHECK(exchange_order_id IS NULL),
                    capability_status TEXT NOT NULL CHECK(capability_status = 'read_only'),
                    risk_status TEXT NOT NULL CHECK(risk_status = 'blocked'),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, proposal_id),
                    UNIQUE(run_id, signal_id),
                    FOREIGN KEY(run_id, signal_id)
                        REFERENCES shadow_soak_signals(run_id, signal_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_soak_processed_bars (
                    run_id TEXT NOT NULL,
                    processing_key TEXT NOT NULL,
                    bar_timestamp TEXT NOT NULL,
                    confirmed INTEGER NOT NULL,
                    signal_id TEXT NOT NULL,
                    proposal_id TEXT,
                    strategy_state_hash TEXT NOT NULL,
                    strategy_state_json TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, processing_key),
                    UNIQUE(run_id, bar_timestamp),
                    FOREIGN KEY(run_id, signal_id)
                        REFERENCES shadow_soak_signals(run_id, signal_id),
                    FOREIGN KEY(run_id, proposal_id)
                        REFERENCES shadow_soak_proposals(run_id, proposal_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_soak_checkpoints (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    checkpoint_version INTEGER NOT NULL,
                    strategy_name TEXT NOT NULL,
                    strategy_config_hash TEXT NOT NULL,
                    data_source_hash TEXT NOT NULL,
                    candle_hash TEXT NOT NULL,
                    bar_interval TEXT NOT NULL,
                    last_processed_timestamp TEXT NOT NULL,
                    processed_count INTEGER NOT NULL,
                    window_json TEXT NOT NULL,
                    window_total_volume TEXT NOT NULL,
                    window_weighted_total TEXT NOT NULL,
                    signal_count INTEGER NOT NULL,
                    proposal_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    UNIQUE(run_id, processed_count),
                    FOREIGN KEY(run_id) REFERENCES shadow_soak_runs(run_id)
                );
                """
            )

    def create_run(
        self,
        *,
        run_id: str,
        config: RunConfig,
        bar_interval: str,
        strategy_config_hash: str,
        source: SoakDataSource,
    ) -> None:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO shadow_soak_runs (
                    run_id,strategy_name,instrument_id,bar_interval,status,mode,
                    capability_status,execution_status,strategy_config_hash,
                    data_source_hash,candle_hash,data_source_json,checkpoint_version,
                    bars_received,bars_confirmed,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    _STRATEGY_NAME,
                    config.market.instrument_id,
                    bar_interval,
                    "running",
                    _MODE,
                    _CAPABILITY,
                    _EXECUTION_STATUS,
                    strategy_config_hash,
                    source.identity_hash,
                    source.candle_hash,
                    json.dumps(source.parameters, ensure_ascii=False, sort_keys=True),
                    CHECKPOINT_VERSION,
                    len(source.candles),
                    sum(candle.confirmed for candle in source.candles),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO shadow_soak_run_events (
                    run_id,event_type,reason,created_at
                ) VALUES (?,?,?,?)
                """,
                (run_id, "started", None, now),
            )

    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_soak_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def resume_run(self, run_id: str) -> None:
        now = _now()
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE shadow_soak_runs
                SET status='running',resume_count=resume_count+1,updated_at=?,
                    interrupted_at=NULL,failure_reason=NULL
                WHERE run_id=? AND status IN ('interrupted','completed','failed')
                """,
                (now, run_id),
            ).rowcount
            if updated == 1:
                connection.execute(
                    """
                    INSERT INTO shadow_soak_run_events (
                        run_id,event_type,reason,created_at
                    ) VALUES (?,?,?,?)
                    """,
                    (run_id, "resumed", None, now),
                )
        if updated != 1:
            raise ShadowSoakError("run is not resumable")

    def last_processed(self, run_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM shadow_soak_processed_bars
                WHERE run_id=? ORDER BY bar_timestamp DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_checkpoint(self, run_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM shadow_soak_checkpoints
                WHERE run_id=? ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def persist_bar(
        self,
        *,
        run_id: str,
        processing_key: str,
        candle: Candle,
        signal_id: str,
        signal: Signal,
        proposal_id: str | None,
        strategy_state: dict[str, object],
        interrupt_after_signal_save: bool,
        bar_number: int,
    ) -> bool:
        state_json = json.dumps(strategy_state, ensure_ascii=False, sort_keys=True)
        state_hash = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        explanation = _signal_explanation(signal)
        now = _now()
        with self._bar_transaction() as connection:
            existing = connection.execute(
                """SELECT signal_id,proposal_id,strategy_state_hash
                FROM shadow_soak_processed_bars WHERE run_id=? AND processing_key=?""",
                (run_id, processing_key),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["signal_id"]) != signal_id
                    or existing["proposal_id"] != proposal_id
                    or str(existing["strategy_state_hash"]) != state_hash
                ):
                    raise ShadowSoakError("persisted bar conflicts with deterministic replay")
                return False
            self._inject("shadow_soak.signal.before_insert")
            connection.execute(
                """
                    INSERT OR IGNORE INTO shadow_soak_signals (
                        run_id,signal_id,processing_key,bar_timestamp,action,
                        explanation_json,created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                (
                    run_id,
                    signal_id,
                    processing_key,
                    candle.timestamp.isoformat(),
                    signal.action.value,
                    json.dumps(explanation, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            if interrupt_after_signal_save:
                raise ShadowSoakInterruption(
                    InterruptionPoint.AFTER_SIGNAL_SAVE_BEFORE_PROPOSAL_SAVE,
                    bar_number,
                )
            if proposal_id is not None:
                validate_shadow_proposal(
                    quantity=Decimal("0"),
                    notional=Decimal("0"),
                    submission_performed=0,
                    exchange_order_id=None,
                    capability_status=_CAPABILITY,
                    risk_status="blocked",
                    decision="blocked",
                    blockers=("shadow_only",),
                )
                self._inject("shadow_soak.proposal.before_insert")
                connection.execute(
                    """
                        INSERT OR IGNORE INTO shadow_soak_proposals (
                            run_id,proposal_id,signal_id,side,reference_price,
                            planned_price,quantity,notional,blockers_json,
                            submission_performed,exchange_order_id,capability_status,
                            risk_status,created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                    (
                        run_id,
                        proposal_id,
                        signal_id,
                        "buy",
                        str(candle.close),
                        str(candle.close),
                        "0",
                        "0",
                        '["shadow_only"]',
                        0,
                        None,
                        _CAPABILITY,
                        "blocked",
                        now,
                    ),
                )
            inserted = connection.execute(
                """
                    INSERT OR IGNORE INTO shadow_soak_processed_bars (
                        run_id,processing_key,bar_timestamp,confirmed,signal_id,
                        proposal_id,strategy_state_hash,strategy_state_json,processed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                (
                    run_id,
                    processing_key,
                    candle.timestamp.isoformat(),
                    int(candle.confirmed),
                    signal_id,
                    proposal_id,
                    state_hash,
                    state_json,
                    now,
                ),
            ).rowcount
            if inserted != 1:
                raise ShadowSoakError(f"duplicate processed candle identity: {processing_key}")
            connection.execute(
                """
                    UPDATE shadow_soak_runs
                    SET bars_processed=bars_processed+1,
                        bars_held=bars_held+?,
                        buy_signals=buy_signals+?,
                        signals_persisted=signals_persisted+1,
                        proposals_persisted=proposals_persisted+?,
                        last_processed_timestamp=?,updated_at=?
                    WHERE run_id=?
                    """,
                (
                    int(signal.action is SignalAction.HOLD),
                    int(signal.action is SignalAction.BUY),
                    int(proposal_id is not None),
                    candle.timestamp.isoformat(),
                    now,
                    run_id,
                ),
            )
        return True

    def save_checkpoint(self, run_id: str, strategy_state: dict[str, object]) -> bool:
        run = self.get_run(run_id)
        if run is None or run["last_processed_timestamp"] is None:
            raise ShadowSoakError("cannot checkpoint a run without processed candles")
        now = _now()
        with self._bar_transaction() as connection:
            self._inject("shadow_soak.checkpoint.before_insert")
            inserted = connection.execute(
                """
                    INSERT OR IGNORE INTO shadow_soak_checkpoints (
                        run_id,sequence,checkpoint_version,strategy_name,
                        strategy_config_hash,data_source_hash,candle_hash,bar_interval,
                        last_processed_timestamp,processed_count,window_json,
                        window_total_volume,window_weighted_total,signal_count,
                        proposal_count,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                (
                    run_id,
                    int(str(run["checkpoint_count"])) + 1,
                    CHECKPOINT_VERSION,
                    run["strategy_name"],
                    run["strategy_config_hash"],
                    run["data_source_hash"],
                    run["candle_hash"],
                    run["bar_interval"],
                    run["last_processed_timestamp"],
                    run["bars_processed"],
                    json.dumps(strategy_state, ensure_ascii=False, sort_keys=True),
                    strategy_state["total_volume"],
                    strategy_state["weighted_total"],
                    run["signals_persisted"],
                    run["proposals_persisted"],
                    now,
                    now,
                ),
            ).rowcount
            if inserted:
                connection.execute(
                    """
                        UPDATE shadow_soak_runs
                        SET checkpoint_count=checkpoint_count+1,updated_at=?
                        WHERE run_id=?
                        """,
                    (now, run_id),
                )
        return inserted == 1

    def mark_status(self, run_id: str, status: str, reason: str | None = None) -> None:
        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError(f"unsupported soak status: {status}")
        now = _now()
        completed_at = now if status == "completed" else None
        interrupted_at = now if status == "interrupted" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE shadow_soak_runs
                SET status=?,updated_at=?,completed_at=?,interrupted_at=?,
                    failure_reason=?
                WHERE run_id=?
                """,
                (status, now, completed_at, interrupted_at, reason, run_id),
            )
            connection.execute(
                """
                INSERT INTO shadow_soak_run_events (
                    run_id,event_type,reason,created_at
                ) VALUES (?,?,?,?)
                """,
                (run_id, status, reason, now),
            )

    def summary(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            raise ShadowSoakError(f"unknown soak run: {run_id}")
        with self.connect() as connection:
            submission_count = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(submission_performed),0)
                    FROM shadow_soak_proposals WHERE run_id=?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            interruption_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM shadow_soak_run_events
                    WHERE run_id=? AND event_type='interrupted'
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
        return {
            "run_id": run_id,
            "strategy": run["strategy_name"],
            "mode": run["mode"],
            "capability_status": run["capability_status"],
            "execution_status": run["execution_status"],
            "status": run["status"],
            "strategy_config_hash": run["strategy_config_hash"],
            "data_source_hash": run["data_source_hash"],
            "candle_hash": run["candle_hash"],
            "bar_interval": run["bar_interval"],
            "bars_received": run["bars_received"],
            "bars_confirmed": run["bars_confirmed"],
            "bars_processed": run["bars_processed"],
            "bars_held": run["bars_held"],
            "buy_signals": run["buy_signals"],
            "signals_persisted": run["signals_persisted"],
            "proposals_persisted": run["proposals_persisted"],
            "duplicate_bars_rejected": run["duplicate_bars_rejected"],
            "resume_count": run["resume_count"],
            "interruption_count": interruption_count,
            "checkpoint_count": run["checkpoint_count"],
            "last_processed_timestamp": run["last_processed_timestamp"],
            "submission_performed": submission_count,
            "broker_objects_created": 0,
            "broker_write_calls": 0,
            "external_network_calls": 0,
            "real_order_submitted": False,
            "demo_order_submitted": False,
            "bounded_demo_started": False,
        }


def run_vwap_shadow_soak(
    *,
    database_path: Path,
    output_dir: Path,
    config: RunConfig,
    source: SoakDataSource,
    bar_interval: str,
    checkpoint_every: int,
    stop_after_bars: int | None = None,
    resume_run_id: str | None = None,
    interruption: InterruptionPlan | None = None,
    fault_injector: FaultInjector | None = None,
) -> dict[str, object]:
    if checkpoint_every <= 0:
        raise ValueError("checkpoint_every must be greater than zero")
    if stop_after_bars is not None and stop_after_bars <= 0:
        raise ValueError("stop_after_bars must be greater than zero")
    normalized_interval = bar_interval.lower()
    if normalized_interval not in BAR_INTERVALS:
        raise MarketDataError(f"不支持的 K 线周期: {bar_interval}")
    store = ShadowSoakStore(database_path, fault_injector=fault_injector)
    existing_run: dict[str, object] | None = None
    if resume_run_id is not None:
        existing_run = store.get_run(resume_run_id)
        if existing_run is None:
            raise ShadowSoakError(f"resume run does not exist: {resume_run_id}")
        if existing_run["bar_interval"] != normalized_interval:
            raise ShadowSoakError("resume bar interval mismatch")
    candles = validate_soak_candles(source.candles, bar_interval=normalized_interval)
    if candles_hash(list(candles)) != source.candle_hash:
        raise ShadowSoakError("data source candle hash does not match its payload")
    instrument = _load_instrument(config)
    strategy_config_hash = _strategy_config_hash(
        config,
        bar_interval=normalized_interval,
    )
    run_id = resume_run_id or uuid4().hex

    strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
    if not isinstance(strategy, VWAPShadowStrategy):
        raise ShadowSoakError("VWAP Shadow soak created an unexpected strategy type")
    interval = BAR_INTERVALS[normalized_interval]
    clock = BacktestClock(candles[0].timestamp)
    empty_portfolio = PortfolioSnapshot({}, {}, {}, trusted_for_trading=False)
    strategy.on_start(
        StrategyContext(
            run_id,
            TradingMode.BACKTEST,
            strategy.name,
            instrument,
            normalized_interval,
            empty_portfolio,
            None,
            clock,
        )
    )

    start_index = 0
    if existing_run is not None:
        _validate_resume(
            existing_run,
            source=source,
            strategy_config_hash=strategy_config_hash,
            bar_interval=normalized_interval,
            candles=candles,
            store=store,
        )
        latest_checkpoint = store.latest_checkpoint(run_id)
        if latest_checkpoint is not None:
            _validate_checkpoint(
                latest_checkpoint,
                source=source,
                strategy_config_hash=strategy_config_hash,
                bar_interval=normalized_interval,
                candles=candles,
                strategy=strategy,
            )
            start_index = _checkpoint_resume_index(candles, latest_checkpoint)
            clock.advance_to(
                datetime.fromisoformat(str(latest_checkpoint["last_processed_timestamp"]))
                + interval
            )

    if resume_run_id is None:
        store.create_run(
            run_id=run_id,
            config=config,
            bar_interval=normalized_interval,
            strategy_config_hash=strategy_config_hash,
            source=source,
        )
    else:
        store.resume_run(run_id)

    current_run = store.get_run(run_id)
    if current_run is None:
        raise ShadowSoakError(f"run disappeared before processing: {run_id}")
    processed_total = int(str(current_run["bars_processed"]))
    store.open_runtime()
    try:
        for index in range(start_index, len(candles)):
            candle = candles[index]
            bar_number = index + 1
            _interrupt_if_requested(
                interruption,
                InterruptionPoint.BEFORE_STRATEGY,
                bar_number,
            )
            clock.advance_to(candle.timestamp + interval)
            context = StrategyContext(
                run_id,
                TradingMode.BACKTEST,
                strategy.name,
                instrument,
                normalized_interval,
                empty_portfolio,
                MarketSnapshot(candle, candle.close),
                clock,
            )
            signal = _single_signal(strategy.on_bar(context, candle))
            _interrupt_if_requested(
                interruption,
                InterruptionPoint.AFTER_STRATEGY_BEFORE_SIGNAL_SAVE,
                bar_number,
            )
            processing_key = _processing_key(
                strategy_name=strategy.name,
                instrument_id=instrument.instrument_id,
                bar_interval=normalized_interval,
                timestamp=candle.timestamp,
                strategy_config_hash=strategy_config_hash,
            )
            signal_id = hashlib.sha256(f"signal:{processing_key}".encode()).hexdigest()
            proposal_id = (
                hashlib.sha256(f"proposal:{signal_id}".encode()).hexdigest()
                if signal.action is SignalAction.BUY
                else None
            )
            state = strategy.checkpoint_state()
            persisted = store.persist_bar(
                run_id=run_id,
                processing_key=processing_key,
                candle=candle,
                signal_id=signal_id,
                signal=signal,
                proposal_id=proposal_id,
                strategy_state=state,
                interrupt_after_signal_save=_matches_interruption(
                    interruption,
                    InterruptionPoint.AFTER_SIGNAL_SAVE_BEFORE_PROPOSAL_SAVE,
                    bar_number,
                ),
                bar_number=bar_number,
            )
            if persisted:
                processed_total += 1
            _interrupt_if_requested(
                interruption,
                InterruptionPoint.AFTER_PROPOSAL_SAVE_BEFORE_CHECKPOINT,
                bar_number,
            )
            checkpoint_due = (
                persisted and processed_total % checkpoint_every == 0
            ) or index == len(candles) - 1
            if checkpoint_due:
                store.save_checkpoint(run_id, state)
                _interrupt_if_requested(
                    interruption,
                    InterruptionPoint.AFTER_CHECKPOINT,
                    bar_number,
                )
            if (
                stop_after_bars is not None
                and processed_total >= stop_after_bars
                and index < len(candles) - 1
            ):
                store.save_checkpoint(run_id, state)
                store.mark_status(run_id, "interrupted", "stop_after_bars")
                summary = store.summary(run_id)
                _write_summary(output_dir, summary)
                return summary

        store.mark_status(run_id, "completed")
        summary = store.summary(run_id)
        _write_summary(output_dir, summary)
        return summary
    except ShadowSoakInterruption as exc:
        store.mark_status(run_id, "interrupted", str(exc))
        _write_summary(output_dir, store.summary(run_id))
        raise
    except Exception as exc:
        store.mark_status(run_id, "failed", str(exc))
        _write_summary(output_dir, store.summary(run_id))
        raise
    finally:
        store.close_runtime()


def read_soak_snapshot(database_path: Path, run_id: str) -> dict[str, object]:
    store = ShadowSoakStore(database_path)
    run = store.get_run(run_id)
    if run is None:
        raise ShadowSoakError(f"unknown soak run: {run_id}")
    with store.connect() as connection:
        signals = [
            dict(row)
            for row in connection.execute(
                """
                SELECT signal_id,processing_key,bar_timestamp,action,explanation_json
                FROM shadow_soak_signals WHERE run_id=? ORDER BY bar_timestamp
                """,
                (run_id,),
            ).fetchall()
        ]
        proposals = [
            dict(row)
            for row in connection.execute(
                """
                SELECT proposal_id,signal_id,side,reference_price,planned_price,
                       quantity,notional,blockers_json,submission_performed,
                       exchange_order_id,capability_status,risk_status
                FROM shadow_soak_proposals WHERE run_id=? ORDER BY proposal_id
                """,
                (run_id,),
            ).fetchall()
        ]
    last_processed = store.last_processed(run_id)
    latest_checkpoint = store.latest_checkpoint(run_id)
    return {
        "status": run["status"],
        "bars_processed": run["bars_processed"],
        "bars_held": run["bars_held"],
        "buy_signals": run["buy_signals"],
        "signals": signals,
        "proposals": proposals,
        "last_processed_timestamp": run["last_processed_timestamp"],
        "strategy_state": (
            _decode_state(str(last_processed["strategy_state_json"]))
            if last_processed is not None
            else None
        ),
        "checkpoint": (
            {
                "processed_count": latest_checkpoint["processed_count"],
                "last_processed_timestamp": latest_checkpoint["last_processed_timestamp"],
                "window_json": json.loads(str(latest_checkpoint["window_json"])),
                "signal_count": latest_checkpoint["signal_count"],
                "proposal_count": latest_checkpoint["proposal_count"],
            }
            if latest_checkpoint is not None
            else None
        ),
    }


def _load_instrument(config: RunConfig) -> Instrument:
    if config.strategy.name != _STRATEGY_NAME:
        raise ShadowSoakError(f"soak requires strategy={_STRATEGY_NAME}")
    snapshot_path = config.data.instrument_snapshot
    if snapshot_path is None:
        raise ShadowSoakError("soak requires a local instrument snapshot")
    instrument = InstrumentSnapshotStore.load(snapshot_path).instrument
    if instrument.instrument_id != config.market.instrument_id:
        raise ShadowSoakError("instrument snapshot does not match soak configuration")
    return instrument


def _strategy_config_hash(config: RunConfig, *, bar_interval: str) -> str:
    return canonical_hash(
        {
            "strategy_name": config.strategy.name,
            "strategy_parameters": config.strategy.parameters,
            "instrument_id": config.market.instrument_id,
            "bar_interval": bar_interval,
        }
    )


def _processing_key(
    *,
    strategy_name: str,
    instrument_id: str,
    bar_interval: str,
    timestamp: datetime,
    strategy_config_hash: str,
) -> str:
    return canonical_hash(
        {
            "strategy_name": strategy_name,
            "instrument_id": instrument_id,
            "bar_interval": bar_interval,
            "bar_timestamp": timestamp.astimezone(UTC).isoformat(),
            "strategy_config_hash": strategy_config_hash,
        }
    )


def _checkpoint_resume_index(candles: tuple[Candle, ...], checkpoint: dict[str, object]) -> int:
    timestamp = datetime.fromisoformat(str(checkpoint["last_processed_timestamp"]))
    for index, candle in enumerate(candles):
        if candle.timestamp == timestamp:
            return index + 1
    raise ShadowSoakError("checkpoint timestamp cannot be located in the data source")


def _validate_resume(
    run: dict[str, object],
    *,
    source: SoakDataSource,
    strategy_config_hash: str,
    bar_interval: str,
    candles: tuple[Candle, ...],
    store: ShadowSoakStore,
) -> None:
    if run["strategy_name"] != _STRATEGY_NAME:
        raise ShadowSoakError("resume strategy name mismatch")
    if run["strategy_config_hash"] != strategy_config_hash:
        raise ShadowSoakError("resume strategy configuration hash mismatch")
    if run["bar_interval"] != bar_interval:
        raise ShadowSoakError("resume bar interval mismatch")
    if run["data_source_hash"] != source.identity_hash or run["candle_hash"] != source.candle_hash:
        raise ShadowSoakError("resume data source identity mismatch")
    if int(str(run["checkpoint_version"])) != CHECKPOINT_VERSION:
        raise ShadowSoakError("resume checkpoint version is unsupported")
    if run["status"] not in {"interrupted", "completed", "failed"}:
        raise ShadowSoakError("resume requires an interrupted, completed, or failed run")
    last_processed = store.last_processed(str(run["run_id"]))
    resume_index = _resume_index(candles, last_processed)
    if int(str(run["bars_processed"])) != resume_index:
        raise ShadowSoakError("resume processed count does not match the durable cursor")


def _validate_checkpoint(
    checkpoint: dict[str, object],
    *,
    source: SoakDataSource,
    strategy_config_hash: str,
    bar_interval: str,
    candles: tuple[Candle, ...],
    strategy: VWAPShadowStrategy,
) -> None:
    if checkpoint["strategy_name"] != _STRATEGY_NAME:
        raise ShadowSoakError("checkpoint strategy name mismatch")
    if checkpoint["strategy_config_hash"] != strategy_config_hash:
        raise ShadowSoakError("checkpoint strategy configuration hash mismatch")
    if checkpoint["data_source_hash"] != source.identity_hash:
        raise ShadowSoakError("checkpoint data source identity mismatch")
    if checkpoint["candle_hash"] != source.candle_hash:
        raise ShadowSoakError("checkpoint candle hash mismatch")
    if checkpoint["bar_interval"] != bar_interval:
        raise ShadowSoakError("checkpoint bar interval mismatch")
    if int(str(checkpoint["checkpoint_version"])) != CHECKPOINT_VERSION:
        raise ShadowSoakError("checkpoint version is unsupported")
    timestamp = datetime.fromisoformat(str(checkpoint["last_processed_timestamp"]))
    matching = [index for index, candle in enumerate(candles) if candle.timestamp == timestamp]
    if len(matching) != 1 or matching[0] + 1 != int(str(checkpoint["processed_count"])):
        raise ShadowSoakError("checkpoint timestamp cannot be located in the data source")
    strategy.restore_checkpoint_state(_decode_state(str(checkpoint["window_json"])))


def _resume_index(
    candles: tuple[Candle, ...],
    last_processed: dict[str, object] | None,
) -> int:
    if last_processed is None:
        return 0
    timestamp = datetime.fromisoformat(str(last_processed["bar_timestamp"]))
    matching = [index for index, candle in enumerate(candles) if candle.timestamp == timestamp]
    if len(matching) != 1:
        raise ShadowSoakError("last processed timestamp cannot be located in the data source")
    return matching[0] + 1


def _signal_explanation(signal: Signal) -> dict[str, object]:
    return {
        "close": str(signal.metadata["close"]),
        "vwap": (str(signal.metadata["vwap"]) if signal.metadata["vwap"] is not None else None),
        "deviation_bps": (
            str(signal.metadata["deviation_bps"])
            if signal.metadata["deviation_bps"] is not None
            else None
        ),
        "vwap_window": signal.metadata["vwap_window"],
        "window_length": signal.metadata["window_length"],
        "reason": signal.reason,
    }


def _single_signal(signals: list[Signal]) -> Signal:
    if len(signals) != 1:
        raise ShadowSoakError("VWAP Shadow strategy must return exactly one signal")
    signal = signals[0]
    if signal.action not in {SignalAction.BUY, SignalAction.HOLD}:
        raise ShadowSoakError("VWAP Shadow strategy emitted an unsupported action")
    return signal


def _matches_interruption(
    plan: InterruptionPlan | None,
    point: InterruptionPoint,
    bar_number: int,
) -> bool:
    return plan is not None and plan.point is point and plan.bar_number == bar_number


def _interrupt_if_requested(
    plan: InterruptionPlan | None,
    point: InterruptionPoint,
    bar_number: int,
) -> None:
    if _matches_interruption(plan, point, bar_number):
        raise ShadowSoakInterruption(point, bar_number)


def _decode_state(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ShadowSoakError("stored VWAP strategy state is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ShadowSoakError("stored VWAP strategy state must be an object")
    return value


def _reject_csv_duplicate_timestamps(path: Path) -> None:
    if not path.is_file():
        raise MarketDataError(f"K 线文件不存在: {path}")
    seen: set[datetime] = set()
    try:
        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames or "timestamp" not in reader.fieldnames:
                return
            for row in reader:
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None:
                    raise MarketDataError("K 线时间必须包含时区")
                normalized = timestamp.astimezone(UTC)
                if normalized in seen:
                    raise MarketDataError(f"重复 K 线时间戳: {normalized.isoformat()}")
                seen.add(normalized)
    except (KeyError, ValueError) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError(f"CSV K 线时间戳格式错误: {exc}") from exc


def _write_summary(output_dir: Path, summary: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{summary['run_id']}.summary.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
