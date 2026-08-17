from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.evidence_coverage import (
    INSUFFICIENT_COVERAGE,
    SAFE_CLASSIFICATION,
    EvidenceCoverageWindow,
    EvidenceCoverageWindowRepository,
)
from app.storage.database import Database

START = datetime(2026, 7, 22, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 28, tzinfo=UTC)


def _window(**overrides: object) -> EvidenceCoverageWindow:
    values: dict[str, object] = {
        "run_id": "r6-run",
        "run_start_at": START,
        "evidence_cutoff_at": CUTOFF,
        "query_completed_at": CUTOFF,
        "orders_coverage_complete": True,
        "fills_coverage_complete": True,
        "pagination_complete": True,
        "retention_covers_window": True,
        "current_open_orders": 0,
        "current_positions": 0,
        "unknown_orders": 0,
        "owner_process_present": False,
        "fresh_heartbeat_present": False,
        "lock_live": False,
        "new_events_after_snapshot": False,
        "snapshot_hash": "a" * 64,
        "limitations": (),
    }
    values.update(overrides)
    return EvidenceCoverageWindow(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    (
        {"orders_coverage_complete": False},
        {"fills_coverage_complete": False},
        {"pagination_complete": False},
        {"retention_covers_window": False},
        {"current_open_orders": 1},
        {"current_positions": 1},
        {"new_events_after_snapshot": True},
    ),
)
def test_incomplete_or_live_evidence_stays_r6(overrides: dict[str, object]) -> None:
    assert _window(**overrides).classification == INSUFFICIENT_COVERAGE


def test_unknown_terminal_time_is_distinct_from_evidence_cutoff() -> None:
    record = _window().as_record()

    assert record["historical_terminal_time"] is None
    assert record["historical_terminal_time_known"] == 0
    assert record["evidence_cutoff_at"] == CUTOFF.isoformat()
    assert record["coverage_end_source"] == "audit_evidence_cutoff"
    assert _window().classification == SAFE_CLASSIFICATION


def test_same_cutoff_is_idempotent_and_conflicting_cutoff_is_rejected(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'coverage.db'}")
    database.initialize()
    repository = EvidenceCoverageWindowRepository(database)

    assert repository.record(_window()) == SAFE_CLASSIFICATION
    assert repository.record(_window()) == SAFE_CLASSIFICATION
    with pytest.raises(ValueError, match="conflicts"):
        repository.record(_window(evidence_cutoff_at=datetime(2026, 7, 29, tzinfo=UTC)))
