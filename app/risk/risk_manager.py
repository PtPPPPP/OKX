from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot
from app.domain.market import Instrument
from app.domain.order import Order, OrderSide, ProposedOrder
from app.domain.position import PortfolioSnapshot
from app.domain.risk import RiskDecision, RiskRuleResult
from app.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class RiskContext:
    mode: TradingMode
    portfolio: PortfolioSnapshot
    instrument: Instrument
    market: MarketSnapshot
    signal: Signal
    now: datetime
    open_orders: tuple[Order, ...] = ()
    open_order_sides: frozenset[OrderSide] = frozenset()
    recent_order_times: tuple[datetime, ...] = ()
    daily_pnl: Decimal | None = None
    drawdown_pct: Decimal | None = None
    circuit_broken: bool = False


class RiskRule(Protocol):
    @property
    def name(self) -> str: ...

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult: ...


class RiskManager:
    def __init__(self, rules: list[RiskRule]) -> None:
        if not rules:
            raise ValueError("至少需要一条风控规则")
        self.rules = tuple(rules)

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskDecision:
        results = tuple(rule.evaluate(context, order) for rule in self.rules)
        rejected = tuple(result for result in results if not result.allowed)
        return RiskDecision(
            allowed=not rejected,
            rejected_by=tuple(result.rule_name for result in rejected),
            reasons=tuple(result.reason for result in rejected) or ("通过全部风控检查",),
            adjusted_order=order if not rejected else None,
            rule_results=results,
            risk_snapshot={
                "mode": context.mode.value,
                "instrument_id": context.instrument.instrument_id,
                "quote_currency": context.instrument.quote_currency,
                "quote_cash_balance": str(
                    context.portfolio.cash_balance(context.instrument.quote_currency)
                ),
                "quote_available_balance": str(
                    context.portfolio.available_balance(context.instrument.quote_currency)
                ),
                "quote_frozen_balance": str(
                    context.portfolio.frozen_balance(context.instrument.quote_currency)
                ),
                "position": str(context.portfolio.position(context.instrument.instrument_id)),
                "requested_notional": str(order.notional),
                "daily_pnl": str(context.daily_pnl),
                "drawdown_pct": str(context.drawdown_pct),
            },
        )


def result(name: str, allowed: bool, reason: str) -> RiskRuleResult:
    return RiskRuleResult(name, allowed, reason)


@dataclass(frozen=True, slots=True)
class TradingModeRule:
    name: str = "trading_mode"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = context.mode in {TradingMode.BACKTEST, TradingMode.DEMO}
        return result(self.name, allowed, "交易模式允许" if allowed else "当前交易模式禁止下单")


@dataclass(frozen=True, slots=True)
class CircuitBreakerRule:
    name: str = "circuit_breaker"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        return result(
            self.name,
            not context.circuit_broken,
            "系统未熔断" if not context.circuit_broken else "系统处于熔断状态",
        )


@dataclass(frozen=True, slots=True)
class TrustedPortfolioRule:
    name: str = "trusted_portfolio"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        return result(
            self.name,
            context.portfolio.trusted_for_trading,
            "账户模型可信" if context.portfolio.trusted_for_trading else "账户模型不可信，禁止下单",
        )


@dataclass(frozen=True, slots=True)
class InstrumentTradableRule:
    name: str = "instrument_tradable"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        return result(
            self.name,
            context.instrument.tradable,
            "交易品种可交易" if context.instrument.tradable else "交易品种不存在或暂停交易",
        )


@dataclass(frozen=True, slots=True)
class ConfirmedMarketDataRule:
    name: str = "confirmed_market_data"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = context.market.candle.confirmed
        return result(self.name, allowed, "K 线已收盘" if allowed else "K 线尚未确认收盘")


@dataclass(frozen=True, slots=True)
class StaleMarketDataRule:
    stale_after: timedelta
    name: str = "stale_market_data"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        age = context.now - context.signal.timestamp
        allowed = timedelta(0) <= age <= self.stale_after
        return result(self.name, allowed, "行情数据新鲜" if allowed else "行情数据已过期或来自未来")


@dataclass(frozen=True, slots=True)
class PricePrecisionRule:
    name: str = "price_precision"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = order.price > 0 and order.price % context.instrument.price_tick == 0
        return result(self.name, allowed, "价格精度正确" if allowed else "订单价格不符合交易规则")


@dataclass(frozen=True, slots=True)
class QuantityPrecisionRule:
    name: str = "quantity_precision"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = order.quantity > 0 and order.quantity % context.instrument.quantity_step == 0
        reason = "数量精度正确" if allowed else "订单数量不符合交易规则"
        return result(self.name, allowed, reason)


@dataclass(frozen=True, slots=True)
class MinimumOrderRule:
    name: str = "minimum_order"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        instrument = context.instrument
        allowed = order.quantity >= instrument.minimum_quantity and (
            instrument.minimum_notional <= 0 or order.notional >= instrument.minimum_notional
        )
        reason = "满足最小订单规则" if allowed else "订单低于最小数量或最小名义金额"
        return result(self.name, allowed, reason)


@dataclass(frozen=True, slots=True)
class MaximumOrderNotionalRule:
    maximum: Decimal
    name: str = "maximum_order_notional"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = order.side is OrderSide.SELL or order.notional <= self.maximum
        return result(
            self.name,
            allowed,
            "减仓订单允许"
            if order.side is OrderSide.SELL
            else "单笔金额合规"
            if allowed
            else "订单超过单笔名义金额限制",
        )


@dataclass(frozen=True, slots=True)
class MaximumExposureRule:
    maximum: Decimal
    name: str = "maximum_exposure"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        current = context.portfolio.position(order.instrument_id) * context.market.price
        pending_buys = sum(
            max(item.request.quantity - item.filled_quantity, Decimal("0")) * item.request.price
            for item in context.open_orders
            if item.request.instrument_id == order.instrument_id
            and item.request.side is OrderSide.BUY
            and item.is_open
        )
        projected = (
            current + pending_buys + order.notional
            if order.side is OrderSide.BUY
            else max(current - order.notional, Decimal("0")) + pending_buys
        )
        allowed = projected <= self.maximum
        return result(self.name, allowed, "总敞口合规" if allowed else "超过最大总风险敞口")


@dataclass(frozen=True, slots=True)
class AvailableBalanceRule:
    name: str = "available_balance"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        if order.side is OrderSide.BUY:
            available = context.portfolio.available_balance(context.instrument.quote_currency)
            allowed = order.notional <= available
        else:
            available = context.portfolio.available_position(
                order.instrument_id, context.instrument.base_currency
            )
            allowed = order.quantity <= available
        return result(self.name, allowed, "可用余额充足" if allowed else "可用余额不足")


@dataclass(frozen=True, slots=True)
class DuplicateOrderRule:
    name: str = "duplicate_order"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = order.side not in context.open_order_sides
        return result(self.name, allowed, "没有重复挂单" if allowed else "存在同方向未完成订单")


@dataclass(frozen=True, slots=True)
class PositionDirectionRule:
    name: str = "position_direction"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        position = context.portfolio.position(order.instrument_id)
        if order.side is OrderSide.BUY:
            allowed = position <= 0
            reason = "允许开仓" if allowed else "已有现货持仓，禁止重复买入"
        else:
            available = context.portfolio.available_position(
                order.instrument_id, context.instrument.base_currency
            )
            allowed = position > 0 and order.quantity <= available
            reason = "允许平仓" if allowed else "没有足够的现货持仓可卖"
        return result(self.name, allowed, reason)


@dataclass(frozen=True, slots=True)
class OrderFrequencyRule:
    maximum_per_minute: int
    name: str = "order_frequency"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        minute_ago = context.now - timedelta(minutes=1)
        count = sum(timestamp >= minute_ago for timestamp in context.recent_order_times)
        allowed = count < self.maximum_per_minute
        return result(self.name, allowed, "订单频率合规" if allowed else "超过最大订单频率")


@dataclass(frozen=True, slots=True)
class DailyLossRule:
    maximum_loss: Decimal
    name: str = "daily_loss"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = order.side is OrderSide.SELL or (
            context.daily_pnl is not None and context.daily_pnl > -self.maximum_loss
        )
        reason = (
            "减仓订单允许"
            if order.side is OrderSide.SELL
            else "每日亏损可计算且合规"
            if allowed
            else "每日亏损未知或达到限制"
        )
        return result(self.name, allowed, reason)


@dataclass(frozen=True, slots=True)
class MaximumDrawdownRule:
    maximum_pct: Decimal
    name: str = "maximum_drawdown"

    def evaluate(self, context: RiskContext, order: ProposedOrder) -> RiskRuleResult:
        allowed = order.side is OrderSide.SELL or (
            context.drawdown_pct is not None and context.drawdown_pct < self.maximum_pct
        )
        reason = (
            "减仓订单允许"
            if order.side is OrderSide.SELL
            else "最大回撤可计算且合规"
            if allowed
            else "最大回撤未知或达到限制"
        )
        return result(self.name, allowed, reason)


def default_risk_manager(
    *,
    maximum_order_notional: Decimal,
    maximum_exposure: Decimal,
    maximum_daily_loss: Decimal,
    maximum_drawdown_pct: Decimal,
    maximum_orders_per_minute: int,
    stale_after_seconds: int,
) -> RiskManager:
    return RiskManager(
        [
            TradingModeRule(),
            CircuitBreakerRule(),
            TrustedPortfolioRule(),
            InstrumentTradableRule(),
            ConfirmedMarketDataRule(),
            StaleMarketDataRule(timedelta(seconds=stale_after_seconds)),
            PricePrecisionRule(),
            QuantityPrecisionRule(),
            MinimumOrderRule(),
            MaximumOrderNotionalRule(maximum_order_notional),
            MaximumExposureRule(maximum_exposure),
            AvailableBalanceRule(),
            DuplicateOrderRule(),
            PositionDirectionRule(),
            OrderFrequencyRule(maximum_orders_per_minute),
            DailyLossRule(maximum_daily_loss),
            MaximumDrawdownRule(maximum_drawdown_pct),
        ]
    )
