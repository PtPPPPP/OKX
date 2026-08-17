from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.config.settings import TradingMode
from app.domain.capability import MaxAvailableSize
from app.domain.market import Instrument, InstrumentType, TradeMode
from app.domain.order import OrderSide, OrderType
from app.services.demo_order_preflight import (
    CONTROLLED_INSTRUMENT_ID,
    MAXIMUM_CONTROLLED_NOTIONAL,
    DemoOrderPreflightService,
    ProposalStatus,
)
from app.services.reconciliation import (
    ReconciliationClient,
    ReconciliationResult,
)
from app.services.spot_cash_capability import SpotCashCapabilityEvaluator
from app.storage.repositories import PrivateStateFenceDeferred, TradingRepository


class RevalidationClient(ReconciliationClient, Protocol):
    def get_instrument(self, instrument_id: str) -> Instrument: ...
    def get_max_available_size(self, instrument_id: str) -> MaxAvailableSize: ...
    def get_last_price(self, instrument_id: str) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class ProposalRevalidationResult:
    proposal_id: str
    passed: bool
    status: ProposalStatus
    reason: str


class DemoOrderProposalRevalidator:
    """Fresh, fail-closed checks immediately before the confirmation boundary."""

    def __init__(
        self,
        repository: TradingRepository,
        client: RevalidationClient,
        *,
        websocket_ready: bool | Callable[[], bool],
        reconcile: Callable[[Instrument], ReconciliationResult] | None = None,
    ) -> None:
        self.repository = repository
        self.client = client
        self.websocket_ready = websocket_ready
        self.reconcile = reconcile

    def _websocket_ready(self) -> bool:
        return self.websocket_ready() if callable(self.websocket_ready) else self.websocket_ready

    async def revalidate(self, proposal_id: str) -> ProposalRevalidationResult:
        proposal = self.repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            raise ValueError("proposal not found")
        now = datetime.now(UTC)
        if proposal.status is not ProposalStatus.READY_FOR_CONFIRMATION:
            return ProposalRevalidationResult(
                proposal_id, False, proposal.status, "proposal_not_ready"
            )
        if not DemoOrderPreflightService.validate_hash(proposal):
            self.repository.transition_demo_order_proposal(
                proposal_id,
                expected=proposal.status,
                new=ProposalStatus.INVALIDATED,
                event_type="hash_validation_failed",
                reason="proposal_hash_invalid",
            )
            return ProposalRevalidationResult(
                proposal_id, False, ProposalStatus.INVALIDATED, "proposal_hash_invalid"
            )
        if now >= proposal.expires_at:
            self.repository.transition_demo_order_proposal(
                proposal_id,
                expected=proposal.status,
                new=ProposalStatus.EXPIRED,
                event_type="expired",
                reason="proposal_expired",
            )
            return ProposalRevalidationResult(
                proposal_id, False, ProposalStatus.EXPIRED, "proposal_expired"
            )
        # The event is deliberately written before live reads; no check is silently skipped.
        self.repository.save_revalidation_event(
            proposal_id, "revalidation_started", "fresh_state_checks"
        )
        try:
            instrument = self.client.get_instrument(proposal.instrument_id)
            portfolio = self.client.get_portfolio(instrument)
            max_size = self.client.get_max_available_size(proposal.instrument_id)
            capability = SpotCashCapabilityEvaluator().evaluate(
                mode=TradingMode.DEMO,
                instrument=instrument,
                portfolio=portfolio,
                max_size=max_size,
                derivative_positions=self.client.get_derivative_positions(),
                open_order_count=len(self.client.get_pending_orders(proposal.instrument_id)),
                checked_at=now,
            )
            if self.reconcile is None:
                raise RuntimeError("缺少私有状态协调器对账入口")
            reconciliation = self.reconcile(instrument)
            current_price = self.client.get_last_price(proposal.instrument_id)
            private_state = self.repository.private_state_snapshot()
            available_limit = (
                max_size.max_buy if proposal.side is OrderSide.BUY else max_size.max_sell
            )
            checks = {
                "websocket_ready": self._websocket_ready(),
                "instrument_id": proposal.instrument_id == CONTROLLED_INSTRUMENT_ID
                and instrument.instrument_id == CONTROLLED_INSTRUMENT_ID
                and max_size.instrument_id == CONTROLLED_INSTRUMENT_ID,
                "instrument_type": proposal.instrument_type is InstrumentType.SPOT
                and instrument.instrument_type is InstrumentType.SPOT,
                "trade_mode": proposal.trade_mode is TradeMode.CASH
                and max_size.trade_mode is TradeMode.CASH,
                "order_type": proposal.order_type is OrderType.LIMIT,
                "instrument_tradable": instrument.tradable,
                "capability": capability.eligible_for_controlled_order_test,
                "reconciliation": reconciliation.order_submission_allowed,
                "private_state_reconciled": private_state.submission_allowed,
                "available_size": available_limit is not None
                and proposal.approved_notional <= available_limit,
                "market_price": current_price > 0,
                "notional_limit": Decimal("0")
                < proposal.quantity * proposal.planned_limit_price
                == proposal.approved_notional
                <= MAXIMUM_CONTROLLED_NOTIONAL
                and Decimal("0") < proposal.requested_notional <= MAXIMUM_CONTROLLED_NOTIONAL,
            }
            valid = all(checks.values())
        except Exception as exc:
            valid, reason = False, f"revalidation_error:{type(exc).__name__}"
        else:
            reason = (
                "passed"
                if valid
                else "fresh_state_changed:"
                + ",".join(key for key, passed in checks.items() if not passed)
            )
        if not valid:
            self.repository.transition_demo_order_proposal(
                proposal_id,
                expected=proposal.status,
                new=ProposalStatus.INVALIDATED,
                event_type="revalidation_failed",
                reason=reason,
            )
            return ProposalRevalidationResult(
                proposal_id, False, ProposalStatus.INVALIDATED, reason
            )
        try:
            snapshot = self.repository.fence_demo_order_proposal(proposal_id)
        except PrivateStateFenceDeferred as exc:
            reason = str(exc)
            self.repository.save_revalidation_event(proposal_id, "revalidation_deferred", reason)
            return ProposalRevalidationResult(proposal_id, False, proposal.status, reason)
        self.repository.save_revalidation_event(proposal_id, "revalidation_passed", reason)
        return ProposalRevalidationResult(
            proposal_id,
            True,
            proposal.status,
            f"passed:private_state_version={snapshot.version}",
        )
