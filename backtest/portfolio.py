"""Finite, offline BTC-USDT cash portfolio replay (no exchange capability)."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import tomllib
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal, getcontext
from importlib.metadata import version
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from app.config.run_config import (
    BacktestConfig,
    MarketConfig,
    ProtectiveExitsConfig,
    RiskConfig,
    RunConfig,
    StrategyConfig,
    StrictModel,
)
from app.config.settings import TradingMode
from app.domain.events import EventBus
from app.domain.market import Candle, Instrument, InstrumentType, validate_confirmed_bar
from app.domain.order import Order, OrderSide, ProposedOrder
from app.domain.risk import RiskDecision
from app.domain.signal import Signal, SignalAction
from app.market.historical_data import BAR_INTERVALS
from app.portfolio.backtest import BudgetPositionSizer, PortfolioBacktestBroker, StrategyBroker
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.reproducibility import canonical_hash
from app.risk.risk_manager import (
    AvailableBalanceRule,
    MaximumExposureRule,
    RiskContext,
    RiskManager,
    default_risk_manager,
)
from app.runtime.clock import BacktestClock
from app.strategies.base import StatefulStrategy, strategy_identity
from app.strategies.registry import configuration_identity, create_strategy
from app.strategies.v3 import CandleCheckpoint
from app.trading_engine import EngineRiskState
from backtest.engine import BacktestEngine

ZERO = Decimal("0")
FORMAT_VERSION = 1


class StrategyInstanceConfig(StrictModel):
    instance_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    priority: int = Field(default=0, ge=0)
    allocation: Decimal = Field(gt=0, allow_inf_nan=False)
    strategy: StrategyConfig
    order_notional: Decimal = Field(gt=0, allow_inf_nan=False)


class PortfolioConfig(StrictModel):
    market: MarketConfig = Field(default_factory=MarketConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    protective_exits: ProtectiveExitsConfig = Field(default_factory=ProtectiveExitsConfig)
    instances: list[StrategyInstanceConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_portfolio(self) -> PortfolioConfig:
        if any(
            not value.is_finite()
            for value in (
                self.risk.max_order_notional,
                self.risk.max_total_exposure,
                self.risk.max_daily_loss,
                self.risk.max_drawdown_pct,
            )
        ):
            raise ValueError("portfolio risk limits must be finite")
        if self.market.instrument_id != "BTC-USDT" or self.market.bar.lower() not in BAR_INTERVALS:
            raise ValueError("portfolio v1 requires BTC-USDT and a supported fixed candle interval")
        identifiers = [item.instance_id for item in self.instances]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("strategy instance IDs must be unique")
        if sum((item.allocation for item in self.instances), ZERO) > self.backtest.initial_capital:
            raise ValueError("fixed allocations exceed shared account capital")
        for item in self.instances:
            if item.strategy.acceptance_only:
                raise ValueError(
                    "demo acceptance configurations cannot run in an offline portfolio"
                )
            configuration_identity(
                item.strategy.name, item.strategy.parameters, item.strategy.implementation_version
            )
        return self


class _PortfolioRiskManager(RiskManager):
    def __init__(self, owner: PortfolioBacktestEngine, instance: StrategyInstanceConfig) -> None:
        risk = owner.config.risk
        delegate = default_risk_manager(
            maximum_order_notional=risk.max_order_notional,
            maximum_exposure=min(instance.allocation, risk.max_total_exposure),
            maximum_daily_loss=risk.max_daily_loss,
            maximum_drawdown_pct=risk.max_drawdown_pct,
            maximum_orders_per_minute=risk.max_orders_per_minute,
            stale_after_seconds=risk.stale_after_seconds,
        )
        super().__init__(list(delegate.rules))
        self.owner = owner

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskDecision:
        # Ordinary entry caps must never prevent owned inventory reduction.
        rules = [
            r
            for r in self.rules
            if order.side is OrderSide.BUY or not isinstance(r, MaximumExposureRule)
        ]
        decision = RiskManager(rules).evaluate(context, order)
        if not decision.allowed or order.side is OrderSide.SELL:
            return decision
        shared = self.owner.broker
        account_context = replace(
            context,
            portfolio=shared.portfolio.snapshot(),
            open_orders=tuple(shared.get_open_orders()),
        )
        account = RiskManager(
            [AvailableBalanceRule(), MaximumExposureRule(self.owner.config.risk.max_total_exposure)]
        ).evaluate(account_context, order)
        combined = (*decision.rule_results, *account.rule_results)
        rejected = tuple(result for result in combined if not result.allowed)
        return replace(
            decision,
            allowed=not rejected,
            adjusted_order=order if not rejected else None,
            rejected_by=tuple(result.rule_name for result in rejected),
            reasons=tuple(result.reason for result in rejected) or decision.reasons,
            rule_results=combined,
            risk_snapshot={**decision.risk_snapshot, "account": account.risk_snapshot},
        )


class _InstanceEngine(BacktestEngine):
    owner: PortfolioBacktestEngine

    def _risk_state(self, price: Decimal) -> EngineRiskState:
        return self.owner.risk_state(price)

    def _execute(
        self, signal: Signal, candle: Candle, price: Decimal, **kwargs: Any
    ) -> Order | None:
        order = super()._execute(signal, candle, price, **kwargs)
        if order is not None:
            self.owner.recent_order_times.append(self.clock.now())
        return order


class PortfolioBacktestEngine:
    """Stable scheduling of existing instance engines over one matching broker.

    Fixed allocations cannot lend to one another. Priority (ascending), then
    stable instance ID orders all work. Existing orders keep FIFO. At a boundary
    owned exits precede entries; known protective brackets precede both, as in
    BacktestEngine. Only the portfolio runner advances matching phases.
    """

    def __init__(
        self,
        config: PortfolioConfig,
        instrument: Instrument,
        *,
        run_id: str,
        warmup_candles: list[Candle] | None = None,
    ) -> None:
        self.config = PortfolioConfig.model_validate(config.model_dump(mode="json"))
        self.config.instances.sort(key=lambda item: (item.priority, item.instance_id))
        if (
            not run_id
            or instrument.instrument_id != "BTC-USDT"
            or instrument.instrument_type is not InstrumentType.SPOT
            or instrument.base_currency != "BTC"
            or instrument.quote_currency != "USDT"
            or not instrument.tradable
            or not instrument.minimum_notional.is_finite()
            or instrument.minimum_notional < 0
            or any(
                not value.is_finite() or value <= 0
                for value in (
                    instrument.price_tick,
                    instrument.quantity_step,
                    instrument.minimum_quantity,
                )
            )
        ):
            raise ValueError("portfolio v1 requires a valid offline BTC-USDT SPOT/cash instrument")
        self.instrument, self.run_id = instrument, run_id
        self.warmup_candles = list(warmup_candles or [])
        self.interval = BAR_INTERVALS[self.config.market.bar.lower()]
        previous = None
        for candle in self.warmup_candles:
            validate_confirmed_bar(candle, previous, interval=self.interval)
            previous = candle
        self.broker = PortfolioBacktestBroker(
            instrument,
            config.backtest.initial_capital,
            {item.instance_id: item.allocation for item in self.config.instances},
            config.backtest.fee_rate,
            config.backtest.slippage_rate,
            volume_participation=config.backtest.volume_participation,
        )
        self.engines: dict[str, _InstanceEngine] = {}
        self.views: dict[str, StrategyBroker] = {}
        self.event_bus = EventBus()
        self.pending: list[tuple[str, Signal, Decimal]] = []
        self.history: list[dict[str, Any]] = []
        self.candles: list[Candle] = []
        self.curve: list[dict[str, Any]] = []
        self.recent_order_times: list[datetime] = []
        self.peak = self.day_start = self.previous_equity = config.backtest.initial_capital
        self.previous_day: date | None = None
        self.started = self.finished = self.failed = False
        self._active: list[_InstanceEngine] = []
        self.runtime_identity = self._runtime_identity()
        for item in self.config.instances:
            view = StrategyBroker(self.broker, item.instance_id)
            self.views[item.instance_id] = view
            local_config = RunConfig(
                mode=TradingMode.BACKTEST,
                market=self.config.market,
                strategy=item.strategy,
                risk=self.config.risk,
                protective_exits=self.config.protective_exits,
                backtest=self.config.backtest.model_copy(
                    update={"initial_capital": item.allocation}
                ),
            )
            strategy = create_strategy(
                item.strategy.name,
                item.strategy.parameters,
                instrument,
                implementation_version=item.strategy.implementation_version,
            )
            engine = _InstanceEngine(
                run_id=run_id,
                config=local_config,
                instrument=instrument,
                provider=self,
                strategy=strategy,
                position_sizer=BudgetPositionSizer(
                    FixedNotionalPositionSizer(item.order_notional), view
                ),
                risk_manager=_PortfolioRiskManager(self, item),
                broker=view,
                clock=BacktestClock(datetime.min.replace(tzinfo=UTC)),
                event_bus=self.event_bus,
                warmup_candles=self.warmup_candles,
                strategy_instance_id=item.instance_id,
            )
            engine.owner = self
            self.engines[item.instance_id] = engine

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        raise RuntimeError("only the portfolio runner may consume the market stream")

    def _start(self) -> None:
        for engine in self.engines.values():
            engine.clock.advance_to((self.warmup_candles or self.candles)[0].timestamp)
            engine.trading_engine.start()
            self._active.append(engine)
            if self.warmup_candles:
                engine.trading_engine.warmup(self.warmup_candles)
        self.started = True

    def risk_state(self, price: Decimal) -> EngineRiskState:
        equity = self.broker.portfolio.equity("BTC-USDT", "BTC", "USDT", price)
        self.peak = max(self.peak, equity)
        return EngineRiskState(
            daily_pnl=equity - self.day_start,
            drawdown_pct=(self.peak - equity) / self.peak * 100 if self.peak else ZERO,
            recent_order_times=tuple(self.recent_order_times),
        )

    def _notify_matches(self, candle: Candle, price: Decimal) -> None:
        for order in self.broker.match_orders():
            self.engines[order.request.strategy_instance_id].trading_engine.record_order_update(
                order, candle, price
            )

    def _cancel_overexposed_entries(self, candle: Candle) -> None:
        """Recheck existing BUY reservations at each newly known opening mark.

        A price move can breach a mark-value cap without a trade. Do not invent
        a liquidation policy: cancel entries, retain owned inventory for its
        existing strategy/protective exits, and expose the breach in the report.
        """
        account = self.broker.portfolio.positions.get("BTC-USDT", ZERO) * candle.open
        pending = sum(
            (order.request.quantity - order.filled_quantity) * order.request.price
            for order in self.broker.get_open_orders()
            if order.request.side is OrderSide.BUY
        )
        account_breached = account + pending > self.config.risk.max_total_exposure
        for item in self.config.instances:
            book = self.broker.books[item.instance_id]
            projected = book.portfolio.positions.get("BTC-USDT", ZERO) * candle.open + sum(
                (order.request.quantity - order.filled_quantity) * order.request.price
                for order in self.broker.orders_for(item.instance_id)
                if order.request.side is OrderSide.BUY
            )
            if account_breached or projected > item.allocation:
                self.engines[item.instance_id]._cancel_orders(candle, side=OrderSide.BUY)

    def advance(self, candle: Candle) -> None:
        if self.finished or self.failed:
            raise ValueError("portfolio is stopped; restore a verified checkpoint")
        history = self.candles or self.warmup_candles
        previous = history[-1] if history else None
        validate_confirmed_bar(candle, previous, interval=self.interval)
        # A failed phase cannot publish a report/checkpoint. Recovery always
        # reconstructs from the previous *complete* immutable checkpoint.
        self.candles.append(candle)
        try:
            if not self.started:
                self._start()
            for engine in self.engines.values():
                engine.clock.advance_to(candle.timestamp)
            if candle.timestamp.date() != self.previous_day:
                self.day_start, self.previous_day = self.previous_equity, candle.timestamp.date()
            self.broker.begin_bar(candle)
            self._cancel_overexposed_entries(candle)
            state = self.risk_state(candle.open)
            if (
                state.daily_pnl is not None and state.daily_pnl <= -self.config.risk.max_daily_loss
            ) or (
                state.drawdown_pct is not None
                and state.drawdown_pct >= self.config.risk.max_drawdown_pct
            ):
                for engine in self.engines.values():
                    engine._cancel_orders(candle, side=OrderSide.BUY)
            protective = {
                key: engine._protective_exit(candle, intrabar=False)
                for key, engine in self.engines.items()
            }
            self._notify_matches(candle, candle.open)
            ranks = {
                item.instance_id: (item.priority, item.instance_id)
                for item in self.config.instances
            }
            pending = sorted(
                enumerate(self.pending),
                key=lambda item: (
                    item[1][1].action is SignalAction.BUY,
                    *ranks[item[1][0]],
                    item[0],
                ),
            )
            for _, (key, signal, limit) in pending:
                if not protective[key]:
                    self.engines[key]._execute(signal, candle, candle.open, limit_price=limit)
            self.pending.clear()
            for engine in self.engines.values():
                engine.clock.advance_to(candle.timestamp + self.interval)
            self.broker.begin_intrabar(candle.timestamp + self.interval)
            for key, engine in self.engines.items():
                protective[key] = engine._protective_exit(candle, intrabar=True) or protective[key]
            self._notify_matches(candle, candle.close)
            self.risk_state(candle.close)
            self.previous_equity = self.broker.portfolio.equity(
                "BTC-USDT", "BTC", "USDT", candle.close
            )
            self.curve.append(
                {
                    "timestamp": candle.timestamp + self.interval,
                    **self._account_report(candle.close),
                }
            )
            for key, engine in self.engines.items():
                signals = engine.trading_engine.generate_signals(candle)
                if not protective[key]:
                    self.pending.extend(
                        (key, signal, candle.close)
                        for signal in signals
                        if signal.action is not SignalAction.HOLD
                    )
            self.history.append({"type": "bar", "candle": asdict(candle)})
            self.assert_conservation(candle.close)
        except BaseException as exc:
            self.failed = True
            self._stop_after_failure(exc)
            raise

    def _stop_after_failure(self, cause: BaseException) -> None:
        for engine in self._active:
            try:
                engine.trading_engine.stop()
            except BaseException as cleanup_error:
                cause.add_note(f"strategy stop failed: {type(cleanup_error).__name__}")
        self._active.clear()

    def cancel_order(self, instance_id: str, order_id: str) -> None:
        if self.failed or self.finished or not self.candles:
            raise ValueError("cannot cancel in a stopped/uninitialized portfolio")
        view = self.views[instance_id]
        view.get_order("BTC-USDT", order_id)  # Reject foreign ownership before mutation.
        try:
            order = view.cancel_order("BTC-USDT", order_id)
            candle = self.candles[-1]
            self.engines[instance_id].trading_engine.record_order_update(
                order, candle, candle.close
            )
            self.history.append(
                {"type": "cancel", "instance_id": instance_id, "order_id": order_id}
            )
        except BaseException as exc:
            self.failed = True
            self._stop_after_failure(exc)
            raise

    def finish(self) -> dict[str, Any]:
        if self.failed or not self.candles:
            raise ValueError("failed/empty portfolio cannot produce a successful report")
        if not self.finished:
            try:
                for engine in self.engines.values():
                    engine._cancel_orders(self.candles[-1])
                for engine in list(self._active):
                    self._active.remove(engine)
                    engine.trading_engine.stop()
            except BaseException as exc:
                self.failed = True
                self._stop_after_failure(exc)
                raise
            self._active.clear()
            self.finished = True
        return self.report()

    def run(self, candles: list[Candle]) -> dict[str, Any]:
        for candle in candles:
            self.advance(candle)
        return self.finish()

    def _account_report(self, mark: Decimal) -> dict[str, Any]:
        portfolio = self.broker.portfolio
        return {
            "cash": portfolio.balances["USDT"],
            "available_cash": self.broker.available_cash(),
            "reserved_cash": self.broker.reserved_cash(),
            "quantity": portfolio.positions.get("BTC-USDT", ZERO),
            "mark_exposure": portfolio.positions.get("BTC-USDT", ZERO) * mark,
            "exposure_limit": self.config.risk.max_total_exposure,
            "exposure_breached_by_mark": portfolio.positions.get("BTC-USDT", ZERO) * mark
            > self.config.risk.max_total_exposure,
            "cost_notional": self.broker._position_notional,
            "equity": portfolio.equity("BTC-USDT", "BTC", "USDT", mark),
            "realized_pnl": portfolio.realized_pnl,
            "unrealized_pnl": self.broker.unrealized_pnl(mark),
            "fees": sum((fill.fee for fill in self.broker.fills), ZERO),
            "mark_price": mark,
            "unallocated_cash": self.broker.unallocated_cash,
            "unallocated_quantity": ZERO,
        }

    def report(self) -> dict[str, Any]:
        if self.failed or not self.candles:
            raise ValueError("failed/empty portfolio has no valid report")
        mark = self.candles[-1].close
        strategies = {}
        for item in self.config.instances:
            key, book = item.instance_id, self.broker.books[item.instance_id]
            position = book.portfolio.positions.get("BTC-USDT", ZERO)
            strategies[key] = {
                "strategy_instance_id": key,
                "strategy_identity": strategy_identity(self.engines[key].strategy),
                "allocated_capital": item.allocation,
                "cash": book.portfolio.balances["USDT"],
                "available_budget": self.broker.available_cash(key),
                "reserved_cash": self.broker.reserved_cash(key),
                "quantity": position,
                "cost_notional": book._position_notional,
                "capital_in_use": book._position_notional + self.broker.reserved_cash(key),
                "mark_exposure": position * mark,
                "exposure_limit": min(item.allocation, self.config.risk.max_total_exposure),
                "exposure_breached_by_mark": position * mark
                > min(item.allocation, self.config.risk.max_total_exposure),
                "equity": book.portfolio.equity("BTC-USDT", "BTC", "USDT", mark),
                "realized_pnl": book.portfolio.realized_pnl,
                "unrealized_pnl": book.unrealized_pnl(mark),
                "fees": sum((fill.fee for fill in book.fills), ZERO),
                "dust_quantity": position
                if position < self.instrument.minimum_quantity
                or position * mark < self.instrument.minimum_notional
                else ZERO,
                "closed_trades": [asdict(trade) for trade in book.closed_trades],
            }
        return {
            "format_version": FORMAT_VERSION,
            "mode": "backtest",
            "run_id": self.run_id,
            "trading_permission": False,
            "execution_model": "ohlc_limit_v2",
            "allocation_policy": "fixed_partition_exit_then_priority_then_instance_fifo",
            "initial_capital": self.config.backtest.initial_capital,
            "config": self.config.model_dump(mode="json"),
            "runtime_identity": self.runtime_identity,
            "data_identity": {
                "candles_hash": canonical_hash([asdict(candle) for candle in self.candles]),
                "warmup_hash": canonical_hash([asdict(candle) for candle in self.warmup_candles]),
                "bar_count": len(self.candles),
                "first_open": self.candles[0].timestamp,
                "last_close": self.candles[-1].timestamp + self.interval,
            },
            "account": self._account_report(mark),
            "strategies": strategies,
            "benchmark": {
                "name": "cash",
                "initial_capital": self.config.backtest.initial_capital,
                "final_equity": self.config.backtest.initial_capital,
            },
            "fills": [asdict(fill) for fill in self.broker.fills],
            "orders": [asdict(order) for order in self.broker._orders.values()],
            "equity_curve": [dict(point) for point in self.curve],
            "signals": [
                {key: value for key, value in asdict(event).items() if key != "event_id"}
                for event in self.event_bus.events
                if event.event_type == "SignalGenerated"
            ],
            "decisions": [
                {key: value for key, value in asdict(event).items() if key != "event_id"}
                for event in self.event_bus.events
                if event.event_type in {"PositionSizeCalculated", "RiskRejected", "RiskApproved"}
            ],
        }

    def assert_conservation(self, mark: Decimal) -> None:
        account = self._account_report(mark)
        if account["cash"] < 0 or account["available_cash"] < 0:
            raise ValueError("negative account cash / reservation invariant")
        if any(
            self.broker.available_cash(key) < 0 or self.broker.available_quantity(key) < 0
            for key in self.engines
        ):
            raise ValueError("negative owned budget / quantity invariant")
        # Decimal prorating can differ in the final decimal place. No tolerance
        # is used for actual cash/quantity; PnL alone allows 1e-20 USDT rounding.
        expected = (
            self.config.backtest.initial_capital
            + account["realized_pnl"]
            + account["unrealized_pnl"]
        )
        if abs(account["equity"] - expected) > Decimal("1e-20"):
            raise ValueError("portfolio PnL conservation failed")
        quantity = sum(
            (b.portfolio.positions.get("BTC-USDT", ZERO) for b in self.broker.books.values()), ZERO
        )
        if quantity != account["quantity"]:
            raise ValueError("portfolio ownership conservation failed")
        cash = self.broker.unallocated_cash + sum(
            (book.portfolio.balances["USDT"] for book in self.broker.books.values()), ZERO
        )
        if cash != account["cash"]:
            raise ValueError("portfolio cash partition conservation failed")

    @staticmethod
    def _runtime_identity() -> dict[str, Any]:
        root = Path(__file__).resolve().parents[1]
        paths = sorted(p for name in ("app", "backtest") for p in (root / name).rglob("*.py"))
        paths += [root / "pyproject.toml", root / "uv.lock"]
        declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "dependencies"
        ]
        return {
            "source_hash": canonical_hash(
                {
                    p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in paths
                }
            ),
            "python": platform.python_version(),
            "platform": sys.platform,
            "dependencies": {
                re.split(r"[<>=!~\[;\s]", item)[0]: version(re.split(r"[<>=!~\[;\s]", item)[0])
                for item in declared
            },
            "decimal_context": {
                name: str(getattr(getcontext(), name))
                for name in ("prec", "rounding", "Emin", "Emax", "capitals", "clamp", "traps")
            },
        }

    def save_checkpoint(self, path: Path) -> None:
        if self.failed or self.finished or not self.candles:
            raise ValueError("checkpoint requires a complete, active bar boundary")
        payload = {
            "format_version": FORMAT_VERSION,
            "runtime_identity": self.runtime_identity,
            "config": self.config.model_dump(mode="json"),
            "instrument": asdict(self.instrument),
            "run_id": self.run_id,
            "warmup": [asdict(c) for c in self.warmup_candles],
            "history": self.history,
            "state_hash": canonical_hash(self._state()),
        }
        payload["content_hash"] = canonical_hash(payload)
        write_immutable_json(path, payload)

    def _state(self) -> dict[str, Any]:
        return {
            "report": self.report(),
            "pending": [(key, asdict(signal), price) for key, signal, price in self.pending],
            "strategies": {
                key: engine.strategy.checkpoint_state()
                for key, engine in self.engines.items()
                if isinstance(engine.strategy, StatefulStrategy)
            },
            "peak": self.peak,
            "day_start": self.day_start,
            "recent_order_times": self.recent_order_times,
        }

    @classmethod
    def restore(
        cls, path: Path, config: PortfolioConfig, instrument: Instrument
    ) -> PortfolioBacktestEngine:
        data = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "content_hash",
            "format_version",
            "runtime_identity",
            "config",
            "instrument",
            "run_id",
            "warmup",
            "history",
            "state_hash",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("checkpoint format mismatch")
        content_hash = data.pop("content_hash", None)
        if content_hash != canonical_hash(data) or data.get("format_version") != FORMAT_VERSION:
            raise ValueError("checkpoint integrity / format mismatch")
        if data.get("runtime_identity") != cls._runtime_identity():
            raise ValueError(
                "checkpoint source, locked dependencies or Python/Decimal environment mismatch"
            )
        runner = cls(
            config,
            instrument,
            run_id=data["run_id"],
            warmup_candles=[
                CandleCheckpoint.model_validate(candle).candle() for candle in data["warmup"]
            ],
        )
        if canonical_hash(data["config"]) != canonical_hash(
            runner.config.model_dump(mode="json")
        ) or canonical_hash(data["instrument"]) != canonical_hash(asdict(instrument)):
            raise ValueError(
                "checkpoint strategy/budget/execution/instrument configuration mismatch"
            )
        try:
            for event in data["history"]:
                if event["type"] == "bar":
                    runner.advance(CandleCheckpoint.model_validate(event["candle"]).candle())
                elif event["type"] == "cancel":
                    runner.cancel_order(event["instance_id"], event["order_id"])
                else:
                    raise ValueError("unsupported checkpoint replay event")
            if data["state_hash"] != canonical_hash(runner._state()):
                raise ValueError("checkpoint replay state mismatch; keep stopped")
        except BaseException as error:
            runner.failed = True
            runner._stop_after_failure(error)
            raise
        return runner


def write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Complete file then exclusive atomic link; never replace existing evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink()
