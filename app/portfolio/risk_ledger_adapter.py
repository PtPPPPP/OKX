"""Copy actual local inventory into a risk request without inferring missing facts."""

from __future__ import annotations

from decimal import Decimal

from app.portfolio.personal_ledger import LedgerState
from app.portfolio.personal_risk import CashSnapshot, RiskRequest, SpotHolding


def request_from_ledger(
    request: RiskRequest,
    state: LedgerState,
    *,
    verified: bool = False,
    available_cash: Decimal | None = None,
) -> RiskRequest:
    """The confirmation is supplied by the user, never by an exchange connection.

    Cash scope and completeness remain the caller's explicit declarations.
    Historical market-value snapshots are deliberately excluded.
    """
    if request.cash is not None or request.spot_positions:
        raise ValueError("ledger cannot be merged with existing cash or spot positions")
    if request.currency != state.currency:
        raise ValueError("ledger currency must match risk request currency")
    if type(verified) is not bool:
        raise ValueError("verified must be an explicit boolean")
    if available_cash is not None and (
        not isinstance(available_cash, Decimal)
        or not available_cash.is_finite()
        or available_cash < 0
        or state.cash is None
        or available_cash > state.cash
    ):
        raise ValueError("available cash must be a finite Decimal between zero and known balance")
    payload = request.model_dump(mode="python")
    payload["cash"] = CashSnapshot.model_validate(
        {
            "balance": state.cash,
            "available": available_cash,
            "updated_at": state.cash_updated_at,
            "source": state.cash_source,
            "verified": verified,
        }
    )
    payload["spot_positions"] = [
        SpotHolding.model_validate(
            {
                "instrument": position.instrument,
                "quantity": position.quantity,
                "updated_at": position.updated_at,
                "source": position.source,
                "verified": verified,
            }
        )
        for position in state.positions.values()
    ]
    return RiskRequest.model_validate(payload)
