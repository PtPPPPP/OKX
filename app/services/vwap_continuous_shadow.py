from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.signal import SignalAction
from app.market.historical_data import BAR_INTERVALS, MarketDataError, normalize_candles
from app.market.providers import MarketDataProvider
from app.market.websocket import PublicWebSocketEvent, PublicWebSocketEventType
from app.runtime.clock import BacktestClock, Clock, SystemClock
from app.services.continuous_shadow_repository import (
    ContinuousRunLock,
    ContinuousShadowRepository,
    ContinuousShadowResumeContext,
    configuration_fingerprint,
)
from app.services.runtime_observation import RuntimeObservation, RuntimeObserver, assess
from app.storage.database import Database, StorageError
from app.storage.migrations import MIGRATIONS, MigrationManager
from app.strategies.registry import STRATEGY_REGISTRY, create_strategy
from app.strategies.vwap_shadow import VWAPShadowStrategy
from app.testing.fault_injection import FaultInjector

_STRATEGY_VERSION = STRATEGY_REGISTRY["vwap_shadow"].implementation_version
_INSTRUMENT = "BTC-USDT"
_BAR = "1h"
_RECONCILIATION_LIMIT = 300


async def _next_public_event(
    iterator: AsyncIterator[PublicWebSocketEvent],
) -> PublicWebSocketEvent:
    return await anext(iterator)


class ContinuousShadowLifecycle(StrEnum):
    BOOTSTRAPPING = "bootstrapping"
    READY = "ready"
    STALE = "stale"
    RECONCILING = "reconciling"
    BLOCKED = "blocked"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ContinuousVWAPShadowConfiguration:
    instrument_id: str = _INSTRUMENT
    timeframe: str = _BAR
    strategy_name: str = "vwap_shadow"
    bootstrap_margin: int = 5
    vwap_window: int = 24
    buy_deviation_bps: Decimal = Decimal("100")


@dataclass(frozen=True, slots=True)
class ContinuousVWAPShadowResult:
    run_id: str
    bootstrap_bars: int
    confirmed_bars_processed: int
    hold_signals: int
    buy_signals: int
    proposals: int
    duplicates: int
    gaps: int


@dataclass(slots=True)
class ContinuousShadowSession:
    """Minimal state shared by socket lifecycle, recovery, and live processing."""

    run_id: str
    lifecycle_state: ContinuousShadowLifecycle
    last_committed_checkpoint: datetime | None
    live_boundary: datetime
    strategy: VWAPShadowStrategy
    portfolio: PortfolioSnapshot
    clock: BacktestClock
    lock: ContinuousRunLock
    bootstrap_bars: int
    bootstrap_latest_confirmed_timestamp: datetime
    processed: int = 0
    holds: int = 0
    buys: int = 0
    proposals: int = 0
    duplicates: int = 0
    gaps: int = 0
    disconnect_count: int = 0
    rejected_live_candles: int = 0
    connection_generation: int = 0
    stop_requested: bool = False
    lock_released: bool = False

    @property
    def can_process_live_candle(self) -> bool:
        return self.lifecycle_state is ContinuousShadowLifecycle.READY and not self.stop_requested


class ContinuousVWAPShadowRunner:
    """Public-market-only VWAP coordinator; it has no execution dependency."""

    def __init__(
        self,
        database: Database,
        config: RunConfig,
        instrument: Instrument,
        history: MarketDataProvider,
        *,
        fault_injector: FaultInjector | None = None,
        observation_clock: Clock | None = None,
        heartbeat_seconds: float = 10,
    ) -> None:
        self.database = database
        self.config = config
        self.instrument = instrument
        self.history = history
        self.repository = ContinuousShadowRepository(database, fault_injector=fault_injector)
        self.session: ContinuousShadowSession | None = None
        self._blocked = False
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.observation_clock = observation_clock or SystemClock()
        self.heartbeat_seconds = heartbeat_seconds
        self.observer: RuntimeObserver | None = None
        self._recovery_pending = False

    def start_session(self, *, resume_run_id: str | None = None) -> ContinuousShadowSession:
        if self._blocked:
            raise RuntimeError(
                "continuous VWAP Shadow runner requires a new instance after failure"
            )
        if self.session is not None and not self.session.lock_released:
            raise RuntimeError("continuous VWAP Shadow session is already active")
        settings = self._settings()
        self._validate(settings)
        if MigrationManager(self.database.path).status().current_version != MIGRATIONS[-1].version:
            raise RuntimeError(f"continuous VWAP Shadow requires schema v{MIGRATIONS[-1].version}")

        run_id = resume_run_id or uuid4().hex
        context: ContinuousShadowResumeContext | None = None
        if resume_run_id is None:
            self.repository.create_run(run_id, settings, datetime.now(UTC))
        else:
            context = self.repository.load_vwap_shadow_resume_context(run_id)
            if context is None:
                raise RuntimeError("continuous VWAP Shadow resume state does not exist")
            self._validate_resume_context(context, settings)

        lock = ContinuousRunLock(self.database, lock_name="continuous-vwap-shadow")
        lock.acquire(run_id)
        try:
            self.observer = RuntimeObserver(
                self.database,
                RuntimeObservation(
                    run_id,
                    "shadow",
                    _STRATEGY_VERSION,
                    self.observation_clock.now().isoformat(),
                    private_connection="not_applicable",
                ),
                self.observation_clock,
            )
            self.observer.persist()
            bootstrap = self._bootstrap(settings)
            strategy = create_strategy(
                self.config.strategy.name,
                self.config.strategy.parameters,
                self.instrument,
            )
            if not isinstance(strategy, VWAPShadowStrategy):
                raise RuntimeError("continuous VWAP Shadow created an unexpected strategy")
            clock = BacktestClock(bootstrap[0].timestamp)
            portfolio = PortfolioSnapshot({}, {}, {}, trusted_for_trading=False)
            strategy.on_start(
                self._strategy_context(run_id, strategy, portfolio, clock, candle=None)
            )
            if context is None:
                for candle in bootstrap:
                    strategy.on_bar(
                        self._strategy_context(run_id, strategy, portfolio, clock, candle=candle),
                        candle,
                    )
                session = ContinuousShadowSession(
                    run_id=run_id,
                    lifecycle_state=ContinuousShadowLifecycle.BOOTSTRAPPING,
                    last_committed_checkpoint=None,
                    live_boundary=bootstrap[-1].timestamp,
                    strategy=strategy,
                    portfolio=portfolio,
                    clock=clock,
                    lock=lock,
                    bootstrap_bars=len(bootstrap),
                    bootstrap_latest_confirmed_timestamp=bootstrap[-1].timestamp,
                )
            else:
                self._validate_checkpoint_history(context, settings)
                strategy.restore_checkpoint_state(context.runtime_state)
                session = ContinuousShadowSession(
                    run_id=run_id,
                    lifecycle_state=ContinuousShadowLifecycle.BOOTSTRAPPING,
                    last_committed_checkpoint=context.checkpoint,
                    live_boundary=context.checkpoint,
                    strategy=strategy,
                    portfolio=portfolio,
                    clock=clock,
                    lock=lock,
                    bootstrap_bars=len(bootstrap),
                    bootstrap_latest_confirmed_timestamp=bootstrap[-1].timestamp,
                    processed=context.processed_count,
                    holds=context.signal_count - context.proposal_count,
                    buys=context.proposal_count,
                    proposals=context.proposal_count,
                )
            self.repository.record_run_event(
                run_id,
                "shadow_smoke_bootstrap_completed",
                {
                    "confirmed_history_bars": len(bootstrap),
                    "bootstrap_latest_confirmed_timestamp": bootstrap[-1].timestamp.isoformat(),
                },
            )
        except Exception as exc:
            self._blocked = True
            try:
                if self.observer is not None:
                    self.observer.state.lifecycle = "blocked"
                    self.observer.state.stop_reason = f"startup_failed:{type(exc).__name__}"
                    self.observer.alert("checkpoint_invalid")
                    self.observer.persist()
                self.repository.finish(
                    run_id, "failed", f"startup_failed:{type(exc).__name__}", touch_heartbeat=False
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "startup failure evidence could not be persisted", extra={"run_id": run_id}
                )
            finally:
                lock.release(run_id, "continuous_vwap_shadow_start_failed")
            raise
        self.session = session
        self._recovery_pending = resume_run_id is not None
        return session

    def handle_ws_connected(self, session: ContinuousShadowSession, generation: int) -> None:
        if session.lifecycle_state is not ContinuousShadowLifecycle.BOOTSTRAPPING:
            raise RuntimeError("initial connection is only valid while bootstrapping")
        if generation <= 0:
            raise ValueError("connection generation must be positive")
        session.connection_generation = generation
        session.lifecycle_state = ContinuousShadowLifecycle.READY
        self._observe(session)

    def handle_ws_disconnected(self, session: ContinuousShadowSession, generation: int) -> None:
        if generation != session.connection_generation:
            return
        if session.lifecycle_state in {
            ContinuousShadowLifecycle.READY,
            ContinuousShadowLifecycle.RECONCILING,
        }:
            session.disconnect_count += 1
            session.lifecycle_state = ContinuousShadowLifecycle.STALE
            self._observe(session)
            if self.observer is not None:
                self.observer.alert("public_disconnected")

    def handle_ws_reconnected(self, session: ContinuousShadowSession, generation: int) -> None:
        if session.lifecycle_state is not ContinuousShadowLifecycle.STALE:
            raise RuntimeError("reconnect is only valid after a disconnect")
        if generation <= session.connection_generation:
            raise RuntimeError("reconnect generation must advance")
        session.connection_generation = generation
        session.lifecycle_state = ContinuousShadowLifecycle.RECONCILING
        self._observe(session)

    async def reconcile_after_reconnect(self, session: ContinuousShadowSession) -> None:
        if session.lifecycle_state is not ContinuousShadowLifecycle.RECONCILING:
            raise RuntimeError("reconciliation requires reconnecting state")
        try:
            worker = asyncio.create_task(
                asyncio.to_thread(self._reconcile_from_persisted_checkpoint, session)
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling to_thread alone leaves a writer behind. Join it
                # before releasing the lease; the provider owns its I/O timeout.
                session.stop_requested = True
                await asyncio.gather(worker, return_exceptions=True)
                raise
        except Exception as exc:
            session.lifecycle_state = ContinuousShadowLifecycle.BLOCKED
            self._blocked = True
            self._record_failure(session, exc)
            raise
        if session.lifecycle_state is ContinuousShadowLifecycle.RECONCILING:
            session.lifecycle_state = ContinuousShadowLifecycle.READY
            if self.observer is not None:
                self.observer.alert("public_disconnected", active=False)
            self._observe(session)

    def handle_live_candle(
        self,
        session: ContinuousShadowSession,
        candle: Candle,
        *,
        generation: int | None = None,
    ) -> bool:
        if self.observer is not None and (
            generation is None or generation == session.connection_generation
        ):
            self.observer.state.market_received_at = self.observation_clock.now().isoformat()
        if not session.can_process_live_candle or (
            generation is not None and generation != session.connection_generation
        ):
            session.rejected_live_candles += 1
            return False
        if not candle.confirmed:
            return False
        if candle.timestamp <= session.live_boundary:
            session.duplicates += 1
            return False
        interval = BAR_INTERVALS[_BAR]
        try:
            if candle.timestamp > session.live_boundary + interval:
                session.gaps += 1
                for missing in self._reconcile_live_gap(
                    session.live_boundary, candle.timestamp, self._settings()
                ):
                    self._process(session, missing)
            if candle.timestamp != session.live_boundary + interval:
                raise MarketDataError("public candle continuity is not restored")
            self._process(session, candle)
        except Exception as exc:
            session.lifecycle_state = ContinuousShadowLifecycle.BLOCKED
            self._blocked = True
            self._record_failure(session, exc)
            raise
        return True

    def stop_session(
        self,
        session: ContinuousShadowSession,
        *,
        persist: bool = True,
        reason: str = "bounded_observation_complete",
        final_status: str = "stopped",
    ) -> None:
        session.stop_requested = True
        try:
            if persist:
                self.repository.finish(session.run_id, final_status, reason)
                if self.observer is not None:
                    self.observer.state.lifecycle = final_status
                    self.observer.state.stop_reason = reason
                    self.observer.persist()
            session.lifecycle_state = ContinuousShadowLifecycle.STOPPED
        finally:
            self._release_lock(session, "continuous_vwap_shadow_finished")

    async def run(
        self,
        live_candles: AsyncIterable[Candle],
        *,
        maximum_confirmed_bars: int = 0,
        resume_run_id: str | None = None,
    ) -> ContinuousVWAPShadowResult:
        """Compatibility orchestration for deterministic candle-only inputs."""
        session = self.start_session(resume_run_id=resume_run_id)
        processed_at_start = session.processed
        self.handle_ws_connected(session, 1)
        try:
            async for candle in live_candles:
                self.handle_live_candle(session, candle, generation=1)
                if (
                    maximum_confirmed_bars
                    and session.processed - processed_at_start >= maximum_confirmed_bars
                ):
                    break
            self.stop_session(session)
            return self._result(session)
        except asyncio.CancelledError:
            self.stop_session(
                session,
                reason="continuous_vwap_shadow_task_cancelled",
                final_status="interrupted",
            )
            raise
        except Exception as exc:
            self._record_failure(session, exc)
            raise
        finally:
            close = getattr(live_candles, "aclose", None)
            if callable(close):
                await close()

    async def run_events(
        self,
        events: AsyncIterable[PublicWebSocketEvent],
        *,
        maximum_confirmed_bars: int = 0,
        resume_run_id: str | None = None,
    ) -> ContinuousVWAPShadowResult:
        """Orchestrate lifecycle events and keep reconnect reconciliation mandatory."""
        session = self.start_session(resume_run_id=resume_run_id)
        processed_at_start = session.processed
        iterator = events.__aiter__()
        next_event: asyncio.Task[PublicWebSocketEvent] = asyncio.create_task(
            _next_public_event(iterator)
        )
        reconciliation: asyncio.Task[None] | None = None
        try:
            while not session.stop_requested:
                waiting: set[asyncio.Task[object]] = {next_event}
                if reconciliation is not None:
                    waiting.add(reconciliation)
                done, _ = await asyncio.wait(
                    waiting, timeout=self.heartbeat_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    self.pulse(session)
                    continue

                if next_event in done:
                    try:
                        event = next_event.result()
                    except StopAsyncIteration:
                        break
                    next_event = asyncio.create_task(_next_public_event(iterator))
                    if event.event_type is PublicWebSocketEventType.CONNECTED:
                        self.handle_ws_connected(session, event.generation)
                    elif event.event_type is PublicWebSocketEventType.DISCONNECTED:
                        self.handle_ws_disconnected(session, event.generation)
                    elif event.event_type is PublicWebSocketEventType.RECONNECTED:
                        self.handle_ws_reconnected(session, event.generation)
                        if reconciliation is not None:
                            await reconciliation
                        reconciliation = asyncio.create_task(
                            self.reconcile_after_reconnect(session)
                        )
                    elif event.event_type is PublicWebSocketEventType.CANDLE:
                        if event.candle is None:
                            raise MarketDataError("WebSocket candle event has no candle")
                        self.handle_live_candle(session, event.candle, generation=event.generation)
                    elif event.event_type is PublicWebSocketEventType.CLOSED:
                        session.stop_requested = True

                    if (
                        maximum_confirmed_bars
                        and session.processed - processed_at_start >= maximum_confirmed_bars
                    ):
                        session.stop_requested = True

                if reconciliation is not None and reconciliation in done:
                    await reconciliation
                    reconciliation = None

            if reconciliation is not None:
                await reconciliation
            self.stop_session(session)
            return self._result(session)
        except asyncio.CancelledError:
            session.stop_requested = True
            if reconciliation is not None:
                reconciliation.cancel()
                await asyncio.gather(reconciliation, return_exceptions=True)
            self.stop_session(
                session,
                reason="continuous_vwap_shadow_task_cancelled",
                final_status="interrupted",
            )
            raise
        except Exception as exc:
            if session.lifecycle_state is not ContinuousShadowLifecycle.BLOCKED:
                session.lifecycle_state = ContinuousShadowLifecycle.BLOCKED
                self._blocked = True
            session.stop_requested = True
            if reconciliation is not None:
                reconciliation.cancel()
                await asyncio.gather(reconciliation, return_exceptions=True)
            self._record_failure(session, exc)
            raise
        finally:
            pending = [
                task
                for task in (next_event, reconciliation)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(
                *[task for task in (next_event, reconciliation) if task is not None],
                return_exceptions=True,
            )
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await close()

    def _observe(self, session: ContinuousShadowSession) -> None:
        if self.observer is None:
            return
        self.observer.state.lifecycle = session.lifecycle_state.value
        self.observer.state.public_connection = (
            "connected"
            if session.lifecycle_state is ContinuousShadowLifecycle.READY
            else session.lifecycle_state.value
        )
        self.observer.state.public_reconnects = max(0, session.connection_generation - 1)
        self.observer.state.checkpoint = (
            session.last_committed_checkpoint.isoformat()
            if session.last_committed_checkpoint
            else None
        )
        self.observer.persist()

    def pulse(self, session: ContinuousShadowSession) -> None:
        """Heartbeat, lease renewal and independent market/progress watchdog."""
        self._observe(session)
        session.lock.renew(session.run_id)
        assert self.observer is not None
        health = assess(asdict(self.observer.state), self.observation_clock.now())
        for reason in ("market_stale", "progress_stalled"):
            if reason in health["blocking_reasons"]:
                self.observer.alert(reason)
                session.lifecycle_state = ContinuousShadowLifecycle.BLOCKED
                self._blocked = True
                self.observer.state.lifecycle = "blocked"
                self.observer.state.stop_reason = reason
                self.observer.persist()
                raise MarketDataError(reason)

    def _record_failure(self, session: ContinuousShadowSession, error: Exception) -> None:
        self._blocked = True
        session.lifecycle_state = ContinuousShadowLifecycle.BLOCKED
        try:
            if self.observer is not None:
                self.observer.state.lifecycle = "blocked"
                self.observer.state.stop_reason = (
                    self.observer.state.stop_reason or type(error).__name__
                )
                self.observer.alert(
                    "storage_failure" if isinstance(error, StorageError) else "task_failed"
                )
                self.observer.persist()
            self.repository.finish(
                session.run_id,
                "failed",
                self.observer.state.stop_reason if self.observer else type(error).__name__,
                touch_heartbeat=False,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "runtime failure evidence could not be persisted", extra={"run_id": session.run_id}
            )
        finally:
            try:
                self._release_lock(session, "continuous_vwap_shadow_failed")
            except Exception:
                logging.getLogger(__name__).exception(
                    "runtime lease release failed; stale-owner recovery required",
                    extra={"run_id": session.run_id},
                )

    def _process(self, session: ContinuousShadowSession, candle: Candle) -> None:
        if session.stop_requested:
            raise RuntimeError("checkpoint reconciliation stopped")
        settings = self._settings()
        session.clock.advance_to(candle.timestamp + BAR_INTERVALS[_BAR])
        signal = session.strategy.on_bar(
            self._strategy_context(
                session.run_id,
                session.strategy,
                session.portfolio,
                session.clock,
                candle=candle,
            ),
            candle,
        )[0]
        state = session.strategy.checkpoint_state()
        next_buys = session.buys + int(signal.action is SignalAction.BUY)
        next_proposals = session.proposals + int(signal.action is SignalAction.BUY)
        next_holds = session.holds + int(signal.action is SignalAction.HOLD)
        committed = self.repository.commit_vwap_shadow_candle(
            run_id=session.run_id,
            config=settings,
            candle=candle,
            strategy_version=_STRATEGY_VERSION,
            signal_id=signal.signal_id,
            signal_type=signal.action.value,
            signal_value=json.dumps(
                {
                    "close": str(signal.metadata["close"]),
                    "vwap": str(signal.metadata["vwap"])
                    if signal.metadata["vwap"] is not None
                    else None,
                    "deviation_bps": str(signal.metadata["deviation_bps"])
                    if signal.metadata["deviation_bps"] is not None
                    else None,
                    "vwap_window": signal.metadata["vwap_window"],
                },
                sort_keys=True,
            ),
            runtime_state=json.dumps(state, sort_keys=True),
            warmup_count=int(signal.metadata["window_length"]),
            warmup_completed=signal.metadata["vwap"] is not None,
            proposal_price=candle.close if signal.action is SignalAction.BUY else None,
            processed_count=session.processed + 1,
            signal_count=next_buys + next_holds,
            proposal_count=next_proposals,
        )
        if not committed:
            raise RuntimeError("duplicate candle reached the VWAP business transaction")
        session.buys = next_buys
        session.proposals = next_proposals
        session.holds = next_holds
        session.processed += 1
        session.last_committed_checkpoint = candle.timestamp
        session.live_boundary = candle.timestamp
        if self.observer is not None:
            self.observer.state.confirmed_processed_at = self.observation_clock.now().isoformat()
            if self._recovery_pending:
                for code in ("storage_failure", "task_failed", "market_stale", "progress_stalled"):
                    self.observer.alert(code, active=False)
                self._recovery_pending = False
            self._observe(session)
        session.lock.renew(session.run_id)

    def _reconcile_from_persisted_checkpoint(self, session: ContinuousShadowSession) -> None:
        settings = self._settings()
        context = self.repository.load_vwap_shadow_resume_context(session.run_id)
        if context is None:
            raise MarketDataError("reconnect has no persisted VWAP checkpoint")
        self._validate_resume_context(context, settings)
        self._validate_checkpoint_history(context, settings)
        session.strategy.restore_checkpoint_state(context.runtime_state)
        session.last_committed_checkpoint = context.checkpoint
        session.live_boundary = context.checkpoint
        session.processed = context.processed_count
        session.buys = session.proposals = context.proposal_count
        session.holds = context.signal_count - context.proposal_count

        raw = self.history.get_historical_bars(_INSTRUMENT, _BAR, limit=_RECONCILIATION_LIMIT)
        confirmed = [candle for candle in raw if candle.confirmed]
        if not confirmed:
            raise MarketDataError("public REST reconciliation has no confirmed candles")
        timestamps = [candle.timestamp.astimezone(UTC) for candle in confirmed]
        if len(set(timestamps)) != len(timestamps):
            raise MarketDataError("public REST reconciliation contains duplicate candles")
        normalized = normalize_candles(confirmed, bar=_BAR)
        missing = [candle for candle in normalized if candle.timestamp > context.checkpoint]
        interval = BAR_INTERVALS[_BAR]
        if missing and missing[0].timestamp != context.checkpoint + interval:
            raise MarketDataError("public REST reconciliation is incomplete")
        for candle in missing:
            self._process(session, candle)

        expected = missing[-1].timestamp if missing else context.checkpoint
        persisted = self.repository.load_vwap_shadow_resume_context(session.run_id)
        if persisted is None or persisted.checkpoint != expected:
            raise RuntimeError("persisted checkpoint did not reach reconciliation boundary")

    def _bootstrap(self, settings: ContinuousVWAPShadowConfiguration) -> list[Candle]:
        required = settings.vwap_window + settings.bootstrap_margin
        candles = [
            candle
            # OKX may include the currently open, unconfirmed bar in a limit response.
            # Request one raw bar beyond the confirmed VWAP bootstrap requirement.
            for candle in self.history.get_historical_bars(_INSTRUMENT, _BAR, limit=required + 1)
            if candle.confirmed
        ]
        normalized = normalize_candles(candles, bar=_BAR)
        if len(normalized) < required:
            raise MarketDataError("continuous VWAP Shadow bootstrap history is insufficient")
        return normalized[-required:]

    def _reconcile_live_gap(
        self,
        last: datetime,
        current: datetime,
        settings: ContinuousVWAPShadowConfiguration,
    ) -> list[Candle]:
        interval = BAR_INTERVALS[_BAR]
        missing_count = int((current - last) / interval) - 1
        candles = self.history.get_historical_bars(
            _INSTRUMENT,
            _BAR,
            limit=missing_count + settings.bootstrap_margin + 1,
        )
        missing = [
            candle
            for candle in normalize_candles(candles, bar=_BAR)
            if last < candle.timestamp < current and candle.confirmed
        ]
        expected = [last + interval * index for index in range(1, missing_count + 1)]
        if [candle.timestamp for candle in missing] != expected:
            raise MarketDataError("public REST reconciliation is incomplete")
        return missing

    def _result(self, session: ContinuousShadowSession) -> ContinuousVWAPShadowResult:
        return ContinuousVWAPShadowResult(
            session.run_id,
            session.bootstrap_bars,
            session.processed,
            session.holds,
            session.buys,
            session.proposals,
            session.duplicates,
            session.gaps,
        )

    def _release_lock(self, session: ContinuousShadowSession, reason: str) -> None:
        if not session.lock_released:
            session.lock.release(session.run_id, reason)
            session.lock_released = True

    def _strategy_context(
        self,
        run_id: str,
        strategy: VWAPShadowStrategy,
        portfolio: PortfolioSnapshot,
        clock: BacktestClock,
        *,
        candle: Candle | None,
    ) -> StrategyContext:
        return StrategyContext(
            run_id,
            TradingMode.BACKTEST,
            strategy.name,
            self.instrument,
            _BAR,
            portfolio,
            MarketSnapshot(candle, candle.close) if candle is not None else None,
            clock,
            read_only=True,
        )

    def _validate(self, settings: ContinuousVWAPShadowConfiguration) -> None:
        if (
            self.config.market.instrument_id,
            self.config.market.bar.lower(),
            self.config.strategy.name,
        ) != (_INSTRUMENT, _BAR, "vwap_shadow"):
            raise ValueError("continuous VWAP Shadow is fixed to BTC-USDT 1H vwap_shadow")
        if self.instrument.instrument_id != _INSTRUMENT:
            raise ValueError("continuous VWAP Shadow instrument mismatch")
        if settings.bootstrap_margin <= 0:
            raise ValueError("bootstrap margin must be positive")

    def _settings(self) -> ContinuousVWAPShadowConfiguration:
        parameters = self.config.strategy.parameters
        return ContinuousVWAPShadowConfiguration(
            vwap_window=int(parameters["vwap_window"]),
            buy_deviation_bps=Decimal(str(parameters["buy_deviation_bps"])),
        )

    def _validate_resume_context(
        self,
        context: ContinuousShadowResumeContext,
        settings: ContinuousVWAPShadowConfiguration,
    ) -> None:
        if (
            context.strategy_name,
            context.instrument_id,
            context.timeframe,
        ) != (settings.strategy_name, settings.instrument_id, settings.timeframe):
            raise RuntimeError("continuous VWAP Shadow resume identity does not match")
        if context.configuration_hash != configuration_fingerprint(settings):
            raise RuntimeError("continuous VWAP Shadow resume configuration does not match")

    def _validate_checkpoint_history(
        self,
        context: ContinuousShadowResumeContext,
        settings: ContinuousVWAPShadowConfiguration,
    ) -> None:
        bars = context.runtime_state.get("bars")
        if not isinstance(bars, list) or not bars:
            raise RuntimeError("continuous VWAP Shadow checkpoint has no rolling window")
        checkpoint_times = [str(item.get("timestamp")) for item in bars if isinstance(item, dict)]
        if len(checkpoint_times) != len(bars):
            raise RuntimeError("continuous VWAP Shadow checkpoint bars are invalid")
        historical = self.history.get_historical_bars(
            _INSTRUMENT,
            _BAR,
            limit=_RECONCILIATION_LIMIT,
        )
        normalized = normalize_candles(
            [candle for candle in historical if candle.confirmed], bar=_BAR
        )
        window = [candle for candle in normalized if candle.timestamp <= context.checkpoint][
            -len(bars) :
        ]
        if (
            len(window) != len(bars)
            or window[-1].timestamp != context.checkpoint
            or [candle.timestamp.isoformat() for candle in window] != checkpoint_times
        ):
            raise MarketDataError("continuous VWAP Shadow checkpoint history does not align")
        for candle, saved in zip(window, bars, strict=True):
            assert isinstance(saved, dict)
            if saved.get("confirmed") is not True or any(
                Decimal(str(saved[field])) != getattr(candle, field)
                for field in ("open", "high", "low", "close", "volume")
            ):
                raise MarketDataError(
                    "continuous VWAP Shadow checkpoint history content does not match"
                )
