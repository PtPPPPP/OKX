from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from app.domain.environment import TradingEnvironment
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import (
    AlgoStopOrder,
    AlgoStopOrderRequest,
    ApprovedOrder,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
)
from app.exchange.exceptions import NetworkError, OrderStateUnknown
from app.execution.demo_write_authorization import (
    MAXIMUM_DEMO_NOTIONAL,
    DemoWriteAuthorization,
    DemoWriteOperation,
    _issue_demo_write_authorization,
)
from app.execution.order_manager import resolve_bbo_maker_price
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
        *,
        max_order_notional: Decimal = MAXIMUM_DEMO_NOTIONAL,
        allowed_instrument_id: str = "BTC-USDT",
        environment: TradingEnvironment = TradingEnvironment.DEMO,
        use_post_only: bool = False,
        price_tick: Decimal = Decimal("0.1"),
    ) -> None:
        self.repository = repository
        self.client = client
        self.max_order_notional = max_order_notional
        self.allowed_instrument_id = allowed_instrument_id
        self.environment = environment
        self.use_post_only = use_post_only
        self.price_tick = price_tick

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
        if self.use_post_only or request.order_type is OrderType.POST_ONLY:
            bid_px: Decimal | None = None
            ask_px: Decimal | None = None
            ticker_fn = getattr(self.client, "get_ticker", None)
            if ticker_fn is not None:
                try:
                    bid_px, ask_px, _, _ = ticker_fn(request.instrument_id)
                except Exception:
                    bid_px, ask_px = None, None
            maker_price = resolve_bbo_maker_price(
                request.side,
                request.price,
                bid_price=bid_px,
                ask_price=ask_px,
                price_tick=self.price_tick,
            )
            request = replace(request, price=maker_price, order_type=OrderType.POST_ONLY)
        local_order = Order(request=request)
        self.repository.save_order(local_order)
        authorization = _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE,
            proposal_id=request.signal_id,
            request=request,
            instrument_type=InstrumentType.SPOT,
            trade_mode=TradeMode.CASH,
            private_state_version=private_state_version,
            environment=self.environment,
            max_notional=self.max_order_notional,
            allowed_instrument_id=self.allowed_instrument_id,
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

    def cancel_open_orders(
        self,
        open_orders: Sequence[Order],
        *,
        private_state_version: int = 1,
    ) -> list[Order]:
        cancelled: list[Order] = []
        for order in open_orders:
            if not order.is_open or order.state is OrderState.UNKNOWN:
                continue
            request = order.request
            if request.instrument_id != self.allowed_instrument_id:
                continue
            proposal_id = request.signal_id or f"cancel-{request.client_order_id}"
            auth_request = (
                request
                if request.signal_id
                else replace(
                    request,
                    signal_id=proposal_id,
                    mode="live" if self.environment is TradingEnvironment.LIVE else "demo",
                    order_type=OrderType.LIMIT,
                )
            )
            authorization = _issue_demo_write_authorization(
                operation=DemoWriteOperation.CANCEL,
                proposal_id=proposal_id,
                request=auth_request,
                instrument_type=InstrumentType.SPOT,
                trade_mode=TradeMode.CASH,
                private_state_version=private_state_version,
                environment=self.environment,
                max_notional=max(self.max_order_notional, auth_request.notional),
                allowed_instrument_id=self.allowed_instrument_id,
            )
            remote = self.client.cancel_order(
                request.instrument_id,
                request.client_order_id,
                authorization=authorization,
            )
            controlled = replace(remote, request=request)
            self.repository.save_order(controlled)
            cancelled.append(controlled)
        return cancelled

    def place_exchange_stop_loss(
        self,
        parent_order: Order,
        *,
        stop_price: Decimal,
        private_state_version: int = 1,
    ) -> AlgoStopOrder | None:
        place_fn = getattr(self.client, "place_algo_stop_order", None)
        if place_fn is None:
            return None
        req = parent_order.request
        mode_str = req.mode or ("live" if self.environment is TradingEnvironment.LIVE else "demo")
        client_algo_id = f"sl{req.client_order_id}"[:32]
        algo_req = AlgoStopOrderRequest(
            client_algo_id=client_algo_id,
            instrument_id=req.instrument_id,
            side=OrderSide.SELL,
            quantity=req.quantity,
            trigger_price=stop_price,
            signal_id=req.signal_id,
            created_at=req.created_at,
            mode=mode_str,
        )
        synthetic_req = OrderRequest(
            client_order_id=algo_req.client_algo_id,
            instrument_id=algo_req.instrument_id,
            side=algo_req.side,
            order_type=OrderType.LIMIT,
            quantity=algo_req.quantity,
            price=algo_req.trigger_price,
            signal_id=algo_req.signal_id,
            created_at=algo_req.created_at,
            mode=mode_str,
        )
        authorization = _issue_demo_write_authorization(
            operation=DemoWriteOperation.PLACE_ALGO,
            proposal_id=algo_req.signal_id,
            request=synthetic_req,
            instrument_type=InstrumentType.SPOT,
            trade_mode=TradeMode.CASH,
            private_state_version=private_state_version,
            environment=self.environment,
            max_notional=max(self.max_order_notional, synthetic_req.notional),
            allowed_instrument_id=self.allowed_instrument_id,
        )
        placed_algo: AlgoStopOrder = place_fn(algo_req, authorization=authorization)
        return placed_algo

    def cancel_algo_stop_orders(
        self,
        algo_orders: Sequence[AlgoStopOrder],
        *,
        private_state_version: int = 1,
    ) -> list[AlgoStopOrder]:
        cancel_fn = getattr(self.client, "cancel_algo_stop_order", None)
        if cancel_fn is None:
            return []
        cancelled: list[AlgoStopOrder] = []
        mode_str = "live" if self.environment is TradingEnvironment.LIVE else "demo"
        for algo in algo_orders:
            if algo.state != "live" or not algo.algo_id:
                continue
            req = algo.request
            synthetic_req = OrderRequest(
                client_order_id=req.client_algo_id,
                instrument_id=req.instrument_id,
                side=req.side,
                order_type=OrderType.LIMIT,
                quantity=req.quantity,
                price=req.trigger_price,
                signal_id=req.signal_id,
                created_at=req.created_at,
                mode=mode_str,
            )
            authorization = _issue_demo_write_authorization(
                operation=DemoWriteOperation.CANCEL_ALGO,
                proposal_id=req.signal_id,
                request=synthetic_req,
                instrument_type=InstrumentType.SPOT,
                trade_mode=TradeMode.CASH,
                private_state_version=private_state_version,
                environment=self.environment,
                max_notional=max(self.max_order_notional, synthetic_req.notional),
                allowed_instrument_id=self.allowed_instrument_id,
            )
            cancel_fn(
                req.instrument_id,
                algo.algo_id,
                req.client_algo_id,
                authorization=authorization,
            )
            algo.state = "cancelled"
            cancelled.append(algo)
        return cancelled

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
        enable_exchange_stop_loss: bool = True,
    ) -> None:
        self.service = service
        self._open_orders = list(open_orders)
        self._active_algo_orders: list[AlgoStopOrder] = []
        self.private_state_version = private_state_version
        self.enable_exchange_stop_loss = enable_exchange_stop_loss

    def submit_order(self, order: ApprovedOrder) -> Order:
        if order.proposed.side is OrderSide.SELL and self._active_algo_orders:
            self.cancel_active_algo_orders()
        placed = self.service.submit_approved_order(
            order,
            private_state_version=self.private_state_version,
        )
        if placed.is_open:
            self._open_orders.append(placed)
        if (
            self.enable_exchange_stop_loss
            and order.proposed.side is OrderSide.BUY
            and order.proposed.stop_loss_price is not None
            and placed.state
            in {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
        ):
            algo = self.service.place_exchange_stop_loss(
                placed,
                stop_price=order.proposed.stop_loss_price,
                private_state_version=self.private_state_version,
            )
            if algo is not None:
                self._active_algo_orders.append(algo)
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

    def cancel_pending_orders(self) -> list[Order]:
        cancelled = self.service.cancel_open_orders(
            self._open_orders,
            private_state_version=self.private_state_version,
        )
        cancelled_ids = {o.request.client_order_id for o in cancelled}
        self._open_orders = [
            o for o in self._open_orders if o.request.client_order_id not in cancelled_ids
        ]
        return cancelled

    def cancel_active_algo_orders(self) -> list[AlgoStopOrder]:
        cancelled = self.service.cancel_algo_stop_orders(
            self._active_algo_orders,
            private_state_version=self.private_state_version,
        )
        self._active_algo_orders = [a for a in self._active_algo_orders if a.state == "live"]
        return cancelled

    @property
    def active_algo_orders(self) -> tuple[AlgoStopOrder, ...]:
        return tuple(self._active_algo_orders)
