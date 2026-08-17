from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.domain.capability import MaxAvailableSize
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import OrderSide, OrderType
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    ProposalStatus,
)
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from tests.conftest import make_instrument


def test_proposal_is_persisted_with_immutable_audit_and_no_order(tmp_path: Path) -> None:
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
        config=load_run_config(Path("configs/btc_ma_demo.yaml"), environ={}),
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
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'proposals.db'}"))
    repository.database.initialize()
    repository.save_demo_order_proposal(proposal)
    loaded = repository.load_demo_order_proposal(proposal.proposal_id)
    assert loaded is not None
    assert loaded.client_order_id == proposal.client_order_id
    assert not loaded.submission_performed
    assert DemoOrderPreflightService.validate_hash(loaded)
    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM demo_order_proposal_events").fetchone()[0] >= 1
        )


def test_proposal_state_machine_rejects_reactivation(tmp_path: Path) -> None:
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'state.db'}"))
    repository.database.initialize()
    with pytest.raises(ValueError, match="illegal proposal transition"):
        repository.transition_demo_order_proposal(
            "missing",
            expected=ProposalStatus.EXPIRED,
            new=ProposalStatus.READY_FOR_CONFIRMATION,
            event_type="bad",
            reason="bad",
        )
