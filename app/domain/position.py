from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType


class AccountMode(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"
    MULTI_CURRENCY_MARGIN = "multi_currency_margin"
    PORTFOLIO_MARGIN = "portfolio_margin"
    UNKNOWN = "unknown"


class BalanceSource(StrEnum):
    REST = "rest"
    PRIVATE_WEBSOCKET = "private_websocket"
    LEGACY = "legacy"


class BalanceValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class AccountConfiguration:
    account_mode: AccountMode
    position_mode: str | None
    auto_loan_enabled: bool | None
    greeks_type: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class AssetBalance:
    """Raw OKX asset fields, never relabeling cashBal as a total balance."""

    currency: str
    cash_balance: Decimal | None
    available_balance: Decimal | None
    frozen_balance: Decimal | None
    equity: Decimal | None
    equity_usd: Decimal | None
    discount_equity: Decimal | None
    liabilities: Decimal | None
    unrealized_pnl: Decimal | None
    holding_quantity: Decimal | None
    spendable_quantity: Decimal | None
    account_mode: AccountMode
    source: BalanceSource
    fetched_at: datetime
    raw_field_presence: frozenset[str]
    is_authoritative: bool
    validation_status: BalanceValidationStatus

    def __post_init__(self) -> None:
        if not self.currency or self.fetched_at.tzinfo is None:
            raise ValueError("资产余额缺少币种或带时区的时间")
        values = (
            self.cash_balance,
            self.available_balance,
            self.frozen_balance,
            self.equity,
            self.equity_usd,
            self.discount_equity,
            self.liabilities,
            self.unrealized_pnl,
            self.holding_quantity,
            self.spendable_quantity,
        )
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("资产余额包含无效数字")
        non_negative = (
            self.cash_balance,
            self.available_balance,
            self.frozen_balance,
            self.equity,
            self.equity_usd,
            self.discount_equity,
            self.liabilities,
            self.holding_quantity,
            self.spendable_quantity,
        )
        if any(value is not None and value < 0 for value in non_negative):
            raise ValueError("资产余额包含负数")


@dataclass(frozen=True, slots=True)
class BalanceInvariantResult:
    rule_name: str
    status: BalanceValidationStatus
    account_mode: AccountMode
    currencies: tuple[str, ...]
    observed_values: Mapping[str, str | None]
    reason: str


@dataclass(frozen=True, slots=True)
class AccountEquitySnapshot:
    okx_total_equity: Decimal | None
    okx_adjusted_equity: Decimal | None
    okx_equity_usd: Decimal | None
    account_mode: AccountMode


@dataclass(frozen=True, slots=True)
class PositionCost:
    average_entry_price: Decimal | None
    cost_source: str
    cost_is_reliable: bool

    def __post_init__(self) -> None:
        if self.average_entry_price is not None and (
            not self.average_entry_price.is_finite() or self.average_entry_price <= 0
        ):
            raise ValueError("平均开仓成本必须是大于 0 的有限数")
        if not self.cost_source:
            raise ValueError("成本来源不能为空")
        if self.cost_is_reliable and self.average_entry_price is None:
            raise ValueError("可靠成本必须包含平均开仓价格")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    balances: Mapping[str, Decimal]
    positions: Mapping[str, Decimal]
    average_entry_prices: Mapping[str, Decimal]
    realized_pnl: Decimal = Decimal("0")
    asset_balances: Mapping[str, AssetBalance] = field(default_factory=dict)
    position_costs: Mapping[str, PositionCost] = field(default_factory=dict)
    account_configuration: AccountConfiguration | None = None
    account_equity: AccountEquitySnapshot | None = None
    model_version: str = "v4"
    trusted_for_trading: bool = True

    def cash_balance(self, currency: str) -> Decimal | None:
        asset = self.asset_balances.get(currency)
        return asset.cash_balance if asset is not None else self.balances.get(currency)

    def available_balance(self, currency: str) -> Decimal:
        asset = self.asset_balances.get(currency)
        if asset is not None:
            return asset.available_balance or Decimal("0")
        return self.balances.get(currency, Decimal("0"))

    def frozen_balance(self, currency: str) -> Decimal | None:
        asset = self.asset_balances.get(currency)
        return asset.frozen_balance if asset is not None else None

    def position(self, instrument_id: str) -> Decimal:
        return self.positions.get(instrument_id, Decimal("0"))

    def available_position(self, instrument_id: str, base_currency: str) -> Decimal:
        asset = self.asset_balances.get(base_currency)
        if asset is not None:
            return asset.spendable_quantity or Decimal("0")
        return self.position(instrument_id)

    def position_cost(self, instrument_id: str) -> PositionCost:
        cost = self.position_costs.get(instrument_id)
        if cost is not None:
            return cost
        average = self.average_entry_prices.get(instrument_id)
        return PositionCost(
            average, "legacy_snapshot" if average is not None else "unknown", average is not None
        )


@dataclass(slots=True)
class Portfolio:
    balances: dict[str, Decimal]
    positions: dict[str, Decimal] = field(default_factory=dict)
    average_entry_prices: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")

    def snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            balances=MappingProxyType(dict(self.balances)),
            positions=MappingProxyType(dict(self.positions)),
            average_entry_prices=MappingProxyType(dict(self.average_entry_prices)),
            realized_pnl=self.realized_pnl,
            position_costs=MappingProxyType(
                {
                    key: PositionCost(value, "backtest", True)
                    for key, value in self.average_entry_prices.items()
                }
            ),
            model_version="backtest-v1",
        )

    def equity(
        self,
        instrument_id: str,
        base_currency: str,
        quote_currency: str,
        price: Decimal,
    ) -> Decimal:
        """现货账户权益：计价币现金余额加上持仓按标记价格折算的市值。

        与模拟盘对账用的权益口径一致（见 bootstrap 中的 current_equity）。
        base_currency 为接口对称保留：现货单品种回测中基础币余额与该品种
        持仓数量由 Broker 保持一致，故以 positions 计算持仓市值。
        """
        quote_balance = self.balances.get(quote_currency, Decimal("0"))
        position = self.positions.get(instrument_id, Decimal("0"))
        return quote_balance + position * price
