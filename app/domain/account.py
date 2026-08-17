from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AssetBalanceUpdate:
    currency: str
    cash_balance: Decimal
    available_balance: Decimal | None
    frozen_balance: Decimal | None
    equity: Decimal | None
    usd_equity: Decimal | None
    updated_at: datetime

    def __post_init__(self) -> None:
        values = (
            self.cash_balance,
            self.available_balance,
            self.frozen_balance,
            self.equity,
            self.usd_equity,
        )
        if not self.currency or any(
            value is not None and (not value.is_finite() or value < 0) for value in values
        ):
            raise ValueError("私有账户余额更新无效")
        if self.updated_at.tzinfo is None:
            raise ValueError("私有账户更新时间必须包含时区")


@dataclass(frozen=True, slots=True)
class PrivateAccountState:
    event_kind: str
    event_time: datetime
    balances: Mapping[str, AssetBalanceUpdate]
    derivative_positions: Mapping[str, Decimal]
    needs_reconciliation: bool = True

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None:
            raise ValueError("私有状态事件时间必须包含时区")
        if any(not value.is_finite() for value in self.derivative_positions.values()):
            raise ValueError("私有持仓更新包含无效数量")

    @property
    def scope_key(self) -> str:
        currencies = ",".join(sorted(self.balances)) or "none"
        return f"{self.event_kind}:{currencies}"

    @property
    def has_nonzero_derivative_position(self) -> bool:
        return any(quantity != 0 for quantity in self.derivative_positions.values())
