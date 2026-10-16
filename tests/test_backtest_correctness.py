from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from random import Random
from typing import Any

import pytest

from app.config.run_config import BacktestConfig, RunConfig
from app.domain.context import StrategyContext
from app.domain.events import EventBus
from app.domain.market import Candle, Instrument, InstrumentType
from app.domain.order import ApprovedOrder, Order, OrderSide, OrderState, OrderType, ProposedOrder
from app.domain.position import Portfolio
from app.domain.signal import Signal, SignalAction
from app.execution.backtest_broker import BacktestBroker
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.risk.risk_manager import default_risk_manager
from app.runtime.clock import BacktestClock
from app.strategies.base import Strategy
from app.strategies.vwap_mean_reversion import (
    VWAPMeanReversionParameters,
    VWAPMeanReversionStrategy,
)
from backtest.engine import BacktestEngine, BacktestResult, _annualized_return
from backtest.report import write_backtest_report

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)


def candle(
    index: int,
    open_: str = "100",
    high: str = "100",
    low: str = "100",
    close: str = "100",
    volume: str = "100",
    *,
    start: datetime = START,
) -> Candle:
    return Candle(
        start + timedelta(minutes=5 * index),
        D(open_),
        D(high),
        D(low),
        D(close),
        D(volume),
        True,
    )


def approve(
    id_: str,
    side: OrderSide = OrderSide.BUY,
    price: str = "100",
    quantity: str = "1",
    at: datetime = START,
    order_type: OrderType = OrderType.LIMIT,
) -> ApprovedOrder:
    return ApprovedOrder(
        ProposedOrder(
            id_,
            "run",
            "scripted",
            "BTC-USDT",
            side,
            order_type,
            D(quantity),
            D(price),
            id_,
            at,
        ),
        at,
        "offline test",
    )


def broker(instrument: Instrument, *, fee: str = "0.001", slip: str = "0") -> BacktestBroker:
    return BacktestBroker(Portfolio({"USDT": D("1000")}), instrument, D(fee), D(slip))


def test_limit_fill_respects_both_sides_and_tick(btc_instrument: Instrument) -> None:
    b = broker(btc_instrument, slip="0.0005")
    b.begin_bar(candle(0))
    buy = b.submit_order(approve("buy"))
    assert buy.state is OrderState.FILLED
    assert buy.average_price == D("100")
    sell = b.submit_order(approve("sell", OrderSide.SELL))
    assert sell.state is OrderState.FILLED
    assert sell.average_price == D("100")
    assert all(fill.fill_price % btc_instrument.price_tick == 0 for fill in b.fills)
    assert b.closed_trade_pnls == [D("-0.2")]


def test_unfilled_limit_waits_and_does_not_fill_before_market_event(
    btc_instrument: Instrument,
) -> None:
    b = broker(btc_instrument)
    order = b.submit_order(approve("buy"))
    assert order.state is OrderState.ACCEPTED and not b.fills
    b.begin_bar(candle(0, "105", "106", "101", "105"))
    assert not b.match_orders()
    b.begin_intrabar(START + timedelta(minutes=5))
    assert not b.match_orders()
    b.begin_bar(candle(1, "102", "103", "99", "101"))
    assert not b.match_orders()
    b.begin_intrabar(START + timedelta(minutes=10))
    assert b.match_orders() == [order]
    assert order.average_price == D("100")
    assert b.fills[0].timestamp == START + timedelta(minutes=10)


def test_open_price_improvement_with_slippage_within_limit(btc_instrument: Instrument) -> None:
    b = broker(btc_instrument, slip="0.0005")
    b.begin_bar(candle(0, "95", "96", "94", "95"))
    buy = b.submit_order(approve("buy"))
    assert buy.average_price == D("95.1")
    assert b.fills[0].reference_price == D("95")
    assert b.fills[0].slippage_cost == D("0.1")
    b.begin_bar(candle(1, "110", "111", "109", "110"))
    sell = b.submit_order(approve("sell", OrderSide.SELL, at=START + timedelta(minutes=5)))
    assert sell.average_price == D("109.9") >= sell.request.price


def test_volume_budget_is_shared_and_zero_volume_never_fills(btc_instrument: Instrument) -> None:
    b = broker(btc_instrument)
    b.volume_participation = D("0.5")
    b.begin_bar(candle(0, volume="0"))
    first = b.submit_order(approve("first", quantity="0.1"))
    second = b.submit_order(approve("second", quantity="0.1"))
    assert not b.fills
    b.begin_bar(candle(1, volume="0.2"))
    b.match_orders()
    assert first.state is OrderState.FILLED
    assert second.state is OrderState.ACCEPTED
    b.begin_intrabar(START + timedelta(minutes=10))
    assert not b.match_orders()  # no second consumption of this bar's volume
    assert sum(fill.quantity for fill in b.fills) == D("0.1")
    with pytest.raises(ValueError, match="重复"):
        b.begin_bar(candle(1, volume="0.2"))


def test_partial_fills_weighted_cost_fees_and_complete_trade_count(
    btc_instrument: Instrument,
) -> None:
    b = broker(btc_instrument)
    b.begin_bar(candle(0, volume="0.1"))
    buy = b.submit_order(approve("buy", quantity="0.2"))
    assert buy.state is OrderState.PARTIALLY_FILLED
    assert b.portfolio.realized_pnl == D("-0.010")
    b.begin_bar(candle(1, "90", "90", "90", "90", "0.1"))
    b.match_orders()
    assert buy.state is OrderState.FILLED
    assert buy.average_price == D("95")
    assert b.portfolio.average_entry_prices["BTC-USDT"] == D("95")
    assert b.portfolio.realized_pnl == D("-0.019")
    b.begin_bar(candle(2, "110", "110", "110", "110", "0.05"))
    sell = b.submit_order(
        approve("sell", OrderSide.SELL, "95", "0.2", START + timedelta(minutes=10))
    )
    assert sell.state is OrderState.PARTIALLY_FILLED
    assert not b.closed_trades and not b.closed_trade_pnls
    assert b.portfolio.realized_pnl == D("0.7255")
    b.begin_bar(candle(3, volume="0.15"))
    b.match_orders()
    assert sell.state is OrderState.FILLED
    assert len(b.closed_trades) == 1
    trade = b.closed_trades[0]
    assert trade.entry_notional == D("19")
    assert trade.entry_fees == D("0.019")
    assert trade.exit_notional == D("20.5")
    assert trade.exit_fees == D("0.0205")
    assert trade.pnl == b.portfolio.realized_pnl == D("1.4605")
    assert trade.holding_time_hours == D("0.25")
    assert b.portfolio.balances["USDT"] == D("1001.4605")


def test_pending_orders_reserve_cash_including_fees_and_cancel_releases_it(
    btc_instrument: Instrument,
) -> None:
    b = broker(btc_instrument)
    first = b.submit_order(approve("first", quantity="9"))
    assert first.state is OrderState.ACCEPTED
    rejected = b.submit_order(approve("overbook", quantity="1"))
    assert rejected.state is OrderState.REJECTED
    b.cancel_order("BTC-USDT", "first")
    assert first.state is OrderState.CANCELLED
    assert b.submit_order(approve("replacement", quantity="9")).state is OrderState.ACCEPTED


def test_idempotency_rejects_changed_payload_and_preserves_budget(
    btc_instrument: Instrument,
) -> None:
    b = broker(btc_instrument)
    b.begin_bar(candle(0))
    approved = approve("buy")
    order = b.submit_order(approved)
    assert b.submit_order(approved) is order
    assert len(b.fills) == 1
    with pytest.raises(ValueError, match="不同请求"):
        b.submit_order(approve("buy", price="99"))


@pytest.mark.parametrize("participation", ["0", "-0.1", "1.1", "NaN", "Infinity"])
def test_volume_participation_is_validated(participation: str) -> None:
    with pytest.raises(ValueError):
        BacktestConfig(volume_participation=D(participation))


class ScriptedStrategy:
    name = "scripted"
    description = "Deterministic test signals"
    supported_market_types = frozenset({InstrumentType.SPOT})
    required_history = 1

    def __init__(self, actions: dict[int, SignalAction]) -> None:
        self.actions = actions
        self.index = 0
        self.updates: list[tuple[OrderState, Decimal]] = []

    def on_start(self, context: StrategyContext) -> None:
        self.index = 0

    def on_stop(self, context: StrategyContext) -> None:
        pass

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        self.updates.append((order.state, order.filled_quantity))

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        action = self.actions.get(self.index, SignalAction.HOLD)
        signal = Signal(
            str(self.index),
            self.name,
            context.instrument.instrument_id,
            action,
            context.now,
            "scripted",
            D("1"),
            metadata={"candle_confirmed": True},
        )
        self.index += 1
        return [signal]


class History:
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
        return self.candles


def run(
    instrument: Instrument,
    candles: list[Candle],
    actions: dict[int, SignalAction] | None = None,
    *,
    protective: bool = True,
    fee: str = "0.001",
    slip: str = "0",
    daily_loss: str = "1000",
    participation: str = "1",
    strategy_override: Strategy | None = None,
) -> tuple[BacktestResult, EventBus, Strategy]:
    config = RunConfig.model_validate(
        {
            "strategy": {"name": "buy_and_hold", "parameters": {}},
            "backtest": {
                "initial_capital": "1000",
                "fee_rate": fee,
                "slippage_rate": slip,
                "volume_participation": participation,
            },
            "risk": {
                "max_order_notional": "100",
                "max_total_exposure": "1000",
                "max_daily_loss": daily_loss,
                "max_drawdown_pct": "100",
                "max_orders_per_minute": 100,
            },
            "protective_exits": {"enabled": protective},
            "data": {"limit": len(candles)},
        }
    )
    b = broker(instrument, fee=fee, slip=slip)
    b.volume_participation = config.backtest.volume_participation
    strategy = strategy_override or ScriptedStrategy(
        actions if actions is not None else {0: SignalAction.BUY}
    )
    events = EventBus()
    manager = default_risk_manager(
        maximum_order_notional=config.risk.max_order_notional,
        maximum_exposure=config.risk.max_total_exposure,
        maximum_daily_loss=config.risk.max_daily_loss,
        maximum_drawdown_pct=config.risk.max_drawdown_pct,
        maximum_orders_per_minute=config.risk.max_orders_per_minute,
        stale_after_seconds=config.risk.stale_after_seconds,
    )
    result = BacktestEngine(
        run_id="run",
        config=config,
        instrument=instrument,
        provider=History(candles),
        strategy=strategy,
        position_sizer=FixedNotionalPositionSizer(D("100")),
        risk_manager=manager,
        broker=b,
        clock=BacktestClock(START),
        event_bus=events,
    ).run()
    return result, events, strategy


@pytest.mark.parametrize(
    ("exit_bar", "slip", "reason", "price"),
    [
        (candle(2, "100", "101", "98", "100"), "0", "stop_loss", "99"),
        (candle(2, "100", "103", "98", "100"), "0", "stop_loss", "99"),
        (candle(2, "95", "96", "94", "95"), "0.001", "stop_loss", "94.9"),
        (candle(2, "100", "103", "99.5", "100"), "0", "take_profit", "102"),
        (candle(2, "105", "106", "104", "105"), "0", "take_profit", "105"),
        (candle(2, "105", "106", "90", "100"), "0", "take_profit", "105"),
    ],
    ids=[
        "wick-stop",
        "both-stop-first",
        "gap-stop",
        "wick-take",
        "gap-take",
        "open-take-precedes-low",
    ],
)
def test_protective_exits_and_net_pnl(
    btc_instrument: Instrument,
    exit_bar: Candle,
    slip: str,
    reason: str,
    price: str,
) -> None:
    result, _, _ = run(btc_instrument, [candle(0), candle(1), exit_bar], slip=slip)
    assert len(result.closed_trades) == 1
    assert result.fills[-1].fill_price == D(price)
    assert result.fills[-1].exit_reason == reason
    assert result.summary["exit_reason_percentages"] == {reason: D("100")}
    fees = D("100") * D("0.001") + D(price) * D("0.001")
    assert result.final_equity == D("1000") + D(price) - D("100") - fees
    assert result.summary["realized_pnl"] == result.summary["net_pnl"]
    assert result.summary["unrealized_pnl"] == 0
    assert result.closed_trades[0].pnl == result.summary["net_pnl"]


def test_open_entry_has_intrabar_stop_without_using_future_close_for_entry(
    btc_instrument: Instrument,
) -> None:
    result, _, _ = run(btc_instrument, [candle(0), candle(1, "100", "103", "98", "100")])
    assert result.fills[0].timestamp == START + timedelta(minutes=5)
    assert result.fills[-1].timestamp == START + timedelta(minutes=10)
    assert result.fills[-1].fill_price == D("99")


def test_unknown_intrabar_entry_is_not_stopped_retroactively(btc_instrument: Instrument) -> None:
    result, _, _ = run(btc_instrument, [candle(0), candle(1, "105", "106", "98", "101")])
    assert len(result.fills) == 1
    assert result.fills[0].timestamp == START + timedelta(minutes=10)
    assert result.summary["has_open_position"]
    assert result.summary["unrealized_pnl"] == D("1")
    assert result.summary["realized_pnl"] == D("-0.1")
    assert result.summary["net_pnl"] == D("0.9")


def test_stop_remainder_continues_at_next_open_after_partial_execution(
    btc_instrument: Instrument,
) -> None:
    result, _, strategy = run(
        btc_instrument,
        [
            candle(0),
            candle(1),
            candle(2, "100", "101", "98", "100", "0.4"),
            candle(3, "95", "96", "94", "95"),
        ],
    )
    sells = [fill for fill in result.fills if fill.side is OrderSide.SELL]
    assert [fill.quantity for fill in sells] == [D("0.4"), D("0.6")]
    assert [fill.fill_price for fill in sells] == [D("99"), D("95")]
    assert result.summary["closed_trade_count"] == 1
    assert isinstance(strategy, ScriptedStrategy)
    assert any(state is OrderState.PARTIALLY_FILLED for state, _ in strategy.updates)


def test_complete_trades_distinguish_average_win_loss_ratio_and_profit_factor(
    btc_instrument: Instrument,
    tmp_path: Path,
) -> None:
    bars = [
        candle(0),
        candle(1, "100", "110", "100", "110"),
        candle(2, "110", "110", "100", "100"),
        candle(3, "100", "120", "100", "120"),
        candle(4, "120", "120", "100", "100"),
        candle(5, "100", "100", "95", "95"),
        candle(6, "95", "95", "95", "95"),
    ]
    actions = {
        index: SignalAction.BUY if index % 2 == 0 else SignalAction.SELL for index in range(6)
    }
    result, _, _ = run(btc_instrument, bars, actions, protective=False, fee="0")
    assert result.closed_trade_pnls == (D("10"), D("20"), D("-5"))
    assert result.summary["profit_loss_ratio"] == D("3")
    assert result.summary["profit_factor"] == D("6")
    assert result.summary["average_holding_time_hours"] == D("300") / D("3600")
    assert result.summary["realized_pnl"] == result.summary["net_pnl"] == D("25")
    write_backtest_report(result, tmp_path)
    trades = list(csv.DictReader((tmp_path / "closed_trades.csv").open(encoding="utf-8")))
    assert len(trades) == 3
    assert sum(D(row["pnl"]) for row in trades) == D("25")
    summary = json.loads((tmp_path / "backtest_summary.json").read_text(encoding="utf-8"))
    assert summary["execution_model"] == "ohlc_limit_v2"


@pytest.mark.parametrize("cross_day", [False, True])
def test_daily_loss_uses_equity_including_fees_unrealized_loss_and_midnight_gap(
    btc_instrument: Instrument,
    cross_day: bool,
) -> None:
    start = START.replace(hour=23, minute=50) if cross_day else START
    bars = [
        candle(0, start=start),
        candle(1, start=start),
        candle(2, "95", "95", "95", "95", start=start),
    ]
    result, events, _ = run(
        btc_instrument,
        bars,
        {0: SignalAction.BUY, 1: SignalAction.BUY},
        protective=False,
        daily_loss="1",
    )
    assert result.summary["buy_count"] == 1
    rejection = [event for event in events.events if event.event_type == "RiskRejected"][-1]
    assert "daily_loss" in rejection.payload["rejected_by"]
    snapshot: Any = rejection.payload["risk_snapshot"]
    assert D(snapshot["daily_pnl"]) == (D("-5") if cross_day else D("-5.1"))


def test_unfilled_orders_are_cancelled_at_dataset_end_without_fake_trades(
    btc_instrument: Instrument,
) -> None:
    result, _, _ = run(
        btc_instrument,
        [candle(0), candle(1, "105", "106", "101", "105")],
        protective=False,
    )
    assert not result.fills and not result.closed_trades
    assert result.summary["pending_order_count_at_end"] == 1
    assert result.final_equity == D("1000")


def test_annualization_total_loss_and_overflow_are_not_reported_as_zero() -> None:
    assert _annualized_return(D("100"), D("0"), START, START + timedelta(days=1)) == D("-100")
    assert _annualized_return(D("100"), D("200"), START, START + timedelta(minutes=5)) is None


def test_vwap_atr_stop_uses_prior_bars_even_with_global_brackets_disabled(
    btc_instrument: Instrument,
) -> None:
    strategy = VWAPMeanReversionStrategy(
        VWAPMeanReversionParameters(vwap_window=3, rsi_period=2, atr_period=2)
    )
    bars = [
        candle(0),
        candle(1),
        candle(2, "98.5", "98.5", "98.5", "98.5"),
        candle(3, "98.5", "98.5", "98.5", "98.5"),
        candle(4, "98.5", "99", "95", "98.5"),
    ]
    result, _, _ = run(btc_instrument, bars, protective=False, strategy_override=strategy)
    assert result.fills[0].fill_price == D("98.5")
    assert result.fills[-1].fill_price == D("97.3")
    assert result.fills[-1].exit_reason == "stop_loss"
    assert result.summary["closed_trade_count"] == 1


def test_many_partial_trades_reconcile_cash_fees_and_realized_pnl_exactly(
    btc_instrument: Instrument,
) -> None:
    random = Random(2048)
    b = broker(btc_instrument)
    index = 0
    for cycle in range(30):
        quantity = str(D(random.randint(10, 90)) / D("100"))
        for side, limit in ((OrderSide.BUY, "200"), (OrderSide.SELL, "1")):
            price = str(D(random.randint(500, 1500)) / D("10"))
            bar = candle(index, price, price, price, price, "0.07")
            b.begin_bar(bar)
            index += 1
            order = b.submit_order(approve(f"{cycle}-{side}", side, limit, quantity, bar.timestamp))
            while order.is_open:
                price = str(D(random.randint(500, 1500)) / D("10"))
                b.begin_bar(candle(index, price, price, price, price, "0.07"))
                index += 1
                b.match_orders()
    net = b.portfolio.balances["USDT"] - D("1000")
    assert len(b.closed_trades) == 30
    assert len(b.fills) == 448
    assert sum(b.closed_trade_pnls, D("0")) == b.portfolio.realized_pnl == net
    assert sum((trade.entry_fees + trade.exit_fees for trade in b.closed_trades), D("0")) == sum(
        (fill.fee for fill in b.fills), D("0")
    )
