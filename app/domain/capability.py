from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.market import TradeMode
from app.domain.position import AccountMode


@dataclass(frozen=True, slots=True)
class MaxAvailableSize:
    instrument_id: str
    trade_mode: TradeMode
    max_buy: Decimal | None
    max_sell: Decimal | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SpotCashCapabilityReport:
    eligible_for_dry_run: bool
    eligible_for_controlled_order_test: bool
    account_mode: AccountMode
    instrument_id: str
    trade_mode: TradeMode
    checks: dict[str, str]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    checked_at: datetime

    @property
    def status(self) -> str:
        return "allowed" if self.eligible_for_dry_run else "blocked"
