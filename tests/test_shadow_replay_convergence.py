"""Phase 2B1 convergence tests: legacy shadow replay → canonical atomic commit.

Covers behavior equivalence against a golden snapshot captured from the legacy
persistence sequence (the legacy repository methods still exist and serve the
continuous/bounded engines), per-candle transaction structure, failure
injection, duplicate handling, and the one documented intentional difference
(``current_relation``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import app.services.shadow_replay as shadow_replay_module
from app.config.run_config import RunConfig, load_run_config
from app.domain.market import Candle
from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.services.shadow_replay import run_shadow_replay
from app.storage.database import Database, StorageError
from benchmarks.persistence_metrics import PersistenceMetrics, instrumented_sqlite
from tests.migration_fakes import _now

_FIXTURE = Path("tests/fixtures/vwap/btc_usdt_1h_live.csv")
_CANDLES = 119

# Golden digests from the legacy per-method persistence sequence
# (claim_candle → save_runtime → save_signal → save_proposal → heartbeat),
# captured before convergence. Projections exclude volatile columns (uuid ids,
# timestamps) and, for signal_events, the intentionally corrected
# current_relation column (legacy stored str(vwap) — including the string
# "None" during warmup — in a column designed for fast/slow relations; the
# vwap value itself is preserved verbatim inside signal_value JSON).
_GOLDEN = {
    "processed_candles": "c7c28c05bd21e74cdf4638d40b842e2e41a3e9d5ab8e861cf74b6394824553bd",
    "runtime_states": "637bd34004b1a498bc90a6ab9d66acbfd0d13ab76206141e547ef9c73223dc3e",
    "signal_events_reduced": "ad1d1079c70770584b52e440586bea86a3bb3ccb0c947364ce8ea7c01bb38a3b",
    "proposals": "2fe45554f671ae20fd7fea7cbdc3d44cf00bf94b190b8e93b6ddf77a61e42c3c",
    "proposal_events": "6595fb10c55ea2f19fa7d18f6de785b86dfe52bc8226ced0d88b190106337fdc",
    "run": "0366e8c61d137d11194431e01b8e3d974efff70b5e30606000646f96b03cd85e",
}

_QUERIES = {
    "processed_candles": (
        "SELECT instrument_id,timeframe,candle_open_time,candle_close_time,is_confirmed,"
        "market_data_source,strategy_version FROM processed_candles ORDER BY candle_open_time"
    ),
    "runtime_states": (
        "SELECT strategy_name,strategy_version,timeframe,last_candle_open_time,"
        "previous_fast_value,previous_slow_value,previous_relation,last_signal_type,"
        "last_signal_candle_time,warmup_completed,warmup_candle_count,state_json"
        " FROM strategy_runtime_states"
    ),
    "signal_events_reduced": (
        "SELECT instrument_id,candle_open_time,signal_type,signal_value,decision,"
        "blockers_json,strategy_name,strategy_version,timeframe,previous_relation,"
        "warnings_json FROM strategy_signal_events ORDER BY candle_open_time"
    ),
    "proposals": (
        "SELECT instrument_id,side,order_type,reference_price,planned_price,quantity,"
        "notional,estimated_fee,inventory_scope,blockers_json,warnings_json,"
        "submission_performed,exchange_order_id,instrument_type,trade_mode,decision,"
        "is_shadow,capability_status,risk_status FROM shadow_order_proposals"
    ),
    "proposal_events": "SELECT event_type,reason FROM shadow_order_proposal_events",
    "run": (
        "SELECT strategy_name,instrument_id,timeframe,status,mode,processed_candle_count,"
        "generated_signal_count,shadow_proposal_count,private_stream_status,"
        "public_stream_status,stop_reason FROM continuous_demo_runs"
    ),
}


class _ReplayEnvironment:
    def __init__(self, tmp_path: Path, name: str = "replay.db") -> None:
        self.database = Database(f"sqlite:///{tmp_path / name}")
        self.database.initialize()
        with sqlite3.connect(self.database.path) as connection:
            connection.execute(
                """INSERT INTO runtime_generations(generation_id,generation_number,
                status,created_at,activated_at,manifest_sha256,database_sha256_before,
                authorization_json,notes) VALUES ('gen-replay-test',1,'active',?,'',
                'test','test','{}','convergence test')""",
                (_now(),),
            )
            connection.commit()
        self.config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})

    def run(self, maximum: int = _CANDLES) -> dict[str, object]:
        return run_shadow_replay(self.database, self.config, _FIXTURE, maximum)

    def counts(self) -> dict[str, int]:
        with sqlite3.connect(self.database.path) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "processed_candles",
                    "strategy_signal_events",
                    "shadow_order_proposals",
                    "strategy_runtime_states",
                )
            }

    def digest(self, projection: str) -> tuple[int, str]:
        with sqlite3.connect(self.database.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(_QUERIES[projection])]
        return len(rows), hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()
        ).hexdigest()


def test_converged_replay_matches_legacy_golden_snapshot(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    result = environment.run()

    assert result["processed_candles"] == _CANDLES
    assert result["entry_signals"] == 1
    assert result["shadow_proposals"] == 1
    for projection, expected in _GOLDEN.items():
        rows, digest = environment.digest(projection)
        assert digest == expected, projection
        expected_rows = (
            _CANDLES
            if projection
            in (
                "processed_candles",
                "signal_events_reduced",
            )
            else 1
        )
        assert rows == expected_rows, projection


def test_current_relation_difference_is_intentional_and_information_preserved(
    tmp_path: Path,
) -> None:
    """Legacy stuffed str(vwap) into the relation column; canonical keeps 'vwap'."""
    environment = _ReplayEnvironment(tmp_path)
    environment.run(maximum=30)
    with sqlite3.connect(environment.database.path) as connection:
        relations = {
            str(row[0])
            for row in connection.execute("SELECT current_relation FROM strategy_signal_events")
        }
        vwap_values = [
            json.loads(str(row[0])).get("vwap")
            for row in connection.execute("SELECT signal_value FROM strategy_signal_events")
        ]
    assert relations == {"vwap"}
    assert any(value is not None for value in vwap_values), (
        "vwap values must remain preserved inside signal_value JSON"
    )


def test_replay_uses_one_connection_and_commit_per_candle(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    metrics = PersistenceMetrics()
    with instrumented_sqlite(metrics):
        result = environment.run()

    candles = int(str(result["processed_candles"]))
    assert candles == _CANDLES
    assert metrics.connections_opened / candles <= 1.2
    assert metrics.commit_calls / candles <= 1.2
    # Every candle write set still happens (no audit/state dropped).
    assert metrics.table_writes["processed_candles"] == candles
    assert metrics.table_writes["strategy_signal_events"] == candles
    assert metrics.table_writes["strategy_runtime_states"] == candles
    assert metrics.table_writes["continuous_demo_runs"] >= candles


_BUY_TIMESTAMP = "2026-07-22T12:00:00+00:00"


class _PointFailureInjector:
    is_local_adapter = True

    def __init__(self, point: str) -> None:
        self.point = point

    def inject(self, injection_point: str) -> None:
        if injection_point == self.point:
            raise RuntimeError(f"injected failure at {injection_point}")


@pytest.mark.parametrize(
    ("injection_point", "surviving_bars"),
    [
        ("continuous_shadow.after_processed_identity", 0),  # fails on bar 1
        ("continuous_shadow.after_signal", 0),  # fails on bar 1
        ("continuous_shadow.after_proposal", 115),  # fails on the buy bar
    ],
    ids=["after_processed_write", "after_signal_write", "after_proposal_write"],
)
def test_candle_transaction_rolls_back_completely_on_injected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injection_point: str,
    surviving_bars: int,
) -> None:
    environment = _ReplayEnvironment(tmp_path)
    original = ContinuousShadowRepository

    def repository_with_injector(database: Database) -> ContinuousShadowRepository:
        return original(database, fault_injector=_PointFailureInjector(injection_point))  # type: ignore[arg-type]

    monkeypatch.setattr(
        shadow_replay_module, "ContinuousShadowRepository", repository_with_injector
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        environment.run()

    counts = environment.counts()
    assert counts["processed_candles"] == surviving_bars
    assert counts["strategy_signal_events"] == surviving_bars
    assert counts["shadow_order_proposals"] == 0, "the failing buy bar rolled back fully"
    assert counts["strategy_runtime_states"] == (1 if surviving_bars else 0)
    with sqlite3.connect(environment.database.path) as connection:
        failed_bar = connection.execute(
            "SELECT COUNT(*) FROM processed_candles WHERE candle_open_time=?",
            (_BUY_TIMESTAMP,),
        ).fetchone()[0]
        failed_signal = connection.execute(
            "SELECT COUNT(*) FROM strategy_signal_events WHERE candle_open_time=?",
            (_BUY_TIMESTAMP,),
        ).fetchone()[0]
    assert failed_bar == 0, "the failing candle must not be persisted at all"
    assert failed_signal == 0


def test_database_locked_fails_closed_without_partial_state(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    environment.run(maximum=10)
    before = environment.counts()
    repository = ContinuousShadowRepository(environment.database)
    candle = _unclaimed_candle()

    lock = sqlite3.connect(environment.database.path)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageError):
            repository.commit_vwap_shadow_candle(
                run_id=_existing_run_id(environment.database.path),
                config=_shadow_config(environment.config),
                candle=candle,
                strategy_version="vwap_shadow_v1",
                signal_id="bench-signal",
                signal_type="hold",
                signal_value="{}",
                runtime_state="{}",
                warmup_count=24,
                warmup_completed=True,
                proposal_price=None,
                processed_count=11,
                signal_count=1,
                proposal_count=1,
            )
    finally:
        lock.rollback()
        lock.close()

    assert environment.counts() == before, "locked commit must leave zero partial state"


def test_duplicate_candle_commit_is_idempotent(tmp_path: Path) -> None:
    environment = _ReplayEnvironment(tmp_path)
    environment.run(maximum=10)
    repository = ContinuousShadowRepository(environment.database)
    run_id = _existing_run_id(environment.database.path)
    config = _shadow_config(environment.config)
    candle = _unclaimed_candle()

    def commit() -> bool:
        return repository.commit_vwap_shadow_candle(
            run_id=run_id,
            config=config,
            candle=candle,
            strategy_version="vwap_shadow_v1",
            signal_id="dup-signal",
            signal_type="hold",
            signal_value="{}",
            runtime_state="{}",
            warmup_count=24,
            warmup_completed=True,
            proposal_price=None,
            processed_count=99,
            signal_count=99,
            proposal_count=99,
        )

    assert commit() is True
    before = environment.counts()
    assert commit() is False
    assert environment.counts() == before, "duplicate replay must not duplicate any row"

    with sqlite3.connect(environment.database.path) as connection:
        heartbeat = connection.execute(
            "SELECT processed_candle_count FROM continuous_demo_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    assert heartbeat == 99, "second commit must not touch run counters"


def test_replaying_fixture_twice_creates_two_clean_independent_runs(
    tmp_path: Path,
) -> None:
    environment = _ReplayEnvironment(tmp_path)
    first = environment.run()
    second = environment.run()

    assert first["processed_candles"] == second["processed_candles"] == _CANDLES
    assert first["entry_signals"] == second["entry_signals"] == 1
    with sqlite3.connect(environment.database.path) as connection:
        runs = connection.execute("SELECT COUNT(*) FROM continuous_demo_runs").fetchone()[0]
        processed = connection.execute("SELECT COUNT(*) FROM processed_candles").fetchone()[0]
        signals = connection.execute("SELECT COUNT(*) FROM strategy_signal_events").fetchone()[0]
    assert runs == 2
    assert processed == 2 * _CANDLES
    assert signals == 2 * _CANDLES


def _unclaimed_candle(index: int = 10) -> Candle:
    from app.market.historical_data import load_candles_csv

    return load_candles_csv(_FIXTURE, bar="1h")[index]


def _existing_run_id(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        return str(
            connection.execute(
                "SELECT run_id FROM continuous_demo_runs ORDER BY started_at LIMIT 1"
            ).fetchone()[0]
        )


class _ShadowConfig:
    def __init__(self, config: RunConfig) -> None:
        self.strategy_name = config.strategy.name
        self.instrument_id = config.market.instrument_id
        self.timeframe = config.market.bar


def _shadow_config(config: RunConfig) -> _ShadowConfig:
    return _ShadowConfig(config)
