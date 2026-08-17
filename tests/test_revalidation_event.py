from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from tests.test_demo_write_boundary import _controlled_order

# Immutable lifecycle audit events recorded by earlier phases when a controlled
# order is prepared, fenced and started. Revalidation events must coexist with
# them instead of replacing them.
_LIFECYCLE_EVENTS = ("prepared", "submission_fenced", "submission_started")

_LIFECYCLE_QUERY = (
    "SELECT event_id, event_type, reason FROM demo_order_proposal_events"
    f" WHERE proposal_id=? AND event_type IN ({', '.join('?' for _ in _LIFECYCLE_EVENTS)})"
    " ORDER BY event_id"
)


def test_save_revalidation_event_is_public_and_appends(tmp_path: Path) -> None:
    repository, order = _controlled_order(tmp_path)
    proposal_id = order.request.signal_id
    with sqlite3.connect(repository.database.path) as connection:
        lifecycle_before = connection.execute(
            _LIFECYCLE_QUERY, (proposal_id, *_LIFECYCLE_EVENTS)
        ).fetchall()
    assert [row[1] for row in lifecycle_before] == list(_LIFECYCLE_EVENTS)

    repository.save_revalidation_event(proposal_id, "revalidation_started", "fresh_state_checks")
    repository.save_revalidation_event(proposal_id, "revalidation_passed", "passed")

    with sqlite3.connect(repository.database.path) as connection:
        revalidation = connection.execute(
            """SELECT event_id, event_type, reason, event_time FROM demo_order_proposal_events
            WHERE proposal_id=? AND event_type LIKE 'revalidation%' ORDER BY event_id""",
            (proposal_id,),
        ).fetchall()
        lifecycle_after = connection.execute(
            _LIFECYCLE_QUERY, (proposal_id, *_LIFECYCLE_EVENTS)
        ).fetchall()

    assert [(row[1], row[2]) for row in revalidation] == [
        ("revalidation_started", "fresh_state_checks"),
        ("revalidation_passed", "passed"),
    ]
    for row in revalidation:
        datetime.fromisoformat(row[3])
    assert lifecycle_after == lifecycle_before, "lifecycle audit events must be preserved"
    assert min(row[0] for row in revalidation) > max(row[0] for row in lifecycle_before), (
        "revalidation events must be appended after lifecycle events"
    )


def test_revalidation_service_uses_public_event_api(tmp_path: Path) -> None:
    repository, order = _controlled_order(tmp_path)
    proposal_id = order.request.signal_id
    # The public API must be reachable from the service layer without private access.
    assert not hasattr(repository, "_save_revalidation_event")
    repository.save_revalidation_event(proposal_id, "revalidation_started", "fresh_state_checks")
    with sqlite3.connect(repository.database.path) as connection:
        own = connection.execute(
            """SELECT COUNT(*) FROM demo_order_proposal_events
            WHERE proposal_id=? AND event_type='revalidation_started'""",
            (proposal_id,),
        ).fetchone()[0]
        other = connection.execute(
            """SELECT COUNT(*) FROM demo_order_proposal_events
            WHERE proposal_id<>? AND event_type LIKE 'revalidation%'""",
            (proposal_id,),
        ).fetchone()[0]
    assert own == 1
    assert other == 0, "revalidation events must associate with the target proposal only"
