"""Read-only confirmed-candle adapter. No broker, client, account query or write authorization."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.order import Order
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal
from app.reproducibility import canonical_hash
from app.runtime.clock import BacktestClock
from app.strategies.base import StatefulStrategy, strategy_identity
from app.strategies.registry import create_strategy


class ConfirmedStrategyShadow:
    """Offline/public-input signal observation with externally supplied simulated feedback.

    The caller owns data continuity/reconciliation and durable checkpoint storage.
    This adapter deliberately cannot submit an order or infer a fill from an intent.
    """

    def __init__(
        self,
        config: RunConfig,
        instrument: Instrument,
        portfolio: PortfolioSnapshot,
        *,
        run_id: str = "readonly-shadow",
        resume_state: dict[str, Any] | None = None,
        open_order_ids: tuple[str, ...] = (),
    ) -> None:
        if config.mode is not TradingMode.BACKTEST or config.market.bar.lower() != "1h":
            raise ValueError(
                "confirmed strategy Shadow requires offline/read-only 1H configuration"
            )
        if config.market.instrument_id != instrument.instrument_id:
            raise ValueError("Shadow instrument/config mismatch")
        from app.strategies.registry import validate_strategy_version

        validate_strategy_version(config.strategy.name, config.strategy.implementation_version)
        strategy = create_strategy(
            config.strategy.name,
            config.strategy.parameters,
            instrument,
            implementation_version=config.strategy.implementation_version,
        )
        if not isinstance(strategy, StatefulStrategy):
            raise ValueError("strategy has no versioned state contract; use its legacy runner")
        self.strategy = strategy
        self.config, self.instrument, self.run_id = config, instrument, run_id
        self.portfolio = replace(portfolio, trusted_for_trading=False)
        self.clock = BacktestClock(datetime.min.replace(tzinfo=UTC))
        self.last_signal: Signal | None = None
        if resume_state is None:
            if open_order_ids:
                raise ValueError("pending Shadow orders require a checkpoint")
            strategy.on_start(self._context(None, self.clock, self.portfolio))
        else:
            content = {key: value for key, value in resume_state.items() if key != "state_hash"}
            if (
                set(content) != {"format_version", "clock", "strategy_state"}
                or content["format_version"] != 1
            ):
                raise ValueError("unsupported Shadow checkpoint format")
            if canonical_hash(content) != resume_state.get("state_hash"):
                raise ValueError("Shadow checkpoint hash mismatch")
            stamp = datetime.fromisoformat(content["clock"])
            if stamp.tzinfo is None:
                raise ValueError("Shadow checkpoint clock must be aware")
            self.clock = BacktestClock(stamp)
            strategy.restore_checkpoint_state(
                content["strategy_state"],
                self._context(None, self.clock, self.portfolio),
                open_order_ids=open_order_ids,
            )

    @property
    def identity(self) -> dict[str, Any]:
        return strategy_identity(self.strategy)

    def _context(
        self,
        candle: Candle | None,
        clock: BacktestClock,
        portfolio: PortfolioSnapshot,
        *,
        warmup: bool = False,
    ) -> StrategyContext:
        return StrategyContext(
            self.run_id,
            TradingMode.BACKTEST,
            self.strategy.name,
            self.instrument,
            self.config.market.bar,
            portfolio,
            MarketSnapshot(candle, candle.close) if candle else None,
            clock,
            is_warmup=warmup,
            read_only=True,
        )

    def on_candle(self, candle: Candle, *, warmup: bool = False) -> list[Signal]:
        if candle.timestamp.tzinfo is None:
            raise ValueError("Shadow candle opening time must be aware UTC")
        stamp = candle.timestamp + timedelta(hours=1)
        if stamp < self.clock.now():
            raise ValueError("out-of-order Shadow candle")
        candidate_clock = BacktestClock(stamp)
        signals = self.strategy.on_bar(
            self._context(candle, candidate_clock, self.portfolio, warmup=warmup), candle
        )
        self.clock.advance_to(stamp)
        if signals:
            self.last_signal = signals[-1]
        return signals

    def replay(self, candles: Iterable[Candle], *, warmup: bool = False) -> list[Signal]:
        return [signal for candle in candles for signal in self.on_candle(candle, warmup=warmup)]

    def on_order_update(self, order: Order, portfolio: PortfolioSnapshot, *, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("Shadow feedback time must be aware")
        if at < self.clock.now():
            raise ValueError("out-of-order Shadow feedback")
        candidate_clock = BacktestClock(at)
        candidate_portfolio = replace(portfolio, trusted_for_trading=False)
        self.strategy.on_order_update(
            self._context(None, candidate_clock, candidate_portfolio), order
        )
        self.portfolio = candidate_portfolio
        self.clock.advance_to(at)

    def checkpoint_state(self) -> dict[str, Any]:
        content = {
            "format_version": 1,
            "clock": self.clock.now().isoformat(),
            "strategy_state": self.strategy.checkpoint_state(),
        }
        return {**content, "state_hash": canonical_hash(content)}

    def save_checkpoint(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(self.checkpoint_state(), handle, sort_keys=True, indent=2)
