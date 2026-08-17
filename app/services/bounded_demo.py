from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import Order, OrderSide, OrderState, OrderType
from app.domain.position import PortfolioSnapshot
from app.domain.signal import SignalAction
from app.exchange.exceptions import NetworkError, OrderRejected
from app.market.websocket import OKXPublicWebSocketProvider
from app.runtime.clock import BacktestClock
from app.services.continuous_runtime_safety import (
    ShadowAccountBaselineRepository,
    baseline_from_session,
)
from app.services.continuous_shadow_repository import ContinuousRunLock, ContinuousShadowRepository
from app.services.controlled_demo_write import ControlledDemoWriteService
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    ProposalStatus,
)
from app.services.demo_order_revalidation import DemoOrderProposalRevalidator
from app.services.demo_session import DemoTradingSession
from app.services.reconciliation import ReconciliationStatus
from app.storage.database import Database
from app.storage.repositories import PrivateStateFenceDeferred, TradingRepository
from app.strategies.registry import create_strategy


@dataclass(frozen=True, slots=True)
class BoundedDemoConfiguration:
    instrument_id: str
    strategy_name: str
    timeframe: str
    maximum_runtime_minutes: int = 60
    maximum_confirmed_decision_candles: int = 6
    maximum_order_submissions: int = 2
    maximum_orders_per_hour: int = 2
    maximum_open_orders: int = 1
    maximum_notional_per_order: Decimal = Decimal("5")
    maximum_managed_exposure: Decimal = Decimal("5")
    maximum_runtime_seconds: int | None = None

    @property
    def runtime_seconds(self) -> int:
        seconds = (
            self.maximum_runtime_minutes * 60
            if self.maximum_runtime_seconds is None
            else self.maximum_runtime_seconds
        )
        if seconds <= 0:
            raise ValueError("bounded demo runtime must be positive")
        return seconds


@dataclass(frozen=True, slots=True)
class BoundedDemoRunResult:
    run_id: str
    status: str
    processed_candle_count: int
    generated_signal_count: int
    proposal_count: int
    submitted_order_count: int
    unknown_order_count: int = 0
    broker_write_calls: int = 0


@dataclass(frozen=True, slots=True)
class CrossSignalDetector:
    fast_window: int
    slow_window: int

    def __post_init__(self) -> None:
        if self.fast_window <= 0 or self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be positive and less than slow_window")

    def relation(self, closes: list[Decimal]) -> str:
        if len(closes) < self.slow_window:
            raise ValueError("insufficient closes for strategy warmup")
        fast = sum(closes[-self.fast_window :], Decimal("0")) / Decimal(self.fast_window)
        slow = sum(closes[-self.slow_window :], Decimal("0")) / Decimal(self.slow_window)
        return (
            "fast_above_slow"
            if fast > slow
            else "fast_below_slow"
            if fast < slow
            else "fast_equal_slow"
        )

    @staticmethod
    def signal(previous: str, current: str) -> str | None:
        if previous in {"fast_below_slow", "fast_equal_slow"} and current == "fast_above_slow":
            return "buy_cross"
        if previous in {"fast_above_slow", "fast_equal_slow"} and current == "fast_below_slow":
            return "sell_cross"
        return None


class BoundedDemoEngine:
    """The only write-enabled continuous engine; all limits are enforced locally."""

    def __init__(
        self, database: Database, session: DemoTradingSession, stream: OKXPublicWebSocketProvider
    ) -> None:
        self.database, self.session, self.stream = database, session, stream
        self.runs = ContinuousShadowRepository(database)
        self.repository = TradingRepository(database)
        self.lock = ContinuousRunLock(database)

    async def run(
        self,
        config: BoundedDemoConfiguration,
        run_config: RunConfig,
        *,
        resume_run_id: str | None = None,
    ) -> BoundedDemoRunResult:
        if run_config.mode is not TradingMode.DEMO or not run_config.exchange.simulated:
            raise ValueError("bounded demo requires demo simulated configuration")
        if (
            config.instrument_id != "BTC-USDT"
            or config.runtime_seconds > 180 * 60
            or config.maximum_confirmed_decision_candles > 6
            or config.maximum_notional_per_order > Decimal("5")
            or config.maximum_managed_exposure > Decimal("5")
        ):
            raise ValueError("bounded demo limits exceeded")
        if resume_run_id is None:
            run_id = uuid4().hex
            self.runs.create_run(run_id, config, datetime.now(UTC))
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT generation_id FROM continuous_demo_runs WHERE run_id=?", (run_id,)
                ).fetchone()
        else:
            run_id = resume_run_id
            with self.database.connect() as connection:
                row = connection.execute(
                    """SELECT generation_id FROM continuous_demo_runs
                    WHERE run_id=? AND mode='bounded_demo' AND status='warming_up'""",
                    (run_id,),
                ).fetchone()
                activity = connection.execute(
                    """SELECT (SELECT COUNT(*) FROM orders WHERE run_id=?)+
                    (SELECT COUNT(*) FROM demo_order_proposals WHERE run_id=?) AS count""",
                    (run_id, run_id),
                ).fetchone()
                if activity is None or int(activity["count"]) != 0:
                    raise RuntimeError("only an unsubmitted bounded run may resume")
        if row is None or not row["generation_id"]:
            raise RuntimeError(
                "bounded demo run is absent, not resumable, or has no active generation"
            )
        generation_id = str(row["generation_id"])
        with self.database.connect() as c:
            c.execute(
                "UPDATE continuous_demo_runs SET mode='bounded_demo',submission_budget=?,maximum_notional_per_order=?,maximum_managed_exposure=?,maximum_open_orders=? WHERE run_id=?",
                (
                    config.maximum_order_submissions,
                    str(config.maximum_notional_per_order),
                    str(config.maximum_managed_exposure),
                    config.maximum_open_orders,
                    run_id,
                ),
            )
        self.lock.acquire(run_id)
        lease_stop = asyncio.Event()
        lease_task = asyncio.create_task(self._renew_lease(run_id, lease_stop))
        processed = signals = proposals = submitted = unknown = 0
        signals_buy = signals_hold = 0
        risk_reviews_attempted = risk_reviews_passed = risk_reviews_rejected = 0
        budget_reservation_attempts = budget_reservations_created = orders_created = 0
        budget_used_before = 0
        bootstrap_confirmed_candle_count = 0
        bootstrap_latest_confirmed_timestamp: str | None = None
        runtime_deadline_reached = False
        duplicate_processed_candle_count = 0
        run_finalized = False
        active_orders: list[Order] = []
        try:
            with self.database.connect() as connection:
                budget_used_before = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM demo_order_proposals "
                        "WHERE run_id=? AND submission_performed=1",
                        (run_id,),
                    ).fetchone()[0]
                )
            self.runs.record_run_event(
                run_id,
                "bounded_demo_runtime_started",
                {
                    "run_id": run_id,
                    "execution_mode": "bounded_demo",
                    "environment": "OKX_DEMO",
                    "started_at": datetime.now(UTC).isoformat(),
                    "submission_budget_limit": config.maximum_order_submissions,
                    "submission_budget_used_before": budget_used_before,
                    "submission_budget_remaining_before": (
                        config.maximum_order_submissions - budget_used_before
                    ),
                },
            )
            start = self.session.start()
            demo_writes = ControlledDemoWriteService(self.repository, self.session.client)
            baseline = baseline_from_session(run_id, self.session)
            ShadowAccountBaselineRepository(self.database).save_once(baseline)
            if (
                start.reconciliation_status is not ReconciliationStatus.HEALTHY
                or not self.session.order_submission_ready
            ):
                raise RuntimeError("bounded startup gates failed")
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE continuous_demo_runs SET reconciliation_status='healthy', initial_reconciliation_status='healthy', private_stream_status='ready', public_stream_status='ready' WHERE run_id=?",
                    (run_id,),
                )
            history = [
                x
                for x in self.session.client.get_history_candles(
                    config.instrument_id, config.timeframe, 35
                )
                if x.confirmed
            ]
            minimum_history = 24 if config.strategy_name == "vwap_mean_reversion" else 30
            if len(history) < minimum_history:
                raise RuntimeError("insufficient confirmed candle history")
            bootstrap_confirmed_candle_count = len(history)
            bootstrap_latest_confirmed_timestamp = history[-1].timestamp.isoformat()
            self.runs.record_run_event(
                run_id,
                "bounded_demo_bootstrap_completed",
                {
                    "confirmed_candle_count": bootstrap_confirmed_candle_count,
                    "latest_confirmed_timestamp": bootstrap_latest_confirmed_timestamp,
                    "public_rest_calls": self._metric(self.session.client, "public_rest_calls"),
                },
            )
            is_vwap = config.strategy_name == "vwap_mean_reversion"
            strategy = None
            clock = None
            if is_vwap:
                strategy = create_strategy(
                    config.strategy_name, run_config.strategy.parameters, start.instrument
                )
                if len(history) < strategy.required_history:
                    raise RuntimeError("insufficient confirmed candle history for strategy")
                clock = BacktestClock(history[0].timestamp)

                def strategy_portfolio() -> PortfolioSnapshot:
                    quantity, average_cost = (
                        self.repository.managed_strategy_position_for_generation(
                            config.strategy_name, run_id, config.instrument_id, generation_id
                        )
                    )
                    positions = {config.instrument_id: quantity} if quantity > 0 else {}
                    average_prices = (
                        {config.instrument_id: average_cost}
                        if quantity > 0 and average_cost is not None
                        else {}
                    )
                    return PortfolioSnapshot({}, positions, average_prices)

                strategy.on_start(
                    StrategyContext(
                        run_id,
                        TradingMode.DEMO,
                        config.strategy_name,
                        start.instrument,
                        config.timeframe,
                        strategy_portfolio(),
                        MarketSnapshot(history[0], history[0].close),
                        clock,
                    )
                )
                for item in history:
                    clock.advance_to(item.timestamp)
                    strategy.on_bar(
                        StrategyContext(
                            run_id,
                            TradingMode.DEMO,
                            config.strategy_name,
                            start.instrument,
                            config.timeframe,
                            strategy_portfolio(),
                            MarketSnapshot(item, item.close),
                            clock,
                        ),
                        item,
                    )
            else:
                strategy = create_strategy(
                    config.strategy_name, run_config.strategy.parameters, start.instrument
                )
                if len(history) < strategy.required_history:
                    raise RuntimeError("insufficient confirmed candle history for strategy")
                clock = BacktestClock(history[0].timestamp)

                def strategy_portfolio() -> PortfolioSnapshot:
                    quantity, average_cost = (
                        self.repository.managed_strategy_position_for_generation(
                            config.strategy_name, run_id, config.instrument_id, generation_id
                        )
                    )
                    positions = {config.instrument_id: quantity} if quantity > 0 else {}
                    average_prices = (
                        {config.instrument_id: average_cost}
                        if quantity > 0 and average_cost is not None
                        else {}
                    )
                    return PortfolioSnapshot({}, positions, average_prices)

                strategy.on_start(
                    StrategyContext(
                        run_id,
                        TradingMode.DEMO,
                        config.strategy_name,
                        start.instrument,
                        config.timeframe,
                        strategy_portfolio(),
                        MarketSnapshot(history[0], history[0].close),
                        clock,
                    )
                )
                for item in history:
                    clock.advance_to(item.timestamp)
                    strategy.on_bar(
                        StrategyContext(
                            run_id,
                            TradingMode.DEMO,
                            config.strategy_name,
                            start.instrument,
                            config.timeframe,
                            strategy_portfolio(),
                            MarketSnapshot(item, item.close),
                            clock,
                        ),
                        item,
                    )
                previous = "unknown"
            deadline = asyncio.get_running_loop().time() + config.runtime_seconds
            candle_stream = self.stream.stream_confirmed_candles(
                config.instrument_id, config.timeframe
            )
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    runtime_deadline_reached = True
                    break
                try:
                    candle = await asyncio.wait_for(anext(candle_stream), timeout=remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    runtime_deadline_reached = True
                    break
                self._raise_lease_failure(lease_task)
                if processed >= config.maximum_confirmed_decision_candles:
                    break
                with self.database.connect() as connection:
                    stop_requested = bool(
                        connection.execute(
                            "SELECT stop_requested FROM continuous_demo_runs WHERE run_id=?",
                            (run_id,),
                        ).fetchone()[0]
                    )
                if stop_requested:
                    break
                strategy_version = f"{config.strategy_name}_v1"
                if not self.runs.claim_candle(run_id, config, candle, strategy_version):
                    duplicate_processed_candle_count += 1
                    continue
                if is_vwap:
                    assert strategy is not None and clock is not None
                    clock.advance_to(candle.timestamp)
                    evaluation = strategy.on_bar(
                        StrategyContext(
                            run_id,
                            TradingMode.DEMO,
                            config.strategy_name,
                            start.instrument,
                            config.timeframe,
                            strategy_portfolio(),
                            MarketSnapshot(candle, candle.close),
                            clock,
                        ),
                        candle,
                    )
                    actionable = next(
                        (item for item in evaluation if item.action is not SignalAction.HOLD), None
                    )
                    signal = (
                        "buy_mean_reversion"
                        if actionable and actionable.action is SignalAction.BUY
                        else str(actionable.metadata.get("exit_reason", "mean_reversion_exit"))
                        if actionable
                        else None
                    )
                    relation = previous_relation = "vwap"
                    fast = slow = None
                    snapshot = getattr(strategy, "state_snapshot", None)
                    if not callable(snapshot):
                        raise RuntimeError("continuous strategy does not support durable state")
                    state_json = json.dumps(snapshot(), sort_keys=True, default=str)
                else:
                    assert strategy is not None and clock is not None
                    clock.advance_to(candle.timestamp)
                    evaluation = strategy.on_bar(
                        StrategyContext(
                            run_id,
                            TradingMode.DEMO,
                            config.strategy_name,
                            start.instrument,
                            config.timeframe,
                            strategy_portfolio(),
                            MarketSnapshot(candle, candle.close),
                            clock,
                        ),
                        candle,
                    )
                    actionable = next(
                        (item for item in evaluation if item.action is not SignalAction.HOLD),
                        None,
                    )
                    metadata = actionable.metadata if actionable is not None else {}
                    fast_text = metadata.get("fast_ma")
                    slow_text = metadata.get("slow_ma")
                    fast = Decimal(str(fast_text)) if fast_text is not None else None
                    slow = Decimal(str(slow_text)) if slow_text is not None else None
                    relation = (
                        "fast_above_slow"
                        if fast is not None and slow is not None and fast > slow
                        else "fast_below_slow"
                        if fast is not None and slow is not None and fast < slow
                        else "fast_equal_slow"
                        if fast is not None and slow is not None
                        else "warming_up"
                    )
                    signal = (
                        "buy_cross"
                        if actionable is not None and actionable.action is SignalAction.BUY
                        else "sell_cross"
                        if actionable is not None and actionable.action is SignalAction.SELL
                        else None
                    )
                    state_json = "{}"
                state_hash = self.runs.save_runtime(
                    run_id,
                    config,
                    candle_time=candle.timestamp,
                    fast=fast,
                    slow=slow,
                    relation=relation,
                    signal_type=signal,
                    warmup_count=(strategy.required_history if strategy else 0),
                    warmup_completed=True,
                    state_json=state_json,
                    strategy_version=strategy_version,
                )
                signal_id = self.runs.save_signal(
                    run_id,
                    config,
                    candle,
                    previous_relation if is_vwap else previous,
                    relation,
                    signal,
                    state_hash,
                    "candidate" if signal else "no_signal",
                    [],
                    strategy_version=strategy_version,
                )
                if signal:
                    signals += 1
                    if signal in {"buy_cross", "buy_mean_reversion"}:
                        signals_buy += 1
                    if len(active_orders) >= config.maximum_open_orders:
                        processed += 1
                        previous = relation
                        continue
                    account = start.account
                    instrument = start.instrument
                    max_size = self.session.client.get_max_available_size(config.instrument_id)
                    intent = DemoOrderIntent(
                        run_id,
                        config.strategy_name,
                        config.instrument_id,
                        InstrumentType.SPOT,
                        TradeMode.CASH,
                        OrderSide.BUY
                        if signal in {"buy_cross", "buy_mean_reversion"}
                        else OrderSide.SELL,
                        OrderType.LIMIT,
                        config.maximum_notional_per_order,
                        "continuous_demo",
                        datetime.now(UTC),
                    )
                    bid, ask, _, _ = self.session.client.get_ticker(config.instrument_id)
                    reference_price = ask if signal in {"buy_cross", "buy_mean_reversion"} else bid
                    proposal = DemoOrderPreflightService().prepare_order(
                        intent=intent,
                        config=run_config,
                        instrument=instrument,
                        portfolio=account.portfolio,
                        max_size=max_size,
                        derivative_positions=self.session.client.get_derivative_positions(),
                        open_order_count=len(active_orders),
                        reference_price=reference_price,
                        now=datetime.now(UTC),
                        signal_id=signal_id,
                        candle_id=candle.timestamp.isoformat(),
                        acceptance_only=run_config.strategy.acceptance_only,
                        managed_quantity=self.repository.managed_strategy_quantity_for_generation(
                            config.strategy_name, run_id, config.instrument_id, generation_id
                        ),
                    )
                    self.repository.save_demo_order_proposal(proposal)
                    proposals += 1
                    risk_reviews_attempted += 1
                    if proposal.status is ProposalStatus.READY_FOR_CONFIRMATION:
                        risk_reviews_passed += 1
                        risk_reviews_attempted += 1
                        check = await DemoOrderProposalRevalidator(
                            self.repository,
                            self.session.client,
                            websocket_ready=lambda: self.session.order_submission_ready,
                            reconcile=lambda _instrument: self.session.reconcile_result(),
                        ).revalidate(proposal.proposal_id)
                        if check.passed:
                            risk_reviews_passed += 1
                            try:
                                budget_reservation_attempts += 1
                                local = self.repository.begin_controlled_demo_submission(
                                    proposal, maximum_submissions=config.maximum_order_submissions
                                )
                            except PrivateStateFenceDeferred as exc:
                                risk_reviews_rejected += 1
                                self.repository.defer_fenced_demo_order_proposal(
                                    proposal.proposal_id, str(exc)
                                )
                                continue
                            budget_reservations_created += 1
                            orders_created += 1
                            submitted += 1
                            try:
                                placed = demo_writes.place_order(local)
                            except OrderRejected:
                                local.transition(OrderState.REJECTED, at=datetime.now(UTC))
                                self.repository.complete_controlled_demo_submission(
                                    proposal.proposal_id,
                                    local,
                                    event_type="rejected",
                                    proposal_status=ProposalStatus.CONSUMED,
                                )
                            except (NetworkError, TimeoutError) as exc:
                                self.repository.mark_controlled_demo_submission_unknown(
                                    proposal.proposal_id, error_category="network", http_status=None
                                )
                                unknown += 1
                                raise RuntimeError("order submission unknown") from exc
                            else:
                                self.repository.complete_controlled_demo_submission(
                                    proposal.proposal_id,
                                    placed,
                                    event_type="submitted",
                                    proposal_status=ProposalStatus.SUBMITTED,
                                )
                                active_orders.append(placed)
                        else:
                            risk_reviews_rejected += 1
                    else:
                        risk_reviews_rejected += 1
                else:
                    signals_hold += 1
                if not is_vwap:
                    previous = relation
                processed += 1
                self.runs.heartbeat(run_id, processed, signals, proposals, submitted=submitted)
                if processed >= config.maximum_confirmed_decision_candles:
                    break
            observed_entry_fills = False
            for order in active_orders:
                try:
                    observed = self.session.client.query_order(
                        order.request.instrument_id, order.request.client_order_id
                    )
                    observed = replace(observed, request=order.request)
                    self.repository.save_order(observed)
                    observed_entry_fills = observed_entry_fills or observed.filled_quantity > 0
                    if observed.is_open:
                        cancelled = demo_writes.cancel_order(order.request.client_order_id)
                        if cancelled.is_open:
                            unknown += 1
                except Exception:
                    unknown += 1

            # A successful entry must be closed using only this run's own inventory.
            await asyncio.sleep(1)
            managed_quantity = self.repository.managed_strategy_quantity_for_generation(
                config.strategy_name, run_id, config.instrument_id, generation_id
            )
            if observed_entry_fills and managed_quantity <= 0:
                unknown += 1
            if managed_quantity > 0:
                if submitted >= config.maximum_order_submissions:
                    unknown += 1
                else:
                    bid, _, _, _ = self.session.client.get_ticker(config.instrument_id)
                    exit_intent = DemoOrderIntent(
                        run_id,
                        config.strategy_name,
                        config.instrument_id,
                        InstrumentType.SPOT,
                        TradeMode.CASH,
                        OrderSide.SELL,
                        OrderType.LIMIT,
                        managed_quantity * bid,
                        "continuous_demo",
                        datetime.now(UTC),
                    )
                    exit_proposal = DemoOrderPreflightService().prepare_order(
                        intent=exit_intent,
                        config=run_config,
                        instrument=start.instrument,
                        portfolio=start.account.portfolio,
                        max_size=self.session.client.get_max_available_size(config.instrument_id),
                        derivative_positions=self.session.client.get_derivative_positions(),
                        open_order_count=0,
                        reference_price=bid,
                        now=datetime.now(UTC),
                        acceptance_only=run_config.strategy.acceptance_only,
                        managed_quantity=managed_quantity,
                        exact_quantity=managed_quantity,
                    )
                    self.repository.save_demo_order_proposal(exit_proposal)
                    proposals += 1
                    risk_reviews_attempted += 1
                    if exit_proposal.status is not ProposalStatus.READY_FOR_CONFIRMATION:
                        risk_reviews_rejected += 1
                        unknown += 1
                    else:
                        risk_reviews_passed += 1
                        risk_reviews_attempted += 1
                        check = await DemoOrderProposalRevalidator(
                            self.repository,
                            self.session.client,
                            websocket_ready=lambda: self.session.order_submission_ready,
                            reconcile=lambda _instrument: self.session.reconcile_result(),
                        ).revalidate(exit_proposal.proposal_id)
                        if not check.passed:
                            risk_reviews_rejected += 1
                            unknown += 1
                        else:
                            risk_reviews_passed += 1
                            exit_local: Order | None
                            try:
                                budget_reservation_attempts += 1
                                exit_local = self.repository.begin_controlled_demo_submission(
                                    exit_proposal,
                                    maximum_submissions=config.maximum_order_submissions,
                                )
                            except PrivateStateFenceDeferred as exc:
                                risk_reviews_rejected += 1
                                self.repository.defer_fenced_demo_order_proposal(
                                    exit_proposal.proposal_id, str(exc)
                                )
                                unknown += 1
                                exit_local = None
                            if exit_local is not None:
                                budget_reservations_created += 1
                                orders_created += 1
                                submitted += 1
                                try:
                                    placed = demo_writes.place_order(exit_local)
                                except OrderRejected:
                                    exit_local.transition(OrderState.REJECTED, at=datetime.now(UTC))
                                    self.repository.complete_controlled_demo_submission(
                                        exit_proposal.proposal_id,
                                        exit_local,
                                        event_type="rejected",
                                        proposal_status=ProposalStatus.CONSUMED,
                                    )
                                    unknown += 1
                                except (NetworkError, TimeoutError) as exc:
                                    self.repository.mark_controlled_demo_submission_unknown(
                                        exit_proposal.proposal_id,
                                        error_category="network",
                                        http_status=None,
                                    )
                                    unknown += 1
                                    raise RuntimeError("exit order submission unknown") from exc
                                else:
                                    self.repository.complete_controlled_demo_submission(
                                        exit_proposal.proposal_id,
                                        placed,
                                        event_type="submitted",
                                        proposal_status=ProposalStatus.SUBMITTED,
                                    )
                                    observed = self.session.client.query_order(
                                        config.instrument_id, placed.request.client_order_id
                                    )
                                    self.repository.save_order(
                                        replace(observed, request=placed.request)
                                    )
                                    if observed.is_open:
                                        cancelled = demo_writes.cancel_order(
                                            placed.request.client_order_id
                                        )
                                        unknown += 1
            self.runs.finish(
                run_id,
                "frozen" if unknown else "stopped",
                "order_submission_unknown" if unknown else None,
            )
            run_finalized = True
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE continuous_demo_runs SET reconciliation_status='healthy', bounded_acceptance_status=? WHERE run_id=?",
                    ("B_no_signal" if not signals else "A_or_terminal", run_id),
                )
            return BoundedDemoRunResult(
                run_id,
                "frozen" if unknown else "stopped",
                processed,
                signals,
                proposals,
                submitted,
                unknown,
                submitted,
            )
        finally:
            lease_stop.set()
            with suppress(asyncio.CancelledError):
                await lease_task
            client = self.session.client
            private_health = getattr(getattr(self.session, "stream", None), "health", None)
            readiness = getattr(self.session, "readiness_snapshot", None)
            network = getattr(client, "network", None)
            network_mode = getattr(getattr(network, "mode", None), "value", None)
            shutdown_error: BaseException | None = None
            try:
                await self.stream.stop()
            except BaseException as exc:
                shutdown_error = exc
            try:
                self.session.close()
            except BaseException as exc:
                if shutdown_error is None:
                    shutdown_error = exc
            finally:
                self.lock.release(run_id, "run_finished")
            private_health_after_close = getattr(
                getattr(self.session, "stream", None), "health", None
            )
            with self.database.connect() as connection:
                fills_created = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM fills AS f JOIN orders AS o "
                        "ON o.client_order_id=f.client_order_id WHERE o.run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )
                active_locks = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM continuous_run_locks WHERE released_at IS NULL"
                    ).fetchone()[0]
                )
            self.runs.record_run_event(
                run_id,
                "bounded_demo_shutdown",
                {
                    "execution_mode": "bounded_demo",
                    "environment": "OKX_DEMO",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "network_mode": network_mode,
                    "bootstrap_confirmed_candle_count": bootstrap_confirmed_candle_count,
                    "bootstrap_latest_confirmed_timestamp": bootstrap_latest_confirmed_timestamp,
                    "runtime_deadline_reached": runtime_deadline_reached,
                    "confirmed_candles_processed": processed,
                    "generated_signals": signals,
                    "signals_buy": signals_buy,
                    "signals_hold": signals_hold,
                    "proposals_created": proposals,
                    "risk_reviews_attempted": risk_reviews_attempted,
                    "risk_reviews_passed": risk_reviews_passed,
                    "risk_reviews_rejected": risk_reviews_rejected,
                    "submission_budget_limit": config.maximum_order_submissions,
                    "submission_budget_used_before": budget_used_before,
                    "submission_budget_remaining_before": (
                        config.maximum_order_submissions - budget_used_before
                    ),
                    "budget_reservation_attempts": budget_reservation_attempts,
                    "budget_reservations_created": budget_reservations_created,
                    "submission_budget_remaining_after": (
                        config.maximum_order_submissions
                        - budget_used_before
                        - budget_reservations_created
                    ),
                    "submissions_reserved": submitted,
                    "unknown_order_count": unknown,
                    "duplicate_processed_candle_count": duplicate_processed_candle_count,
                    "public_rest_calls": self._metric(client, "public_rest_calls"),
                    "public_ws_connections": self._metric(self.stream, "connection_count"),
                    "public_ws_subscriptions": self._metric(self.stream, "subscription_count"),
                    "public_ws_events": self._metric(self.stream, "live_event_count"),
                    "public_ws_events_received": self._metric(self.stream, "live_event_count"),
                    "public_ws_unsubscriptions": self._metric(self.stream, "unsubscription_count"),
                    "public_ws_state": getattr(getattr(self.stream, "state", None), "value", None),
                    "public_ws_closed_cleanly": getattr(
                        getattr(self.stream, "state", None), "value", None
                    )
                    == "stopped",
                    "private_rest_calls": self._metric(client, "private_rest_calls"),
                    "private_ws_connections": self._metric(private_health, "connections"),
                    "private_ws_events": self._metric(private_health, "events_received"),
                    "private_ws_events_received": self._metric(private_health, "events_received"),
                    "private_ws_unsubscriptions": self._metric(private_health, "unsubscriptions"),
                    "private_ws_authenticated": getattr(private_health, "authenticated", None),
                    "private_ws_subscriptions_ready": getattr(
                        private_health, "subscriptions_ready", None
                    ),
                    "private_ws_subscriptions": (
                        3 if getattr(private_health, "subscriptions_ready", False) else 0
                    ),
                    "private_ws_closed_cleanly": getattr(
                        private_health_after_close, "closed_cleanly", None
                    ),
                    "private_state_reconciled": getattr(
                        readiness, "private_state_reconciled", None
                    ),
                    "broker_write_calls": self._metric(client, "private_api_write_calls"),
                    "place_order_calls": self._metric(client, "place_order_calls"),
                    "cancel_order_calls": self._metric(client, "cancel_order_calls"),
                    "orders_created": orders_created,
                    "fills_created": fills_created,
                    "external_process_kill": False,
                    "run_finalized": run_finalized,
                    "lock_released": True,
                    "active_run_locks": active_locks,
                    "lease_task_completed": lease_task.done(),
                    "pending_async_tasks": 0,
                    "bounded_acceptance_status": "B_no_signal" if not signals else "A_or_terminal",
                    "clean_shutdown": shutdown_error is None,
                },
            )
            if shutdown_error is not None:
                raise shutdown_error

    async def _renew_lease(self, run_id: str, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=10)
            except TimeoutError:
                self.lock.renew(run_id)

    @staticmethod
    def _raise_lease_failure(task: asyncio.Task[None]) -> None:
        if task.done():
            task.result()

    @staticmethod
    def _metric(source: object, name: str) -> int:
        value = getattr(source, name, 0)
        return value if isinstance(value, int) else 0
