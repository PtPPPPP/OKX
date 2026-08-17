from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.market.historical_data import BAR_INTERVALS, load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from app.runtime.clock import BacktestClock
from app.services.continuous_shadow_repository import (
    ContinuousRunLock,
    ContinuousShadowRepository,
    ShadowReplaySession,
)
from app.services.shadow_replay import (
    ShadowReplayConfiguration,
    _single_signal,
)
from app.storage.database import Database
from app.strategies.registry import create_strategy
from tests.test_shadow_replay_convergence import (
    _CANDLES,
    _FIXTURE,
    _QUERIES,
    _ReplayEnvironment,
)


def _signal_value(signal: Signal) -> str:
    return json.dumps(
        {
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
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _semantic_state(database_path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(database_path) as connection:
        return {
            name: [tuple(row) for row in connection.execute(query)]
            for name, query in _QUERIES.items()
        }


def _resume_lost_tail(database: Database, environment: _ReplayEnvironment, run_id: str) -> int:
    """Test harness for explicit restart/replay from the committed checkpoint."""
    config = environment.config
    candles = load_candles_csv(_FIXTURE, bar=config.market.bar)[:_CANDLES]
    interval = BAR_INTERVALS[config.market.bar.lower()]
    snapshot_path = config.data.instrument_snapshot
    assert snapshot_path is not None
    instrument = InstrumentSnapshotStore.load(snapshot_path).instrument
    repository = ContinuousShadowRepository(database)
    resume = repository.load_vwap_shadow_resume_context(run_id)
    assert resume is not None
    checkpoint_index = next(
        index for index, candle in enumerate(candles) if candle.timestamp == resume.checkpoint
    )

    strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
    clock = BacktestClock(candles[0].timestamp)
    empty_portfolio = PortfolioSnapshot({}, {}, {}, trusted_for_trading=False)
    strategy.on_start(
        StrategyContext(
            run_id,
            TradingMode.DEMO,
            strategy.name,
            instrument,
            config.market.bar,
            empty_portfolio,
            None,
            clock,
        )
    )
    shadow_config = ShadowReplayConfiguration(
        config.market.instrument_id,
        config.strategy.name,
        config.market.bar,
        minimum_confirmed_candles=_CANDLES,
    )
    evaluations = buys = proposals = 0
    lock = ContinuousRunLock(database)
    lock.acquire(run_id)
    try:
        with repository.replay_session() as session:
            for index, candle in enumerate(candles):
                clock.advance_to(candle.timestamp + interval)
                context = StrategyContext(
                    run_id,
                    TradingMode.DEMO,
                    strategy.name,
                    instrument,
                    config.market.bar,
                    empty_portfolio,
                    MarketSnapshot(candle, candle.close),
                    clock,
                )
                signal = _single_signal(strategy.on_bar(context, candle))
                evaluations += 1
                buys += int(signal.action is SignalAction.BUY)
                proposals += int(signal.action is SignalAction.BUY)
                if index <= checkpoint_index:
                    continue

                state_snapshot: dict[str, object] = getattr(
                    strategy, "state_snapshot", lambda: {}
                )()
                committed = session.commit_vwap_shadow_candle(
                    run_id=run_id,
                    config=shadow_config,
                    candle=candle,
                    strategy_version="vwap_shadow_v1",
                    signal_id=signal.signal_id,
                    signal_type=signal.action.value,
                    signal_value=_signal_value(signal),
                    runtime_state=json.dumps(state_snapshot, sort_keys=True),
                    warmup_count=int(signal.metadata["window_length"]),
                    warmup_completed=signal.metadata["vwap"] is not None,
                    proposal_price=(candle.close if signal.action is SignalAction.BUY else None),
                    processed_count=evaluations,
                    signal_count=buys,
                    proposal_count=proposals,
                    market_data_source="local_csv_shadow_replay",
                    private_stream_status="not_applicable",
                    public_stream_status="local_replay",
                )
                assert committed is True

        with database.connect() as connection:
            connection.execute(
                """UPDATE continuous_demo_runs
                SET status='stopped',stopped_at=?,stop_reason='candle_target_reached',
                    reconciliation_status='healthy'
                WHERE run_id=?""",
                (datetime.now(UTC).isoformat(), run_id),
            )
    finally:
        lock.release(run_id, "replay_finished")
    return _CANDLES - checkpoint_index - 1


@pytest.mark.parametrize("lost_tail", [1, 5, 10])
def test_lost_tail_replay_converges_without_duplicates_or_funds_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_tail: int,
) -> None:
    canonical = _ReplayEnvironment(tmp_path / "canonical", "canonical.db")
    snapshot_path = tmp_path / f"lost-tail-{lost_tail}.db"
    snapshot_after = _CANDLES - lost_tail
    original_commit = ShadowReplaySession.commit_vwap_shadow_candle
    committed = 0

    def commit_and_snapshot(session: ShadowReplaySession, **kwargs: object) -> bool:
        nonlocal committed
        result = original_commit(session, **kwargs)
        if result:
            committed += 1
        if committed == snapshot_after and not snapshot_path.exists():
            with sqlite3.connect(snapshot_path) as destination:
                session._require_open().backup(destination)
        return result

    monkeypatch.setattr(ShadowReplaySession, "commit_vwap_shadow_candle", commit_and_snapshot)
    canonical_result = canonical.run()
    run_id = str(canonical_result["run_id"])
    assert snapshot_path.exists()

    recovered_database = Database(f"sqlite:///{snapshot_path}")
    replayed = _resume_lost_tail(recovered_database, canonical, run_id)

    assert replayed == lost_tail
    assert _semantic_state(recovered_database.path) == _semantic_state(canonical.database.path)
    with sqlite3.connect(recovered_database.path) as connection:
        duplicate_processed = connection.execute(
            """SELECT COUNT(*) FROM (
               SELECT run_id,instrument_id,timeframe,candle_open_time,COUNT(*) AS n
               FROM processed_candles GROUP BY 1,2,3,4 HAVING n > 1)"""
        ).fetchone()[0]
        duplicate_signals = connection.execute(
            """SELECT COUNT(*) FROM (
               SELECT run_id,instrument_id,candle_open_time,signal_type,COUNT(*) AS n
               FROM strategy_signal_events GROUP BY 1,2,3,4 HAVING n > 1)"""
        ).fetchone()[0]
        duplicate_proposals = connection.execute(
            """SELECT COUNT(*) FROM (
               SELECT run_id,signal_id,COUNT(*) AS n
               FROM shadow_order_proposals GROUP BY 1,2 HAVING n > 1)"""
        ).fetchone()[0]
        unsafe_proposals = connection.execute(
            """SELECT COUNT(*) FROM shadow_order_proposals
               WHERE submission_performed != 0 OR quantity != '0' OR notional != '0'
                  OR exchange_order_id IS NOT NULL OR is_shadow != 1"""
        ).fetchone()[0]
        executable_orders = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        demo_proposals = connection.execute("SELECT COUNT(*) FROM demo_order_proposals").fetchone()[
            0
        ]
        runtime_rows = connection.execute(
            "SELECT COUNT(*) FROM strategy_runtime_states WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    assert duplicate_processed == 0
    assert duplicate_signals == 0
    assert duplicate_proposals == 0
    assert runtime_rows == 1
    assert unsafe_proposals == 0
    assert executable_orders == 0
    assert demo_proposals == 0
    assert canonical_result["broker_write_calls"] == 0
