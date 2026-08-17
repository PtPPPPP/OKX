from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal


class ShadowProposalValidationError(ValueError):
    """Raised when a Shadow Proposal would be executable or ambiguously persisted."""


def validate_shadow_proposal(
    *,
    quantity: Decimal,
    notional: Decimal,
    submission_performed: int | bool,
    exchange_order_id: str | None,
    capability_status: str,
    risk_status: str,
    decision: str,
    blockers: Collection[str],
) -> None:
    """Enforce the single non-executable Shadow Proposal invariant."""
    violations: list[str] = []
    if quantity != Decimal("0"):
        violations.append("quantity must be zero")
    if notional != Decimal("0"):
        violations.append("notional must be zero")
    if submission_performed != 0:
        violations.append("submission_performed must be false")
    if exchange_order_id is not None:
        violations.append("exchange_order_id must be null")
    if capability_status != "read_only":
        violations.append("capability_status must be read_only")
    if risk_status != "blocked":
        violations.append("risk_status must be blocked")
    if decision != "blocked":
        violations.append("decision must be blocked")
    if "shadow_only" not in blockers:
        violations.append("blockers must include shadow_only")
    if violations:
        raise ShadowProposalValidationError("invalid Shadow Proposal: " + "; ".join(violations))
