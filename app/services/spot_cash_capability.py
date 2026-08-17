from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.config.settings import TradingMode
from app.domain.capability import MaxAvailableSize, SpotCashCapabilityReport
from app.domain.market import Instrument, InstrumentType, TradeMode
from app.domain.position import AccountMode, PortfolioSnapshot


class SpotCashCapabilityEvaluator:
    """Evaluates one SPOT/cash pair without treating Futures mode as a blanket reject."""

    def evaluate(
        self,
        *,
        mode: TradingMode,
        instrument: Instrument,
        portfolio: PortfolioSnapshot,
        max_size: MaxAvailableSize,
        derivative_positions: dict[str, Decimal],
        open_order_count: int,
        checked_at: datetime,
    ) -> SpotCashCapabilityReport:
        configuration = portfolio.account_configuration
        account_mode = configuration.account_mode if configuration else AccountMode.UNKNOWN
        checks = {
            "demo_mode": "passed" if mode is TradingMode.DEMO else "failed",
            "instrument_type": "passed"
            if instrument.instrument_type is InstrumentType.SPOT
            else "failed",
            "instrument_tradable": "passed" if instrument.tradable else "failed",
            "trade_mode": "passed" if max_size.trade_mode is TradeMode.CASH else "failed",
            "account_mode": "passed"
            if account_mode in {AccountMode.SPOT, AccountMode.FUTURES}
            else "unsupported",
            "max_available_size": "passed"
            if max_size.max_buy is not None and max_size.max_sell is not None
            else "insufficient_data",
            "derivative_positions": "passed" if not derivative_positions else "failed",
            "open_orders": "passed" if open_order_count == 0 else "failed",
            "liabilities": "passed"
            if not any(
                asset.liabilities not in (None, Decimal("0"))
                for asset in portfolio.asset_balances.values()
            )
            else "failed",
        }
        blockers = tuple(name for name, status in checks.items() if status != "passed")
        warnings = (
            ("账户模式为 futures；仅本次 SPOT cash 能力证据成立，不扩展至保证金或衍生品交易。",)
            if account_mode is AccountMode.FUTURES
            else ()
        )
        return SpotCashCapabilityReport(
            not blockers,
            not blockers,
            account_mode,
            instrument.instrument_id,
            TradeMode.CASH,
            checks,
            blockers,
            warnings,
            checked_at,
        )
