from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.account import PrivateAccountState
from app.domain.market import Instrument
from app.domain.order import (
    Order,
    OrderSide,
)
from app.domain.position import (
    PortfolioSnapshot,
)
from app.domain.private_state import PrivateStateSnapshot
from app.domain.risk import RiskDecision
from app.domain.signal import Signal
from app.portfolio.cost_basis import CostFill
from app.services.demo_order_preflight import DemoOrderProposal, ProposalStatus
from app.storage.accounts import AccountRepository
from app.storage.audit import AuditRepository
from app.storage.database import Database
from app.storage.orders import OrderRepository
from app.storage.orders import canonical_fill_id as canonical_fill_id
from app.storage.private_state import PrivateStateRepository
from app.storage.proposals import PrivateStateFenceDeferred as PrivateStateFenceDeferred
from app.storage.proposals import ProposalRepository


class TradingRepository:
    """Compatibility facade; transaction-owning methods delegate once, never commit here.

    Stores share Database, not an implicit transaction. Cross-table atomic writes
    remain inside the responsible store on one connection.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.orders = OrderRepository(database)
        self.accounts = AccountRepository(database)
        self.private_state = PrivateStateRepository(database)
        self.audit = AuditRepository(database)
        self.proposals = ProposalRepository(database)

    def save_demo_order_proposal(self, proposal: DemoOrderProposal) -> None:
        return self.proposals.save_demo_order_proposal(proposal)

    def load_demo_order_proposal(self, proposal_id: str) -> DemoOrderProposal | None:
        return self.proposals.load_demo_order_proposal(proposal_id)

    def load_submission_started_at(self, proposal_id: str) -> datetime | None:
        return self.proposals.load_submission_started_at(proposal_id)

    def count_controlled_demo_submissions(self) -> int:
        return self.proposals.count_controlled_demo_submissions()

    def close_unknown_order_operationally_not_created(
        self, proposal_id: str, *, reason: str
    ) -> None:
        return self.proposals.close_unknown_order_operationally_not_created(
            proposal_id, reason=reason
        )

    def resolve_unknown_order_from_authoritative_read(
        self,
        proposal_id: str,
        authoritative: Order,
    ) -> None:
        return self.proposals.resolve_unknown_order_from_authoritative_read(
            proposal_id, authoritative
        )

    def save_unknown_recovery(self, result: Any) -> None:
        return self.proposals.save_unknown_recovery(result)

    def load_unknown_recoveries(self, proposal_id: str) -> list[dict[str, Any]]:
        return self.proposals.load_unknown_recoveries(proposal_id)

    def initialize_inventory_scope(
        self,
        *,
        strategy_name: str,
        run_id: str,
        instrument_id: str,
        scope: str,
        quantity: Decimal,
        average_cost: Decimal | None,
    ) -> None:
        return self.accounts.initialize_inventory_scope(
            strategy_name=strategy_name,
            run_id=run_id,
            instrument_id=instrument_id,
            scope=scope,
            quantity=quantity,
            average_cost=average_cost,
        )

    def managed_strategy_quantity(
        self, strategy_name: str, run_id: str, instrument_id: str
    ) -> Decimal:
        return self.accounts.managed_strategy_quantity(strategy_name, run_id, instrument_id)

    def managed_strategy_position(
        self, strategy_name: str, run_id: str, instrument_id: str
    ) -> tuple[Decimal, Decimal | None]:
        return self.accounts.managed_strategy_position(strategy_name, run_id, instrument_id)

    def managed_strategy_quantity_for_generation(
        self, strategy_name: str, run_id: str, instrument_id: str, generation_id: str
    ) -> Decimal:
        return self.accounts.managed_strategy_quantity_for_generation(
            strategy_name, run_id, instrument_id, generation_id
        )

    def managed_strategy_position_for_generation(
        self, strategy_name: str, run_id: str, instrument_id: str, generation_id: str
    ) -> tuple[Decimal, Decimal | None]:
        return self.accounts.managed_strategy_position_for_generation(
            strategy_name, run_id, instrument_id, generation_id
        )

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
    ) -> None:
        return self.accounts.apply_managed_fill(
            strategy_name=strategy_name,
            run_id=run_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
        )

    def transition_demo_order_proposal(
        self,
        proposal_id: str,
        *,
        expected: ProposalStatus,
        new: ProposalStatus,
        event_type: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        return self.proposals.transition_demo_order_proposal(
            proposal_id,
            expected=expected,
            new=new,
            event_type=event_type,
            reason=reason,
            details=details,
        )

    def save_revalidation_event(self, proposal_id: str, event_type: str, reason: str) -> None:
        return self.proposals.save_revalidation_event(proposal_id, event_type, reason)

    def defer_fenced_demo_order_proposal(self, proposal_id: str, reason: str) -> None:
        return self.proposals.defer_fenced_demo_order_proposal(proposal_id, reason)

    def begin_controlled_demo_submission(
        self, proposal: DemoOrderProposal, *, maximum_submissions: int = 1
    ) -> Order:
        return self.proposals.begin_controlled_demo_submission(
            proposal, maximum_submissions=maximum_submissions
        )

    def authorize_controlled_demo_write(
        self,
        proposal_id: str,
        client_order_id: str,
        *,
        operation: str,
    ) -> int:
        return self.proposals.authorize_controlled_demo_write(
            proposal_id, client_order_id, operation=operation
        )

    def complete_controlled_demo_submission(
        self, proposal_id: str, order: Order, *, event_type: str, proposal_status: ProposalStatus
    ) -> None:
        return self.proposals.complete_controlled_demo_submission(
            proposal_id, order, event_type=event_type, proposal_status=proposal_status
        )

    def mark_controlled_demo_submission_unknown(
        self, proposal_id: str, *, error_category: str, http_status: int | None
    ) -> None:
        return self.proposals.mark_controlled_demo_submission_unknown(
            proposal_id, error_category=error_category, http_status=http_status
        )

    @staticmethod
    def _save_proposal_event(
        connection: Any,
        proposal_id: str,
        event_type: str,
        previous: str | None,
        new: str | None,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        return ProposalRepository._save_proposal_event(
            connection, proposal_id, event_type, previous, new, reason, details
        )

    def save_signal(self, signal: Signal) -> None:
        return self.audit.save_signal(signal)

    def save_audit_record(
        self,
        *,
        record_type: str,
        run_id: str,
        mode: str,
        strategy_name: str,
        instrument_id: str,
        bar: str,
        payload: dict[str, Any],
    ) -> None:
        return self.audit.save_audit_record(
            record_type=record_type,
            run_id=run_id,
            mode=mode,
            strategy_name=strategy_name,
            instrument_id=instrument_id,
            bar=bar,
            payload=payload,
        )

    def save_audit_records(self, records: list[dict[str, Any]]) -> None:
        return self.audit.save_audit_records(records)

    def save_candle_metadata(
        self,
        instrument_id: str,
        bar: str,
        first_timestamp: datetime,
        last_timestamp: datetime,
        row_count: int,
        source: str,
    ) -> None:
        return self.audit.save_candle_metadata(
            instrument_id, bar, first_timestamp, last_timestamp, row_count, source
        )

    def save_risk_decision(self, signal_id: str, decision: RiskDecision) -> None:
        return self.audit.save_risk_decision(signal_id, decision)

    def save_order(self, order: Order) -> None:
        return self.orders.save_order(order)

    def load_order(self, client_order_id: str) -> Order | None:
        return self.orders.load_order(client_order_id)

    def load_open_orders(self) -> list[Order]:
        return self.orders.load_open_orders()

    def load_recent_order_times(self, since: datetime) -> tuple[datetime, ...]:
        return self.orders.load_recent_order_times(since)

    def load_cost_fills(self, instrument_id: str) -> list[CostFill]:
        return self.orders.load_cost_fills(instrument_id)

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
        run_id: str | None = None,
        mode: str | None = None,
    ) -> bool:
        return self.orders.save_fill(
            client_order_id,
            side,
            quantity,
            price,
            fee,
            filled_at,
            fill_id=fill_id,
            exchange_fill_id=exchange_fill_id,
            fee_currency=fee_currency,
            run_id=run_id,
            mode=mode,
        )

    def save_portfolio_snapshot(
        self,
        portfolio: PortfolioSnapshot,
        instrument: Instrument,
        mark_price: Decimal,
        captured_at: datetime,
        *,
        run_id: str,
        mode: str,
        strategy_name: str,
        bar: str,
    ) -> None:
        return self.accounts.save_portfolio_snapshot(
            portfolio,
            instrument,
            mark_price,
            captured_at,
            run_id=run_id,
            mode=mode,
            strategy_name=strategy_name,
            bar=bar,
        )

    def load_latest_portfolio_snapshot(
        self, instrument: Instrument
    ) -> tuple[PortfolioSnapshot, Decimal, datetime] | None:
        return self.accounts.load_latest_portfolio_snapshot(instrument)

    def daily_risk_metrics(
        self, instrument_id: str, now: datetime, current_equity: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        return self.accounts.daily_risk_metrics(instrument_id, now, current_equity)

    def save_runtime_mode(self, mode: str) -> None:
        return self.audit.save_runtime_mode(mode)

    def load_runtime_mode(self) -> str | None:
        return self.audit.load_runtime_mode()

    def save_backtest_run(self, run_id: str, summary: dict[str, Any]) -> None:
        return self.audit.save_backtest_run(run_id, summary)

    def save_run_manifest(self, manifest: dict[str, Any], *, status: str) -> None:
        return self.audit.save_run_manifest(manifest, status=status)

    def save_instrument_snapshot(self, snapshot: dict[str, Any]) -> None:
        return self.audit.save_instrument_snapshot(snapshot)

    def save_dataset_snapshot(self, manifest: dict[str, Any]) -> None:
        return self.audit.save_dataset_snapshot(manifest)

    def save_system_event(self, event_type: str, message: str, details: dict[str, Any]) -> None:
        return self.audit.save_system_event(event_type, message, details)

    def claim_event(self, idempotency_key: str, event_type: str, payload_hash: str) -> bool:
        return self.audit.claim_event(idempotency_key, event_type, payload_hash)

    def apply_private_state_event(
        self,
        idempotency_key: str,
        event_type: str,
        payload_hash: str,
        state: PrivateAccountState,
    ) -> bool:
        return self.private_state.apply_private_state_event(
            idempotency_key, event_type, payload_hash, state
        )

    def record_private_ws_watermark(
        self,
        *,
        connection_epoch: int,
        watermark: int,
        event_kind: str,
        event_at: datetime,
    ) -> None:
        return self.private_state.record_private_ws_watermark(
            connection_epoch=connection_epoch,
            watermark=watermark,
            event_kind=event_kind,
            event_at=event_at,
        )

    def begin_private_connection_epoch(self, connection_epoch: int) -> None:
        return self.private_state.begin_private_connection_epoch(connection_epoch)

    def begin_private_reconciliation(self, reconciliation_id: str) -> None:
        return self.private_state.begin_private_reconciliation(reconciliation_id)

    def freeze_private_state(self, reason: str) -> None:
        return self.private_state.freeze_private_state(reason)

    def confirm_private_state_snapshots(self, confirmed_at: datetime) -> int:
        return self.private_state.confirm_private_state_snapshots(confirmed_at)

    def has_unreconciled_private_state(self) -> bool:
        return self.private_state.has_unreconciled_private_state()

    def private_state_snapshot(self) -> PrivateStateSnapshot:
        return self.private_state.private_state_snapshot()

    def fence_demo_order_proposal(self, proposal_id: str) -> PrivateStateSnapshot:
        return self.proposals.fence_demo_order_proposal(proposal_id)

    @staticmethod
    def _private_snapshot_from_row(row: Any) -> PrivateStateSnapshot:
        return ProposalRepository._private_snapshot_from_row(row)

    def has_nonzero_private_derivative_position(self) -> bool:
        return self.private_state.has_nonzero_private_derivative_position()

    @staticmethod
    def _row_to_order(row: Any) -> Order:
        return OrderRepository._row_to_order(row)
