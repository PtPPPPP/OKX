"""Read-only personal portfolio exposure and explicitly configured risk checks.

This module consumes manually supplied snapshots. It imports no account settings,
credentials, order models, or execution services, and never proposes a trade.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, StrictBool, model_validator

from app.portfolio.personal_ledger import (
    LEDGER_CONTEXT,
    Currency,
    Identifier,
    LedgerModel,
    Money,
    Product,
    Timestamp,
    _decimal_input,
    _schema_version,
    quote_freshness,
)

ZERO = Decimal("0")
ONE = Decimal("1")
CHECK_LABELS = {
    "long_term_budget": "长期现货用途预算",
    "contract_budget": "合约保证金用途预算",
    "reserve_cash_target": "储备可用现金目标",
    "max_gross_exposure_ratio": "总名义敞口比例上限",
    "max_abs_net_exposure_ratio": "绝对净名义敞口比例上限",
    "min_available_cash_ratio": "可用现金比例下限",
    "budget_allocation": "三类用途预算合计",
    "contract_trade_loss_budget": "单笔合约止损情景损失预算",
    "max_scenario_loss": "组合情景损失上限",
    "equity_positive": "净权益必须为正",
}


def _risk_product(value: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+(?:-SWAP)?", value):
        raise ValueError("only spot BASE-QUOTE or linear BASE-QUOTE-SWAP products are supported")
    base, quote, *_ = value.split("-")
    if base == quote:
        raise ValueError("base and quote currencies must differ")
    return value


def _contract_product(value: str) -> str:
    _risk_product(value)
    if not value.endswith("-SWAP"):
        raise ValueError("linear contract instrument must use BASE-QUOTE-SWAP")
    return value


RiskProduct = Annotated[str, AfterValidator(_risk_product)]
ContractProduct = Annotated[str, AfterValidator(_contract_product)]
PositiveMoney = Annotated[Money, Field(gt=0)]
Shock = Annotated[Decimal, BeforeValidator(_decimal_input), Field(ge=-1)]


class RiskSettings(LedgerModel):
    long_term_budget: Money | None = None
    contract_budget: Money | None = None
    reserve_cash_target: Money | None = None
    product_weight_limits: dict[RiskProduct, Money | None] = Field(default_factory=dict)
    contract_trade_loss_budget: Money | None = None
    max_gross_exposure_ratio: Money | None = None
    max_abs_net_exposure_ratio: Money | None = None
    min_available_cash_ratio: Annotated[Money, Field(le=1)] | None = None
    max_scenario_loss: Money | None = None


class ProductMetadata(LedgerModel):
    underlying: Identifier | None = None
    asset_class: Identifier | None = None


class CashSnapshot(LedgerModel):
    balance: Money | None
    available: Money | None = None
    updated_at: Timestamp
    source: Identifier
    verified: StrictBool = False

    @model_validator(mode="after")
    def validate_available(self) -> Self:
        if (
            self.balance is not None
            and self.available is not None
            and self.available > self.balance
        ):
            raise ValueError("available cash cannot exceed settled cash balance")
        return self


class SpotHolding(LedgerModel):
    instrument: Product
    quantity: Money | None
    updated_at: Timestamp
    source: Identifier
    verified: StrictBool = False


class LinearContractHolding(LedgerModel):
    id: Identifier
    instrument: ContractProduct
    direction: Literal["long", "short"]
    base_quantity: Money | None
    entry_price: PositiveMoney | None
    updated_at: Timestamp
    source: Identifier
    verified: StrictBool = False
    opened_at: Timestamp | None = None
    stop_price: PositiveMoney | None = None
    estimated_trade_cost: Money | None = None


class RiskQuote(LedgerModel):
    instrument: RiskProduct
    price: PositiveMoney
    at: Timestamp
    source: Identifier


class ShockScenario(LedgerModel):
    name: Identifier
    shocks: dict[Identifier, Shock]


class RiskRequest(LedgerModel):
    schema_version: Annotated[Literal[1], BeforeValidator(_schema_version)] = 1
    currency: Currency = "USDT"
    as_of: Timestamp
    source: Identifier
    cash: CashSnapshot | None = None
    cash_scope: Literal["unknown", "spot_only", "all_settled_excluding_derivative_pnl"] = "unknown"
    positions_complete: StrictBool = False
    spot_positions: list[SpotHolding] = Field(default_factory=list)
    contracts: list[LinearContractHolding] = Field(default_factory=list)
    contract_collateral: Money | None = None
    products: dict[RiskProduct, ProductMetadata] = Field(default_factory=dict)
    quotes: list[RiskQuote] = Field(default_factory=list)
    settings: RiskSettings = Field(default_factory=RiskSettings)
    scenarios: list[ShockScenario] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        for values, label in (
            ([position.instrument for position in self.spot_positions], "spot product"),
            ([position.id for position in self.contracts], "contract ID"),
            ([quote.instrument for quote in self.quotes], "quote product"),
            ([scenario.name for scenario in self.scenarios], "scenario name"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")
        instruments = (
            [position.instrument for position in self.spot_positions]
            + [position.instrument for position in self.contracts]
            + [quote.instrument for quote in self.quotes]
            + list(self.products)
            + list(self.settings.product_weight_limits)
        )
        if any(instrument.split("-")[1] != self.currency for instrument in instruments):
            raise ValueError("all products must use the ledger currency; implicit FX is forbidden")
        classes: dict[str, str] = {}
        for metadata in self.products.values():
            if metadata.underlying is not None and metadata.asset_class is not None:
                previous = classes.get(metadata.underlying)
                if previous is not None and previous != metadata.asset_class:
                    raise ValueError("conflicting asset classes for the same declared underlying")
                classes[metadata.underlying] = metadata.asset_class
        if self.contract_collateral is not None and self.cash is not None:
            with localcontext(LEDGER_CONTEXT):
                if self.cash.balance is not None:
                    if self.contract_collateral > self.cash.balance:
                        raise ValueError("contract collateral cannot exceed settled cash balance")
                    if (
                        self.cash.available is not None
                        and self.contract_collateral + self.cash.available > self.cash.balance
                    ):
                        raise ValueError("available cash plus collateral exceeds settled balance")
        return self


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


def _ratio(value: Decimal | None, equity: Decimal | None) -> Decimal | None:
    return value / equity if value is not None and equity is not None and equity > 0 else None


def _holding_data(
    *,
    instrument: str,
    quantity: Decimal | None,
    updated_at: datetime,
    verified: bool,
    quote: RiskQuote | None,
    now: datetime,
    max_age: int,
) -> tuple[Decimal | None, str, list[str]]:
    reasons = []
    if not verified:
        reasons.append("实际持仓数量尚未人工核实")
    if quantity is None:
        reasons.append("实际持仓数量未知")
    if updated_at > now:
        reasons.append("持仓更新时间位于未来")
    status = quote_freshness(
        at=quote.at if quote else None, now=now, position_at=updated_at, max_age_seconds=max_age
    )
    if quantity == 0 and not reasons:
        return ZERO, "not_required", reasons
    if status != "fresh":
        labels = {
            "missing": "缺少报价",
            "future": "报价时间位于未来",
            "stale": "报价已过期",
            "before_position": "报价早于持仓更新时间",
        }
        reasons.append(f"{instrument}：{labels[status]}")
    notional = quantity * quote.price if not reasons and quantity is not None and quote else None
    return notional, status, reasons


def _quote_fields(quote: RiskQuote | None, status: str) -> dict[str, Any]:
    return {
        "quote_price": quote.price if quote else None,
        "quote_time": quote.at if quote else None,
        "quote_source": quote.source if quote else None,
        "quote_status": status,
        "quote_is_stale": status == "stale",
    }


def _spot_rows(request: RiskRequest, now: datetime, max_age: int) -> list[dict[str, Any]]:
    quotes = {quote.instrument: quote for quote in request.quotes}
    rows = []
    for holding in request.spot_positions:
        quote = quotes.get(holding.instrument)
        notional, status, reasons = _holding_data(
            instrument=holding.instrument,
            quantity=holding.quantity,
            updated_at=holding.updated_at,
            verified=holding.verified,
            quote=quote,
            now=now,
            max_age=max_age,
        )
        if holding.updated_at > request.as_of:
            reasons.append("持仓更新时间晚于请求快照时点，需要复核")
            notional = None
        rows.append(
            {
                **holding.model_dump(),
                **_quote_fields(quote, status),
                "market_value": notional,
                "signed_notional": notional,
                "gross_notional": notional,
                "exposure_complete": notional is not None,
                "valuation_complete": notional is not None,
                "reasons": reasons,
                "holding_policy": "thesis_hold",
                "max_holding_hours": None,
                "action": None,
            }
        )
    return rows


def _contract_rows(request: RiskRequest, now: datetime, max_age: int) -> list[dict[str, Any]]:
    quotes = {quote.instrument: quote for quote in request.quotes}
    rows = []
    for holding in request.contracts:
        quote = quotes.get(holding.instrument)
        notional, status, reasons = _holding_data(
            instrument=holding.instrument,
            quantity=holding.base_quantity,
            updated_at=holding.updated_at,
            verified=holding.verified,
            quote=quote,
            now=now,
            max_age=max_age,
        )
        holding_time_invalid = holding.updated_at > request.as_of or (
            holding.opened_at is not None
            and holding.opened_at > min(now, holding.updated_at, request.as_of)
        )
        if holding_time_invalid:
            reasons.append("合约开仓或更新时间晚于对应持仓/快照时点或位于未来，需要复核")
            notional = None
        sign = ONE if holding.direction == "long" else -ONE
        signed = sign * notional if notional is not None else None
        pnl = None
        if notional == 0:
            pnl = ZERO
        elif notional is not None:
            if holding.entry_price is None:
                reasons.append("线性合约入场价未知，无法计算未实现盈亏")
            elif holding.base_quantity is not None and quote is not None:
                pnl = sign * holding.base_quantity * (quote.price - holding.entry_price)
        loss = None
        loss_reasons = []
        if (
            not holding.verified
            or holding.updated_at > now
            or holding_time_invalid
            or holding.base_quantity is None
        ):
            loss_reasons.append("止损情景需要已核实且时间有效的实际基础币等价数量")
        if holding.entry_price is None:
            loss_reasons.append("待补充合约入场价")
        if holding.stop_price is None:
            loss_reasons.append("待补充手动止损价格假设")
        if holding.estimated_trade_cost is None:
            loss_reasons.append("待补充该笔交易费用估计，不能默认费用为零")
        if not loss_reasons:
            assert holding.base_quantity is not None
            assert holding.entry_price is not None and holding.stop_price is not None
            assert holding.estimated_trade_cost is not None
            loss = max(
                ZERO,
                sign * holding.base_quantity * (holding.entry_price - holding.stop_price)
                + holding.estimated_trade_cost,
            )
        hours = None
        if holding.opened_at is None:
            time_status = "UNKNOWN"
            time_reason = "合约开仓时间待补充，不能判定持仓时长"
        elif holding_time_invalid:
            time_status = "UNKNOWN"
            time_reason = "合约开仓或更新时间晚于对应持仓/快照时点或位于未来，需要复核"
        else:
            elapsed = now - holding.opened_at
            microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
            hours = Decimal(microseconds) / Decimal("3600000000")
            time_status = "REVIEW_DUE" if elapsed > timedelta(hours=72) else "WITHIN_72_HOURS"
            time_reason = (
                "超过72小时，仅提示复核，不自动退出"
                if time_status == "REVIEW_DUE"
                else "仅记录已知持仓时长"
            )
        rows.append(
            {
                **holding.model_dump(),
                **_quote_fields(quote, status),
                "signed_notional": signed,
                "gross_notional": notional,
                "unrealized_pnl": pnl,
                "exposure_complete": notional is not None,
                "valuation_complete": pnl is not None,
                "reasons": reasons,
                "stop_loss_assumption": loss,
                "stop_loss_reasons": loss_reasons,
                "stop_loss_note": "从入场价到手动止损价并计入估计费用的假设，不是保证的最大损失",
                "holding_hours": hours,
                "holding_time_status": time_status,
                "holding_review_required": time_status in {"UNKNOWN", "REVIEW_DUE"},
                "holding_time_note": time_reason,
                "max_holding_hours": 72,
                "asset_weight": None,
                "action": None,
            }
        )
    return rows


def _groups(
    request: RiskRequest,
    rows: list[dict[str, Any]],
    dimension: Literal["product", "underlying", "asset_class"],
    equity: Decimal | None,
    snapshot_valid: bool,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    metadata_complete = True
    for row in rows:
        symbol = str(row["instrument"])
        metadata = request.products.get(symbol)
        label = symbol if dimension == "product" else getattr(metadata, dimension, None)
        if label is None:
            label = f"未登记:{symbol}"
            metadata_complete = False
        groups.setdefault(label, []).append(row)
    collection_complete = (
        snapshot_valid and metadata_complete and all(row["exposure_complete"] for row in rows)
    )
    result = []
    for name, members in groups.items():
        gross = sum((row["gross_notional"] or ZERO for row in members), ZERO)
        net = sum((row["signed_notional"] or ZERO for row in members), ZERO)
        complete = snapshot_valid and all(row["exposure_complete"] for row in members)
        if dimension != "product":
            complete = complete and metadata_complete
        result.append(
            {
                "name": name,
                "instruments": sorted({str(row["instrument"]) for row in members}),
                "complete": complete,
                "gross_exposure": gross if complete else None,
                "net_exposure": net if complete else None,
                "partial_gross_exposure": gross,
                "partial_net_exposure": net,
                "gross_exposure_ratio": _ratio(gross, equity) if complete else None,
                "product_weight": _ratio(gross, equity) if complete else None,
                "weight_basis": "gross notional / net equity; not contract asset weight",
            }
        )
    return {"rows": result, "complete": collection_complete, "metadata_complete": metadata_complete}


def _scenarios(
    request: RiskRequest, rows: list[dict[str, Any]], equity: Decimal | None, snapshot_valid: bool
) -> list[dict[str, Any]]:
    result = []
    for scenario in request.scenarios:
        legs = []
        reasons: list[str] = []
        for row in rows:
            symbol = str(row["instrument"])
            metadata = request.products.get(symbol)
            underlying = metadata.underlying if metadata else None
            shock = scenario.shocks.get(underlying) if underlying is not None else None
            notional = row["signed_notional"]
            leg_pnl = None
            leg_reasons = []
            if notional == 0:
                leg_pnl = ZERO
            elif notional is None:
                leg_reasons.append("实际数量或有效报价不足，名义敞口未知")
            elif underlying is None:
                leg_reasons.append("未登记底层标的映射，不能猜测情景归属")
            elif shock is None:
                leg_reasons.append(f"情景未覆盖底层标的 {underlying}，不能按零冲击处理")
            else:
                leg_pnl = notional * shock
            reasons.extend(f"{symbol}：{reason}" for reason in leg_reasons)
            legs.append(
                {
                    "instrument": symbol,
                    "contract_id": row.get("id"),
                    "underlying": underlying,
                    "signed_notional": notional,
                    "shock": shock,
                    "pnl": leg_pnl,
                    "reasons": leg_reasons,
                }
            )
        partial = sum((leg["pnl"] or ZERO for leg in legs), ZERO)
        complete = snapshot_valid and equity is not None and not reasons
        if not snapshot_valid:
            reasons.append("持仓范围未确认完整或快照时间无效")
        if equity is None:
            reasons.append("基准净权益不完整，不能给出完整情景结果")
        result.append(
            {
                "name": scenario.name,
                "shocks": scenario.shocks,
                "complete": complete,
                "total_pnl": partial if complete else None,
                "equity_after": equity + partial if complete and equity is not None else None,
                "loss": max(ZERO, -partial) if complete else None,
                "partial_pnl": partial,
                "reasons": reasons,
                "legs": legs,
                "interpretation": "用户设定的线性价格冲击情景，不是概率、预测或最大损失；现金保持不变",
                "limitations": "未模拟跳空成交、资金费、强平、流动性和非线性风险",
            }
        )
    return result


def _check(
    name: str,
    actual: Decimal | None,
    limit: Decimal | None,
    equity: Decimal | None,
    *,
    minimum: bool = False,
    configured: bool | None = None,
) -> dict[str, Any]:
    if configured is None:
        configured = limit is not None
    if not configured:
        status, reasons = "NOT_SET", ["待设置：尚未登记该项个人风险限额"]
    elif actual is None or limit is None:
        status, reasons = "UNKNOWN", ["已设置限额，但当前数据不足，无法完整比较"]
    elif (actual < limit) if minimum else (actual > limit):
        status, reasons = "EXCEEDED", ["已低于要求的最低值" if minimum else "已超过登记的限额"]
    elif equity is not None and equity <= 0:
        status, reasons = "UNKNOWN", ["净权益非正，不能将风险检查展示为通过"]
    else:
        status, reasons = "PASS", ["当前可计算值在登记限额内；不代表安全承诺"]
    return {
        "name": name,
        "status": status,
        "actual": actual,
        "limit": limit,
        "comparison": ">=" if minimum else "<=",
        "reasons": reasons,
    }


def analyze_risk(
    request: RiskRequest, *, now: datetime, max_quote_age_seconds: int = 300
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware evaluation time required")
    now = now.astimezone(UTC)
    # Use the same policy as the personal ledger even if this request has no quotes.
    quote_freshness(at=None, now=now, position_at=now, max_age_seconds=max_quote_age_seconds)
    with localcontext(LEDGER_CONTEXT):
        return _analyze(request, now, max_quote_age_seconds)


def _analyze(request: RiskRequest, now: datetime, max_age: int) -> dict[str, Any]:
    spots = _spot_rows(request, now, max_age)
    contracts = _contract_rows(request, now, max_age)
    rows = [*spots, *contracts]
    snapshot_valid = request.positions_complete and request.as_of <= now
    valuation_reasons = []
    if not request.positions_complete:
        valuation_reasons.append("尚未确认持仓范围完整，不能把已登记部分当成完整账户")
    if request.as_of > now:
        valuation_reasons.append("请求快照时间位于未来")
    cash = request.cash
    cash_valid = cash is not None and cash.verified and cash.updated_at <= min(now, request.as_of)
    balance = cash.balance if cash_valid and cash is not None else None
    available = cash.available if cash_valid and cash is not None else None
    if balance is None:
        valuation_reasons.append("已结算现金余额缺失、未核实或时间无效")
    scope_valid = request.cash_scope != "unknown" and (
        not contracts or request.cash_scope == "all_settled_excluding_derivative_pnl"
    )
    if not scope_valid:
        valuation_reasons.append(
            "现金口径未明确；含合约时须登记包含保证金且不含合约未实现盈亏的已结算现金"
        )
    for row in rows:
        valuation_reasons.extend(f"{row['instrument']}：{reason}" for reason in row["reasons"])
    spot_partial = sum((row["market_value"] or ZERO for row in spots), ZERO)
    pnl_partial = sum((row["unrealized_pnl"] or ZERO for row in contracts), ZERO)
    spot_complete = snapshot_valid and all(row["valuation_complete"] for row in spots)
    contract_complete = snapshot_valid and all(row["valuation_complete"] for row in contracts)
    equity_complete = spot_complete and contract_complete and balance is not None and scope_valid
    equity = (
        balance + spot_partial + pnl_partial if equity_complete and balance is not None else None
    )
    exposures_complete = snapshot_valid and all(row["exposure_complete"] for row in rows)
    gross_partial = sum((row["gross_notional"] or ZERO for row in rows), ZERO)
    net_partial = sum((row["signed_notional"] or ZERO for row in rows), ZERO)
    gross = gross_partial if exposures_complete else None
    net = net_partial if exposures_complete else None
    for row in spots:
        row["asset_weight"] = _ratio(row["market_value"], equity)
    for row in rows:
        row["notional_weight"] = _ratio(row["gross_notional"], equity)
    dimensions: tuple[Literal["product", "underlying", "asset_class"], ...] = (
        "product",
        "underlying",
        "asset_class",
    )
    grouped: dict[str, dict[str, Any]] = {
        dimension: _groups(request, rows, dimension, equity, snapshot_valid)
        for dimension in dimensions
    }
    scenarios = _scenarios(request, rows, equity, snapshot_valid)
    cash_ratio = _ratio(available, equity)
    balance_ratio = _ratio(balance, equity)
    gross_ratio = _ratio(gross, equity)
    net_ratio = _ratio(abs(net) if net is not None else None, equity)
    settings = request.settings
    checks = [
        _check(
            "long_term_budget",
            spot_partial if spot_complete else None,
            settings.long_term_budget,
            equity,
        ),
        _check(
            "contract_budget",
            request.contract_collateral if cash_valid else None,
            settings.contract_budget,
            equity,
        ),
        _check(
            "reserve_cash_target", available, settings.reserve_cash_target, equity, minimum=True
        ),
        _check("max_gross_exposure_ratio", gross_ratio, settings.max_gross_exposure_ratio, equity),
        _check(
            "max_abs_net_exposure_ratio", net_ratio, settings.max_abs_net_exposure_ratio, equity
        ),
        _check(
            "min_available_cash_ratio",
            cash_ratio,
            settings.min_available_cash_ratio,
            equity,
            minimum=True,
        ),
    ]
    budgets = (settings.long_term_budget, settings.contract_budget, settings.reserve_cash_target)
    allocation = (
        sum((budget for budget in budgets if budget is not None), ZERO)
        if all(budget is not None for budget in budgets)
        else None
    )
    checks.append(
        _check("budget_allocation", allocation, equity, equity, configured=allocation is not None)
    )
    product_groups = {row["name"]: row for row in grouped["product"]["rows"]}
    for symbol in sorted(set(product_groups) | set(settings.product_weight_limits)):
        group = product_groups.get(symbol)
        weight = (
            group["product_weight"]
            if group
            else (ZERO if snapshot_valid and equity is not None and equity > 0 else None)
        )
        checks.append(
            _check(
                f"product_weight:{symbol}",
                weight,
                settings.product_weight_limits.get(symbol),
                equity,
            )
        )
    losses = [row["stop_loss_assumption"] for row in contracts]
    max_loss = (
        max(losses, default=ZERO)
        if snapshot_valid and all(loss is not None for loss in losses)
        else None
    )
    checks.append(
        _check("contract_trade_loss_budget", max_loss, settings.contract_trade_loss_budget, equity)
    )
    for row in contracts:
        checks.append(
            _check(
                f"contract_trade_loss:{row['id']}",
                row["stop_loss_assumption"],
                settings.contract_trade_loss_budget,
                equity,
            )
        )
    scenario_losses = [scenario["loss"] for scenario in scenarios]
    maximum_scenario_loss = (
        max(scenario_losses)
        if scenario_losses and all(loss is not None for loss in scenario_losses)
        else None
    )
    checks.append(
        _check("max_scenario_loss", maximum_scenario_loss, settings.max_scenario_loss, equity)
    )
    for scenario in scenarios:
        checks.append(
            _check(
                f"scenario_loss:{scenario['name']}",
                scenario["loss"],
                settings.max_scenario_loss,
                equity,
            )
        )
    positive = equity is not None and equity > 0
    checks.append(
        {
            "name": "equity_positive",
            "status": "PASS" if positive else "EXCEEDED" if equity is not None else "UNKNOWN",
            "actual": equity,
            "limit": ZERO,
            "comparison": ">",
            "reasons": ["净权益为正" if positive else "净权益未知或非正，不能展示风险检查全部通过"],
        }
    )
    reasons = list(valuation_reasons)
    if available is None:
        reasons.append("可用现金待补充或未核实，不能以包含保证金的现金余额替代")
    for dimension, label in (("underlying", "底层标的"), ("asset_class", "资产类别")):
        if not grouped[dimension]["metadata_complete"]:
            reasons.append(f"{label}映射未完整登记，合并敞口报告仅为部分数据")
    for row in contracts:
        reasons.extend(f"{row['id']}：{reason}" for reason in row["stop_loss_reasons"])
        if row["holding_review_required"]:
            reasons.append(f"{row['id']}：{row['holding_time_note']}")
    if not scenarios:
        reasons.append("尚未登记冲击情景，不能默认任何涨跌幅或最大损失")
    if any(check["status"] == "EXCEEDED" for check in checks):
        overall = "EXCEEDED"
    elif reasons or any(check["status"] in {"NOT_SET", "UNKNOWN"} for check in checks):
        overall = "INCOMPLETE"
    else:
        overall = "CHECKED"
    for check in checks:
        name = str(check["name"])
        check_label = CHECK_LABELS.get(name)
        if check_label is None:
            prefix, _, identifier = name.partition(":")
            check_label = {
                "product_weight": "产品总名义敞口比例",
                "contract_trade_loss": "单笔合约止损情景损失",
                "scenario_loss": "组合情景损失",
            }.get(prefix, prefix) + f"（{identifier}）"
        check["label"] = check_label
        if check["status"] != "PASS":
            reasons.extend(f"{check_label}：{reason}" for reason in check["reasons"])
    report = {
        "schema_version": 1,
        "currency": request.currency,
        "source": request.source,
        "as_of": request.as_of,
        "evaluated_at": now,
        "max_quote_age_seconds": max_age,
        "overall_status": overall,
        "status_label": {
            "EXCEEDED": "存在超限",
            "INCOMPLETE": "待补充或设置",
            "CHECKED": "已按设置核对",
        }[overall],
        "reasons": reasons,
        "valuation_complete": equity_complete,
        "net_equity": equity,
        "equity_formula": "已结算现金 + 现货市值 + 线性合约未实现盈亏；不叠加保证金、合约名义金额或用途预算",
        "cash": cash.model_dump() if cash else None,
        "cash_scope": request.cash_scope,
        "cash_balance": balance,
        "available_cash": available,
        "contract_collateral": request.contract_collateral,
        "cash_ratio": cash_ratio,
        "cash_ratio_label": "可用现金 / 净权益",
        "cash_balance_ratio": balance_ratio,
        "cash_balance_ratio_label": "含保证金的已结算现金余额 / 净权益",
        "spot_market_value": spot_partial if spot_complete else None,
        "contract_unrealized_pnl": pnl_partial if contract_complete else None,
        "partial_spot_market_value": spot_partial,
        "partial_contract_unrealized_pnl": pnl_partial,
        "partial_valuation_note": "部分已知金额不能作为完整账户估值",
        "exposure_complete": exposures_complete,
        "gross_exposure": gross,
        "net_exposure": net,
        "partial_gross_exposure": gross_partial,
        "partial_net_exposure": net_partial,
        "gross_exposure_ratio": gross_ratio,
        "abs_net_exposure_ratio": net_ratio,
        "spot_positions": spots,
        "contracts": contracts,
        "by_product": grouped["product"]["rows"],
        "by_underlying": grouped["underlying"]["rows"],
        "by_asset_class": grouped["asset_class"]["rows"],
        "grouping_complete": {dimension: data["complete"] for dimension, data in grouped.items()},
        "settings": settings.model_dump(),
        "budget_allocation": allocation,
        "budget_note": "长期、合约及储备现金预算仅为用途标签，不增加资产",
        "checks": checks,
        "scenarios": scenarios,
        "correlation": {
            "status": "not_calculated",
            "reason": "没有提供对齐的历史价格，不计算或编造历史相关性",
        },
        "correlation_status": "not_calculated",
        "scope": "人工登记的单计价币现货与线性合约风险参考，不是账户对账或安全承诺",
        "action": None,
    }
    output: dict[str, Any] = _json(report)
    return output
