"""Offline accounting and completeness contracts for the personal ledger."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any

import pytest

from app.portfolio.personal_ledger import (
    Opening,
    OpeningPosition,
    Quote,
    Transaction,
    legacy_opening,
    replay,
    value_ledger,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
NOW = START + timedelta(hours=1)


def opening(cash: str | None = "1000", **overrides: Any) -> Opening:
    data: dict[str, Any] = {
        "as_of": START,
        "source": "synthetic fixture",
        "cash": cash,
    }
    data.update(overrides)
    return Opening.model_validate(data)


def transaction(identifier: str, kind: str, **overrides: Any) -> Transaction:
    data: dict[str, Any] = {
        "id": identifier,
        "kind": kind,
        "at": START + timedelta(minutes=1),
        "source": "synthetic manual entry",
    }
    if kind in {"buy", "sell"}:
        data.update(instrument="BTC-USDT", quantity="1", price="100")
    else:
        data["amount"] = "1"
    data.update(overrides)
    return Transaction.model_validate(data)


def quote(price: str = "140", **overrides: Any) -> Quote:
    data: dict[str, Any] = {
        "instrument": "BTC-USDT",
        "price": price,
        "at": NOW,
        "source": "synthetic quote",
    }
    data.update(overrides)
    return Quote.model_validate(data)


def valued(
    entries: list[Transaction], *, start: Opening | None = None, quotes: list[Quote] | None = None
) -> dict[str, Any]:
    return value_ledger(
        replay(start or opening(), entries), [quote()] if quotes is None else quotes, now=NOW
    )


def test_weighted_cost_trade_fees_and_cash_flows_reconcile() -> None:
    report = valued(
        [
            transaction("buy-1", "buy", quantity="2", price="100", fee="2"),
            transaction("buy-2", "buy", quantity="1", price="160", fee="1"),
            transaction("sell-1", "sell", quantity="1", price="150", fee="3"),
            transaction("fee-1", "fee", amount="4"),
            transaction("in-1", "transfer_in", amount="20"),
            transaction("out-1", "transfer_out", amount="30"),
        ]
    )
    position = report["positions"][0]
    assert Decimal(position["quantity"]) == Decimal("2")
    assert Decimal(position["cost_basis"]) == Decimal("242")
    assert Decimal(position["average_cost"]) == Decimal("121")
    assert Decimal(position["market_value"]) == Decimal("280")
    assert Decimal(position["unrealized_pnl"]) == Decimal("38")
    assert Decimal(position["realized_pnl"]) == Decimal("26")
    assert Decimal(report["cash"]) == Decimal("770")
    assert Decimal(report["realized_pnl"]) == Decimal("22")
    assert Decimal(report["account_value"]) == Decimal("1050")
    assert report["complete"] is True


def test_full_sale_clears_cost_then_new_buy_has_its_own_average() -> None:
    entries = [
        transaction("buy", "buy", quantity="3", price="0.1"),
        transaction("sell-part", "sell", quantity="1", price="0.2"),
        transaction("sell-rest", "sell", quantity="2", price="0.2"),
    ]
    flat = valued(entries, quotes=[])
    position = flat["positions"][0]
    assert Decimal(position["quantity"]) == 0
    assert Decimal(position["cost_basis"]) == 0
    assert flat["complete"] is True
    assert Decimal(flat["account_value"]) == Decimal("1000.3")
    rebuilt = valued([*entries, transaction("rebuy", "buy", quantity="1", price="17")])
    assert Decimal(rebuilt["positions"][0]["average_cost"]) == Decimal("17")


def test_decimal_precision_is_independent_of_callers_context() -> None:
    entries = [transaction("buy", "buy", quantity="0.123456789", price="0.123456789")]
    with localcontext() as context:
        context.prec = 6
        report = valued(entries, quotes=[quote("0.234567891")])
    assert Decimal(report["positions"][0]["cost_basis"]) == Decimal("0.015241578750190521")
    assert Decimal(report["positions"][0]["market_value"]) == Decimal("0.028958998625361999")


def test_identical_duplicate_id_is_idempotent_even_if_decimal_scale_differs() -> None:
    entry = transaction("same", "buy", quantity="1.00")
    equivalent = transaction("same", "buy", quantity="1")
    assert valued([entry, equivalent]) == valued([entry])


def test_reimporting_old_id_after_newer_entry_does_not_fail_chronology() -> None:
    old = transaction("old", "buy")
    newer = transaction("new", "buy", at=START + timedelta(minutes=2))
    assert valued([old, newer, old]) == valued([old, newer])


def test_cash_only_transfers_are_not_profit_and_need_no_quote() -> None:
    report = valued(
        [
            transaction("in", "transfer_in", amount="123.45"),
            transaction("out", "transfer_out", amount="100"),
        ],
        quotes=[],
    )
    assert report["complete"] is True
    assert Decimal(report["account_value"]) == Decimal("1023.45")
    assert Decimal(report["realized_pnl"]) == 0
    assert Decimal(report["unrealized_pnl"]) == 0


def test_partial_valuation_cannot_hide_another_unknown_position() -> None:
    start = opening(
        positions=[OpeningPosition(instrument="ETH-USDT", quantity=None, average_cost=None)]
    )
    report = valued([transaction("btc", "buy")], start=start)
    assert report["complete"] is False
    assert report["account_value"] is None
    assert report["unrealized_pnl"] is None
    assert Decimal(report["priced_positions_subtotal"]) == Decimal("140")


def test_valuation_preserves_record_and_quote_provenance() -> None:
    entry = transaction("manual", "buy", source="synthetic statement")
    report = valued([entry], quotes=[quote(source="synthetic captured quote")])
    position = report["positions"][0]
    assert position["source"] == "synthetic statement"
    assert position["updated_at"] == entry.at.isoformat()
    assert position["quote_source"] == "synthetic captured quote"
    assert position["quote_time"] == NOW.isoformat()
    assert report["cash_source"] == "synthetic statement"
    assert report["cash_updated_at"] == entry.at.isoformat()


def test_conflicting_duplicate_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        replay(opening(), [transaction("same", "buy"), transaction("same", "buy", price="101")])


def test_overselling_is_rejected_without_short_position() -> None:
    with pytest.raises(ValueError):
        replay(opening(), [transaction("buy", "buy"), transaction("sell", "sell", quantity="2")])


@pytest.mark.parametrize("kind", ["buy", "fee", "transfer_out"])
def test_known_insufficient_cash_is_rejected(kind: str) -> None:
    with pytest.raises(ValueError):
        replay(opening("0"), [transaction("cash", kind)])


def test_sell_fee_cannot_create_negative_cash() -> None:
    start = opening(
        "0", positions=[OpeningPosition(instrument="BTC-USDT", quantity=1, average_cost=1)]
    )
    with pytest.raises(ValueError):
        replay(start, [transaction("sale", "sell", price="1", fee="2")])


def test_unknown_cash_remains_unknown_after_transfers_and_trades() -> None:
    report = valued(
        [transaction("in", "transfer_in", amount="1000"), transaction("buy", "buy")],
        start=opening(None),
    )
    assert report["cash"] is None
    assert report["account_value"] is None
    assert report["complete"] is False
    assert report["reasons"]
    assert Decimal(report["positions"][0]["market_value"]) == Decimal("140")


@pytest.mark.parametrize("quantity,average_cost", [(None, "100"), ("1", None)])
def test_unknown_quantity_or_cost_does_not_become_complete(
    quantity: Any, average_cost: Any
) -> None:
    start = opening(
        positions=[
            OpeningPosition.model_validate(
                {"instrument": "BTC-USDT", "quantity": quantity, "average_cost": average_cost}
            )
        ]
    )
    report = valued([], start=start)
    assert report["complete"] is False
    assert report["account_value"] is None
    assert report["positions"][0]["unrealized_pnl"] is None
    with pytest.raises(ValueError):
        replay(start, [transaction("buy", "buy")])
    with pytest.raises(ValueError):
        replay(start, [transaction("sell", "sell")])


def test_legacy_snapshot_never_derives_quantity_from_any_new_quote() -> None:
    start = legacy_opening(
        [{"instrument": "BTC-USDT", "cost": "100", "market_value_usdt": "999"}],
        as_of=START,
        source="synthetic legacy file",
    )
    assert start.positions[0].quantity is None
    assert start.positions[0].historical_market_value == Decimal("999")
    assert start.positions[0].average_cost == Decimal("100")
    assert start.positions[0].historical_updated_at is None
    assert start.cash is None
    for price in ("200", "400"):
        report = valued([], start=start, quotes=[quote(price)])
        assert report["positions"][0]["quantity"] is None
        assert report["positions"][0]["market_value"] is None
        assert report["account_value"] is None
        assert report["complete"] is False
        assert report["positions"][0]["historical_time_status"] == "unknown"


@pytest.mark.parametrize("age_seconds", [301, -1])
def test_stale_and_future_quotes_prevent_current_valuation(age_seconds: int) -> None:
    report = valued(
        [transaction("buy", "buy")],
        quotes=[quote(at=NOW - timedelta(seconds=age_seconds))],
    )
    assert report["complete"] is False
    assert report["account_value"] is None
    assert report["positions"][0]["market_value"] is None
    assert report["positions"][0]["unrealized_pnl"] is None
    assert report["positions"][0]["quote_status"] in {"stale", "future"}


def test_freshness_boundary_is_inclusive() -> None:
    report = valued([transaction("buy", "buy")], quotes=[quote(at=NOW - timedelta(seconds=300))])
    assert report["complete"] is True


def test_quote_before_latest_position_change_is_not_current_valuation() -> None:
    report = valued(
        [transaction("buy", "buy", at=NOW)],
        quotes=[quote(at=NOW - timedelta(seconds=1))],
    )
    assert report["complete"] is False
    assert report["account_value"] is None


def test_missing_quote_does_not_silently_zero_open_position() -> None:
    report = valued([transaction("buy", "buy")], quotes=[])
    assert report["complete"] is False
    assert report["account_value"] is None
    assert report["positions"][0]["market_value"] is None
    assert report["positions"][0]["quote_status"] == "missing"


def test_duplicate_quotes_are_rejected_instead_of_implicitly_selecting_one() -> None:
    with pytest.raises(ValueError):
        valued([transaction("buy", "buy")], quotes=[quote(), quote("200")])


@pytest.mark.parametrize("max_age", [0, -1])
def test_invalid_freshness_policy_is_rejected(max_age: int) -> None:
    with pytest.raises(ValueError):
        value_ledger(replay(opening(), []), [], now=NOW, max_age_seconds=max_age)


def test_valuation_cannot_label_future_ledger_as_current() -> None:
    report = valued([transaction("future", "buy", at=NOW + timedelta(seconds=1))])
    assert report["complete"] is False
    assert report["account_value"] is None


@pytest.mark.parametrize("bad", [1.1, True, "NaN", "Infinity", "-Infinity", "-1"])
def test_invalid_financial_inputs_are_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        opening(bad)
    with pytest.raises(ValueError):
        transaction("bad", "buy", quantity=bad)
    with pytest.raises(ValueError):
        quote(bad)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"source": " "},
        {"quantity": "0"},
        {"price": "0"},
        {"amount": "1"},
        {"fee": "-1"},
        {"instrument": "BTC-USDT-SWAP"},
        {"instrument": "BTC-USD"},
        {"unexpected": "ignored?"},
    ],
)
def test_trade_schema_rejects_ambiguous_or_unsupported_records(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        transaction("bad", "buy", **overrides)


@pytest.mark.parametrize(
    "overrides",
    [{"instrument": "BTC-USDT"}, {"fee": "1"}, {"quantity": "1"}, {"price": "1"}, {"amount": "0"}],
)
def test_cash_event_cannot_hide_trade_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        transaction("bad", "fee", **overrides)


def test_currency_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        replay(opening(), [transaction("usd", "transfer_in", currency="USD")])


def test_duplicate_opening_products_rejected() -> None:
    position = OpeningPosition(instrument="BTC-USDT", quantity=1, average_cost=100)
    with pytest.raises(ValueError):
        opening(positions=[position, position])


def test_timestamps_require_timezone_and_normalize_to_utc() -> None:
    with pytest.raises(ValueError):
        opening(as_of=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        transaction("bad", "buy", at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        quote(at=datetime(2026, 1, 1))
    local = START.astimezone(timezone(timedelta(hours=8)))
    assert opening(as_of=local).as_of.utcoffset() == timedelta(0)


def test_out_of_order_and_preopening_transactions_are_rejected() -> None:
    with pytest.raises(ValueError):
        replay(opening(), [transaction("early", "buy", at=START - timedelta(seconds=1))])
    with pytest.raises(ValueError):
        replay(
            opening(),
            [transaction("late", "buy", at=NOW), transaction("early", "buy")],
        )
