from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from app.exchange.exceptions import ExchangeError
from app.exchange.okx_client import OkxClient
from app.exchange.recovery_models import (
    ExchangeOrder,
    RecoveryQueryEvidence,
    SubmissionRecoveryState,
    UnknownOrderRecoveryResult,
)
from app.services.demo_order_preflight import ProposalStatus
from app.services.recovery_evidence import (
    RecoveryEvidenceCompletenessEvaluator,
    official_recovery_capabilities,
)
from app.storage.repositories import TradingRepository


@dataclass(slots=True)
class UnknownOrderRecoveryService:
    repository: TradingRepository
    client: OkxClient

    def recover(self, proposal_id: str) -> UnknownOrderRecoveryResult:
        proposal = self.repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            raise ValueError("proposal not found")
        if proposal.status is ProposalStatus.SUBMISSION_IN_PROGRESS:
            self.repository.mark_controlled_demo_submission_unknown(
                proposal_id,
                error_category="operator_read_only_recovery_started",
                http_status=None,
            )
            proposal = self.repository.load_demo_order_proposal(proposal_id)
            if proposal is None:
                raise RuntimeError("proposal disappeared during recovery fencing")
        order = self.repository.load_order(proposal.client_order_id)
        if (
            order is None
            or proposal.status is not ProposalStatus.UNKNOWN
            or not proposal.submission_performed
        ):
            raise ValueError("proposal is not an unknown submitted order")
        started = (
            self.repository.load_submission_started_at(proposal_id) or order.request.created_at
        )
        begin, end = started - timedelta(minutes=15), started + timedelta(minutes=60)
        queries: list[RecoveryQueryEvidence] = [
            RecoveryQueryEvidence(
                "/api/v5/trade/fills-history-archive",
                begin,
                end,
                0,
                0,
                True,
                error_classification="unsupported_endpoint_contract",
                contract_status="not_officially_documented",
                applicability_status="not_applicable",
                blocking=False,
                superseded_by="/api/v5/trade/fills-history",
            )
        ]
        candidates: list[ExchangeOrder] = []
        warnings = ["original client order id preserved; no write endpoint called"]
        calls: tuple[Callable[[], Any], ...] = (
            lambda: self.client.get_recovery_orders_pending(proposal.instrument_id, begin, end),
            lambda: self.client.get_recovery_orders(proposal.instrument_id, begin, end),
            lambda: self.client.get_recovery_orders(
                proposal.instrument_id, begin, end, archive=True
            ),
            lambda: self.client.get_recovery_fills(proposal.instrument_id, begin, end),
            lambda: self.client.get_recovery_bills(proposal.instrument_id, begin, end),
        )
        for call in calls:
            try:
                records, evidence = cast(tuple[list[object], RecoveryQueryEvidence], call())
                queries.append(evidence)
                candidates.extend(item for item in records if isinstance(item, ExchangeOrder))
            except (ExchangeError, OSError, TimeoutError, ValueError) as exc:
                endpoint = self._endpoint_for_failure(call)
                queries.append(
                    RecoveryQueryEvidence(
                        endpoint,
                        begin,
                        end,
                        0,
                        0,
                        False,
                        error_classification=type(exc).__name__,
                        error_message=str(exc)[:300],
                    )
                )
        account_state_complete = self._query_account_state(proposal.instrument_id, warnings)
        completeness = RecoveryEvidenceCompletenessEvaluator().evaluate(
            required_begin=begin,
            required_end=end,
            endpoint_capabilities=official_recovery_capabilities(self.client.clock.now(), begin),
            query_evidence=queries,
            account_state_complete=account_state_complete,
        )
        matches = [
            item
            for item in candidates
            if item.client_order_id == proposal.client_order_id
            and item.instrument_id == proposal.instrument_id
        ]
        blockers = list(completeness.blockers)
        matching_exchange_ids = {
            item.exchange_order_id for item in matches if item.exchange_order_id
        }
        if len(matching_exchange_ids) == 1 and all(
            item.exchange_order_id is not None for item in matches
        ):
            candidate_exchange_id = next(iter(matching_exchange_ids))
            try:
                authoritative = self.client.query_order(
                    proposal.instrument_id,
                    proposal.client_order_id,
                )
                if authoritative.exchange_order_id != candidate_exchange_id:
                    raise ValueError("authoritative order conflicts with recovery evidence")
                self.repository.resolve_unknown_order_from_authoritative_read(
                    proposal_id,
                    authoritative,
                )
            except ValueError as exc:
                status, confidence, exchange_id = (
                    SubmissionRecoveryState.TERMINAL_FAILURE.value,
                    "authoritative_evidence_conflict",
                    None,
                )
                blockers.append(f"authoritative_order_query_failed:{type(exc).__name__}")
            except (ExchangeError, OSError, TimeoutError) as exc:
                status, confidence, exchange_id = (
                    SubmissionRecoveryState.RECOVERABLE_FAILURE.value,
                    "authoritative_read_failed",
                    None,
                )
                blockers.append(f"authoritative_order_query_failed:{type(exc).__name__}")
            else:
                status, confidence, exchange_id = (
                    SubmissionRecoveryState.CONFIRMED_SUBMITTED.value,
                    "authoritative_identifier",
                    candidate_exchange_id,
                )
        elif matches:
            status, confidence, exchange_id = (
                SubmissionRecoveryState.UNKNOWN_SUBMISSION_STATE.value,
                "ambiguous_matching_identifiers",
                None,
            )
            blockers.append("matching_order_identifier_ambiguous")
        elif candidates:
            status, confidence, exchange_id = (
                SubmissionRecoveryState.UNKNOWN_SUBMISSION_STATE.value,
                "candidate_or_contradictory_evidence",
                None,
            )
            blockers.append("candidate_order_present")
        elif completeness.overall_complete:
            status, confidence, exchange_id = (
                SubmissionRecoveryState.CONFIRMED_NOT_SUBMITTED.value,
                "high_with_order_retention_limitation",
                None,
            )
            warnings.append(
                "operational conclusion; invalid legacy clOrdId prevents direct order-detail proof"
            )
        else:
            status, confidence, exchange_id = (
                SubmissionRecoveryState.RECOVERABLE_FAILURE.value,
                "insufficient_coverage",
                None,
            )
        result = UnknownOrderRecoveryResult(
            uuid4().hex,
            proposal_id,
            proposal.client_order_id,
            proposal.client_order_id,
            status,
            confidence,
            exchange_id,
            tuple(queries),
            tuple(sorted(set(blockers))),
            tuple(warnings),
            self.client.clock.now(),
            completeness,
        )
        self.repository.save_unknown_recovery(result)
        return result

    def _query_account_state(self, instrument_id: str, warnings: list[str]) -> bool:
        try:
            instrument = self.client.get_instrument(instrument_id)
            configuration = self.client.get_account_configuration()
            portfolio = self.client.get_portfolio(instrument, configuration=configuration)
            positions = self.client.get_derivative_positions()
            maximum = self.client.get_max_available_size(instrument_id)
            if (
                portfolio.available_balance(instrument.base_currency) != 0
                or (maximum.max_sell or Decimal("0")) != 0
                or any(value != 0 for value in positions.values())
            ):
                warnings.append(
                    "account state contains exposure; no not-created conclusion is inferred from balances"
                )
                return False
            return True
        except (ExchangeError, OSError, TimeoutError, ValueError) as exc:
            warnings.append(f"account_state_query_failed:{type(exc).__name__}")
            return False

    @staticmethod
    def _endpoint_for_failure(call: Callable[[], object]) -> str:
        # The callable identity is not introspectable; failed endpoint is still captured safely.
        return "recovery_read_only_query"
