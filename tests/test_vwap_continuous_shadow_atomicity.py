from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from app.config.run_config import RunConfig, load_run_config
from app.domain.market import Candle, Instrument
from app.reproducibility import InstrumentSnapshotStore
from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.vwap_continuous_shadow import (
    ContinuousVWAPShadowConfiguration,
    ContinuousVWAPShadowRunner,
)
from app.storage.database import Database, StorageError
from app.testing.fault_injection import (
    FaultAction,
    FaultInjector,
    FaultPlan,
    FaultStep,
    VirtualClock,
)


class _LocalAdapter:
    is_local_adapter = True


class _History:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def get_historical_bars(
        self, *_: object, limit: int | None = None, **__: object
    ) -> list[Candle]:
        return self.candles[-limit:] if limit is not None else self.candles


async def _feed(candles: list[Candle]) -> AsyncIterator[Candle]:
    for candle in candles:
        yield candle


def _candle(timestamp: datetime, close: str) -> Candle:
    value = Decimal(close)
    return Candle(timestamp, value, value + 1, value - 1, value, Decimal("10"), True)


def _prepared_run(
    tmp_path: Path, *, b_price: str = "98"
) -> tuple[Database, RunConfig, Instrument, _History, str, Candle, Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [_candle(start + timedelta(hours=index), "100") for index in range(29)]
    candle_a = _candle(start + timedelta(hours=29), "100")
    candle_b = _candle(start + timedelta(hours=30), b_price)
    database = Database(f"sqlite:///{tmp_path / 'continuous.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    history = _History(candles)
    baseline = asyncio.run(
        ContinuousVWAPShadowRunner(database, config, instrument, history).run(
            _feed([candle_a]), maximum_confirmed_bars=1
        )
    )
    history.candles = [*candles, candle_a, candle_b]
    return database, config, instrument, history, baseline.run_id, candle_a, candle_b


def _independent_state(
    database: Database, run_id: str, candle_a: Candle, candle_b: Candle
) -> dict[str, object]:
    with sqlite3.connect(database.path) as connection:
        runtime = connection.execute(
            "SELECT last_candle_open_time,state_json FROM strategy_runtime_states WHERE run_id=?",
            (run_id,),
        ).fetchone()
        heartbeat = connection.execute(
            "SELECT processed_candle_count,generated_signal_count,shadow_proposal_count,last_heartbeat_at "
            "FROM continuous_demo_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return {
            "processed_b": connection.execute(
                "SELECT COUNT(*) FROM processed_candles WHERE run_id=? AND candle_open_time=?",
                (run_id, candle_b.timestamp.isoformat()),
            ).fetchone()[0],
            "signal_b": connection.execute(
                "SELECT COUNT(*) FROM strategy_signal_events WHERE run_id=? AND candle_open_time=?",
                (run_id, candle_b.timestamp.isoformat()),
            ).fetchone()[0],
            "proposal_b": connection.execute(
                "SELECT COUNT(*) FROM shadow_order_proposals WHERE run_id=? AND signal_id IN "
                "(SELECT signal_id FROM strategy_signal_events WHERE run_id=? AND candle_open_time=?)",
                (run_id, run_id, candle_b.timestamp.isoformat()),
            ).fetchone()[0],
            "checkpoint": runtime[0],
            "state": json.loads(runtime[1]),
            "heartbeat": tuple(heartbeat),
            "expected_checkpoint": candle_a.timestamp.isoformat(),
        }


def _fault_runner(
    database: Database,
    config: RunConfig,
    instrument: Instrument,
    history: _History,
    run_id: str,
    point: str,
) -> tuple[ContinuousVWAPShadowRunner, FaultInjector]:
    injector = FaultInjector(
        FaultPlan("continuous-shadow", 1, (FaultStep(point, FaultAction.STORAGE_ERROR),)),
        _LocalAdapter(),
        VirtualClock(),
    )
    return (
        ContinuousVWAPShadowRunner(database, config, instrument, history, fault_injector=injector),
        injector,
    )


def _business_snapshot(database: Database, run_id: str) -> dict[str, object]:
    with sqlite3.connect(database.path) as connection:
        return {
            "processed": connection.execute(
                "SELECT candle_open_time FROM processed_candles WHERE run_id=? ORDER BY candle_open_time",
                (run_id,),
            ).fetchall(),
            "signals": connection.execute(
                "SELECT candle_open_time,signal_type,signal_value FROM strategy_signal_events "
                "WHERE run_id=? ORDER BY candle_open_time",
                (run_id,),
            ).fetchall(),
            "proposals": connection.execute(
                "SELECT signal.candle_open_time,proposal.side,proposal.quantity,proposal.notional,"
                "proposal.submission_performed,proposal.exchange_order_id,proposal.capability_status,"
                "proposal.risk_status FROM shadow_order_proposals AS proposal JOIN strategy_signal_events "
                "AS signal ON signal.signal_id=proposal.signal_id WHERE proposal.run_id=? "
                "ORDER BY signal.candle_open_time",
                (run_id,),
            ).fetchall(),
            "runtime": connection.execute(
                "SELECT last_candle_open_time,state_json FROM strategy_runtime_states WHERE run_id=?",
                (run_id,),
            ).fetchone(),
        }


def test_continuous_shadow_after_signal_fault_is_triggered_and_rolled_back(tmp_path: Path) -> None:
    database, config, instrument, history, run_id, candle_a, candle_b = _prepared_run(tmp_path)
    runner, injector = _fault_runner(
        database, config, instrument, history, run_id, "continuous_shadow.after_signal"
    )
    before = _independent_state(database, run_id, candle_a, candle_b)

    with pytest.raises(StorageError):
        asyncio.run(runner.run(_feed([candle_b]), maximum_confirmed_bars=1, resume_run_id=run_id))

    injector.assert_consumed()
    triggered = [
        entry for entry in injector.trace.entries if entry.action is FaultAction.STORAGE_ERROR
    ]
    assert [(entry.injection_point, entry.occurrence) for entry in triggered] == [
        ("continuous_shadow.after_signal", 1)
    ]
    after = _independent_state(database, run_id, candle_a, candle_b)
    assert after["processed_b"] == after["signal_b"] == after["proposal_b"] == 0
    assert after["checkpoint"] == after["expected_checkpoint"]
    assert after["state"] == before["state"]
    assert after["heartbeat"] == before["heartbeat"]
    with pytest.raises(RuntimeError, match="requires a new instance"):
        asyncio.run(runner.run(_feed([]), resume_run_id=run_id))


@pytest.mark.parametrize(
    "point",
    (
        "continuous_shadow.after_processed_identity",
        "continuous_shadow.after_runtime",
        "continuous_shadow.after_signal",
        "continuous_shadow.after_proposal",
        "continuous_shadow.after_heartbeat",
        "continuous_shadow.before_commit",
    ),
)
def test_continuous_shadow_buy_rollback_matrix_uses_independent_reader(
    tmp_path: Path, point: str
) -> None:
    database, config, instrument, history, run_id, candle_a, candle_b = _prepared_run(tmp_path)
    runner, injector = _fault_runner(database, config, instrument, history, run_id, point)
    before = _independent_state(database, run_id, candle_a, candle_b)
    with pytest.raises(StorageError):
        asyncio.run(runner.run(_feed([candle_b]), maximum_confirmed_bars=1, resume_run_id=run_id))
    injector.assert_consumed()
    assert [
        entry.injection_point
        for entry in injector.trace.entries
        if entry.action is FaultAction.STORAGE_ERROR
    ] == [point]
    after = _independent_state(database, run_id, candle_a, candle_b)
    assert after["processed_b"] == after["signal_b"] == after["proposal_b"] == 0
    assert after["checkpoint"] == after["expected_checkpoint"]
    assert after["state"] == before["state"]
    assert after["heartbeat"] == before["heartbeat"]


@pytest.mark.parametrize(
    "point",
    (
        "continuous_shadow.after_processed_identity",
        "continuous_shadow.after_runtime",
        "continuous_shadow.after_signal",
        "continuous_shadow.after_heartbeat",
        "continuous_shadow.before_commit",
    ),
)
def test_continuous_shadow_hold_rollback_matrix_uses_independent_reader(
    tmp_path: Path, point: str
) -> None:
    database, config, instrument, history, run_id, candle_a, candle_b = _prepared_run(
        tmp_path, b_price="101"
    )
    runner, injector = _fault_runner(database, config, instrument, history, run_id, point)
    before = _independent_state(database, run_id, candle_a, candle_b)
    with pytest.raises(StorageError):
        asyncio.run(runner.run(_feed([candle_b]), maximum_confirmed_bars=1, resume_run_id=run_id))
    injector.assert_consumed()
    after = _independent_state(database, run_id, candle_a, candle_b)
    assert after["processed_b"] == after["signal_b"] == after["proposal_b"] == 0
    assert after["checkpoint"] == after["expected_checkpoint"]
    assert after["state"] == before["state"]
    assert after["heartbeat"] == before["heartbeat"]


def test_continuous_shadow_fault_fires_once_then_new_runner_commits(tmp_path: Path) -> None:
    database, config, instrument, history, run_id, candle_a, candle_b = _prepared_run(tmp_path)
    runner, injector = _fault_runner(
        database, config, instrument, history, run_id, "continuous_shadow.after_signal"
    )
    with pytest.raises(StorageError):
        asyncio.run(runner.run(_feed([candle_b]), maximum_confirmed_bars=1, resume_run_id=run_id))
    injector.assert_consumed()

    result = asyncio.run(
        ContinuousVWAPShadowRunner(database, config, instrument, history).run(
            _feed([candle_b]), maximum_confirmed_bars=1, resume_run_id=run_id
        )
    )
    state = _independent_state(database, run_id, candle_a, candle_b)
    assert result.confirmed_bars_processed == 2
    assert state["processed_b"] == state["signal_b"] == state["proposal_b"] == 1


def test_continuous_shadow_hold_does_not_claim_unreached_proposal_fault(tmp_path: Path) -> None:
    database, config, instrument, history, run_id, candle_a, candle_b = _prepared_run(
        tmp_path, b_price="101"
    )
    runner, injector = _fault_runner(
        database, config, instrument, history, run_id, "continuous_shadow.after_proposal"
    )
    result = asyncio.run(
        runner.run(_feed([candle_b]), maximum_confirmed_bars=2, resume_run_id=run_id)
    )
    assert result.confirmed_bars_processed == 2
    with pytest.raises(AssertionError, match="were not triggered"):
        injector.assert_consumed()
    assert all(entry.action is FaultAction.PASS for entry in injector.trace.entries)
    state = _independent_state(database, run_id, candle_a, candle_b)
    assert state["processed_b"] == state["signal_b"] == 1
    assert state["proposal_b"] == 0


def test_continuous_shadow_native_sqlite_commit_failure_rolls_back_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _config, _, _, run_id, candle_a, candle_b = _prepared_run(tmp_path)
    repository = ContinuousShadowRepository(database)
    context = repository.load_vwap_shadow_resume_context(run_id)
    assert context is not None
    original_connect = sqlite3.connect

    def commit_denied(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = cast(sqlite3.Connection, original_connect(*args, **kwargs))

        def authorizer(
            action: int,
            arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr("app.storage.database.sqlite3.connect", commit_denied)
    with pytest.raises(StorageError):
        repository.commit_vwap_shadow_candle(
            run_id=run_id,
            config=ContinuousVWAPShadowConfiguration(),
            candle=candle_b,
            strategy_version="vwap_shadow_v1",
            signal_id="commit-failure-signal",
            signal_type="BUY",
            signal_value="{}",
            runtime_state=json.dumps(context.runtime_state, sort_keys=True),
            warmup_count=24,
            warmup_completed=True,
            proposal_price=candle_b.close,
            processed_count=2,
            signal_count=2,
            proposal_count=1,
        )
    monkeypatch.undo()
    failed = _independent_state(database, run_id, candle_a, candle_b)
    assert failed["processed_b"] == failed["signal_b"] == failed["proposal_b"] == 0
    assert failed["checkpoint"] == failed["expected_checkpoint"]

    assert repository.commit_vwap_shadow_candle(
        run_id=run_id,
        config=ContinuousVWAPShadowConfiguration(),
        candle=candle_b,
        strategy_version="vwap_shadow_v1",
        signal_id="commit-failure-signal",
        signal_type="BUY",
        signal_value="{}",
        runtime_state=json.dumps(context.runtime_state, sort_keys=True),
        warmup_count=24,
        warmup_completed=True,
        proposal_price=candle_b.close,
        processed_count=2,
        signal_count=2,
        proposal_count=1,
    )
    retried = _independent_state(database, run_id, candle_a, candle_b)
    assert retried["processed_b"] == retried["signal_b"] == retried["proposal_b"] == 1


def test_continuous_shadow_new_instance_resume_matches_uninterrupted_baseline(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    bootstrap = [_candle(start + timedelta(hours=index), "100") for index in range(29)]
    live = [
        _candle(start + timedelta(hours=29), "101"),
        _candle(start + timedelta(hours=30), "100"),
        _candle(start + timedelta(hours=31), "98"),
        _candle(start + timedelta(hours=32), "101"),
        _candle(start + timedelta(hours=33), "97"),
    ]

    def prepare(path: Path) -> tuple[Database, RunConfig, Instrument, _History]:
        database = Database(f"sqlite:///{path / 'continuous.db'}")
        database.initialize()
        generation = RuntimeGenerationService(database, start)
        generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
        generation.activate(generation_id)
        config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
        assert config.data.instrument_snapshot is not None
        instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
        return database, config, instrument, _History(bootstrap)

    baseline_db, baseline_config, baseline_instrument, baseline_history = prepare(
        tmp_path / "baseline"
    )
    baseline = asyncio.run(
        ContinuousVWAPShadowRunner(
            baseline_db, baseline_config, baseline_instrument, baseline_history
        ).run(_feed(live), maximum_confirmed_bars=len(live))
    )

    database, config, instrument, history = prepare(tmp_path / "resume")
    first = asyncio.run(
        ContinuousVWAPShadowRunner(database, config, instrument, history).run(
            _feed(live[:2]), maximum_confirmed_bars=2
        )
    )
    history.candles = [*bootstrap, *live]
    failing, injector = _fault_runner(
        database, config, instrument, history, first.run_id, "continuous_shadow.after_proposal"
    )
    with pytest.raises(StorageError):
        asyncio.run(
            failing.run(_feed([live[2]]), maximum_confirmed_bars=1, resume_run_id=first.run_id)
        )
    injector.assert_consumed()
    interrupted = _independent_state(database, first.run_id, live[1], live[2])
    assert interrupted["processed_b"] == interrupted["signal_b"] == interrupted["proposal_b"] == 0

    resumed = asyncio.run(
        ContinuousVWAPShadowRunner(database, config, instrument, history).run(
            _feed(live[2:]), maximum_confirmed_bars=len(live), resume_run_id=first.run_id
        )
    )
    assert resumed.confirmed_bars_processed == len(live)
    assert _business_snapshot(database, first.run_id) == _business_snapshot(
        baseline_db, baseline.run_id
    )


@pytest.mark.parametrize(
    "field,value",
    (
        ("strategy", "other"),
        ("instrument", "ETH-USDT"),
        ("interval", "5m"),
        ("vwap_window", 25),
        ("buy_deviation_bps", 101),
    ),
)
def test_continuous_shadow_resume_configuration_mismatches_fail_closed(
    tmp_path: Path, field: str, value: str | int
) -> None:
    database, config, instrument, history, run_id, _, _ = _prepared_run(tmp_path)
    altered = config.model_copy(deep=True)
    if field == "strategy":
        altered.strategy.name = str(value)
    elif field == "instrument":
        altered.market.instrument_id = str(value)
    elif field == "interval":
        altered.market.bar = str(value)
    else:
        altered.strategy.parameters[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(
            ContinuousVWAPShadowRunner(database, altered, instrument, history).run(
                _feed([]), resume_run_id=run_id
            )
        )
