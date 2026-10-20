from __future__ import annotations

import asyncio

from app.domain.order import Order, OrderState
from app.exchange.exceptions import ExchangeError, NetworkError, OrderRejected
from app.exchange.okx_client import OkxClient
from app.services.controlled_demo_write import ControlledDemoWriteService
from app.services.demo_order_preflight import ProposalStatus
from app.services.demo_order_revalidation import (
    DemoOrderProposalRevalidator,
    ProposalRevalidationResult,
)
from app.services.demo_session import DemoSubmissionGate
from app.storage.repositories import TradingRepository


class DemoProposalApplication:
    """Proposal workflows; the caller owns the client and started session.

    Exchange writes still require ControlledDemoWriteService. Reconciliation
    remains behind the session's coordinator; this service cannot merge state.
    """

    def __init__(
        self, repository: TradingRepository, client: OkxClient, session: DemoSubmissionGate
    ) -> None:
        self.repository = repository
        self.client = client
        self.session = session

    def revalidate(self, proposal_id: str) -> ProposalRevalidationResult:
        return asyncio.run(
            DemoOrderProposalRevalidator(
                self.repository,
                self.client,
                websocket_ready=self.session.order_submission_ready,
                reconcile=lambda _instrument: self.session.reconcile_result(),
            ).revalidate(proposal_id)
        )

    def submit(self, proposal_id: str) -> Order:
        proposal = self.repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            raise ValueError("proposal not found")
        check = self.revalidate(proposal_id)
        if not check.passed:
            raise ValueError(f"proposal revalidation failed: {check.reason}")
        proposal = self.repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            raise ValueError("proposal disappeared")
        local_order = self.repository.begin_controlled_demo_submission(proposal)
        try:
            remote_order = ControlledDemoWriteService(self.repository, self.client).place_order(
                local_order
            )
        except OrderRejected:
            local_order.transition(OrderState.REJECTED, at=self.client.clock.now())
            self.repository.complete_controlled_demo_submission(
                proposal_id,
                local_order,
                event_type="submission_failed",
                proposal_status=ProposalStatus.CONSUMED,
            )
            raise
        except NetworkError:
            local_order.transition(OrderState.UNKNOWN, at=self.client.clock.now())
            self.repository.complete_controlled_demo_submission(
                proposal_id,
                local_order,
                event_type="submission_unknown",
                proposal_status=ProposalStatus.UNKNOWN,
            )
            raise
        except ExchangeError as exc:
            if any(code in str(exc) for code in ("502", "503", "504")):
                self.repository.mark_controlled_demo_submission_unknown(
                    proposal_id, error_category="exchange_unavailable", http_status=503
                )
            else:
                # An ambiguous POST outcome must be resolved by read-only recovery.
                self.repository.mark_controlled_demo_submission_unknown(
                    proposal_id,
                    error_category=f"exchange_error_after_submit_attempt:{type(exc).__name__}",
                    http_status=None,
                )
            raise
        self.repository.complete_controlled_demo_submission(
            proposal_id,
            remote_order,
            event_type="submission_succeeded",
            proposal_status=ProposalStatus.SUBMITTED,
        )
        reconciliation = self.session.reconcile_result()
        if not reconciliation.order_submission_allowed:
            raise ValueError(f"订单提交后对账未通过: {reconciliation.message}")
        confirmed = self.repository.load_order(proposal.client_order_id)
        if confirmed is None:
            raise ValueError("订单提交后受控对账未获得本地状态")
        return confirmed
