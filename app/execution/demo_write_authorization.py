from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import StrEnum

from app.domain.environment import TradingEnvironment
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import OrderRequest, OrderType

CONTROLLED_INSTRUMENT_ID = "BTC-USDT"
MAXIMUM_DEMO_NOTIONAL = Decimal("5")

_ISSUER = object()


class DemoWriteOperation(StrEnum):
    PLACE = "place"
    CANCEL = "cancel"
    PLACE_ALGO = "place_algo"
    CANCEL_ALGO = "cancel_algo"


class DemoWriteAuthorization:
    """One-use capability passed explicitly across the final Demo write boundary."""

    __slots__ = (
        "_client_order_id",
        "_environment",
        "_instrument_id",
        "_operation",
        "_private_state_version",
        "_proposal_id",
        "_request_fingerprint",
        "_used",
    )

    def __init__(
        self,
        issuer: object,
        *,
        operation: DemoWriteOperation,
        proposal_id: str,
        request: OrderRequest,
        instrument_type: InstrumentType,
        trade_mode: TradeMode,
        private_state_version: int,
        environment: TradingEnvironment,
        max_notional: Decimal = MAXIMUM_DEMO_NOTIONAL,
        allowed_instrument_id: str = CONTROLLED_INSTRUMENT_ID,
    ) -> None:
        if issuer is not _ISSUER:
            raise PermissionError(
                "Demo write authorization can only be issued by the controlled gate"
            )
        _validate_scope(
            proposal_id=proposal_id,
            request=request,
            instrument_type=instrument_type,
            trade_mode=trade_mode,
            private_state_version=private_state_version,
            environment=environment,
            max_notional=max_notional,
            allowed_instrument_id=allowed_instrument_id,
        )
        self._operation = operation
        self._environment = environment
        self._proposal_id = proposal_id
        self._instrument_id = request.instrument_id
        self._client_order_id = request.client_order_id
        self._private_state_version = private_state_version
        self._request_fingerprint = _fingerprint(request)
        self._used = False

    @property
    def used(self) -> bool:
        return self._used

    def assert_place_matches(self, request: OrderRequest) -> None:
        if self._used:
            raise PermissionError("Demo write authorization has already been used")
        self._assert_matches(DemoWriteOperation.PLACE, request)

    def consume_place(self, request: OrderRequest) -> None:
        self._consume(DemoWriteOperation.PLACE, request)

    def consume_place_algo(self, request: OrderRequest) -> None:
        self._consume(DemoWriteOperation.PLACE_ALGO, request)

    def consume_cancel_algo(self, instrument_id: str, client_order_id: str) -> None:
        if self._used:
            raise PermissionError("Demo write authorization has already been used")
        self._used = True
        if self._operation is not DemoWriteOperation.CANCEL_ALGO:
            raise PermissionError("Demo write authorization operation mismatch")
        if instrument_id != self._instrument_id or client_order_id != self._client_order_id:
            raise PermissionError("Algo cancellation authorization target mismatch")

    def assert_cancel_matches(self, instrument_id: str, client_order_id: str) -> None:
        if self._used:
            raise PermissionError("Demo write authorization has already been used")
        self._assert_cancel_matches(instrument_id, client_order_id)

    def assert_consumed_for(self, operation: DemoWriteOperation) -> None:
        if not self._used or self._operation is not operation:
            raise PermissionError("Demo write authorization is not consumed for this operation")

    def assert_environment_bound(self, environment: TradingEnvironment) -> None:
        if (
            self._environment not in {TradingEnvironment.DEMO, TradingEnvironment.LIVE}
            or environment is not self._environment
        ):
            raise PermissionError("Demo write authorization environment mismatch")

    def consume_cancel(self, instrument_id: str, client_order_id: str) -> None:
        if self._used:
            raise PermissionError("Demo write authorization has already been used")
        self._used = True
        self._assert_cancel_matches(instrument_id, client_order_id)

    def _assert_cancel_matches(self, instrument_id: str, client_order_id: str) -> None:
        if self._operation is not DemoWriteOperation.CANCEL:
            raise PermissionError("Demo write authorization operation mismatch")
        if instrument_id != self._instrument_id or client_order_id != self._client_order_id:
            raise PermissionError("Demo cancellation authorization target mismatch")

    def _consume(self, operation: DemoWriteOperation, request: OrderRequest) -> None:
        if self._used:
            raise PermissionError("Demo write authorization has already been used")
        self._used = True
        self._assert_matches(operation, request)

    def _assert_matches(self, operation: DemoWriteOperation, request: OrderRequest) -> None:
        if self._operation is not operation:
            raise PermissionError("Demo write authorization operation mismatch")
        if request.signal_id != self._proposal_id:
            raise PermissionError("Demo write authorization proposal mismatch")
        if _fingerprint(request) != self._request_fingerprint:
            raise PermissionError("Demo write authorization request mismatch")


def require_place_authorization(
    authorization: DemoWriteAuthorization | None, request: OrderRequest
) -> None:
    if authorization is None:
        raise PermissionError("Demo order submission requires explicit one-use authorization")
    authorization.consume_place(request)


def require_cancel_authorization(
    authorization: DemoWriteAuthorization | None,
    instrument_id: str,
    client_order_id: str,
) -> None:
    if authorization is None:
        raise PermissionError("Demo order cancellation requires explicit one-use authorization")
    authorization.consume_cancel(instrument_id, client_order_id)


def _issue_demo_write_authorization(
    *,
    operation: DemoWriteOperation,
    proposal_id: str,
    request: OrderRequest,
    instrument_type: InstrumentType,
    trade_mode: TradeMode,
    private_state_version: int,
    environment: TradingEnvironment,
    max_notional: Decimal = MAXIMUM_DEMO_NOTIONAL,
    allowed_instrument_id: str = CONTROLLED_INSTRUMENT_ID,
) -> DemoWriteAuthorization:
    return DemoWriteAuthorization(
        _ISSUER,
        operation=operation,
        proposal_id=proposal_id,
        request=request,
        instrument_type=instrument_type,
        trade_mode=trade_mode,
        private_state_version=private_state_version,
        environment=environment,
        max_notional=max_notional,
        allowed_instrument_id=allowed_instrument_id,
    )


def _validate_scope(
    *,
    proposal_id: str,
    request: OrderRequest,
    instrument_type: InstrumentType,
    trade_mode: TradeMode,
    private_state_version: int,
    environment: TradingEnvironment,
    max_notional: Decimal = MAXIMUM_DEMO_NOTIONAL,
    allowed_instrument_id: str = CONTROLLED_INSTRUMENT_ID,
) -> None:
    if not proposal_id or request.signal_id != proposal_id:
        raise PermissionError("Demo write authorization requires a matching Proposal")
    if environment not in {TradingEnvironment.DEMO, TradingEnvironment.LIVE}:
        raise PermissionError("Demo write authorization requires the Demo or Live environment")
    if request.instrument_id != allowed_instrument_id:
        raise PermissionError(f"Writes are restricted to {allowed_instrument_id}")
    if instrument_type is not InstrumentType.SPOT:
        raise PermissionError("Demo writes are restricted to SPOT")
    if trade_mode is not TradeMode.CASH:
        raise PermissionError("Demo writes are restricted to cash mode")
    if request.order_type is not OrderType.LIMIT:
        raise PermissionError("Demo writes are restricted to LIMIT orders")
    expected_mode = "live" if environment is TradingEnvironment.LIVE else "demo"
    if request.mode != expected_mode:
        raise PermissionError(f"Write authorization requires mode={expected_mode}")
    if (
        not request.quantity.is_finite()
        or not request.price.is_finite()
        or request.quantity <= 0
        or request.price <= 0
        or not max_notional.is_finite()
        or max_notional <= 0
        or request.notional > max_notional
    ):
        raise PermissionError(f"Write exceeds the positive finite {max_notional} USDT budget")
    if private_state_version < 0:
        raise PermissionError("Demo write authorization requires a private-state fence")


def _fingerprint(request: OrderRequest) -> str:
    payload = "\x1f".join(
        (
            request.client_order_id,
            request.instrument_id,
            request.side.value,
            request.order_type.value,
            str(request.quantity),
            str(request.price),
            request.signal_id,
            request.run_id,
            request.strategy_name,
            request.mode,
            request.bar,
            request.order_source.value,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
