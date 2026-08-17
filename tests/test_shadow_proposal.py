from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.shadow_proposal import (
    ShadowProposalValidationError,
    validate_shadow_proposal,
)
from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.storage.database import Database


@dataclass(frozen=True)
class _ShadowValues:
    quantity: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    submission_performed: int = 0
    exchange_order_id: str | None = None
    capability_status: str = "read_only"
    risk_status: str = "blocked"
    decision: str = "blocked"
    blockers: tuple[str, ...] = ("shadow_only",)

    def validate(self) -> None:
        validate_shadow_proposal(
            quantity=self.quantity,
            notional=self.notional,
            submission_performed=self.submission_performed,
            exchange_order_id=self.exchange_order_id,
            capability_status=self.capability_status,
            risk_status=self.risk_status,
            decision=self.decision,
            blockers=self.blockers,
        )


@dataclass(frozen=True)
class _Configuration:
    strategy_name: str = "test_shadow"
    instrument_id: str = "BTC-USDT"
    timeframe: str = "1h"


@pytest.mark.parametrize(
    "candidate",
    (
        replace(_ShadowValues(), quantity=Decimal("0.00001")),
        replace(_ShadowValues(), quantity=Decimal("-0.00001")),
        replace(_ShadowValues(), notional=Decimal("1")),
        replace(_ShadowValues(), submission_performed=1),
        replace(_ShadowValues(), exchange_order_id="exchange-1"),
        replace(_ShadowValues(), blockers=()),
        replace(_ShadowValues(), capability_status="unknown"),
        replace(_ShadowValues(), risk_status="allowed"),
        replace(_ShadowValues(), decision="prepared"),
    ),
)
def test_shadow_proposal_invariant_rejects_each_invalid_field(
    candidate: _ShadowValues,
) -> None:
    with pytest.raises(ShadowProposalValidationError):
        candidate.validate()


def test_continuous_shadow_repository_persists_only_non_executable_values(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'shadow.db'}")
    database.initialize()
    repository = ContinuousShadowRepository(database)

    proposal_id = repository.save_proposal(
        "run-1",
        "signal-1",
        _Configuration(),
        "buy",
        Decimal("100"),
        Decimal("0.00"),
        "blocked",
        ["shadow_only", "not_sized"],
    )

    with database.connect() as connection:
        proposal = connection.execute(
            """
            SELECT quantity,notional,submission_performed,exchange_order_id,
                   capability_status,risk_status,decision,blockers_json
            FROM shadow_order_proposals WHERE shadow_proposal_id=?
            """,
            (proposal_id,),
        ).fetchone()
        event = connection.execute(
            """
            SELECT event_type FROM shadow_order_proposal_events
            WHERE shadow_proposal_id=?
            """,
            (proposal_id,),
        ).fetchone()

    assert tuple(proposal) == (
        "0",
        "0",
        0,
        None,
        "read_only",
        "blocked",
        "blocked",
        '["shadow_only", "not_sized"]',
    )
    assert tuple(event) == ("blocked",)


@pytest.mark.parametrize(
    ("quantity", "blockers"),
    ((Decimal("0.00001"), ["shadow_only"]), (Decimal("0"), [])),
)
def test_continuous_shadow_repository_fails_closed_before_insert(
    tmp_path: Path, quantity: Decimal, blockers: list[str]
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'shadow-rejected.db'}")
    database.initialize()
    repository = ContinuousShadowRepository(database)

    with pytest.raises(ShadowProposalValidationError):
        repository.save_proposal(
            "run-1",
            "signal-1",
            _Configuration(),
            "buy",
            Decimal("100"),
            quantity,
            "blocked",
            blockers,
        )

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM shadow_order_proposals").fetchone()[0]
    assert count == 0
