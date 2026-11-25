from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from app.domain.environment import TradingEnvironment
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import ApprovedOrder, Order, OrderRequest, OrderState, OrderType
from app.exchange.exceptions import NetworkError, OrderStateUnknown
from app.execution.demo_write_authorization import (
    MAXIMUM_DEMO_NOTIONAL,
    DemoWriteAuthorization,
    DemoWriteOperation,
    _issue_demo_write_authorization,
)
from app.services.demo_order_preflight import DemoOrderProposal
from app.storage.repositories import TradingRepository


class ControlledDemoWriteClient(Protocol):
    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order: ...

    def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order: ...


class ControlledDemoWriteService:
    """The sole production service allowed to issue OKX Demo write capabilities."""

    def __init__(
        self,
        repository: TradingRepository,
        client: ControlledDemoWriteClient,
    ) -> None:
        self.repository = repository
        self.client = client

    def place_order(self, local_order: Order) -> Order:
        proposal_id = local_order.request.signal_id
        proposal = self.repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            raise PermissionError("Demo order submission requires a persisted Proposal")
        self._validate_linkage(local_order, proposal)
        private_state_version = self.repository.authorize_controlled_demo_write(
            proposal_id,
            local_order.request.client_order_id,
            operation=DemoWriteOperation.PLACE.value,
        )
        authorization = _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE,
            proposal_id=proposal_id,
            request=local_order.request,
            instrument_type=proposal.instrument_type,
            trade_mode=proposal.trade_mode,
            private_state_version=private_state_version,
            environment=TradingEnvironment.DEMO,
        )
        try:
            remote_order = self.client.place_order(local_order.request, authorization=authorization)
        except NetworkError:
            self.repository.mark_controlled_demo_submission_unknown(
                proposal_id,
                error_category="transport_outcome_unknown",
                http_status=None,
            )
            raise
        if remote_order.state is OrderState.UNKNOWN:
            self.repository.mark_controlled_demo_submission_unknown(
                proposal_id,
                error_category="transport_outcome_unknown",
                http_status=None,
            )
            raise OrderStateUnknown("controlled Demo order outcome is unknown")
        return remote_order

    def cancel_order(self, client_order_id: str) -> Order:
        local_order = self.repository.load_order(client_order_id)
        if local_order is None:
            raise PermissionError("Demo cancellation requires a controlled local order")
        proposal_id = local_order.request.signal_id
        proposal = self.repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            raise PermissionError("Demo cancellation requires a persisted Proposal")
        self._validate_linkage(local_order, proposal)
        private_state_version = self.repository.authorize_controlled_demo_write(
            proposal_id,
            client_order_id,
            operation=DemoWriteOperation.CANCEL.value,
        )
        authorization = _issue_demo_write_authorization(
            operation=DemoWriteOperation.CANCEL,
            proposal_id=proposal_id,
            request=local_order.request,
            instrument_type=proposal.instrument_type,
            trade_mode=proposal.trade_mode,
            private_state_version=private_state_version,
            environment=TradingEnvironment.DEMO,
        )
        remote = self.client.cancel_order(
            local_order.request.instrument_id,
            client_order_id,
            authorization=authorization,
        )
        controlled = replace(remote, request=local_order.request)
        self.repository.save_order(controlled)
        return controlled

    def submit_approved_order(
        self,
        approved: ApprovedOrder,
        *,
        private_state_version: int = 1,
    ) -> Order:
        request = approved.proposed.to_request()
        local_order = Order(request=request)
        self.repository.save_order(local_order)
        authorization = _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE,
            proposal_id=request.signal_id,
            request=request,
            instrument_type=InstrumentType.SPOT,
            trade_mode=TradeMode.CASH,
            private_state_version=private_state_version,
            environment=TradingEnvironment.DEMO,
        )
        try:
            remote_order = self.client.place_order(request, authorization=authorization)
        except NetworkError:
            local_order.transition(OrderState.SUBMITTED, at=approved.approved_at)
            local_order.transition(OrderState.UNKNOWN, at=approved.approved_at)
            self.repository.save_order(local_order)
            raise
        self.repository.save_order(remote_order)
        return remote_order

    def _validate_linkage(self, order: Order, proposal: DemoOrderProposal) -> None:
        request = order.request
        if (
            proposal.instrument_id != "BTC-USDT"
            or proposal.instrument_type is not InstrumentType.SPOT
            or proposal.trade_mode is not TradeMode.CASH
            or proposal.order_type is not OrderType.LIMIT
            or proposal.client_order_id != request.client_order_id
            or proposal.quantity != request.quantity
            or proposal.planned_limit_price != request.price
            or proposal.approved_notional != request.notional
            or proposal.approved_notional <= 0
            or proposal.approved_notional > MAXIMUM_DEMO_NOTIONAL
        ):
            raise PermissionError("controlled Demo Proposal and order do not match")


class ControlledDemoExecutionBroker:
    """Minimal Broker adapter routing TradingEngine Demo orders through ControlledDemoWriteService."""

    def __init__(
        self,
        service: ControlledDemoWriteService,
        open_orders: tuple[Order, ...] = (),
        *,
        private_state_version: int = 1,
    ) -> None:
        self.service = service
        self._open_orders = list(open_orders)
        self.private_state_version = private_state_version

    def submit_order(self, order: ApprovedOrder) -> Order:
        placed = self.service.submit_approved_order(
            order,
            private_state_version=self.private_state_version,
        )
        if placed.is_open:
            self._open_orders.append(placed)
        return placed

    def cancel_order(self, instrument_id: str, order_id: str) -> Order:
        return self.service.cancel_order(order_id)

    def get_order(self, instrument_id: str, order_id: str) -> Order:
        loaded = self.service.repository.load_order(order_id)
        if loaded is None:
            raise ValueError(f"本地不存在订单: {order_id}")
        return loaded

    def get_open_orders(self, instrument_id: str | None = None) -> list[Order]:
        if instrument_id is None:
            return list(self._open_orders)
        return [o for o in self._open_orders if o.request.instrument_id == instrument_id]
