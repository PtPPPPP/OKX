from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PrivateStateStatus(StrEnum):
    BOOTSTRAPPING = "bootstrapping"
    HEALTHY = "healthy"
    RECONCILING_EXPECTED = "reconciling_expected"
    DIRTY_UNEXPLAINED = "dirty_unexplained"
    FROZEN = "frozen"


@dataclass(frozen=True, slots=True)
class PrivateStateSnapshot:
    epoch: int
    version: int
    ws_watermark: int
    status: PrivateStateStatus
    last_consistent_at: datetime | None
    last_event_at: datetime | None
    dirty_reasons: tuple[str, ...]
    unknown_order_count: int

    @property
    def submission_allowed(self) -> bool:
        return self.status is PrivateStateStatus.HEALTHY and self.unknown_order_count == 0


@dataclass(frozen=True, slots=True)
class ProposalStateToken:
    private_state_epoch: int
    private_state_version: int


@dataclass(frozen=True, slots=True)
class ReconciliationToken:
    reconciliation_id: str
    connection_epoch: int
    starting_ws_watermark: int
    starting_private_state_version: int
    started_at: datetime
