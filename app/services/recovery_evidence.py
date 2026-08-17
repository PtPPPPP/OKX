from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.exchange.recovery_models import (
    EvidenceCoverage,
    RecoveryEndpointCapability,
    RecoveryEvidenceCompleteness,
    RecoveryQueryEvidence,
    TimeInterval,
)

_RECOVERY_LOOKBACK_MONTHS = 3


def _subtract_months(value: datetime, months: int) -> datetime:
    """Subtract calendar months, clamping invalid target days to month end."""
    if months < 0:
        raise ValueError("months must be non-negative")

    month_index = value.year * 12 + value.month - 1 - months
    target_year, target_month_index = divmod(month_index, 12)
    target_month = target_month_index + 1
    target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
    return value.replace(year=target_year, month=target_month, day=target_day)


def official_recovery_capabilities(
    now: datetime, window_begin: datetime
) -> tuple[RecoveryEndpointCapability, ...]:
    three_months_ago = _subtract_months(now, _RECOVERY_LOOKBACK_MONTHS)
    return (
        RecoveryEndpointCapability(
            "/api/v5/trade/orders-pending",
            True,
            None,
            None,
            now,
            "current open orders",
            True,
            True,
            False,
            True,
            True,
            "current order state",
        ),
        RecoveryEndpointCapability(
            "/api/v5/trade/orders-history",
            True,
            None,
            now.replace(day=max(1, now.day - 7)),
            now,
            "completed orders in the last 7 days",
            True,
            True,
            True,
            True,
            True,
            "recent completed order coverage",
        ),
        RecoveryEndpointCapability(
            "/api/v5/trade/orders-history-archive",
            True,
            None,
            three_months_ago,
            now,
            "completed orders in the last 3 months",
            True,
            True,
            True,
            True,
            window_begin < now.replace(day=max(1, now.day - 7)),
            "archive needed outside recent range",
        ),
        RecoveryEndpointCapability(
            "/api/v5/trade/fills-history",
            True,
            None,
            three_months_ago,
            now,
            "fills in the last 3 months",
            True,
            True,
            True,
            True,
            True,
            "only documented fill history covering this case",
        ),
        RecoveryEndpointCapability(
            "/api/v5/account/bills",
            True,
            None,
            None,
            now,
            "recent account bills",
            True,
            True,
            True,
            True,
            True,
            "recent bill evidence",
        ),
        RecoveryEndpointCapability(
            "/api/v5/account/bills-archive",
            True,
            None,
            three_months_ago,
            now,
            "bills in the last 3 months",
            True,
            True,
            True,
            True,
            False,
            "optional when recent bills cover the target window",
        ),
        RecoveryEndpointCapability(
            "/api/v5/trade/fills-history-archive",
            False,
            None,
            None,
            None,
            "not an official V5 endpoint",
            False,
            False,
            False,
            False,
            False,
            "not applicable; fills-history supersedes it",
        ),
    )


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceCompletenessEvaluator:
    def evaluate(
        self,
        *,
        required_begin: datetime,
        required_end: datetime,
        endpoint_capabilities: Sequence[RecoveryEndpointCapability],
        query_evidence: Sequence[RecoveryQueryEvidence],
        account_state_complete: bool = True,
    ) -> RecoveryEvidenceCompleteness:
        by_endpoint = {item.endpoint: item for item in query_evidence}
        required = tuple(
            item.endpoint for item in endpoint_capabilities if item.required_for_current_case
        )
        not_applicable = tuple(
            item.endpoint for item in endpoint_capabilities if not item.officially_documented
        )
        optional = tuple(
            item.endpoint
            for item in endpoint_capabilities
            if not item.required_for_current_case and item.officially_documented
        )
        orders = self._covered(
            "order",
            required_begin,
            required_end,
            (
                "/api/v5/trade/orders-pending",
                "/api/v5/trade/orders-history",
                "/api/v5/trade/orders-history-archive",
            ),
            by_endpoint,
        )
        fills = self._covered(
            "fill", required_begin, required_end, ("/api/v5/trade/fills-history",), by_endpoint
        )
        bills = self._covered(
            "bill",
            required_begin,
            required_end,
            ("/api/v5/account/bills", "/api/v5/account/bills-archive"),
            by_endpoint,
        )
        blockers = tuple(item.reason for item in (orders, fills, bills) if not item.completed)
        return RecoveryEvidenceCompleteness(
            orders.completed,
            fills.completed,
            bills.completed,
            account_state_complete,
            required,
            optional,
            not_applicable,
            tuple(),
            tuple(
                interval for item in (orders, fills, bills) for interval in item.uncovered_intervals
            ),
            blockers + (() if account_state_complete else ("account_state_coverage_incomplete",)),
            tuple(),
            not blockers and account_state_complete,
        )

    @staticmethod
    def _covered(
        evidence_type: str,
        begin: datetime,
        end: datetime,
        endpoints: tuple[str, ...],
        evidence: dict[str, RecoveryQueryEvidence],
    ) -> EvidenceCoverage:
        successful = [
            evidence[path]
            for path in endpoints
            if path in evidence and evidence[path].completed and evidence[path].blocking
        ]
        if not successful:
            return EvidenceCoverage(
                evidence_type,
                begin,
                end,
                (),
                (TimeInterval(begin, end),),
                endpoints,
                False,
                f"{evidence_type}_history_query_failed",
            )
        # Each successful request is signed with exactly the requested window; pagination_completed is represented by completed.
        return EvidenceCoverage(
            evidence_type,
            begin,
            end,
            (TimeInterval(begin, end),),
            (),
            tuple(item.endpoint for item in successful),
            True,
            "requested_window_covered",
        )
