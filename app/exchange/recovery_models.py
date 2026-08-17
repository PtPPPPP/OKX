from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class SubmissionRecoveryState(StrEnum):
    CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted"
    CONFIRMED_SUBMITTED = "confirmed_submitted"
    UNKNOWN_SUBMISSION_STATE = "unknown_submission_state"
    RECOVERABLE_FAILURE = "recoverable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class ClientOrderId:
    value: str

    @classmethod
    def parse(cls, value: str) -> ClientOrderId:
        if not value or len(value) > 32 or re.fullmatch(r"[A-Za-z0-9]+", value) is None:
            raise ValueError("clOrdId must be 1-32 ASCII letters or digits")
        return cls(value)

    @classmethod
    def generate(cls, *, proposal_id: str, timestamp: datetime) -> ClientOrderId:
        value = ("D" + proposal_id + timestamp.astimezone(UTC).strftime("%H%M%S%f"))[:32]
        return cls.parse(value)


@dataclass(frozen=True, slots=True)
class RecoveryQueryEvidence:
    endpoint: str
    begin: datetime | None
    end: datetime | None
    pages_read: int
    records_read: int
    completed: bool
    http_status: int | None = None
    okx_code: str | None = None
    error_classification: str | None = None
    error_message: str | None = None
    contract_status: str = "officially_documented"
    applicability_status: str = "applicable"
    blocking: bool = True
    first_record_time: datetime | None = None
    last_record_time: datetime | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryEndpointCapability:
    endpoint: str
    officially_documented: bool
    supported_in_demo: bool | None
    coverage_start: datetime | None
    coverage_end: datetime | None
    retention_description: str
    supports_instrument_type: bool
    supports_instrument_id: bool
    supports_begin_end: bool
    supports_pagination: bool
    required_for_current_case: bool
    applicability_reason: str


@dataclass(frozen=True, slots=True)
class TimeInterval:
    begin: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class EvidenceCoverage:
    evidence_type: str
    required_begin: datetime
    required_end: datetime
    covered_intervals: tuple[TimeInterval, ...]
    uncovered_intervals: tuple[TimeInterval, ...]
    supporting_endpoints: tuple[str, ...]
    completed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceCompleteness:
    order_history_coverage_complete: bool
    fill_history_coverage_complete: bool
    bill_history_coverage_complete: bool
    account_state_coverage_complete: bool
    required_endpoints: tuple[str, ...]
    optional_endpoints: tuple[str, ...]
    not_applicable_endpoints: tuple[str, ...]
    unsupported_endpoints: tuple[str, ...]
    uncovered_intervals: tuple[TimeInterval, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    overall_complete: bool


@dataclass(frozen=True, slots=True)
class UnknownOrderRecoveryResult:
    recovery_id: str
    proposal_id: str
    local_order_id: str
    original_client_order_id: str
    recovery_status: str
    confidence: str
    exchange_order_id: str | None
    queries: tuple[RecoveryQueryEvidence, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    created_at: datetime
    completeness: RecoveryEvidenceCompleteness | None = None


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    exchange_order_id: str | None
    client_order_id: str | None
    instrument_id: str
    side: str
    order_type: str
    price: str | None
    quantity: str | None
    state: str | None
    created_at: datetime | None
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class ExchangeFill:
    fill_id: str | None
    exchange_order_id: str | None
    client_order_id: str | None
    instrument_id: str
    side: str
    price: str | None
    quantity: str | None
    filled_at: datetime | None
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class AccountBill:
    bill_id: str | None
    instrument_id: str | None
    bill_type: str | None
    amount: str | None
    created_at: datetime | None
    raw: dict[str, object]
