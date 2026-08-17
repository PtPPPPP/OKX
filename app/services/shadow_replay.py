from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.market.historical_data import (
    BAR_INTERVALS,
    MarketDataError,
    load_candles_csv,
)
from app.reproducibility import InstrumentSnapshotStore, candles_hash
from app.runtime.clock import BacktestClock
from app.services.continuous_shadow_repository import (
    ContinuousRunLock,
    ContinuousShadowRepository,
)
from app.storage.database import Database
from app.strategies.registry import create_strategy

_STRATEGY_NAME = "vwap_shadow"
_STRATEGY_VERSION = "vwap_shadow_v1"


@dataclass(frozen=True)
class ShadowReplayConfiguration:
    instrument_id: str
    strategy_name: str
    timeframe: str
    minimum_confirmed_candles: int


def run_shadow_replay(
    database: Database,
    config: RunConfig,
    data_path: Path,
    maximum: int,
) -> dict[str, object]:
    _validate_replay_config(config, maximum)
    interval = BAR_INTERVALS[config.market.bar.lower()]
    candles = _load_shadow_candles(data_path, bar=config.market.bar)
    if len(candles) < maximum:
        raise ValueError(f"shadow replay requires {maximum} candles")
    selected = candles[:maximum]

    snapshot_path = config.data.instrument_snapshot
    if snapshot_path is None:
        raise ValueError("shadow replay requires a local instrument snapshot")
    instrument = InstrumentSnapshotStore.load(snapshot_path).instrument
    if instrument.instrument_id != config.market.instrument_id:
        raise ValueError("instrument snapshot does not match shadow configuration")

    run_id = uuid4().hex
    runtime = ContinuousShadowRepository(database)
    shadow_config = ShadowReplayConfiguration(
        config.market.instrument_id,
        config.strategy.name,
        config.market.bar,
        minimum_confirmed_candles=maximum,
    )
    runtime.create_run(run_id, shadow_config, datetime.now(UTC))
    lock = ContinuousRunLock(database)
    lock.acquire(run_id)

    strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
    clock = BacktestClock(selected[0].timestamp)
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

    evaluations = buys = holds = proposals = 0
    buy_signal_ids: list[str] = []
    buy_signal_times: list[str] = []
    try:
        # Path-scoped connection owner: one configured connection for the whole
        # replay run; every candle still commits as its own atomic transaction.
        with runtime.replay_session() as session:
            for candle in selected:
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
                holds += int(signal.action is SignalAction.HOLD)

                state_snapshot: dict[str, object] = getattr(
                    strategy, "state_snapshot", lambda: {}
                )()
                signal_value = json.dumps(
                    {
                        "close": str(signal.metadata["close"]),
                        "vwap": (
                            str(signal.metadata["vwap"])
                            if signal.metadata["vwap"] is not None
                            else None
                        ),
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
                committed = session.commit_vwap_shadow_candle(
                    run_id=run_id,
                    config=shadow_config,
                    candle=candle,
                    strategy_version=_STRATEGY_VERSION,
                    signal_id=signal.signal_id,
                    signal_type=signal.action.value,
                    signal_value=signal_value,
                    runtime_state=json.dumps(state_snapshot, sort_keys=True),
                    warmup_count=int(signal.metadata["window_length"]),
                    warmup_completed=signal.metadata["vwap"] is not None,
                    proposal_price=(candle.close if signal.action is SignalAction.BUY else None),
                    processed_count=evaluations,
                    signal_count=buys,
                    proposal_count=proposals + int(signal.action is SignalAction.BUY),
                    market_data_source="local_csv_shadow_replay",
                    private_stream_status="not_applicable",
                    public_stream_status="local_replay",
                )
                if not committed:
                    raise MarketDataError(
                        f"duplicate candle rejected: {candle.timestamp.isoformat()}"
                    )

                if signal.action is SignalAction.BUY:
                    # The canonical commit created the shadow proposal atomically,
                    # bound to this signal_id (shadow_order_proposals.signal_id).
                    proposals += 1
                    buy_signal_ids.append(signal.signal_id)
                    buy_signal_times.append(candle.timestamp.isoformat())

        with database.connect() as connection:
            connection.execute(
                """UPDATE continuous_demo_runs
                SET status='stopped',stopped_at=?,stop_reason='candle_target_reached',
                    reconciliation_status='healthy'
                WHERE run_id=?""",
                (datetime.now(UTC).isoformat(), run_id),
            )
        return {
            "run_id": run_id,
            "data_hash": candles_hash(selected),
            "input_candles": len(candles),
            "processed_candles": evaluations,
            "confirmed_candles": sum(candle.confirmed for candle in selected),
            "unique_confirmed_candles": sum(candle.confirmed for candle in selected),
            "duplicate_candles": 0,
            "strategy_evaluations": evaluations,
            "entry_signals": buys,
            "hold_signals": holds,
            "exit_signals": 0,
            "blocked_signals": 0,
            "shadow_proposals": proposals,
            "buy_signal_ids": buy_signal_ids,
            "buy_signal_times": buy_signal_times,
            "proposal_signal_ids": list(buy_signal_ids),
            "broker_objects_created": 0,
            "broker_write_calls": 0,
            "external_network_calls": 0,
            "status": "stopped",
        }
    finally:
        lock.release(run_id, "replay_finished")


def _validate_replay_config(config: RunConfig, maximum: int) -> None:
    if config.mode is not TradingMode.DEMO:
        raise ValueError("shadow replay requires mode=demo")
    if config.strategy.name != _STRATEGY_NAME:
        raise ValueError(f"shadow replay requires strategy={_STRATEGY_NAME}")
    if config.data.source != "csv":
        raise ValueError("shadow replay requires a local CSV data source")
    if maximum <= 0:
        raise ValueError("maximum must be greater than zero")
    if config.market.bar.lower() not in BAR_INTERVALS:
        raise MarketDataError(f"unsupported candle timeframe: {config.market.bar}")


def _load_shadow_candles(path: Path, *, bar: str) -> list[Candle]:
    _reject_duplicate_timestamps(path)
    try:
        return load_candles_csv(path, bar=bar)
    except MarketDataError as exc:
        if "K 线缺失" in str(exc):
            raise MarketDataError(f"{exc}; 预期周期: {bar}") from exc
        raise


def _reject_duplicate_timestamps(path: Path) -> None:
    if not path.is_file():
        return
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


def _single_signal(signals: list[Signal]) -> Signal:
    if len(signals) != 1:
        raise RuntimeError("pure VWAP shadow strategy must return exactly one signal")
    signal = signals[0]
    if signal.action not in {SignalAction.BUY, SignalAction.HOLD}:
        raise RuntimeError("pure VWAP shadow strategy emitted an unsupported action")
    return signal
