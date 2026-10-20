"""Deterministic LOCAL ONLY demonstration; no account, sockets or broker."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.run_config import load_run_config
from app.domain.market import Candle
from app.market.synthetic_candles import SyntheticCandleRequest, generate_synthetic_candles
from app.reproducibility import InstrumentSnapshotStore, candles_hash
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.runtime_observation import read_runtime_status
from app.services.vwap_continuous_shadow import ContinuousVWAPShadowRunner
from app.storage.database import Database, StorageError
from app.testing.fault_injection import (
    FaultAction,
    FaultInjector,
    FaultPlan,
    FaultStep,
    VirtualClock,
)


class LocalHistory:
    is_local_adapter = True

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        return self.candles[-limit:] if limit else self.candles


def local_fixture(
    root: Path,
) -> tuple[Database, ContinuousVWAPShadowRunner, LocalHistory, list[Candle], VirtualClock]:
    database = Database(f"sqlite:///{root / 'runtime.db'}")
    database.initialize()
    clock = VirtualClock()
    generation = RuntimeGenerationService(database, clock.now())
    generation_id = generation.create_preparing(
        "local_synthetic_fixture", "temporary_database", {}, "offline_test"
    )
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    candles = generate_synthetic_candles(SyntheticCandleRequest(count=60, seed=20261020))
    history = LocalHistory(candles[:29])
    runner = ContinuousVWAPShadowRunner(
        database, config, instrument, history, observation_clock=clock
    )
    return database, runner, history, candles, clock


def semantic_snapshot(database: Database, run_id: str) -> dict[str, Any]:
    with database.connect() as connection:
        return {
            "signals": [
                tuple(row)
                for row in connection.execute(
                    "SELECT candle_open_time,signal_type,signal_value,strategy_version FROM strategy_signal_events WHERE run_id=? ORDER BY candle_open_time",
                    (run_id,),
                )
            ],
            "checkpoint": tuple(
                connection.execute(
                    "SELECT last_candle_open_time,state_json,state_hash FROM strategy_runtime_states WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            ),
            "proposals": [
                tuple(row)
                for row in connection.execute(
                    "SELECT s.candle_open_time,p.reference_price,p.quantity,p.notional,p.submission_performed FROM shadow_order_proposals p JOIN strategy_signal_events s ON s.signal_id=p.signal_id WHERE p.run_id=? ORDER BY s.candle_open_time",
                    (run_id,),
                )
            ],
        }


def demonstrate(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    database, runner, history, candles, clock = local_fixture(output)
    session = runner.start_session()
    runner.handle_ws_connected(session, 1)
    runner.handle_live_candle(session, candles[29], generation=1)
    normal = read_runtime_status(database.path, session.run_id, now=clock.now())
    before = semantic_snapshot(database, session.run_id)
    injector = FaultInjector(
        FaultPlan(
            "local_runtime_recovery",
            1,
            (FaultStep("continuous_shadow.after_runtime", FaultAction.STORAGE_ERROR),),
        ),
        history,
        clock,
    )
    runner.repository.fault_injector = injector
    try:
        runner.handle_live_candle(session, candles[30], generation=1)
    except StorageError:
        pass
    else:
        raise AssertionError("fault was not injected")
    injector.assert_consumed()
    assert before == semantic_snapshot(database, session.run_id)
    fault = read_runtime_status(database.path, session.run_id, now=clock.now())
    history.candles = candles[:30]
    resumed_runner = ContinuousVWAPShadowRunner(
        database, runner.config, runner.instrument, history, observation_clock=clock
    )
    resumed = resumed_runner.start_session(resume_run_id=session.run_id)
    resumed_runner.handle_ws_connected(resumed, 1)
    for candle in candles[30:]:
        clock.advance(1000)
        resumed_runner.handle_live_candle(resumed, candle, generation=1)
    recovered = read_runtime_status(database.path, session.run_id, now=clock.now())
    resumed_runner.stop_session(resumed)

    reference_db, reference, _, reference_candles, _ = local_fixture(output / "reference")
    reference_session = reference.start_session()
    reference.handle_ws_connected(reference_session, 1)
    for candle in reference_candles[29:]:
        reference.handle_live_candle(reference_session, candle, generation=1)
    reference.stop_session(reference_session)
    assert semantic_snapshot(database, session.run_id) == semantic_snapshot(
        reference_db, reference_session.run_id
    )
    report = {
        "data_kind": "deterministic_synthetic_offline",
        "accelerated_simulation_not_elapsed_market_days": True,
        "database": str(database.path),
        "run_id": session.run_id,
        "data_hash": candles_hash(candles),
        "normal": normal,
        "fault": fault,
        "recovered": recovered,
        "business_results_equal_uninterrupted": True,
        "processed_candles": resumed.processed,
        "broker_write_calls": 0,
        "network_calls": 0,
    }
    (output / "demonstration.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = demonstrate(args.output)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "run_id",
                    "database",
                    "processed_candles",
                    "business_results_equal_uninterrupted",
                    "broker_write_calls",
                    "network_calls",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
