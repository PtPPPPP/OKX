from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.context import MarketSnapshot
from app.domain.market import Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.position_sizing.base import PositionSizeDecision, PositionSizer
from app.risk.position_sizing import quantize_down


def _total_equity(
    portfolio: PortfolioSnapshot,
    market: MarketSnapshot,
    instrument: Instrument,
) -> tuple[Decimal, Decimal]:
    available_quote = portfolio.available_balance(instrument.quote_currency)
    position_qty = portfolio.position(instrument.instrument_id)
    equity = available_quote + max(position_qty, Decimal("0")) * market.price
    return equity, available_quote


def _sell_decision(
    portfolio: PortfolioSnapshot,
    market: MarketSnapshot,
    instrument: Instrument,
) -> PositionSizeDecision:
    position = portfolio.available_position(instrument.instrument_id, instrument.base_currency)
    quantity = quantize_down(position, instrument.quantity_step)
    return PositionSizeDecision(
        quantity=quantity,
        notional=quantity * market.price,
        reason="卖出当前策略持仓",
    )


class FixedNotionalPositionSizer:
    name = "fixed_notional"

    def __init__(self, order_notional: Decimal) -> None:
        if not order_notional.is_finite() or order_notional <= 0:
            raise ValueError("order_notional 必须大于 0")
        self.order_notional = order_notional

    def calculate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        instrument: Instrument,
    ) -> PositionSizeDecision:
        if market.price <= 0:
            raise ValueError("市场价格必须大于 0")
        if signal.action is SignalAction.BUY:
            requested = signal.suggested_notional or self.order_notional
            quantity = quantize_down(requested / market.price, instrument.quantity_step)
            return PositionSizeDecision(
                quantity=quantity,
                notional=quantity * market.price,
                reason=f"按 {instrument.quote_currency} 固定名义金额计算",
            )
        if signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            return _sell_decision(portfolio, market, instrument)
        return PositionSizeDecision(Decimal("0"), Decimal("0"), "hold 信号无需仓位")


class PercentEquityPositionSizer:
    name = "percent_equity"

    def __init__(self, equity_pct: Decimal) -> None:
        if not equity_pct.is_finite() or equity_pct <= 0 or equity_pct > 1:
            raise ValueError("equity_pct 必须在 (0, 1] 范围内")
        self.equity_pct = equity_pct

    def calculate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        instrument: Instrument,
    ) -> PositionSizeDecision:
        if market.price <= 0:
            raise ValueError("市场价格必须大于 0")
        if signal.action is SignalAction.BUY:
            equity, available_quote = _total_equity(portfolio, market, instrument)
            requested = min(equity * self.equity_pct, available_quote)
            if requested <= 0:
                return PositionSizeDecision(Decimal("0"), Decimal("0"), "可用资金不足")
            quantity = quantize_down(requested / market.price, instrument.quantity_step)
            return PositionSizeDecision(
                quantity=quantity,
                notional=quantity * market.price,
                reason=f"按账户总权益比例 ({self.equity_pct}) 计算",
            )
        if signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            return _sell_decision(portfolio, market, instrument)
        return PositionSizeDecision(Decimal("0"), Decimal("0"), "hold 信号无需仓位")


class RiskPerTradePositionSizer:
    name = "risk_per_trade"

    def __init__(
        self,
        risk_pct: Decimal,
        *,
        default_stop_pct: Decimal = Decimal("0.02"),
        max_notional_pct: Decimal = Decimal("0.25"),
    ) -> None:
        if not risk_pct.is_finite() or risk_pct <= 0 or risk_pct >= 1:
            raise ValueError("risk_pct 必须在 (0, 1) 范围内")
        if not default_stop_pct.is_finite() or default_stop_pct <= 0 or default_stop_pct >= 1:
            raise ValueError("default_stop_pct 必须在 (0, 1) 范围内")
        if not max_notional_pct.is_finite() or max_notional_pct <= 0 or max_notional_pct > 1:
            raise ValueError("max_notional_pct 必须在 (0, 1] 范围内")
        self.risk_pct = risk_pct
        self.default_stop_pct = default_stop_pct
        self.max_notional_pct = max_notional_pct

    def calculate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        instrument: Instrument,
    ) -> PositionSizeDecision:
        if market.price <= 0:
            raise ValueError("市场价格必须大于 0")
        if signal.action is SignalAction.BUY:
            equity, available_quote = _total_equity(portfolio, market, instrument)
            stop_distance = self._resolve_stop_distance(signal)
            risk_budget = equity * self.risk_pct
            raw_notional = risk_budget / stop_distance
            capped_notional = min(raw_notional, equity * self.max_notional_pct, available_quote)
            if capped_notional <= 0:
                return PositionSizeDecision(Decimal("0"), Decimal("0"), "可用资金不足")
            quantity = quantize_down(capped_notional / market.price, instrument.quantity_step)
            return PositionSizeDecision(
                quantity=quantity,
                notional=quantity * market.price,
                reason=f"按单笔风险比例 ({self.risk_pct}) 与止损距离 ({stop_distance}) 以损定仓",
            )
        if signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            return _sell_decision(portfolio, market, instrument)
        return PositionSizeDecision(Decimal("0"), Decimal("0"), "hold 信号无需仓位")

    def _resolve_stop_distance(self, signal: Signal) -> Decimal:
        raw = signal.metadata.get("stop_distance_pct")
        if raw is not None:
            try:
                parsed = Decimal(str(raw))
                if parsed.is_finite() and Decimal("0") < parsed < Decimal("1"):
                    return parsed
            except Exception:
                pass
        return self.default_stop_pct


def validate_position_sizing_parameters(name: str, parameters: dict[str, Any]) -> None:
    create_position_sizer(name, parameters)


def create_position_sizer(name: str, parameters: dict[str, Any]) -> PositionSizer:
    if name == "fixed_notional":
        allowed = {"order_notional"}
        unknown = set(parameters) - allowed
        if unknown:
            raise ValueError(f"fixed_notional 不支持参数: {', '.join(sorted(unknown))}")
        value = Decimal(str(parameters.get("order_notional", "0")))
        return FixedNotionalPositionSizer(value)
    if name == "percent_equity":
        allowed = {"equity_pct"}
        unknown = set(parameters) - allowed
        if unknown:
            raise ValueError(f"percent_equity 不支持参数: {', '.join(sorted(unknown))}")
        value = Decimal(str(parameters.get("equity_pct", "0")))
        return PercentEquityPositionSizer(value)
    if name == "risk_per_trade":
        allowed = {"risk_pct", "default_stop_pct", "max_notional_pct"}
        unknown = set(parameters) - allowed
        if unknown:
            raise ValueError(f"risk_per_trade 不支持参数: {', '.join(sorted(unknown))}")
        risk_pct = Decimal(str(parameters.get("risk_pct", "0")))
        default_stop = Decimal(str(parameters.get("default_stop_pct", "0.02")))
        max_notional = Decimal(str(parameters.get("max_notional_pct", "0.25")))
        return RiskPerTradePositionSizer(
            risk_pct,
            default_stop_pct=default_stop,
            max_notional_pct=max_notional,
        )
    raise ValueError(f"不支持的仓位计算器: {name}")
