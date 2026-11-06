"""Risk conversion uses actual synthetic ledger quantities, never historical values."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.portfolio.personal_ledger import Opening, OpeningPosition, legacy_opening, replay
from app.portfolio.personal_risk import RiskRequest, analyze_risk
from app.portfolio.risk_ledger_adapter import request_from_ledger

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def request(**changes: Any) -> RiskRequest:
    payload: dict[str, Any] = {
        "as_of": NOW,
        "source": "synthetic risk input",
        "positions_complete": True,
        "cash_scope": "spot_only",
        "products": {"BTC-USDT": {"underlying": "BTC", "asset_class": "crypto"}},
        "quotes": [
            {"instrument": "BTC-USDT", "price": "100", "at": NOW, "source": "synthetic quote"}
        ],
    }
    payload.update(changes)
    return RiskRequest.model_validate(payload)


def opening() -> Opening:
    return Opening(
        as_of=NOW,
        source="synthetic opening",
        cash=Decimal("1000"),
        positions=[
            OpeningPosition(
                instrument="BTC-USDT",
                quantity=Decimal("2"),
                average_cost=Decimal("80"),
                updated_at=NOW,
                source="synthetic spot source",
                historical_market_value=Decimal("99999"),
            )
        ],
    )


def test_actual_inventory_and_sources_survive_without_default_confirmation_or_available_cash() -> (
    None
):
    base = request(positions_complete=False)
    state = replay(opening(), [])
    result = request_from_ledger(base, state)
    assert result.cash is not None
    assert result.cash.balance == Decimal("1000")
    assert result.cash.available is None
    assert result.cash.source == state.cash_source
    assert result.cash.updated_at == NOW
    assert result.cash.verified is False
    assert result.spot_positions[0].quantity == Decimal("2")
    assert result.spot_positions[0].source == "synthetic spot source"
    assert result.spot_positions[0].updated_at == NOW
    assert result.spot_positions[0].verified is False
    assert result.positions_complete is False
    assert result.cash_scope == base.cash_scope
    assert result.products == base.products
    assert result.quotes == base.quotes
    assert base.cash is None and base.spot_positions == []
    assert "historical_market_value" not in result.model_dump_json()
    assert analyze_risk(result, now=NOW)["net_equity"] is None


def test_confirmed_actual_ledger_is_valued_with_explicit_scope_and_quotes() -> None:
    converted = request_from_ledger(
        request(),
        replay(opening(), []),
        verified=True,
        available_cash=Decimal("800"),
    )
    report = analyze_risk(converted, now=NOW)
    assert report["valuation_complete"] is True
    assert Decimal(report["net_equity"]) == Decimal("1200")
    assert Decimal(report["available_cash"]) == Decimal("800")
    assert report["action"] is None
    # No user risk budget is guessed merely because valuations are available.
    assert report["overall_status"] == "INCOMPLETE"


def test_legacy_unknown_quantity_and_cash_are_not_inferred() -> None:
    state = replay(
        legacy_opening(
            [{"instrument": "BTC-USDT", "cost": "100", "market_value_usdt": "900"}],
            as_of=NOW,
            source="synthetic legacy",
        ),
        [],
    )
    result = request_from_ledger(request(), state, verified=True)
    assert result.cash is not None and result.cash.balance is None
    assert result.spot_positions[0].quantity is None
    report = analyze_risk(result, now=NOW)
    assert report["valuation_complete"] is False
    assert report["net_equity"] is None


@pytest.mark.parametrize("existing", ["cash", "spot_positions"])
def test_merging_even_identical_cash_or_existing_spot_is_rejected(existing: str) -> None:
    changes: dict[str, Any]
    if existing == "cash":
        changes = {"cash": {"balance": "1000", "updated_at": NOW, "source": "synthetic"}}
    else:
        changes = {
            "spot_positions": [
                {
                    "instrument": "BTC-USDT",
                    "quantity": "2",
                    "updated_at": NOW,
                    "source": "synthetic",
                }
            ]
        }
    with pytest.raises(ValueError, match="cannot be merged"):
        request_from_ledger(request(**changes), replay(opening(), []))


def test_currency_mismatch_is_rejected() -> None:
    base = RiskRequest(as_of=NOW, source="synthetic", currency="EUR")
    with pytest.raises(ValueError, match="currency"):
        request_from_ledger(base, replay(opening(), []))


@pytest.mark.parametrize(
    "available", [Decimal("-1"), Decimal("1001"), Decimal("NaN"), Decimal("Infinity"), 1.0]
)
def test_invalid_available_cash_is_rejected(available: Any) -> None:
    with pytest.raises(ValueError, match="available cash"):
        request_from_ledger(request(), replay(opening(), []), available_cash=available)


def test_unknown_balance_cannot_have_invented_available_cash() -> None:
    state = replay(Opening(as_of=NOW, source="synthetic", cash=None), [])
    with pytest.raises(ValueError, match="available cash"):
        request_from_ledger(request(), state, available_cash=Decimal("0"))


def test_contracts_require_explicit_all_settled_cash_scope() -> None:
    contract = {
        "id": "synthetic-contract",
        "instrument": "BTC-USDT-SWAP",
        "direction": "long",
        "base_quantity": "1",
        "entry_price": "90",
        "updated_at": NOW,
        "source": "synthetic contract",
        "verified": True,
    }
    base = request(
        cash_scope="unknown",
        contracts=[contract],
        quotes=[
            {"instrument": "BTC-USDT", "price": "100", "at": NOW, "source": "synthetic"},
            {"instrument": "BTC-USDT-SWAP", "price": "100", "at": NOW, "source": "synthetic"},
        ],
    )
    converted = request_from_ledger(base, replay(opening(), []), verified=True)
    assert converted.contracts == base.contracts
    assert converted.cash_scope == "unknown"
    assert analyze_risk(converted, now=NOW)["net_equity"] is None
