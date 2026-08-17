from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from app.config.run_config import RunConfig
from app.domain.capability import MaxAvailableSize
from app.domain.market import Instrument, InstrumentType, TradeMode
from app.domain.order import OrderSide, OrderType
from app.domain.position import PortfolioSnapshot
from app.domain.private_state import ProposalStateToken
from app.exchange.recovery_models import ClientOrderId
from app.services.spot_cash_capability import SpotCashCapabilityEvaluator

CONTROLLED_INSTRUMENT_ID = "BTC-USDT"
MAXIMUM_CONTROLLED_NOTIONAL = Decimal("5")


class ProposalStatus(StrEnum):
    PREPARED = "prepared"
    BLOCKED = "blocked"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    READY_FOR_CONFIRMATION = "ready_for_confirmation"
    SUBMISSION_IN_PROGRESS = "submission_in_progress"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    OPERATIONALLY_NOT_CREATED = "operationally_not_created"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class DemoOrderIntent:
    run_id: str
    strategy_name: str
    instrument_id: str
    instrument_type: InstrumentType
    trade_mode: TradeMode
    side: OrderSide
    order_type: OrderType
    requested_notional: Decimal
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DemoOrderProposal:
    proposal_id: str
    proposal_version: int
    run_id: str
    instrument_id: str
    trade_mode: TradeMode
    side: OrderSide
    planned_limit_price: Decimal
    quantity: Decimal
    approved_notional: Decimal
    estimated_fee: Decimal
    created_at: datetime
    expires_at: datetime
    status: ProposalStatus
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    submission_performed: bool
    proposal_hash: str
    source: str = "manual_demo_test"
    strategy_name: str = ""
    instrument_type: InstrumentType = InstrumentType.SPOT
    order_type: OrderType = OrderType.LIMIT
    requested_notional: Decimal = Decimal("0")
    client_order_id: str = ""
    instrument_rule_snapshot_id: str = ""
    account_snapshot_id: str = ""
    reconciliation_snapshot_id: str = ""
    capability_audit_id: str = ""
    risk_decision_id: str = ""
    signal_id: str = ""
    candle_id: str = ""
    acceptance_only: bool = False
    inventory_scope: str = "strategy_managed"
    submission_sequence: int = 0
    private_state_epoch: int = 1
    private_state_version: int = 0

    @property
    def private_state_token(self) -> ProposalStateToken:
        return ProposalStateToken(self.private_state_epoch, self.private_state_version)


class DemoOrderPreflightService:
    def prepare_order(
        self,
        *,
        intent: DemoOrderIntent,
        config: RunConfig,
        instrument: Instrument,
        portfolio: PortfolioSnapshot,
        max_size: MaxAvailableSize,
        derivative_positions: dict[str, Decimal],
        open_order_count: int,
        reference_price: Decimal,
        now: datetime,
        signal_id: str = "",
        candle_id: str = "",
        acceptance_only: bool = False,
        managed_quantity: Decimal | None = None,
        exact_quantity: Decimal | None = None,
    ) -> DemoOrderProposal:
        scope_blockers: list[str] = []
        if (
            intent.instrument_id != CONTROLLED_INSTRUMENT_ID
            or instrument.instrument_id != CONTROLLED_INSTRUMENT_ID
            or config.market.instrument_id != CONTROLLED_INSTRUMENT_ID
            or max_size.instrument_id != CONTROLLED_INSTRUMENT_ID
        ):
            scope_blockers.append("instrument_not_allowed")
        if (
            intent.instrument_type is not InstrumentType.SPOT
            or instrument.instrument_type is not InstrumentType.SPOT
        ):
            scope_blockers.append("spot_required")
        if intent.trade_mode is not TradeMode.CASH or max_size.trade_mode is not TradeMode.CASH:
            scope_blockers.append("cash_required")
        if intent.order_type is not OrderType.LIMIT:
            scope_blockers.append("limit_required")
        if not config.exchange.simulated:
            scope_blockers.append("simulated_exchange_required")
        amounts_valid = (
            instrument.quantity_step.is_finite()
            and instrument.quantity_step > 0
            and intent.requested_notional.is_finite()
            and intent.requested_notional > 0
            and reference_price.is_finite()
            and reference_price > 0
            and (exact_quantity is None or (exact_quantity.is_finite() and exact_quantity > 0))
        )
        if not amounts_valid:
            scope_blockers.append("invalid_order_amount")
        if (
            not intent.requested_notional.is_finite()
            or intent.requested_notional <= 0
            or intent.requested_notional > MAXIMUM_CONTROLLED_NOTIONAL
        ):
            scope_blockers.append("notional_budget_exceeded")
        capability = SpotCashCapabilityEvaluator().evaluate(
            mode=config.mode,
            instrument=instrument,
            portfolio=portfolio,
            max_size=max_size,
            derivative_positions=derivative_positions,
            open_order_count=open_order_count,
            checked_at=now,
        )
        quantity = Decimal("0")
        if amounts_valid:
            quantity = (
                exact_quantity
                if exact_quantity is not None
                else (intent.requested_notional / reference_price // instrument.quantity_step)
                * instrument.quantity_step
            )
        notional = quantity * reference_price if amounts_valid else Decimal("0")
        blockers = [*scope_blockers, *capability.blockers]
        if config.mode.value != "demo":
            blockers.append("controlled_submission_disabled")
        if intent.source not in {"manual_demo_test", "continuous_demo"}:
            blockers.append("unsupported_source")
        if quantity < instrument.minimum_quantity or (
            instrument.minimum_notional > 0 and notional < instrument.minimum_notional
        ):
            blockers.append("minimum_order")
        if notional <= 0 or notional > MAXIMUM_CONTROLLED_NOTIONAL:
            blockers.append("notional_budget_exceeded")
        available_limit = max_size.max_buy if intent.side is OrderSide.BUY else max_size.max_sell
        if available_limit is None or notional > available_limit:
            blockers.append("max_buy" if intent.side is OrderSide.BUY else "max_sell")
        if intent.side is OrderSide.SELL and (managed_quantity is None or managed_quantity <= 0):
            blockers.append("no_strategy_managed_inventory")
        if (
            intent.side is OrderSide.SELL
            and managed_quantity is not None
            and quantity > managed_quantity
        ):
            blockers.append("managed_quantity_exceeded")
        status = ProposalStatus.BLOCKED if blockers else ProposalStatus.READY_FOR_CONFIRMATION
        proposal_id = uuid4().hex
        client_order_id = ClientOrderId.generate(proposal_id=proposal_id, timestamp=now).value
        snapshot_ids = {
            "instrument_rule_snapshot_id": _snapshot_id(
                "instrument", instrument.instrument_id, now
            ),
            "account_snapshot_id": _snapshot_id("account", intent.instrument_id, now),
            "reconciliation_snapshot_id": _snapshot_id("reconciliation", intent.instrument_id, now),
            "capability_audit_id": _snapshot_id("capability", intent.instrument_id, now),
            "risk_decision_id": _snapshot_id("risk", intent.instrument_id, now),
        }
        expires_at = now + timedelta(minutes=5)
        payload = {
            "proposal_version": 1,
            "run_id": intent.run_id,
            "source": intent.source,
            "instrument_id": intent.instrument_id,
            "instrument_type": intent.instrument_type.value,
            "trade_mode": intent.trade_mode.value,
            "side": intent.side.value,
            "order_type": intent.order_type.value,
            "planned_limit_price": str(reference_price),
            "quantity": str(quantity),
            "approved_notional": str(notional),
            **snapshot_ids,
            "signal_id": signal_id,
            "candle_id": candle_id,
            "acceptance_only": acceptance_only,
            "client_order_id": client_order_id,
            "created_at": _utc(now),
            "expires_at": _utc(expires_at),
        }
        proposal_hash = _proposal_hash(payload)
        return DemoOrderProposal(
            proposal_id,
            1,
            intent.run_id,
            intent.instrument_id,
            intent.trade_mode,
            intent.side,
            reference_price,
            quantity,
            notional,
            notional * config.backtest.fee_rate,
            now,
            expires_at,
            status,
            tuple(dict.fromkeys(blockers)),
            capability.warnings,
            False,
            proposal_hash,
            intent.source,
            intent.strategy_name,
            intent.instrument_type,
            intent.order_type,
            intent.requested_notional,
            client_order_id,
            instrument_rule_snapshot_id=snapshot_ids["instrument_rule_snapshot_id"],
            account_snapshot_id=snapshot_ids["account_snapshot_id"],
            reconciliation_snapshot_id=snapshot_ids["reconciliation_snapshot_id"],
            capability_audit_id=snapshot_ids["capability_audit_id"],
            risk_decision_id=snapshot_ids["risk_decision_id"],
            signal_id=signal_id,
            candle_id=candle_id,
            acceptance_only=acceptance_only,
        )

    @staticmethod
    def audit_payload(proposal: DemoOrderProposal) -> dict[str, object]:
        payload = asdict(proposal)
        normalized = json.loads(json.dumps(payload, default=str))
        if not isinstance(normalized, dict):
            raise RuntimeError("订单提案序列化结果无效")
        return {str(key): value for key, value in normalized.items()}

    @staticmethod
    def validate_hash(proposal: DemoOrderProposal) -> bool:
        payload = {
            "proposal_version": proposal.proposal_version,
            "run_id": proposal.run_id,
            "source": proposal.source,
            "instrument_id": proposal.instrument_id,
            "instrument_type": proposal.instrument_type.value,
            "trade_mode": proposal.trade_mode.value,
            "side": proposal.side.value,
            "order_type": proposal.order_type.value,
            "planned_limit_price": str(proposal.planned_limit_price),
            "quantity": str(proposal.quantity),
            "approved_notional": str(proposal.approved_notional),
            "instrument_rule_snapshot_id": proposal.instrument_rule_snapshot_id,
            "account_snapshot_id": proposal.account_snapshot_id,
            "reconciliation_snapshot_id": proposal.reconciliation_snapshot_id,
            "capability_audit_id": proposal.capability_audit_id,
            "risk_decision_id": proposal.risk_decision_id,
            "signal_id": proposal.signal_id,
            "candle_id": proposal.candle_id,
            "acceptance_only": proposal.acceptance_only,
            "client_order_id": proposal.client_order_id,
            "created_at": _utc(proposal.created_at),
            "expires_at": _utc(proposal.expires_at),
        }
        return _proposal_hash(payload) == proposal.proposal_hash


def _proposal_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _snapshot_id(kind: str, instrument_id: str, now: datetime) -> str:
    return hashlib.sha256(f"{kind}:{instrument_id}:{_utc(now)}".encode()).hexdigest()[:32]
