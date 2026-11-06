"""Local, manually supplied spot inventory; never an authoritative trading account.

Money is serialized as decimal strings. Moving average cost includes buy fees;
sell fees reduce realized PnL. Separate fees are period expenses. Every fee is
denominated in the ledger's one quote currency. No exchange clients or settings
are imported here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

ZERO = Decimal("0")
LEDGER_CONTEXT = Context(prec=50)


def _decimal_input(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("amounts must be Decimal, integer, or decimal string; float is forbidden")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not number.is_finite():
        raise ValueError("amounts must be finite")
    # Bound pathological inputs; calculations use the documented 50-digit context.
    if len(number.as_tuple().digits) > 40 or abs(number.adjusted()) > 40:
        raise ValueError("decimal exceeds ledger input precision (40 digits/exponent)")
    return number


def _timestamp_input(value: Any) -> Any:
    if not isinstance(value, (str, datetime)):
        raise ValueError("timestamp must be an ISO 8601 string or aware datetime")
    return value


def _schema_version(value: Any) -> Any:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be integer 1")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC)


def _nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("nonempty identifier/source required")
    return value


def _currency(value: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]+", value):
        raise ValueError("currency must be an uppercase currency code")
    return value


def _instrument(value: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+", value):
        raise ValueError("only BASE-QUOTE spot products are supported")
    base, quote = value.split("-")
    if base == quote:
        raise ValueError("base and quote currencies must differ")
    return value


Money = Annotated[Decimal, BeforeValidator(_decimal_input), Field(ge=0)]
Timestamp = Annotated[datetime, BeforeValidator(_timestamp_input), AfterValidator(_utc)]
Identifier = Annotated[str, AfterValidator(_nonempty)]
Currency = Annotated[str, AfterValidator(_currency)]
Product = Annotated[str, AfterValidator(_instrument)]


class LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class OpeningPosition(LedgerModel):
    instrument: Product
    quantity: Money | None
    average_cost: Money | None
    updated_at: Timestamp | None = None
    source: Identifier | None = None
    historical_market_value: Money | None = None
    historical_updated_at: Timestamp | None = None
    historical_source: Identifier | None = None


class Opening(LedgerModel):
    schema_version: Annotated[Literal[1], BeforeValidator(_schema_version)] = 1
    currency: Currency = "USDT"
    as_of: Timestamp
    source: Identifier
    cash: Money | None
    positions: list[OpeningPosition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_positions(self) -> Self:
        instruments: set[str] = set()
        for position in self.positions:
            if position.instrument in instruments:
                raise ValueError("duplicate opening product")
            instruments.add(position.instrument)
            if position.instrument.split("-")[1] != self.currency:
                raise ValueError("product quote currency must match ledger currency")
            if position.updated_at is not None and position.updated_at > self.as_of:
                raise ValueError("opening position timestamp is after opening as_of")
        return self


class Transaction(LedgerModel):
    id: Identifier
    kind: Literal["buy", "sell", "fee", "transfer_in", "transfer_out"]
    at: Timestamp
    source: Identifier
    currency: Currency = "USDT"
    instrument: Product | None = None
    quantity: Money = ZERO
    price: Money = ZERO
    amount: Money = ZERO
    fee: Money = ZERO

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if self.kind in {"buy", "sell"}:
            if self.instrument is None or self.quantity <= 0 or self.price <= 0:
                raise ValueError("buy/sell requires product, positive quantity and price")
            if self.amount != 0:
                raise ValueError("trade amount is computed from quantity * price")
            if self.instrument.split("-")[1] != self.currency:
                raise ValueError("product quote currency must match transaction currency")
        elif (
            self.amount <= 0
            or self.instrument is not None
            or self.quantity != 0
            or self.price != 0
            or self.fee != 0
        ):
            raise ValueError("fee/transfer requires only a positive amount, no trade fields")
        return self


class Quote(LedgerModel):
    instrument: Product
    price: Annotated[Money, Field(gt=0)]
    at: Timestamp
    source: Identifier


@dataclass(slots=True)
class PositionState:
    instrument: str
    quantity: Decimal | None
    cost_basis: Decimal | None
    updated_at: datetime
    source: str
    average_cost_hint: Decimal | None = None
    realized_pnl: Decimal = ZERO
    historical_market_value: Decimal | None = None
    historical_updated_at: datetime | None = None
    historical_source: str | None = None

    @property
    def average_cost(self) -> Decimal | None:
        if self.quantity is None:
            return self.average_cost_hint
        if self.quantity == 0:
            return None
        if self.cost_basis is None:
            return None
        with localcontext(LEDGER_CONTEXT):
            return self.cost_basis / self.quantity


@dataclass(slots=True)
class LedgerState:
    currency: str
    opened_at: datetime
    updated_at: datetime
    source: str
    cash: Decimal | None
    cash_updated_at: datetime
    cash_source: str
    positions: dict[str, PositionState] = field(default_factory=dict)
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    standalone_fees: Decimal = ZERO
    transaction_count: int = 0


def replay(opening: Opening, transactions: list[Transaction]) -> LedgerState:
    """Rebuild from an immutable opening and ordered, idempotent local entries.

    A repeated ID with identical normalized fields is a no-op, including an old
    entry repeated after newer entries. Same-time entries use input order.
    """
    with localcontext(LEDGER_CONTEXT):
        state = LedgerState(
            currency=opening.currency,
            opened_at=opening.as_of,
            updated_at=opening.as_of,
            source=opening.source,
            cash=opening.cash,
            cash_updated_at=opening.as_of,
            cash_source=opening.source,
        )
        for position in opening.positions:
            cost_basis = (
                ZERO
                if position.quantity == 0
                else position.quantity * position.average_cost
                if position.quantity is not None and position.average_cost is not None
                else None
            )
            state.positions[position.instrument] = PositionState(
                instrument=position.instrument,
                quantity=position.quantity,
                cost_basis=cost_basis,
                updated_at=position.updated_at or opening.as_of,
                source=position.source or opening.source,
                average_cost_hint=position.average_cost,
                historical_market_value=position.historical_market_value,
                historical_updated_at=position.historical_updated_at,
                historical_source=position.historical_source,
            )
        seen: dict[str, Transaction] = {}
        for transaction in transactions:
            previous = seen.get(transaction.id)
            if previous is not None:
                if previous != transaction:
                    raise ValueError(f"conflicting transaction ID: {transaction.id}")
                continue
            if transaction.currency != state.currency:
                raise ValueError("transaction currency does not match ledger")
            if transaction.at < state.updated_at:
                raise ValueError("transactions must be at/after opening and in chronological order")
            _apply(state, transaction)
            state.updated_at = transaction.at
            state.source = transaction.source
            state.cash_updated_at = transaction.at
            state.cash_source = transaction.source
            state.transaction_count += 1
            seen[transaction.id] = transaction
        return state


def _change_cash(state: LedgerState, delta: Decimal) -> None:
    if state.cash is not None:
        if state.cash + delta < 0:
            raise ValueError("insufficient cash; negative cash/borrowing is unsupported")
        state.cash += delta


def _apply(state: LedgerState, transaction: Transaction) -> None:
    if transaction.kind in {"fee", "transfer_in", "transfer_out"}:
        delta = transaction.amount if transaction.kind == "transfer_in" else -transaction.amount
        _change_cash(state, delta)
        if transaction.kind == "fee":
            state.realized_pnl -= transaction.amount
            state.fees_paid += transaction.amount
            state.standalone_fees += transaction.amount
        return
    assert transaction.instrument is not None
    position = state.positions.get(transaction.instrument)
    if position is None:
        position = PositionState(
            instrument=transaction.instrument,
            quantity=ZERO,
            cost_basis=ZERO,
            updated_at=state.opened_at,
            source=state.source,
        )
        state.positions[transaction.instrument] = position
    if position.quantity is None or position.cost_basis is None:
        raise ValueError(
            "quantity/cost unknown; supply a verified new opening before trading entries"
        )
    notional = transaction.quantity * transaction.price
    if transaction.kind == "buy":
        added_cost = notional + transaction.fee
        _change_cash(state, -added_cost)
        position.cost_basis += added_cost
        position.quantity += transaction.quantity
    else:
        if transaction.quantity > position.quantity:
            raise ValueError("sell quantity exceeds actual holding")
        # Full liquidation consumes the exact remaining basis, avoiding dust.
        removed_cost = (
            position.cost_basis
            if transaction.quantity == position.quantity
            else position.cost_basis * transaction.quantity / position.quantity
        )
        proceeds = notional - transaction.fee
        _change_cash(state, proceeds)
        realized = proceeds - removed_cost
        position.realized_pnl += realized
        state.realized_pnl += realized
        position.cost_basis -= removed_cost
        position.quantity -= transaction.quantity
    position.updated_at = transaction.at
    position.source = transaction.source
    position.average_cost_hint = None
    state.fees_paid += transaction.fee


def _text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def quote_freshness(
    *, at: datetime | None, now: datetime, position_at: datetime, max_age_seconds: int
) -> str:
    """Shared spot/linear-contract quote clock checks, independent of trading settings."""
    now, position_at = _utc(now), _utc(position_at)
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be a positive integer")
    if at is None:
        return "missing"
    at = _utc(at)
    if at > now:
        return "future"
    if now - at > timedelta(seconds=max_age_seconds):
        return "stale"
    if at < position_at:
        return "before_position"
    return "fresh"


def value_ledger(
    state: LedgerState,
    quotes: list[Quote],
    *,
    now: datetime,
    max_age_seconds: int = 300,
) -> dict[str, Any]:
    """Only fresh prices value actual quantities; never impute missing facts."""
    now = _utc(now)
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
        raise ValueError("max_age_seconds must be a positive integer")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    by_instrument: dict[str, Quote] = {}
    for quote in quotes:
        if quote.instrument in by_instrument:
            raise ValueError("duplicate quote product")
        if quote.instrument.split("-")[1] != state.currency:
            raise ValueError("quote currency does not match ledger")
        by_instrument[quote.instrument] = quote
    with localcontext(LEDGER_CONTEXT):
        return _value(state, by_instrument, now, max_age_seconds)


def _value(
    state: LedgerState, quotes: dict[str, Quote], now: datetime, max_age_seconds: int
) -> dict[str, Any]:
    reasons = []
    if state.cash is None:
        reasons.append("cash_unknown")
    if state.updated_at > now:
        reasons.append("ledger_timestamp_in_future")
    positions = []
    subtotal = ZERO
    total_unrealized = ZERO
    pnl_complete = state.updated_at <= now
    for position in state.positions.values():
        quote = quotes.get(position.instrument)
        status = quote_freshness(
            at=quote.at if quote else None,
            now=now,
            position_at=position.updated_at,
            max_age_seconds=max_age_seconds,
        )
        market_value: Decimal | None = None
        unrealized: Decimal | None = None
        position_reasons = []
        if position.quantity is None:
            position_reasons.append("quantity_unknown")
        elif position.quantity == 0:
            market_value = ZERO
            unrealized = ZERO
            status = "not_required"
        else:
            if position.cost_basis is None:
                position_reasons.append("cost_unknown")
            if status != "fresh":
                position_reasons.append(f"quote_{status}")
            elif quote is not None and position.updated_at <= now:
                market_value = position.quantity * quote.price
                if position.cost_basis is not None:
                    unrealized = market_value - position.cost_basis
        reasons.extend(f"{position.instrument}:{reason}" for reason in position_reasons)
        if market_value is not None:
            subtotal += market_value
        if unrealized is None:
            pnl_complete = False
        else:
            total_unrealized += unrealized
        positions.append(
            {
                "instrument": position.instrument,
                "quantity": _text(position.quantity),
                "quantity_status": "unknown" if position.quantity is None else "recorded",
                "average_cost": _text(position.average_cost),
                "cost_basis": _text(position.cost_basis),
                "realized_pnl": _text(position.realized_pnl),
                "unrealized_pnl": _text(unrealized),
                "market_value": _text(market_value),
                "updated_at": _time(position.updated_at),
                "source": position.source,
                "quote_time": _time(quote.at) if quote else None,
                "quote_source": quote.source if quote else None,
                "quote_price": _text(quote.price) if quote else None,
                "quote_status": status,
                "quote_is_stale": status == "stale",
                "historical_market_value": _text(position.historical_market_value),
                "historical_updated_at": _time(position.historical_updated_at),
                "historical_source": position.historical_source,
                "historical_time_status": (
                    "recorded" if position.historical_updated_at is not None else "unknown"
                ),
                "reasons": position_reasons,
            }
        )
    complete = not reasons
    return {
        "schema_version": 1,
        "scope": "local_manual_ledger_only; not reconciled account equity",
        "currency": state.currency,
        "valued_at": now.isoformat(),
        "opened_at": state.opened_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
        "source": state.source,
        "max_age_seconds": max_age_seconds,
        "complete": complete,
        "reasons": reasons,
        "cash": _text(state.cash),
        "cash_updated_at": state.cash_updated_at.isoformat(),
        "cash_source": state.cash_source,
        "account_value": _text(state.cash + subtotal)
        if complete and state.cash is not None
        else None,
        "priced_positions_subtotal": _text(subtotal),
        "subtotal_note": "fresh known positions only; excludes unknown/stale holdings and cash",
        "realized_pnl": _text(state.realized_pnl),
        "realized_pnl_scope": "since_opening; includes standalone fee expenses",
        "unrealized_pnl": _text(total_unrealized) if pnl_complete else None,
        "fees_paid": _text(state.fees_paid),
        "standalone_fees": _text(state.standalone_fees),
        "transaction_count": state.transaction_count,
        "positions": positions,
    }


def legacy_opening(rows: list[dict[str, Any]], *, as_of: datetime, source: str) -> Opening:
    """Preserve supplied old values as historical evidence, without deriving quantity.

    as_of is the import/baseline time. It is deliberately not assigned to the
    historical price snapshot: an absent original time stays unknown.
    """
    positions = []
    for row in rows:
        positions.append(
            OpeningPosition.model_validate(
                {
                    "instrument": row.get("instrument"),
                    "quantity": row.get("quantity"),
                    "average_cost": row.get("cost"),
                    "updated_at": row.get("updated_at"),
                    "source": row.get("source") or source,
                    "historical_market_value": row.get("market_value_usdt"),
                    "historical_updated_at": row.get("updated_at"),
                    "historical_source": row.get("source") or source,
                }
            )
        )
    return Opening(as_of=as_of, source=source, cash=None, positions=positions)
