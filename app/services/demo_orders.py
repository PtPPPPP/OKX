from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.config.run_config import RunConfig
from app.config.settings import TradingMode
from app.domain.market import Instrument, InstrumentType
from app.domain.order import Order, OrderSide
from app.services.controlled_demo_write import (
    ControlledDemoWriteClient,
    ControlledDemoWriteService,
)
from app.services.demo_session import DemoSubmissionGate
from app.services.reconciliation import ReconciliationStatus
from app.storage.repositories import TradingRepository


@dataclass(frozen=True, slots=True)
class DemoOrderResult:
    order: Order
    pre_reconciliation: ReconciliationStatus
    post_reconciliation: ReconciliationStatus


class DemoOrderClient(ControlledDemoWriteClient, Protocol):
    def get_instrument(self, instrument_id: str) -> Instrument: ...


class DemoOrderService:
    maximum_controlled_notional = Decimal("5")

    def __init__(
        self,
        config: RunConfig,
        client: DemoOrderClient,
        repository: TradingRepository,
        submission_gate: DemoSubmissionGate | None = None,
    ) -> None:
        if config.mode is not TradingMode.DEMO or not config.exchange.simulated:
            raise ValueError("模拟盘订单服务只允许 mode=demo 且 simulated=true")
        if config.market.instrument_id != "BTC-USDT":
            raise ValueError("模拟盘订单服务只允许 BTC-USDT")
        self.config = config
        self.client = client
        self.repository = repository
        self.submission_gate = submission_gate

    def submit(self, *, side: OrderSide, price: Decimal) -> DemoOrderResult:
        raise PermissionError(
            "one-step Demo submission is retired; use a persisted Proposal and submit-demo-order"
        )

    def query(self, client_order_id: str) -> Order:
        if self.submission_gate is None or not self.submission_gate.order_submission_ready:
            raise ValueError("私有 WebSocket 受控会话未就绪，禁止查询订单状态")
        result = self.submission_gate.reconcile_result()
        if not result.order_submission_allowed:
            raise ValueError(f"订单查询前对账未通过: {result.message}")
        order = self.repository.load_order(client_order_id)
        if order is None:
            raise ValueError("本地不存在可由受控对账恢复的订单")
        return order

    def cancel(self, client_order_id: str) -> Order:
        if self.submission_gate is None or not self.submission_gate.order_submission_ready:
            raise ValueError("私有 WebSocket 受控会话未就绪，禁止撤单")
        instrument = self.client.get_instrument(self.config.market.instrument_id)
        if (
            instrument.instrument_id != "BTC-USDT"
            or instrument.instrument_type is not InstrumentType.SPOT
        ):
            raise PermissionError("Demo cancellation is restricted to BTC-USDT SPOT")
        before = self.submission_gate.reconcile_result()
        if not before.order_submission_allowed:
            raise ValueError(f"撤单前对账未通过: {before.message}")
        order = ControlledDemoWriteService(self.repository, self.client).cancel_order(
            client_order_id
        )
        after = self.submission_gate.reconcile_result()
        if not after.order_submission_allowed or after.status is ReconciliationStatus.UNKNOWN:
            raise ValueError("撤单后状态无法确认")
        return order
