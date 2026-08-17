from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.domain.capability import MaxAvailableSize
from app.domain.environment import TradingEnvironment
from app.domain.market import (
    Instrument,
    InstrumentStatus,
    InstrumentType,
    TradeMode,
)
from app.domain.order import (
    Order,
    OrderRequest,
    OrderSide,
    OrderSource,
    OrderState,
    OrderType,
)
from app.domain.position import (
    AccountConfiguration,
    AccountMode,
    PortfolioSnapshot,
)
from app.execution.demo_write_authorization import (
    DemoWriteAuthorization,
    DemoWriteOperation,
    _issue_demo_write_authorization,
    require_place_authorization,
)
from app.services.controlled_demo_write import ControlledDemoWriteService
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    ProposalStatus,
)
from app.storage.database import Database
from app.storage.repositories import TradingRepository


class FakeWriteClient:
    def __init__(self) -> None:
        self.place_calls = 0
        self.cancel_calls = 0
        self.request: OrderRequest | None = None

    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        require_place_authorization(authorization, request)
        self.place_calls += 1
        self.request = request
        order = Order(request)
        order.transition(OrderState.SUBMITTED, at=request.created_at)
        order.transition(OrderState.ACCEPTED, at=request.created_at)
        return order

    def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        if authorization is None:
            raise PermissionError("missing authorization")
        authorization.consume_cancel(instrument_id, client_order_id)
        self.cancel_calls += 1
        if self.request is None:
            raise AssertionError("place must precede cancel")
        order = Order(self.request, state=OrderState.ACCEPTED)
        order.transition(OrderState.CANCEL_PENDING, at=self.request.created_at)
        order.transition(OrderState.CANCELLED, at=self.request.created_at)
        return order


def _controlled_order(tmp_path: Path) -> tuple[TradingRepository, Order]:
    database = Database(f"sqlite:///{tmp_path / 'controlled-write.db'}")
    database.initialize()
    repository = TradingRepository(database)
    now = datetime.now(UTC)
    instrument = Instrument(
        "BTC-USDT",
        "BTC",
        "USDT",
        InstrumentType.SPOT,
        Decimal("0.1"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("0"),
        InstrumentStatus.LIVE,
    )
    portfolio = PortfolioSnapshot(
        {"USDT": Decimal("100")},
        {"BTC-USDT": Decimal("0")},
        {},
        account_configuration=AccountConfiguration(
            AccountMode.SPOT,
            None,
            False,
            None,
            now,
        ),
    )
    proposal = DemoOrderPreflightService().prepare_order(
        intent=DemoOrderIntent(
            "run-1",
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
        portfolio=portfolio,
        max_size=MaxAvailableSize(
            "BTC-USDT",
            TradeMode.CASH,
            Decimal("100"),
            Decimal("1"),
            now,
        ),
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("100"),
        now=now,
    )
    assert proposal.status is ProposalStatus.READY_FOR_CONFIRMATION
    repository.save_demo_order_proposal(proposal)
    with database.connect() as connection:
        connection.execute(
            """UPDATE private_state_control
            SET status='healthy',last_consistent_at=?,updated_at=? WHERE control_id=1""",
            (now.isoformat(), now.isoformat()),
        )
    repository.fence_demo_order_proposal(proposal.proposal_id)
    return repository, repository.begin_controlled_demo_submission(proposal)


def test_controlled_service_places_and_cancels_with_distinct_one_use_authorizations(
    tmp_path: Path,
) -> None:
    repository, local = _controlled_order(tmp_path)
    client = FakeWriteClient()
    service = ControlledDemoWriteService(repository, client)

    placed = service.place_order(local)
    repository.complete_controlled_demo_submission(
        local.request.signal_id,
        placed,
        event_type="submitted",
        proposal_status=ProposalStatus.SUBMITTED,
    )
    cancelled = service.cancel_order(local.request.client_order_id)

    assert placed.state is OrderState.ACCEPTED
    assert cancelled.state is OrderState.CANCELLED
    assert (client.place_calls, client.cancel_calls) == (1, 1)


def test_controlled_service_rejects_reissuing_place_authorization(tmp_path: Path) -> None:
    repository, local = _controlled_order(tmp_path)
    client = FakeWriteClient()
    service = ControlledDemoWriteService(repository, client)
    service.place_order(local)

    with pytest.raises(PermissionError, match="already issued"):
        service.place_order(local)

    assert client.place_calls == 1


def test_controlled_service_rejects_reissuing_cancel_authorization(tmp_path: Path) -> None:
    repository, local = _controlled_order(tmp_path)
    client = FakeWriteClient()
    service = ControlledDemoWriteService(repository, client)
    placed = service.place_order(local)
    repository.complete_controlled_demo_submission(
        local.request.signal_id,
        placed,
        event_type="submitted",
        proposal_status=ProposalStatus.SUBMITTED,
    )
    service.cancel_order(local.request.client_order_id)

    with pytest.raises(PermissionError, match="already issued"):
        service.cancel_order(local.request.client_order_id)

    assert client.cancel_calls == 1


def test_private_state_change_rejects_before_fake_client(tmp_path: Path) -> None:
    repository, local = _controlled_order(tmp_path)
    with repository.database.connect() as connection:
        connection.execute("UPDATE private_state_control SET version=version+1 WHERE control_id=1")
    client = FakeWriteClient()

    with pytest.raises(PermissionError, match="fence rejected"):
        ControlledDemoWriteService(repository, client).place_order(local)

    assert client.place_calls == 0


def _request() -> OrderRequest:
    return OrderRequest(
        "client-1",
        "BTC-USDT",
        OrderSide.BUY,
        OrderType.LIMIT,
        Decimal("0.01"),
        Decimal("100"),
        "proposal-1",
        datetime.now(UTC),
        run_id="run-1",
        strategy_name="moving_average_cross",
        mode="demo",
        order_source=OrderSource.MANUAL_DEMO_TEST,
    )


def _authorization(request: OrderRequest) -> DemoWriteAuthorization:
    return _issue_demo_write_authorization(
        operation=DemoWriteOperation.PLACE,
        proposal_id=request.signal_id,
        request=request,
        instrument_type=InstrumentType.SPOT,
        trade_mode=TradeMode.CASH,
        private_state_version=1,
        environment=TradingEnvironment.DEMO,
    )


def test_authorization_object_cannot_be_reused() -> None:
    request = _request()
    authorization = _authorization(request)
    require_place_authorization(authorization, request)

    with pytest.raises(PermissionError, match="already been used"):
        require_place_authorization(authorization, request)


def test_authorization_object_cannot_be_constructed_by_business_code() -> None:
    request = _request()
    with pytest.raises(PermissionError, match="controlled gate"):
        DemoWriteAuthorization(
            object(),
            operation=DemoWriteOperation.PLACE,
            proposal_id=request.signal_id,
            request=request,
            instrument_type=InstrumentType.SPOT,
            trade_mode=TradeMode.CASH,
            private_state_version=1,
            environment=TradingEnvironment.DEMO,
        )


def test_authorization_rejects_live_environment() -> None:
    request = _request()

    with pytest.raises(PermissionError, match="Demo environment"):
        _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE,
            proposal_id=request.signal_id,
            request=request,
            instrument_type=InstrumentType.SPOT,
            trade_mode=TradeMode.CASH,
            private_state_version=1,
            environment=TradingEnvironment.LIVE,
        )


@pytest.mark.parametrize(
    ("order_request", "instrument_type", "trade_mode", "message"),
    [
        (
            replace(_request(), instrument_id="ETH-USDT"),
            InstrumentType.SPOT,
            TradeMode.CASH,
            "BTC-USDT",
        ),
        (_request(), InstrumentType.SWAP, TradeMode.CASH, "SPOT"),
        (_request(), InstrumentType.SPOT, TradeMode.CROSS, "cash"),
        (
            replace(_request(), order_type=OrderType.MARKET),
            InstrumentType.SPOT,
            TradeMode.CASH,
            "LIMIT",
        ),
        (
            replace(_request(), quantity=Decimal("0.1")),
            InstrumentType.SPOT,
            TradeMode.CASH,
            "5 USDT",
        ),
    ],
)
def test_authorization_rejects_scope_and_budget_bypasses(
    order_request: OrderRequest,
    instrument_type: InstrumentType,
    trade_mode: TradeMode,
    message: str,
) -> None:
    with pytest.raises(PermissionError, match=message):
        _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE,
            proposal_id=order_request.signal_id,
            request=order_request,
            instrument_type=instrument_type,
            trade_mode=trade_mode,
            private_state_version=1,
            environment=TradingEnvironment.DEMO,
        )
