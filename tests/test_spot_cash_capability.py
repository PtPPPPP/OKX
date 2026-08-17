from datetime import UTC, datetime
from decimal import Decimal

from app.config.settings import TradingMode
from app.domain.capability import MaxAvailableSize
from app.domain.market import TradeMode
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.services.spot_cash_capability import SpotCashCapabilityEvaluator
from tests.conftest import make_instrument


def test_futures_account_can_pass_spot_cash_dry_run_with_evidence() -> None:
    now = datetime.now(UTC)
    report = SpotCashCapabilityEvaluator().evaluate(
        mode=TradingMode.DEMO,
        instrument=make_instrument("BTC-USDT", "BTC", "USDT", "0.0001", "0.1"),
        portfolio=PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.FUTURES, None, False, None, now),
        ),
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("100"), Decimal("1"), now),
        derivative_positions={},
        open_order_count=0,
        checked_at=now,
    )
    assert report.eligible_for_dry_run
    assert report.eligible_for_controlled_order_test
    assert report.warnings


def test_derivative_position_blocks_spot_cash_dry_run() -> None:
    now = datetime.now(UTC)
    report = SpotCashCapabilityEvaluator().evaluate(
        mode=TradingMode.DEMO,
        instrument=make_instrument("BTC-USDT", "BTC", "USDT", "0.0001", "0.1"),
        portfolio=PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.FUTURES, None, False, None, now),
        ),
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("100"), Decimal("1"), now),
        derivative_positions={"BTC-USDT-SWAP": Decimal("1")},
        open_order_count=0,
        checked_at=now,
    )
    assert not report.eligible_for_dry_run
    assert "derivative_positions" in report.blockers
