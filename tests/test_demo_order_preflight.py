from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import RunConfig, load_run_config
from app.domain.capability import MaxAvailableSize
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import OrderSide, OrderType
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    ProposalStatus,
)
from tests.conftest import make_instrument


def test_preflight_creates_non_submittable_proposal() -> None:
    now = datetime.now(UTC)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    proposal = DemoOrderPreflightService().prepare_order(
        intent=DemoOrderIntent(
            "run",
            "moving_average_cross",
            "BTC-USDT",
            InstrumentType.SPOT,
            TradeMode.CASH,
            OrderSide.BUY,
            OrderType.LIMIT,
            Decimal("5"),
            "manual_demo_test",
            now,
        ),
        config=RunConfig(),
        instrument=instrument,
        portfolio=PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.FUTURES, None, False, None, now),
        ),
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("5000"), Decimal("1"), now),
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("50000"),
        now=now,
    )
    assert proposal.status is ProposalStatus.BLOCKED
    assert not proposal.submission_performed
    assert "controlled_submission_disabled" in proposal.blockers
    assert len(proposal.proposal_hash) == 64


@pytest.mark.parametrize(
    ("case", "expected_blocker"),
    [
        ("wrong_instrument", "instrument_not_allowed"),
        ("non_spot", "spot_required"),
        ("non_cash", "cash_required"),
        ("non_limit", "limit_required"),
        ("over_budget", "notional_budget_exceeded"),
    ],
)
def test_preflight_rejects_every_fixed_demo_scope_bypass(
    case: str,
    expected_blocker: str,
) -> None:
    now = datetime.now(UTC)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    intent = DemoOrderIntent(
        "run",
        "moving_average_cross",
        "BTC-USDT",
        InstrumentType.SPOT,
        TradeMode.CASH,
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("5"),
        "manual_demo_test",
        now,
    )
    max_size = MaxAvailableSize(
        "BTC-USDT",
        TradeMode.CASH,
        Decimal("5000"),
        Decimal("1"),
        now,
    )
    if case == "wrong_instrument":
        intent = replace(intent, instrument_id="ETH-USDT")
    elif case == "non_spot":
        intent = replace(intent, instrument_type=InstrumentType.SWAP)
        instrument = replace(instrument, instrument_type=InstrumentType.SWAP)
    elif case == "non_cash":
        intent = replace(intent, trade_mode=TradeMode.CROSS)
        max_size = replace(max_size, trade_mode=TradeMode.CROSS)
    elif case == "non_limit":
        intent = replace(intent, order_type=OrderType.MARKET)
    elif case == "over_budget":
        intent = replace(intent, requested_notional=Decimal("5.01"))
    proposal = DemoOrderPreflightService().prepare_order(
        intent=intent,
        config=load_run_config(Path("configs/btc_ma_demo.yaml"), environ={}),
        instrument=instrument,
        portfolio=PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(
                AccountMode.SPOT,
                None,
                False,
                None,
                now,
            ),
        ),
        max_size=max_size,
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("50000"),
        now=now,
    )
    assert proposal.status is ProposalStatus.BLOCKED
    assert expected_blocker in proposal.blockers
