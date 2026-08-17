from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot
from app.domain.market import Instrument, InstrumentStatus
from app.domain.order import Order, OrderSide, OrderState, OrderType, ProposedOrder
from app.domain.position import (
    AccountMode,
    AssetBalance,
    BalanceSource,
    BalanceValidationStatus,
    Portfolio,
    PortfolioSnapshot,
)
from app.domain.signal import Signal, SignalAction
from app.risk.risk_manager import RiskContext, RiskManager, default_risk_manager
from tests.conftest import make_candles, make_instrument

NOW = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


@pytest.fixture
def setup(btc_instrument: Instrument) -> tuple[RiskContext, ProposedOrder]:
    candle = make_candles(["100"])[0]
    signal = Signal(
        "signal",
        "buy_and_hold",
        btc_instrument.instrument_id,
        SignalAction.BUY,
        NOW,
        "test",
        Decimal("1"),
        metadata={"candle_confirmed": True},
    )
    portfolio = Portfolio(
        {btc_instrument.quote_currency: Decimal("100")},
        {btc_instrument.instrument_id: Decimal("0")},
    )
    context = RiskContext(
        mode=TradingMode.BACKTEST,
        portfolio=portfolio.snapshot(),
        instrument=btc_instrument,
        market=MarketSnapshot(candle, Decimal("100.0")),
        signal=signal,
        now=NOW,
        daily_pnl=Decimal("0"),
        drawdown_pct=Decimal("0"),
    )
    order = ProposedOrder(
        "client",
        "run",
        "buy_and_hold",
        btc_instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.1"),
        Decimal("100.0"),
        signal.signal_id,
        NOW,
    )
    return context, order


def manager() -> RiskManager:
    return default_risk_manager(
        maximum_order_notional=Decimal("20"),
        maximum_exposure=Decimal("100"),
        maximum_daily_loss=Decimal("10"),
        maximum_drawdown_pct=Decimal("5"),
        maximum_orders_per_minute=2,
        stale_after_seconds=600,
    )


def test_all_rules_allow_valid_order(setup: tuple[RiskContext, ProposedOrder]) -> None:
    context, order = setup
    decision = manager().evaluate(context, order)
    assert decision.allowed
    assert len(decision.rule_results) == 17


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"mode": TradingMode.LIVE}, "trading_mode"),
        ({"circuit_broken": True}, "circuit_breaker"),
        ({"daily_pnl": None}, "daily_loss"),
        ({"daily_pnl": Decimal("-10")}, "daily_loss"),
        ({"drawdown_pct": Decimal("5")}, "maximum_drawdown"),
        ({"recent_order_times": (NOW, NOW)}, "order_frequency"),
        ({"open_order_sides": frozenset({OrderSide.BUY})}, "duplicate_order"),
        ({"now": NOW + timedelta(minutes=11)}, "stale_market_data"),
    ],
)
def test_independent_rules_reject(
    setup: tuple[RiskContext, ProposedOrder], change: dict[str, object], rule: str
) -> None:
    context, order = setup
    decision = manager().evaluate(replace(context, **cast(Any, change)), order)
    assert rule in decision.rejected_by


def test_suspended_instrument_is_rejected(setup: tuple[RiskContext, ProposedOrder]) -> None:
    context, order = setup
    suspended = replace(context.instrument, status=InstrumentStatus.SUSPENDED)
    decision = manager().evaluate(replace(context, instrument=suspended), order)
    assert "instrument_tradable" in decision.rejected_by


def test_price_precision_is_dynamic(setup: tuple[RiskContext, ProposedOrder]) -> None:
    context, order = setup
    decision = manager().evaluate(context, replace(order, price=Decimal("100.01")))
    assert "price_precision" in decision.rejected_by


def test_minimum_notional_is_enforced(setup: tuple[RiskContext, ProposedOrder]) -> None:
    context, order = setup
    decision = manager().evaluate(context, replace(order, quantity=Decimal("0.001")))
    assert "minimum_order" in decision.rejected_by


def test_quantity_step_is_enforced(setup: tuple[RiskContext, ProposedOrder]) -> None:
    context, order = setup
    decision = manager().evaluate(context, replace(order, quantity=Decimal("0.100001")))
    assert "quantity_precision" in decision.rejected_by


def test_quote_currency_balance_is_not_assumed_usdt() -> None:
    instrument = make_instrument("BTC-USDC", "BTC", "USDC", "0.001", "0.1")
    candle = make_candles(["100"])[0]
    signal = Signal(
        "signal",
        "buy_and_hold",
        instrument.instrument_id,
        SignalAction.BUY,
        NOW,
        "test",
        Decimal("1"),
        metadata={"candle_confirmed": True},
    )
    portfolio = Portfolio({"USDT": Decimal("1000"), "USDC": Decimal("0")})
    context = RiskContext(
        TradingMode.BACKTEST,
        portfolio.snapshot(),
        instrument,
        MarketSnapshot(candle, Decimal("100.0")),
        signal,
        NOW,
        daily_pnl=Decimal("0"),
        drawdown_pct=Decimal("0"),
    )
    order = ProposedOrder(
        "client",
        "run",
        "buy_and_hold",
        instrument.instrument_id,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.1"),
        Decimal("100.0"),
        signal.signal_id,
        NOW,
    )
    assert "available_balance" in manager().evaluate(context, order).rejected_by


def test_risk_reducing_sell_is_not_blocked_by_entry_limits(
    btc_instrument: Instrument,
) -> None:
    candle = make_candles(["200"])[0]
    signal = Signal(
        "close-signal",
        "buy_and_hold",
        btc_instrument.instrument_id,
        SignalAction.CLOSE,
        NOW,
        "protective exit",
        Decimal("1"),
    )
    portfolio = Portfolio(
        {"USDT": Decimal("0")},
        {btc_instrument.instrument_id: Decimal("0.2")},
    )
    context = RiskContext(
        mode=TradingMode.BACKTEST,
        portfolio=portfolio.snapshot(),
        instrument=btc_instrument,
        market=MarketSnapshot(candle, Decimal("200.0")),
        signal=signal,
        now=NOW,
        daily_pnl=None,
        drawdown_pct=None,
    )
    sell = ProposedOrder(
        "close-client",
        "run",
        "buy_and_hold",
        btc_instrument.instrument_id,
        OrderSide.SELL,
        OrderType.LIMIT,
        Decimal("0.2"),
        Decimal("200.0"),
        signal.signal_id,
        NOW,
    )
    decision = manager().evaluate(context, sell)
    assert "maximum_order_notional" not in decision.rejected_by
    assert "maximum_exposure" not in decision.rejected_by
    assert "daily_loss" not in decision.rejected_by
    assert "maximum_drawdown" not in decision.rejected_by
    assert decision.allowed


def asset(currency: str, total: str, available: str, frozen: str) -> AssetBalance:
    return AssetBalance(
        currency=currency,
        cash_balance=Decimal(total),
        available_balance=Decimal(available),
        frozen_balance=Decimal(frozen),
        equity=Decimal(total),
        equity_usd=None,
        discount_equity=None,
        liabilities=None,
        unrealized_pnl=None,
        holding_quantity=Decimal(total),
        spendable_quantity=Decimal(available),
        account_mode=AccountMode.SPOT,
        source=BalanceSource.REST,
        fetched_at=NOW,
        raw_field_presence=frozenset(),
        is_authoritative=True,
        validation_status=BalanceValidationStatus.PASSED,
    )


def test_buy_uses_available_not_total_balance(
    setup: tuple[RiskContext, ProposedOrder],
) -> None:
    context, order = setup
    snapshot = PortfolioSnapshot(
        balances={"USDT": Decimal("100")},
        positions={context.instrument.instrument_id: Decimal("0")},
        average_entry_prices={},
        asset_balances={"USDT": asset("USDT", "100", "5", "95")},
    )
    decision = manager().evaluate(replace(context, portfolio=snapshot), order)
    assert "available_balance" in decision.rejected_by


def test_sell_uses_available_base_quantity(
    setup: tuple[RiskContext, ProposedOrder],
) -> None:
    context, order = setup
    snapshot = PortfolioSnapshot(
        balances={"BTC": Decimal("1"), "USDT": Decimal("0")},
        positions={context.instrument.instrument_id: Decimal("1")},
        average_entry_prices={},
        asset_balances={"BTC": asset("BTC", "1", "0.4", "0.6")},
    )
    sell = replace(order, side=OrderSide.SELL, quantity=Decimal("0.5"))
    decision = manager().evaluate(replace(context, portfolio=snapshot), sell)
    assert "available_balance" in decision.rejected_by
    assert "position_direction" in decision.rejected_by


def test_frozen_base_asset_remains_in_total_exposure(
    setup: tuple[RiskContext, ProposedOrder],
) -> None:
    context, order = setup
    snapshot = PortfolioSnapshot(
        balances={"BTC": Decimal("0.8"), "USDT": Decimal("100")},
        positions={context.instrument.instrument_id: Decimal("0.8")},
        average_entry_prices={},
        asset_balances={
            "BTC": asset("BTC", "0.8", "0.1", "0.7"),
            "USDT": asset("USDT", "100", "100", "0"),
        },
    )
    proposed = replace(order, quantity=Decimal("0.3"))
    decision = manager().evaluate(replace(context, portfolio=snapshot), proposed)
    assert "maximum_exposure" in decision.rejected_by


def test_pending_buy_is_included_in_projected_exposure(
    setup: tuple[RiskContext, ProposedOrder],
) -> None:
    context, order = setup
    pending_request = replace(
        order,
        client_order_id="pending",
        quantity=Decimal("0.6"),
    ).to_request()
    pending = Order(pending_request, state=OrderState.ACCEPTED)
    proposed = replace(order, quantity=Decimal("0.5"))
    decision = manager().evaluate(replace(context, open_orders=(pending,)), proposed)
    assert "maximum_exposure" in decision.rejected_by
