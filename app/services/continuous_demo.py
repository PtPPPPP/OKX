from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.domain.market import Candle
from app.market.websocket import OKXPublicWebSocketProvider
from app.services.continuous_runtime_safety import (
    ContinuousTaskSupervisor,
    ShadowAccountBaseline,
    ShadowAccountBaselineRepository,
    SupervisedTaskDefinition,
    baseline_from_session,
)
from app.services.continuous_safety import (
    ContinuousDemoCircuitBreaker,
    ContinuousDemoCircuitBreakerExecutor,
    ContinuousDemoReconciliationLoop,
    ContinuousRunContext,
)
from app.services.continuous_shadow_repository import ContinuousRunLock, ContinuousShadowRepository
from app.services.demo_session import DemoTradingSession
from app.storage.database import Database
from app.strategies.vwap_mean_reversion import _rsi, _vwap


@dataclass(frozen=True, slots=True)
class ContinuousDemoConfiguration:
    instrument_id: str
    strategy_name: str
    timeframe: str
    maximum_runtime_minutes: int = 30
    warmup_candle_count: int = 35
    maximum_order_submissions: int = 0
    fault_injection: str | None = None
    minimum_confirmed_candles: int = 3


@dataclass(frozen=True, slots=True)
class ContinuousDemoRunResult:
    run_id: str
    status: str
    processed_candle_count: int
    generated_signal_count: int
    shadow_proposal_count: int
    broker_write_calls: int = 0


class ContinuousDemoEngine:
    """Strictly read-only shadow loop; it deliberately has no broker member."""

    def __init__(
        self, database: Database, session: DemoTradingSession, stream: OKXPublicWebSocketProvider
    ) -> None:
        self.database, self.session, self.stream = database, session, stream
        self.repository = ContinuousShadowRepository(database)
        self.lock = ContinuousRunLock(database)
        self.breaker = ContinuousDemoCircuitBreaker()
        self.breaker_executor = ContinuousDemoCircuitBreakerExecutor(database)
        self._stop = False

    async def run(self, configuration: ContinuousDemoConfiguration) -> ContinuousDemoRunResult:
        if configuration.maximum_order_submissions != 0:
            raise ValueError("shadow mode requires maximum_order_submissions=0")
        run_id = uuid4().hex
        self.repository.create_run(run_id, configuration, datetime.now(UTC))
        self.lock.acquire(run_id)
        processed = signals = proposals = 0
        frozen = False
        closes: list[Decimal] = []
        vwap_bars: deque[Candle] = deque(maxlen=26)
        previous_relation: str | None = None
        if configuration.fault_injection is not None:
            fixture_baseline = ShadowAccountBaseline(
                uuid4().hex,
                run_id,
                "fixture",
                "spot",
                "cash",
                "demo",
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                (),
                (),
                (),
                None,
                None,
                0,
                0,
                datetime.now(UTC),
            )
            ShadowAccountBaselineRepository(self.database).save_once(fixture_baseline)
            fault_codes = {
                "private-ws-disconnect": "private_stream_unhealthy",
                "reconciliation-failure": "reconciliation_task_failed",
                "heartbeat-failure": "heartbeat_failed",
                "lease-loss": "lock_lost",
                "database-write-failure": "database_write_failed",
                "public-ws-disconnect": "public_stream_reconnect_exhausted",
                "external-order-detected": "external_order_detected",
                "external-fill-detected": "external_fill_detected",
                "btc-balance-change": "external_account_balance_change",
                "usdt-balance-change": "external_account_balance_change",
            }
            code = fault_codes.get(configuration.fault_injection)
            if code is None:
                self.lock.release(run_id, "invalid_fault_injection")
                raise ValueError("unsupported fault injection")
            fault_context = ContinuousRunContext(
                reason=code,
                reconciliation_healthy="reconciliation" not in code,
                private_stream_healthy="private" not in code,
                lock_healthy=code != "lock_lost",
                database_healthy="database" not in code,
            )
            decision = self.breaker.evaluate(fault_context)
            if decision.code == "clear":
                decision = type(decision)("freeze", code, "p1", False)
            await self.breaker_executor.apply(run_id, decision)
            self.lock.release(run_id, "fault_injection")
            return ContinuousDemoRunResult(run_id, "frozen", 0, 0, 0)
        self.session.start()
        baseline_repository = ShadowAccountBaselineRepository(self.database)
        baseline = baseline_from_session(run_id, self.session)
        baseline_repository.save_once(baseline)
        reconciliation = ContinuousDemoReconciliationLoop(
            self.database, self.session, run_id, baseline
        )
        initial_reconciliation = await reconciliation.run_once(run_id)
        if initial_reconciliation.status != "healthy":
            frozen = True
            await self.breaker_executor.apply(
                run_id, self.breaker.evaluate(ContinuousRunContext(reconciliation_healthy=False))
            )
            self.session.close()
            self.lock.release(run_id, "initial_reconciliation_failed")
            return ContinuousDemoRunResult(run_id, "frozen", 0, 0, 0)
        with self.database.connect() as c:
            c.execute(
                "UPDATE continuous_demo_runs SET reconciliation_status='healthy',initial_reconciliation_status='healthy',last_reconciliation_at=? WHERE run_id=?",
                (datetime.now(UTC).isoformat(), run_id),
            )
        history = [
            item
            for item in self.session.client.get_history_candles(
                configuration.instrument_id,
                configuration.timeframe,
                max(configuration.warmup_candle_count, configuration.minimum_confirmed_candles, 35),
            )
            if item.confirmed
        ]
        history.sort(key=lambda item: item.timestamp)
        closes = [item.close for item in history[-max(configuration.warmup_candle_count, 35) :]]
        if len(closes) < 30:
            raise RuntimeError("shadow warmup history is insufficient")
        fast = sum(closes[-10:], Decimal("0")) / Decimal("10")
        slow = sum(closes[-30:], Decimal("0")) / Decimal("30")
        previous_relation = (
            "fast_above_slow"
            if fast > slow
            else "fast_below_slow"
            if fast < slow
            else "fast_equal_slow"
        )
        self.repository.save_runtime(
            run_id,
            configuration,
            candle_time=history[-1].timestamp,
            fast=fast,
            slow=slow,
            relation=previous_relation,
            signal_type=None,
            warmup_count=len(closes),
            warmup_completed=True,
        )
        reconciliation_stop = asyncio.Event()

        async def task_failure(failed_run_id: str, task_name: str) -> None:
            await self.breaker_executor.apply(
                failed_run_id,
                self.breaker.evaluate(
                    ContinuousRunContext(reconciliation_healthy=False, reason=f"{task_name}_failed")
                ),
            )

        supervisor = ContinuousTaskSupervisor(self.database, task_failure)

        async def heartbeat_loop() -> None:
            while not reconciliation_stop.is_set():
                await asyncio.sleep(10)
                self.repository.heartbeat(
                    run_id,
                    processed,
                    signals,
                    proposals,
                    private_status="ready",
                    public_status="ready",
                )
                self.lock.renew(run_id)

        async def private_watch() -> None:
            while not reconciliation_stop.is_set():
                health = self.session.stream.health
                if (
                    not (health.connected and health.authenticated and health.subscriptions_ready)
                    or health.stale
                ):
                    await self.breaker_executor.apply(
                        run_id,
                        self.breaker.evaluate(ContinuousRunContext(private_stream_healthy=False)),
                    )
                    raise RuntimeError("private_stream_unhealthy")
                await asyncio.sleep(2)

        reconciliation_task = asyncio.create_task(
            supervisor.run(
                run_id,
                [
                    SupervisedTaskDefinition("reconciliation_loop", True, "never", 0),
                    SupervisedTaskDefinition("heartbeat_and_lease", True, "never", 0),
                    SupervisedTaskDefinition("private_websocket", True, "never", 0),
                ],
                {
                    "reconciliation_loop": reconciliation.start(run_id, reconciliation_stop),
                    "heartbeat_and_lease": heartbeat_loop(),
                    "private_websocket": private_watch(),
                },
            ),
            name="task_supervisor",
        )
        deadline = asyncio.get_running_loop().time() + configuration.maximum_runtime_minutes * 60
        try:
            with self.database.connect() as c:
                c.execute(
                    "UPDATE continuous_demo_runs SET status='shadow_running',private_stream_status='ready' WHERE run_id=?",
                    (run_id,),
                )

            async def candidate_candles() -> AsyncIterator[Candle]:
                if configuration.minimum_confirmed_candles > 3:
                    for item in history[-configuration.minimum_confirmed_candles :]:
                        yield item
                async for item in self.stream.stream_confirmed_candles(
                    configuration.instrument_id, configuration.timeframe
                ):
                    yield item

            candle_iterator = candidate_candles().__aiter__()
            while True:
                if self._stop or processed >= configuration.minimum_confirmed_candles:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    candle = await asyncio.wait_for(candle_iterator.__anext__(), timeout=remaining)
                except (StopAsyncIteration, TimeoutError):
                    break
                if not self.repository.claim_candle(
                    run_id, configuration, candle, "moving_average_cross_v1"
                ):
                    continue
                health = self.session.stream.health
                with self.database.connect() as status_connection:
                    reconciliation_row = status_connection.execute(
                        "SELECT reconciliation_status FROM continuous_demo_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                if reconciliation_row is None or str(reconciliation_row[0]) != "healthy":
                    frozen = True
                    await self.breaker_executor.apply(
                        run_id,
                        self.breaker.evaluate(ContinuousRunContext(reconciliation_healthy=False)),
                    )
                    break
                if reconciliation_task.done() and (
                    reconciliation_task.exception() is not None
                    or reconciliation_task.result().status == "failed"
                ):
                    frozen = True
                    await self.breaker_executor.apply(
                        run_id,
                        self.breaker.evaluate(ContinuousRunContext(reconciliation_healthy=False)),
                    )
                    break
                decision = self.breaker.evaluate(
                    ContinuousRunContext(
                        private_stream_healthy=health.connected
                        and health.authenticated
                        and health.subscriptions_ready
                        and not health.stale
                    )
                )
                if decision.action != "continue":
                    frozen = True
                    await self.breaker_executor.apply(run_id, decision)
                    break
                closes.append(candle.close)
                if configuration.strategy_name == "vwap_mean_reversion":
                    vwap_bars.append(candle)
                    signal = None
                    if len(vwap_bars) >= 24:
                        vwap = _vwap(vwap_bars, 24)
                        rsi = _rsi(vwap_bars, 14)
                        if (
                            vwap is not None
                            and rsi is not None
                            and candle.close <= vwap * Decimal("0.992")
                            and rsi < Decimal("35")
                        ):
                            signal = "buy_mean_reversion"
                    state_hash = self.repository.save_runtime(
                        run_id,
                        configuration,
                        candle_time=candle.timestamp,
                        fast=None,
                        slow=None,
                        relation=None,
                        signal_type=signal,
                        warmup_count=len(vwap_bars),
                        warmup_completed=len(vwap_bars) >= 24,
                    )
                    signal_id = self.repository.save_signal(
                        run_id,
                        configuration,
                        candle,
                        "vwap",
                        "vwap",
                        signal,
                        state_hash,
                        "shadow_candidate" if signal else "no_signal",
                        [],
                    )
                    if signal:
                        self.repository.save_proposal(
                            run_id,
                            signal_id,
                            configuration,
                            "buy",
                            candle.close,
                            Decimal("0"),
                            "blocked",
                            ["shadow_only", "not_sized"],
                        )
                        signals += 1
                        proposals += 1
                    processed += 1
                    self.repository.heartbeat(
                        run_id,
                        processed,
                        signals,
                        proposals,
                        private_status="ready",
                        public_status="ready",
                    )
                    self.lock.renew(run_id)
                    continue
                if len(closes) < 30:
                    self.repository.save_runtime(
                        run_id,
                        configuration,
                        candle_time=candle.timestamp,
                        fast=None,
                        slow=None,
                        relation=None,
                        signal_type=None,
                        warmup_count=len(closes),
                        warmup_completed=False,
                    )
                else:
                    fast = sum(closes[-10:], Decimal("0")) / Decimal("10")
                    slow = sum(closes[-30:], Decimal("0")) / Decimal("30")
                    relation = (
                        "fast_above_slow"
                        if fast > slow
                        else "fast_below_slow"
                        if fast < slow
                        else "fast_equal_slow"
                    )
                    signal = (
                        "buy_cross"
                        if previous_relation in {"fast_below_slow", "fast_equal_slow"}
                        and relation == "fast_above_slow"
                        else "sell_cross"
                        if previous_relation in {"fast_above_slow", "fast_equal_slow"}
                        and relation == "fast_below_slow"
                        else None
                    )
                    state_hash = self.repository.save_runtime(
                        run_id,
                        configuration,
                        candle_time=candle.timestamp,
                        fast=fast,
                        slow=slow,
                        relation=relation,
                        signal_type=signal,
                        warmup_count=len(closes),
                        warmup_completed=True,
                    )
                    signal_id = self.repository.save_signal(
                        run_id,
                        configuration,
                        candle,
                        previous_relation or "unknown",
                        relation,
                        signal,
                        state_hash,
                        "shadow_candidate" if signal else "no_signal",
                        [],
                    )
                    if signal:
                        self.repository.save_proposal(
                            run_id,
                            signal_id,
                            configuration,
                            "buy" if signal == "buy_cross" else "sell",
                            candle.close,
                            Decimal("0"),
                            "blocked",
                            (
                                ["shadow_only", "no_strategy_managed_inventory", "not_sized"]
                                if signal == "sell"
                                else ["shadow_only", "not_sized"]
                            ),
                        )
                        signals += 1
                        proposals += 1
                    previous_relation = relation
                processed += 1
                self.repository.heartbeat(
                    run_id,
                    processed,
                    signals,
                    proposals,
                    private_status="ready",
                    public_status="ready",
                )
                self.lock.renew(run_id)
                if processed >= 3:
                    break
            final_reconciliation = await reconciliation.run_once(run_id)
            final_status = (
                "stopped" if not frozen and final_reconciliation.status == "healthy" else "frozen"
            )
            self.repository.finish(
                run_id, final_status, "manual_stop_requested" if self._stop else None
            )
            return ContinuousDemoRunResult(run_id, final_status, processed, signals, proposals)
        except Exception as exc:
            self.repository.finish(run_id, "frozen", f"{type(exc).__name__}:{exc}")
            raise
        finally:
            reconciliation_stop.set()
            reconciliation_task.cancel()
            await asyncio.gather(reconciliation_task, return_exceptions=True)
            await self.stream.stop()
            self.session.close()
            self.lock.release(run_id, "run_finished")
