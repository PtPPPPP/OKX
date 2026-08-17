from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.order import ProposedOrder


@dataclass(frozen=True, slots=True)
class RiskRuleResult:
    rule_name: str
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    rejected_by: tuple[str, ...]
    reasons: tuple[str, ...]
    adjusted_order: ProposedOrder | None
    rule_results: tuple[RiskRuleResult, ...]
    risk_snapshot: dict[str, Any]
