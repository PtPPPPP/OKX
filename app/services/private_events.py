from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.domain.account import PrivateAccountState
from app.domain.order import Order, OrderSide
from app.exchange.okx_models import parse_private_account_state
from app.market.private_websocket import PrivateEvent, PrivateEventKind


class PrivateEventRepository(Protocol):
    def claim_event(self, idempotency_key: str, event_type: str, payload_hash: str) -> bool: ...

    def save_order(self, order: Order) -> None: ...

    def load_order(self, client_order_id: str) -> Order | None: ...

    def save_fill(
        self,
        client_order_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        filled_at: datetime,
        *,
        fill_id: str | None = None,
        exchange_fill_id: str | None = None,
        fee_currency: str | None = None,
    ) -> bool: ...

    def apply_managed_fill(
        self,
        *,
        strategy_name: str,
        run_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
    ) -> None: ...

    def apply_private_state_event(
        self,
        idempotency_key: str,
        event_type: str,
        payload_hash: str,
        state: PrivateAccountState,
    ) -> bool: ...

    def save_system_event(self, event_type: str, message: str, details: dict[str, Any]) -> None: ...


class PrivateEventProcessor:
    def __init__(self, repository: PrivateEventRepository) -> None:
        self.repository = repository

    def process(self, event: PrivateEvent) -> bool:
        payload_hash = hashlib.sha256(
            json.dumps(
                event.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if event.kind is PrivateEventKind.ORDER and event.order is not None:
            local = self.repository.load_order(event.order.request.client_order_id)
            order = (
                replace(event.order, request=local.request) if local is not None else event.order
            )
            self.repository.save_order(order)
            self._save_incremental_fill(event, order)
            return self.repository.claim_event(
                event.idempotency_key, event.kind.value, payload_hash
            )
        state = parse_private_account_state(
            event.payload,
            event_kind=event.kind.value,
        )
        return self.repository.apply_private_state_event(
            event.idempotency_key,
            event.kind.value,
            payload_hash,
            state,
        )

    def _save_incremental_fill(self, event: PrivateEvent, order: Order) -> None:
        fill_size = Decimal(str(event.payload.get("fillSz") or "0"))
        trade_id = str(event.payload.get("tradeId") or "")
        if fill_size <= 0 or not trade_id:
            return
        fill_price = Decimal(
            str(event.payload.get("fillPx") or order.average_price or order.request.price)
        )
        fee = abs(Decimal(str(event.payload.get("fillFee") or event.payload.get("fee") or "0")))
        fee_currency = (
            str(event.payload.get("fillFeeCcy") or event.payload.get("feeCcy") or "") or None
        )
        timestamp_ms = int(event.payload.get("fillTime") or event.payload.get("uTime") or 0)
        filled_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        inserted = self.repository.save_fill(
            order.request.client_order_id,
            order.request.side,
            fill_size,
            fill_price,
            fee,
            filled_at,
            fill_id=f"okx:{trade_id}",
            exchange_fill_id=trade_id,
            fee_currency=fee_currency,
        )
        if (
            inserted
            and order.request.mode == "demo"
            and order.request.strategy_name
            and order.request.order_source.value not in {"administrative_cleanup", "reconciliation"}
        ):
            self.repository.apply_managed_fill(
                strategy_name=order.request.strategy_name,
                run_id=order.request.run_id,
                instrument_id=order.request.instrument_id,
                side=order.request.side,
                quantity=fill_size,
                price=fill_price,
                fee=fee,
            )
