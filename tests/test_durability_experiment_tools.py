"""Tests for the replay-scoped durability experiment tooling.

Verifies the benchmark-only replay factory override, default production
behavior unchanged, FULL/NORMAL count
equivalence on the real replay workload, exception rollback under both modes,
and process-termination semantics. No timing assertions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.market.historical_data import save_candles_csv
from app.market.synthetic_candles import SyntheticCandleRequest, generate_synthetic_candles
from app.storage.database import Database
from benchmarks.durability_experiment import (
    FULL,
    NORMAL,
    _replay_runner,
    _seed_active_generation,
    experiment_sqlite,
    process_termination_experiment,
)
from benchmarks.persistence_metrics import PersistenceMetrics


def test_injection_sets_and_verifies_effective_synchronous(tmp_path: Path) -> None:
    for mode, expected in ((FULL, 2), (NORMAL, 1)):
        database = Database(f"sqlite:///{tmp_path / f'probe-{mode}.db'}")
        database.initialize()
        metrics = PersistenceMetrics()
        with experiment_sqlite(metrics, mode):
            connection = database.open_reconstructible_replay_connection()
            effective = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            connection.commit()
            connection.close()
        assert effective == expected
        assert metrics.extra["effective_synchronous_verified"] is True
        assert metrics.extra["effective_synchronous"] == ("FULL" if mode == FULL else "NORMAL")
    # The benchmark override is gone: formal replay returns to NORMAL.
    replay = database.open_reconstructible_replay_connection()
    assert int(replay.execute("PRAGMA synchronous").fetchone()[0]) == 1
    replay.close()


def test_production_default_stays_full_without_wrapper(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'prod.db'}")
    database.initialize()
    with database.connect() as connection:
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    assert synchronous == 2, "production Database.connect must remain FULL by default"
    assert journal == "wal"


@pytest.mark.parametrize("mode", [FULL, NORMAL], ids=["full", "normal"])
def test_replay_counts_and_results_identical_across_modes(tmp_path: Path, mode: int) -> None:
    runner = _replay_runner(120)
    metrics = PersistenceMetrics()
    result = runner(metrics, mode)

    assert result["candles"] == 120
    assert metrics.extra["effective_synchronous_verified"] is True
    # Structural counts are mode-independent; the golden reference comes from
    # the converged canonical path (Phase 2B1 after benchmark).
    counts = metrics.snapshot_counts()
    # Since Phase 2B3 the replay holds one scoped session connection; only the
    # handful of run-lifecycle blocks open their own connections.
    assert counts["connections_opened"] <= 8
    assert counts["commit_calls"] == 125  # 120 candle transactions + 5 run-level blocks
    assert counts["sql_writes"] == 621
    assert counts["table_writes"]["processed_candles"] == 120
    assert counts["table_writes"]["strategy_signal_events"] == 120


class _ExplodeAfterWrite:
    def __init__(self) -> None:
        self.fired = False

    def inject(self, point: str) -> None:
        if point == "continuous_shadow.after_signal" and not self.fired:
            self.fired = True
            raise RuntimeError("injected mid-transaction failure")


@pytest.mark.parametrize("mode", [FULL, NORMAL], ids=["full", "normal"])
def test_exception_before_commit_rolls_back_under_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    import app.services.shadow_replay as shadow_replay_module
    from app.services.continuous_shadow_repository import ContinuousShadowRepository
    from app.services.shadow_replay import run_shadow_replay

    database = Database(f"sqlite:///{tmp_path / 'rollback.db'}")
    database.initialize()
    _seed_active_generation(database.path)
    original = ContinuousShadowRepository
    injector = _ExplodeAfterWrite()

    def with_injector(db: Database) -> ContinuousShadowRepository:
        return original(db, fault_injector=injector)  # type: ignore[arg-type]

    monkeypatch.setattr(shadow_replay_module, "ContinuousShadowRepository", with_injector)
    csv_path = tmp_path / "candles.csv"
    save_candles_csv(
        generate_synthetic_candles(
            SyntheticCandleRequest(count=30, seed=20260731, bar_interval="1h")
        ),
        csv_path,
    )
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})

    metrics = PersistenceMetrics()
    with experiment_sqlite(metrics, mode), pytest.raises(RuntimeError, match="injected"):
        run_shadow_replay(database, config, csv_path, 30)

    assert injector.fired
    with sqlite3.connect(database.path) as connection:
        for table in ("processed_candles", "strategy_signal_events"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} must be empty after rollback ({mode=})"


def test_process_termination_semantics_before_and_after_commit() -> None:
    results = process_termination_experiment()
    by_key = {(entry["variant"], entry["phase"]): entry for entry in results}
    for variant in ("FULL", "NORMAL"):
        before = by_key[(variant, "before_commit")]
        after = by_key[(variant, "after_commit")]
        assert before["database_consistent"] and after["database_consistent"]
        assert before["probe_rows_after_kill"] == 0
        assert before["partial_transaction_present"] is False
        assert after["probe_rows_after_kill"] == 2
        assert after["committed_transaction_present"] is True
