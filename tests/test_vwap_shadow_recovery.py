from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from app.config.run_config import RunConfig, load_run_config
from app.market.synthetic_candles import SyntheticCandleRequest
from app.services.vwap_shadow_soak import (
    InterruptionPlan,
    InterruptionPoint,
    ShadowSoakError,
    ShadowSoakInterruption,
    ShadowSoakStore,
    SoakDataSource,
    build_synthetic_soak_source,
    read_soak_snapshot,
    run_vwap_shadow_soak,
)
from app.storage.database import StorageError
from app.testing.fault_injection import (
    FaultAction,
    FaultInjector,
    FaultPlan,
    FaultStep,
    VirtualClock,
)

_CONFIG = Path("configs/btc_vwap_shadow.yaml")


@dataclass(frozen=True)
class RecoveryBaseline:
    config: RunConfig
    source: SoakDataSource
    snapshot: dict[str, object]


@pytest.fixture(scope="module")
def recovery_baseline(tmp_path_factory: pytest.TempPathFactory) -> RecoveryBaseline:
    root = tmp_path_factory.mktemp("vwap-shadow-recovery-baseline")
    config = load_run_config(_CONFIG, environ={})
    source = build_synthetic_soak_source(
        SyntheticCandleRequest(
            count=1_000,
            seed=314159,
            bar_interval="1h",
        )
    )
    database_path = root / "baseline.db"
    result = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=root / "output",
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=100,
    )
    return RecoveryBaseline(
        config=config,
        source=source,
        snapshot=read_soak_snapshot(database_path, str(result["run_id"])),
    )


@pytest.mark.parametrize("point", list(InterruptionPoint))
def test_each_interruption_point_recovers_to_the_same_final_state(
    point: InterruptionPoint,
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    bar_number = _interruption_bar(point, recovery_baseline.snapshot)
    database_path = tmp_path / f"{point.value}.db"
    output_dir = tmp_path / point.value

    with pytest.raises(ShadowSoakInterruption, match=point.value):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=output_dir,
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=100,
            interruption=InterruptionPlan(point, bar_number),
        )
    interrupted_run_id = _only_run_id(database_path)
    interrupted = read_soak_snapshot(database_path, interrupted_run_id)
    assert interrupted["status"] == "interrupted"

    resumed = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=recovery_baseline.config,
        source=recovery_baseline.source,
        bar_interval="1h",
        checkpoint_every=100,
        resume_run_id=interrupted_run_id,
    )
    recovered = read_soak_snapshot(database_path, interrupted_run_id)

    assert resumed["status"] == "completed"
    assert resumed["resume_count"] == 1
    assert recovered == recovery_baseline.snapshot


def test_multiple_interruptions_recover_without_gaps_or_duplicates(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "multi.db"
    output_dir = tmp_path / "output"
    baseline_signals = cast(
        list[dict[str, object]],
        recovery_baseline.snapshot["signals"],
    )
    buy_bars = [
        index + 1 for index, signal in enumerate(baseline_signals) if signal["action"] == "buy"
    ]
    plans = [
        InterruptionPlan(InterruptionPoint.BEFORE_STRATEGY, 100),
        InterruptionPlan(InterruptionPoint.AFTER_STRATEGY_BEFORE_SIGNAL_SAVE, 250),
        InterruptionPlan(
            InterruptionPoint.AFTER_SIGNAL_SAVE_BEFORE_PROPOSAL_SAVE,
            next(bar for bar in buy_bars if bar > 300),
        ),
        InterruptionPlan(
            InterruptionPoint.AFTER_PROPOSAL_SAVE_BEFORE_CHECKPOINT,
            next(bar for bar in buy_bars if bar > 500),
        ),
        InterruptionPlan(InterruptionPoint.AFTER_CHECKPOINT, 800),
    ]

    run_id: str | None = None
    for plan in plans:
        with pytest.raises(ShadowSoakInterruption, match=plan.point.value):
            run_vwap_shadow_soak(
                database_path=database_path,
                output_dir=output_dir,
                config=recovery_baseline.config,
                source=recovery_baseline.source,
                bar_interval="1h",
                checkpoint_every=100,
                resume_run_id=run_id,
                interruption=plan,
            )
        run_id = _only_run_id(database_path)
    assert run_id is not None

    result = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=recovery_baseline.config,
        source=recovery_baseline.source,
        bar_interval="1h",
        checkpoint_every=100,
        resume_run_id=run_id,
    )
    recovered = read_soak_snapshot(database_path, run_id)

    assert result["resume_count"] == 5
    assert result["interruption_count"] == 5
    assert recovered == recovery_baseline.snapshot
    recovered_signals = cast(list[dict[str, object]], recovered["signals"])
    recovered_proposals = cast(list[dict[str, object]], recovered["proposals"])
    assert len({row["signal_id"] for row in recovered_signals}) == 1_000
    assert len({row["proposal_id"] for row in recovered_proposals}) == recovered["buy_signals"]


def test_stop_after_bars_checkpoints_and_resumes_from_the_next_candle(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stop-after.db"
    output_dir = tmp_path / "output"
    stopped = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=recovery_baseline.config,
        source=recovery_baseline.source,
        bar_interval="1h",
        checkpoint_every=100,
        stop_after_bars=333,
    )
    run_id = str(stopped["run_id"])
    assert stopped["status"] == "interrupted"
    assert stopped["bars_processed"] == 333
    assert stopped["checkpoint_count"] == 4

    resumed = run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=recovery_baseline.config,
        source=recovery_baseline.source,
        bar_interval="1h",
        checkpoint_every=100,
        resume_run_id=run_id,
    )
    assert resumed["status"] == "completed"
    assert resumed["resume_count"] == 1
    assert read_soak_snapshot(database_path, run_id) == recovery_baseline.snapshot


def test_resume_fails_closed_for_configuration_data_and_checkpoint_changes(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "validation.db"
    output_dir = tmp_path / "output"
    with pytest.raises(ShadowSoakInterruption):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=output_dir,
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=25,
            interruption=InterruptionPlan(InterruptionPoint.AFTER_CHECKPOINT, 50),
        )
    run_id = _only_run_id(database_path)

    changed_config = recovery_baseline.config.model_copy(
        update={
            "strategy": recovery_baseline.config.strategy.model_copy(
                update={
                    "parameters": {
                        "vwap_window": 24,
                        "buy_deviation_bps": "101",
                    }
                }
            )
        }
    )
    with pytest.raises(ShadowSoakError, match="configuration hash mismatch"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=output_dir,
            config=changed_config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=25,
            resume_run_id=run_id,
        )

    changed_source = build_synthetic_soak_source(
        SyntheticCandleRequest(
            count=1_000,
            seed=271828,
            bar_interval="1h",
        )
    )
    with pytest.raises(ShadowSoakError, match="data source identity mismatch"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=output_dir,
            config=recovery_baseline.config,
            source=changed_source,
            bar_interval="1h",
            checkpoint_every=25,
            resume_run_id=run_id,
        )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE shadow_soak_checkpoints SET checkpoint_version=999 WHERE run_id=?",
            (run_id,),
        )
    with pytest.raises(ShadowSoakError, match="checkpoint version"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=output_dir,
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=25,
            resume_run_id=run_id,
        )


def test_resume_fails_closed_when_bar_interval_changes(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interval.db"
    with pytest.raises(ShadowSoakInterruption):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=25,
            interruption=InterruptionPlan(InterruptionPoint.BEFORE_STRATEGY, 10),
        )
    run_id = _only_run_id(database_path)
    with pytest.raises(ShadowSoakError, match="bar interval mismatch"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="2h",
            checkpoint_every=25,
            resume_run_id=run_id,
        )


def test_unexpected_local_failure_is_recorded_as_failed(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_persistence(*_: object, **__: object) -> None:
        raise RuntimeError("local persistence failed")

    monkeypatch.setattr(ShadowSoakStore, "persist_bar", fail_persistence)
    database_path = tmp_path / "failed.db"
    with pytest.raises(RuntimeError, match="local persistence failed"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=100,
        )

    with sqlite3.connect(database_path) as connection:
        status = connection.execute("SELECT status FROM shadow_soak_runs").fetchone()[0]
        events = connection.execute(
            "SELECT event_type FROM shadow_soak_run_events ORDER BY event_id"
        ).fetchall()
    assert status == "failed"
    assert [str(row[0]) for row in events] == ["started", "failed"]


class _LocalPersistenceAdapter:
    is_local_adapter = True
    broker_write_calls = 0
    external_network_calls = 0


def _persistence_fault(point: str) -> FaultInjector:
    return FaultInjector(
        FaultPlan("persistence-fault", 20260809, (FaultStep(point, FaultAction.STORAGE_ERROR),)),
        _LocalPersistenceAdapter(),
        VirtualClock(),
    )


def _resume_after_persistence_fault(
    *,
    database_path: Path,
    output_dir: Path,
    baseline: RecoveryBaseline,
    run_id: str,
) -> dict[str, object]:
    run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=baseline.config,
        source=baseline.source,
        bar_interval="1h",
        checkpoint_every=100,
        resume_run_id=run_id,
    )
    return read_soak_snapshot(database_path, run_id)


def test_signal_write_fault_rolls_back_bar_then_resume_matches_baseline(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "signal-failure.db"
    injector = _persistence_fault("shadow_soak.signal.before_insert")

    with pytest.raises(StorageError, match="storage_error"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=100,
            fault_injector=injector,
        )
    injector.assert_consumed()

    run_id = _only_run_id(database_path)
    failed = read_soak_snapshot(database_path, run_id)
    assert failed["status"] == "failed"
    assert failed["signals"] == []
    assert failed["proposals"] == []
    assert failed["checkpoint"] is None

    assert (
        _resume_after_persistence_fault(
            database_path=database_path,
            output_dir=tmp_path / "output",
            baseline=recovery_baseline,
            run_id=run_id,
        )
        == recovery_baseline.snapshot
    )


def test_proposal_write_fault_rolls_back_signal_and_resume_is_idempotent(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "proposal-failure.db"
    injector = _persistence_fault("shadow_soak.proposal.before_insert")

    with pytest.raises(StorageError, match="storage_error"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=100,
            fault_injector=injector,
        )
    injector.assert_consumed()

    run_id = _only_run_id(database_path)
    failed = read_soak_snapshot(database_path, run_id)
    failed_signals = cast(list[dict[str, object]], failed["signals"])
    failed_proposals = cast(list[dict[str, object]], failed["proposals"])
    checkpoint = cast(dict[str, object] | None, failed["checkpoint"])
    assert failed["status"] == "failed"
    assert failed["bars_processed"] == len(failed_signals)
    assert failed["buy_signals"] == len(failed_proposals)
    assert checkpoint is None or int(str(checkpoint["processed_count"])) < int(
        str(failed["bars_processed"])
    )

    assert (
        _resume_after_persistence_fault(
            database_path=database_path,
            output_dir=tmp_path / "output",
            baseline=recovery_baseline,
            run_id=run_id,
        )
        == recovery_baseline.snapshot
    )


def test_checkpoint_write_fault_replays_business_state_without_duplicates(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkpoint-failure.db"
    injector = _persistence_fault("shadow_soak.checkpoint.before_insert")

    with pytest.raises(StorageError, match="storage_error"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=100,
            fault_injector=injector,
        )
    injector.assert_consumed()

    run_id = _only_run_id(database_path)
    failed = read_soak_snapshot(database_path, run_id)
    failed_signals = cast(list[dict[str, object]], failed["signals"])
    assert failed["status"] == "failed"
    assert failed["bars_processed"] == 100
    assert len(failed_signals) == 100
    assert failed["checkpoint"] is None

    recovered = _resume_after_persistence_fault(
        database_path=database_path,
        output_dir=tmp_path / "output",
        baseline=recovery_baseline,
        run_id=run_id,
    )
    assert recovered == recovery_baseline.snapshot
    signals = cast(list[dict[str, object]], recovered["signals"])
    proposals = cast(list[dict[str, object]], recovered["proposals"])
    assert len({row["signal_id"] for row in signals}) == len(signals)
    assert len({row["proposal_id"] for row in proposals}) == len(proposals)


def test_commit_fault_rolls_back_and_leaves_connection_reusable(
    recovery_baseline: RecoveryBaseline,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "commit-failure.db"
    injector = _persistence_fault("shadow_soak.transaction.before_commit")

    with pytest.raises(StorageError, match="storage_error"):
        run_vwap_shadow_soak(
            database_path=database_path,
            output_dir=tmp_path / "output",
            config=recovery_baseline.config,
            source=recovery_baseline.source,
            bar_interval="1h",
            checkpoint_every=100,
            fault_injector=injector,
        )
    injector.assert_consumed()

    run_id = _only_run_id(database_path)
    failed = read_soak_snapshot(database_path, run_id)
    assert failed["signals"] == []
    assert failed["checkpoint"] is None
    assert (
        _resume_after_persistence_fault(
            database_path=database_path,
            output_dir=tmp_path / "output",
            baseline=recovery_baseline,
            run_id=run_id,
        )
        == recovery_baseline.snapshot
    )


def _interruption_bar(
    point: InterruptionPoint,
    snapshot: dict[str, object],
) -> int:
    if point is InterruptionPoint.AFTER_CHECKPOINT:
        return 400
    if point in {
        InterruptionPoint.AFTER_SIGNAL_SAVE_BEFORE_PROPOSAL_SAVE,
        InterruptionPoint.AFTER_PROPOSAL_SAVE_BEFORE_CHECKPOINT,
    }:
        signals = cast(list[dict[str, object]], snapshot["signals"])
        return next(
            index + 1
            for index, signal in enumerate(signals)
            if index > 200 and signal["action"] == "buy"
        )
    return 237


def _only_run_id(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT run_id FROM shadow_soak_runs").fetchall()
    assert len(rows) == 1
    return str(rows[0][0])
