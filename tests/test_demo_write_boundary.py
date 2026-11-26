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


def test_authorization_allows_live_environment_with_risk_configured_budget_and_Rejects_mismatch() -> (
    None
):
    live_req = replace(_request(), mode="live", quantity=Decimal("0.4"), price=Decimal("100"))
    # 0.4 * 100 = 40 USDT, allowed when max_notional is configured to 50 USDT from risk config
    auth = _issue_demo_write_authorization(
        operation=DemoWriteOperation.PLACE,
        proposal_id=live_req.signal_id,
        request=live_req,
        instrument_type=InstrumentType.SPOT,
        trade_mode=TradeMode.CASH,
        private_state_version=1,
        environment=TradingEnvironment.LIVE,
        max_notional=Decimal("50"),
    )
    auth.assert_environment_bound(TradingEnvironment.LIVE)
    with pytest.raises(PermissionError, match="environment mismatch"):
        auth.assert_environment_bound(TradingEnvironment.DEMO)

    # Mode mismatch (request.mode='demo' with TradingEnvironment.LIVE) must be rejected
    with pytest.raises(PermissionError, match="mode=live"):
        _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE,
            proposal_id=live_req.signal_id,
            request=_request(),
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


def test_demo_broker_controlled_engine_submission_and_network_timeout_unknown(
    tmp_path: Path,
) -> None:
    from app.domain.order import ApprovedOrder, ProposedOrder
    from app.exchange.exceptions import NetworkError
    from app.services.controlled_demo_write import ControlledDemoExecutionBroker

    database = Database(f"sqlite:///{tmp_path / 'demo-broker-p3.db'}")
    database.initialize()
    repository = TradingRepository(database)
    req = _request()
    proposed = ProposedOrder(
        client_order_id=req.client_order_id,
        run_id=req.run_id,
        strategy_name=req.strategy_name,
        instrument_id=req.instrument_id,
        side=req.side,
        order_type=req.order_type,
        quantity=req.quantity,
        price=req.price,
        signal_id=req.signal_id,
        created_at=req.created_at,
        mode=req.mode,
        bar=req.bar,
        order_source=req.order_source,
    )
    approved = ApprovedOrder(
        proposed=proposed,
        approved_at=req.created_at,
        approval_reason="approved",
    )

    class _BrokerClient(FakeWriteClient):
        def __init__(self, *, fail_network: bool = False) -> None:
            super().__init__()
            self.fail_network = fail_network

        def place_order(
            self,
            request: OrderRequest,
            *,
            authorization: DemoWriteAuthorization | None = None,
        ) -> Order:
            if self.fail_network:
                require_place_authorization(authorization, request)
                raise NetworkError("simulated transport timeout")
            return super().place_order(request, authorization=authorization)

    # 1. ControlledDemoExecutionBroker issues one-use authorization and persists ACCEPTED order
    ok_client = _BrokerClient()
    active_broker = ControlledDemoExecutionBroker(
        ControlledDemoWriteService(repository, ok_client),
        private_state_version=1,
    )
    placed = active_broker.submit_order(approved)
    assert placed.state is OrderState.ACCEPTED
    assert ok_client.place_calls == 1

    # 2. Network timeout transitions order to UNKNOWN in SQLite so it is never blindly resent
    timeout_proposed = replace(proposed, client_order_id="cl-timeout-1", signal_id="sig-timeout-1")
    timeout_approved = replace(approved, proposed=timeout_proposed)
    timeout_broker = ControlledDemoExecutionBroker(
        ControlledDemoWriteService(repository, _BrokerClient(fail_network=True)),
        private_state_version=1,
    )
    with pytest.raises(NetworkError):
        timeout_broker.submit_order(timeout_approved)
    saved = repository.load_order("cl-timeout-1")
    assert saved is not None
    assert saved.state is OrderState.UNKNOWN

    # 3. Stale pending orders lifecycle: cancel_pending_orders cancels open orders before new submission
    stale_client = _BrokerClient()
    lifecycle_service = ControlledDemoWriteService(
        repository,
        stale_client,
        max_order_notional=Decimal("50"),
        allowed_instrument_id="BTC-USDT",
        environment=TradingEnvironment.LIVE,
    )
    live_proposed = replace(
        proposed,
        client_order_id="cl-live-1",
        signal_id="sig-live-1",
        mode="live",
        quantity=Decimal("0.3"),
        price=Decimal("100"),
    )
    live_approved = replace(approved, proposed=live_proposed)
    lifecycle_broker = ControlledDemoExecutionBroker(lifecycle_service, private_state_version=1)
    placed_live = lifecycle_broker.submit_order(live_approved)
    assert placed_live.state is OrderState.ACCEPTED
    assert len(lifecycle_broker.get_open_orders("BTC-USDT")) == 1

    cancelled = lifecycle_broker.cancel_pending_orders()
    assert len(cancelled) == 1
    assert cancelled[0].state is OrderState.CANCELLED
    assert len(lifecycle_broker.get_open_orders("BTC-USDT")) == 0
    assert stale_client.cancel_calls == 1

    # 4. Exchange-side Algo Stop-Loss: BUY order automatically attaches conditional stop-loss,
    #    and subsequent SELL order automatically cancels the active algo stop-loss first.
    from app.domain.order import AlgoStopOrder, AlgoStopOrderRequest

    class _AlgoBrokerClient(_BrokerClient):
        def __init__(self) -> None:
            super().__init__()
            self.algo_place_calls = 0
            self.algo_cancel_calls = 0

        def place_algo_stop_order(
            self,
            request: AlgoStopOrderRequest,
            *,
            authorization: DemoWriteAuthorization | None = None,
        ) -> AlgoStopOrder:
            assert authorization is not None
            synthetic = OrderRequest(
                client_order_id=request.client_algo_id,
                instrument_id=request.instrument_id,
                side=request.side,
                order_type=OrderType.LIMIT,
                quantity=request.quantity,
                price=request.trigger_price,
                signal_id=request.signal_id,
                created_at=request.created_at,
                mode=request.mode,
            )
            authorization.consume_place_algo(synthetic)
            self.algo_place_calls += 1
            return AlgoStopOrder(
                request=request,
                algo_id="algo-sl-1001",
                state="live",
                updated_at=request.created_at,
            )

        def cancel_algo_stop_order(
            self,
            instrument_id: str,
            algo_id: str,
            client_algo_id: str,
            *,
            authorization: DemoWriteAuthorization | None = None,
        ) -> dict[str, str]:
            assert authorization is not None
            authorization.consume_cancel_algo(instrument_id, client_algo_id)
            self.algo_cancel_calls += 1
            return {"algoId": algo_id, "sCode": "0"}

    algo_client = _AlgoBrokerClient()
    algo_service = ControlledDemoWriteService(
        repository,
        algo_client,
        max_order_notional=Decimal("50"),
        allowed_instrument_id="BTC-USDT",
        environment=TradingEnvironment.LIVE,
    )
    algo_broker = ControlledDemoExecutionBroker(algo_service, private_state_version=1)
    buy_with_sl = replace(
        live_proposed,
        client_order_id="cl-buy-sl-1",
        signal_id="sig-buy-sl-1",
        stop_loss_price=Decimal("95.5"),
    )
    algo_broker.submit_order(replace(approved, proposed=buy_with_sl))
    assert algo_client.algo_place_calls == 1
    assert len(algo_broker.active_algo_orders) == 1
    assert algo_broker.active_algo_orders[0].request.trigger_price == Decimal("95.5")

    sell_exit = replace(
        live_proposed,
        client_order_id="cl-sell-exit-1",
        signal_id="sig-sell-exit-1",
        side=OrderSide.SELL,
    )
    algo_broker.submit_order(replace(approved, proposed=sell_exit))
    assert algo_client.algo_cancel_calls == 1
    assert len(algo_broker.active_algo_orders) == 0
