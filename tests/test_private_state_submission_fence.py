from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.domain.capability import MaxAvailableSize
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import OrderSide, OrderType
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    DemoOrderProposal,
)
from app.services.private_events import PrivateEventProcessor
from app.storage.database import Database
from app.storage.repositories import PrivateStateFenceDeferred, TradingRepository
from tests.conftest import make_instrument


def _repository_with_proposal(tmp_path: Path) -> tuple[TradingRepository, DemoOrderProposal]:
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'fence.db'}"))
    repository.database.initialize()
    now = datetime.now(UTC)
    repository.confirm_private_state_snapshots(now)
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
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("5"), Decimal("1"), now),
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("50000"),
        now=now,
    )
    repository.save_demo_order_proposal(proposal)
    return repository, proposal


def test_private_event_after_fence_blocks_local_submission(tmp_path: Path) -> None:
    repository, proposal = _repository_with_proposal(tmp_path)
    repository.fence_demo_order_proposal(proposal.proposal_id)
    event = PrivateEvent(
        PrivateEventKind.ACCOUNT,
        "account:after-fence",
        {
            "uTime": "2000",
            "details": [
                {
                    "ccy": "USDT",
                    "cashBal": "100",
                    "availBal": "100",
                    "frozenBal": "0",
                    "eq": "100",
                    "uTime": "2000",
                }
            ],
        },
    )
    assert PrivateEventProcessor(repository).process(event)

    with pytest.raises(
        PrivateStateFenceDeferred, match="private_state_changed_after_submission_fence"
    ):
        repository.begin_controlled_demo_submission(proposal)

    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT submission_performed FROM demo_order_proposals WHERE proposal_id=?",
                (proposal.proposal_id,),
            ).fetchone()[0]
            == 0
        )


def test_same_proposal_can_enter_submission_fence_once(tmp_path: Path) -> None:
    repository, proposal = _repository_with_proposal(tmp_path)

    def fence() -> str:
        try:
            repository.fence_demo_order_proposal(proposal.proposal_id)
        except ValueError:
            return "blocked"
        return "fenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fence(), range(2)))

    assert sorted(results) == ["blocked", "fenced"]
