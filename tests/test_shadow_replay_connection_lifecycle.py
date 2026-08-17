"""Phase 2B3 connection-lifecycle tests for the shadow replay session.

Structural assertions only (no wall-clock): one scoped connection per replay,
one transaction per candle, no nesting, clean rollback recovery, fail-closed
locking, single close, and stable PRAGMA configuration.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.domain.market import Candle
from app.services.continuous_shadow_repository import (
    ContinuousShadowRepository,
)
from app.services.shadow_replay import run_shadow_replay
from app.storage.database import Database, StorageError
from benchmarks.persistence_metrics import PersistenceMetrics, instrumented_sqlite
from tests.migration_fakes import _now

_FIXTURE = Path("tests/fixtures/vwap/btc_usdt_1h_live.csv")


class _ReplayEnvironment:
    def __init__(self, tmp_path: Path, name: str = "replay.db") -> None:
        self.database = Database(f"sqlite:///{tmp_path / name}")
        self.database.initialize()
        with sqlite3.connect(self.database.path) as connection:
            connection.execute(
                """INSERT INTO runtime_generations(generation_id,generation_number,
                status,created_at,activated_at,manifest_sha256,database_sha256_before,
                authorization_json,notes) VALUES ('gen-lifecycle',1,'active',?,?,'t','t',
                '{}','connection lifecycle test')""",
                (_now(), _now()),
            )
            connection.commit()
        self.config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})

    def run(self, maximum: int = 119) -> dict[str, object]:
        return run_shadow_replay(self.database, self.config, _FIXTURE, maximum)

    def counts(self, table: str) -> int:
        with sqlite3.connect(self.database.path) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class _CandleConfig:
    strategy_name = "vwap_shadow"
    instrument_id = "BTC-USDT"
    timeframe = "1h"


def _first_candle() -> list[Candle]:
    from app.market.historical_data import load_candles_csv

    return load_candles_csv(_FIXTURE, bar="1h")


def _commit_kwargs(candle: Candle, index: int = 1) -> dict[str, object]:
    return {
        "run_id": "lifecycle-test-run",
        "config": _CandleConfig(),
        "candle": candle,
        "strategy_version": "vwap_shadow_v1",
        "signal_id": f"lifecycle-signal-{index}",
        "signal_type": "hold",
        "signal_value": "{}",
        "runtime_state": "{}",
        "warmup_count": 24,
        "warmup_completed": True,
        "proposal_price": None,
        "processed_count": index,
        "signal_count": 0,
        "proposal_count": 0,
        "market_data_source": "local_csv_shadow_replay",
        "private_stream_status": "not_applicable",
        "public_stream_status": "local_replay",
    }


def _seed_run(database_path: Path, run_id: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO continuous_demo_runs (run_id,strategy_name,instrument_id,
            timeframe,status,mode,configuration_hash,started_at,reconciliation_status,
            private_stream_status,public_stream_status,circuit_breaker_status,generation_id)
            VALUES (?, 'vwap_shadow','BTC-USDT','1h','warming_up','shadow','t',?,
            'unknown','unknown','starting','continue','gen-lifecycle')""",
            (run_id, _now()),
        )
        connection.commit()


def test_replay_uses_one_scoped_connection_and_keeps_one_commit_per_candle(
    tmp_path: Path,
) -> None:
    environment = _ReplayEnvironment(tmp_path)
    metrics = PersistenceMetrics()
    with instrumented_sqlite(metrics):
        result = environment.run()

    candles = int(str(result["processed_candles"]))
    assert candles == 119
    assert metrics.commit_calls / candles <= 1.10
    assert metrics.commit_calls / candles >= 0.95
    # One dedicated persistence connection for the whole run plus a handful of
    # run-lifecycle connections (create_run, generation check, lock, finish...).
    assert metrics.connections_opened <= 8
    assert metrics.connections_opened / candles <= 0.07
    assert metrics.table_writes["processed_candles"] == candles
    assert metrics.table_writes["strategy_signal_events"] == candles
    assert metrics.table_writes["strategy_runtime_states"] == candles


def test_no_transaction_left_open_between_candles(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)
    _seed_run(environment.database.path, "lifecycle-test-run")
    candles = _first_candle()

    with repository.replay_session() as session:
        assert session.commit_vwap_shadow_candle(**_commit_kwargs(candles[0], 1)) is True
        assert session.transaction_open is False
        assert session.commit_vwap_shadow_candle(**_commit_kwargs(candles[1], 2)) is True
        assert session.transaction_open is False

    assert environment.counts("processed_candles") == 2


def test_nested_transaction_is_rejected(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)
    _seed_run(environment.database.path, "lifecycle-test-run")
    candle = _first_candle()[0]

    with repository.replay_session() as session:
        connection = session._require_open()
        connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(RuntimeError, match="must not nest"):
                session.commit_vwap_shadow_candle(**_commit_kwargs(candle, 1))
        finally:
            connection.rollback()
        assert session.transaction_open is False


def test_rollback_leaves_scoped_connection_reusable(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    _seed_run(environment.database.path, "lifecycle-test-run")
    candles = _first_candle()

    class _FailOnceOnSignal:
        def __init__(self) -> None:
            self.fired = False

        def inject(self, point: str) -> None:
            if point == "continuous_shadow.after_signal" and not self.fired:
                self.fired = True
                raise RuntimeError("injected signal failure")

    failing = ContinuousShadowRepository(environment.database, fault_injector=_FailOnceOnSignal())  # type: ignore[arg-type]
    with failing.replay_session() as session:
        with pytest.raises(RuntimeError, match="injected"):
            session.commit_vwap_shadow_candle(**_commit_kwargs(candles[0], 1))
        assert session.transaction_open is False
        assert session.commit_vwap_shadow_candle(**_commit_kwargs(candles[1], 2)) is True

    assert environment.counts("processed_candles") == 1
    with sqlite3.connect(environment.database.path) as connection:
        persisted = connection.execute("SELECT candle_open_time FROM processed_candles").fetchall()
    assert persisted == [(candles[1].timestamp.isoformat(),)]


def test_session_closes_exactly_once_on_all_exit_paths(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)
    _seed_run(environment.database.path, "lifecycle-test-run")

    session = repository.replay_session()
    with session:
        connection = session._require_open()
    with pytest.raises(RuntimeError, match="not open"):
        session._require_open()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    session.__exit__(None, None, None)  # second exit is a safe no-op

    with pytest.raises(KeyboardInterrupt), repository.replay_session() as session:
        inner = session._require_open()
        raise KeyboardInterrupt
    with pytest.raises(sqlite3.ProgrammingError):
        inner.execute("SELECT 1")


def test_locked_session_fails_closed_and_recovers_after_release(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)
    _seed_run(environment.database.path, "lifecycle-test-run")
    candles = _first_candle()

    lock = sqlite3.connect(environment.database.path)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with repository.replay_session() as session:
            with pytest.raises(StorageError):
                session.commit_vwap_shadow_candle(**_commit_kwargs(candles[0], 1))
            assert session.transaction_open is False
            lock.rollback()
            assert session.commit_vwap_shadow_candle(**_commit_kwargs(candles[0], 1)) is True
    finally:
        lock.close()

    assert environment.counts("processed_candles") == 1


def test_unexpected_connection_close_fails_without_silent_reconnect(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)
    _seed_run(environment.database.path, "lifecycle-test-run")
    candle = _first_candle()[0]

    with repository.replay_session() as session:
        connection = session._require_open()
        connection.close()
        with pytest.raises(StorageError):
            session.commit_vwap_shadow_candle(**_commit_kwargs(candle, 1))
    assert environment.counts("processed_candles") == 0


def test_pragma_configuration_holds_for_session_lifetime(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)
    _seed_run(environment.database.path, "lifecycle-test-run")
    candles = _first_candle()

    def effective(connection: sqlite3.Connection) -> tuple[str, str, str]:
        return (
            str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            str(connection.execute("PRAGMA synchronous").fetchone()[0]),
            str(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
        )

    with repository.replay_session() as session:
        connection = session._require_open()
        assert effective(connection) == ("wal", "1", "1")
        session.commit_vwap_shadow_candle(**_commit_kwargs(candles[0], 1))
        assert effective(connection) == ("wal", "1", "1")
        session.commit_vwap_shadow_candle(**_commit_kwargs(candles[1], 2))
        assert effective(connection) == ("wal", "1", "1")


def test_scoped_connection_is_bound_to_its_thread(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    repository = ContinuousShadowRepository(environment.database)

    with repository.replay_session() as session:
        connection = session._require_open()
        errors: list[Exception] = []

        def use_from_other_thread() -> None:
            try:
                connection.execute("SELECT 1")
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=use_from_other_thread)
        thread.start()
        thread.join()
        assert errors and isinstance(errors[0], sqlite3.ProgrammingError)
        # the owning thread keeps working
        connection.execute("SELECT 1").fetchall()
