from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.account import AssetBalanceUpdate, PrivateAccountState
from app.domain.market import Candle, Instrument, InstrumentStatus, InstrumentType
from app.domain.order import Order, OrderRequest, OrderSide, OrderSource, OrderState, OrderType
from app.domain.position import (
    AccountConfiguration,
    AccountEquitySnapshot,
    AccountMode,
    AssetBalance,
    BalanceSource,
    BalanceValidationStatus,
    PortfolioSnapshot,
    PositionCost,
)
from app.portfolio.cost_basis import CostFill


def parse_candle(raw: list[str]) -> Candle:
    if len(raw) < 9:
        raise ValueError("invalid OKX candle")
    return Candle(
        datetime.fromtimestamp(int(raw[0]) / 1000, tz=UTC),
        Decimal(raw[1]),
        Decimal(raw[2]),
        Decimal(raw[3]),
        Decimal(raw[4]),
        Decimal(raw[5]),
        raw[8] == "1",
    )


def parse_instrument(raw: dict[str, Any]) -> Instrument:
    return Instrument(
        str(raw["instId"]),
        str(raw.get("baseCcy") or ""),
        str(raw.get("quoteCcy") or ""),
        InstrumentType.SPOT,
        Decimal(str(raw["tickSz"])),
        Decimal(str(raw["lotSz"])),
        Decimal(str(raw["minSz"])),
        Decimal(str(raw.get("minAmt") or "0")),
        InstrumentStatus.LIVE if raw.get("state") == "live" else InstrumentStatus.SUSPENDED,
        None,
        str(raw.get("settleCcy") or "") or None,
    )


def parse_account_configuration(raw: dict[str, Any]) -> AccountConfiguration:
    modes = {
        "1": AccountMode.SPOT,
        "2": AccountMode.FUTURES,
        "3": AccountMode.MULTI_CURRENCY_MARGIN,
        "4": AccountMode.PORTFOLIO_MARGIN,
    }
    return AccountConfiguration(
        modes.get(str(raw.get("acctLv") or ""), AccountMode.UNKNOWN),
        str(raw.get("posMode") or "") or None,
        raw.get("autoLoan") if isinstance(raw.get("autoLoan"), bool) else None,
        str(raw.get("greeksType") or "") or None,
        datetime.now(UTC),
    )


def parse_asset_balance(
    raw: dict[str, Any],
    *,
    account_mode: AccountMode = AccountMode.UNKNOWN,
    source: BalanceSource = BalanceSource.REST,
) -> AssetBalance:
    updated = _timestamp(str(raw.get("uTime") or ""))
    cash, available, frozen = (
        _optional(raw, "cashBal"),
        _optional(raw, "availBal"),
        _optional(raw, "frozenBal"),
    )
    holding = cash if account_mode is AccountMode.SPOT else None
    spendable = available if account_mode is AccountMode.SPOT else None
    status = (
        BalanceValidationStatus.PASSED
        if holding is not None and spendable is not None
        else BalanceValidationStatus.INSUFFICIENT_DATA
    )
    return AssetBalance(
        str(raw.get("ccy") or ""),
        cash,
        available,
        frozen,
        _optional(raw, "eq"),
        _optional(raw, "eqUsd"),
        _optional(raw, "disEq"),
        _optional(raw, "liab"),
        _signed(raw, "upl"),
        holding,
        spendable,
        account_mode,
        source,
        updated,
        frozenset(k for k, v in raw.items() if v not in (None, "")),
        source is BalanceSource.REST,
        status,
    )


def parse_private_account_state(raw: dict[str, Any], *, event_kind: str) -> PrivateAccountState:
    if event_kind == "account":
        details = raw.get("details", [])
        if not isinstance(details, list):
            raise ValueError("invalid private account details")
        assets = [
            parse_asset_balance(item, source=BalanceSource.PRIVATE_WEBSOCKET)
            for item in details
            if isinstance(item, dict)
        ]
        balances = {
            a.currency: AssetBalanceUpdate(
                a.currency,
                a.cash_balance or Decimal("0"),
                a.available_balance,
                a.frozen_balance,
                a.equity,
                a.equity_usd,
                a.fetched_at,
            )
            for a in assets
        }
        return PrivateAccountState(event_kind, _event_time(raw, details), balances, {})
    balances_raw, positions_raw = raw.get("balData", []), raw.get("posData", [])
    if not isinstance(balances_raw, list) or not isinstance(positions_raw, list):
        raise ValueError("invalid private balance and position data")
    balances = {
        str(item.get("ccy") or ""): AssetBalanceUpdate(
            str(item.get("ccy") or ""),
            _required(item, "cashBal"),
            None,
            None,
            None,
            None,
            _timestamp(str(item.get("uTime") or raw.get("pTime") or "")),
        )
        for item in balances_raw
        if isinstance(item, dict)
    }
    positions = {
        str(item.get("instId") or ""): Decimal(str(item.get("pos") or "0"))
        for item in positions_raw
        if isinstance(item, dict) and str(item.get("instId") or "")
    }
    return PrivateAccountState(
        event_kind, _event_time(raw, [*balances_raw, *positions_raw]), balances, positions
    )


def parse_portfolio(
    raw: dict[str, Any],
    instrument: Instrument,
    *,
    configuration: AccountConfiguration | None = None,
) -> PortfolioSnapshot:
    details = raw.get("details", [])
    if not isinstance(details, list):
        raise ValueError("invalid account balance details")
    mode = configuration.account_mode if configuration else AccountMode.UNKNOWN
    assets = {
        asset.currency: asset
        for row in details
        if isinstance(row, dict)
        for asset in (parse_asset_balance(row, account_mode=mode),)
    }
    if instrument.base_currency not in assets or instrument.quote_currency not in assets:
        raise ValueError("required spot currencies missing")
    base = assets[instrument.base_currency]
    average = _optional(
        next(row for row in details if isinstance(row, dict) and row.get("ccy") == base.currency),
        "openAvgPx",
    )
    reliable = (
        base.holding_quantity is not None
        and base.holding_quantity > 0
        and average is not None
        and average > 0
        and instrument.quote_currency in {"USD", "USDT", "USDC"}
    )
    cost = PositionCost(
        average if reliable else None,
        "okx_account_open_avg_usd" if reliable else "unknown",
        reliable,
    )
    trusted = (
        mode in {AccountMode.SPOT, AccountMode.FUTURES}
        and assets[instrument.quote_currency].available_balance is not None
        and not any(asset.liabilities not in (None, Decimal("0")) for asset in assets.values())
    )
    return PortfolioSnapshot(
        {
            ccy: asset.cash_balance
            for ccy, asset in assets.items()
            if asset.cash_balance is not None
        },
        {instrument.instrument_id: base.holding_quantity or Decimal("0")},
        {instrument.instrument_id: average} if reliable and average is not None else {},
        asset_balances=assets,
        position_costs={instrument.instrument_id: cost},
        account_configuration=configuration,
        account_equity=AccountEquitySnapshot(
            _optional(raw, "totalEq"), _optional(raw, "adjEq"), _optional(raw, "totalEq"), mode
        ),
        trusted_for_trading=trusted,
    )


def parse_order(raw: dict[str, Any]) -> Order:
    states = {
        "live": OrderState.ACCEPTED,
        "partially_filled": OrderState.PARTIALLY_FILLED,
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELLED,
        "mmp_canceled": OrderState.CANCELLED,
    }
    exchange_id = str(raw.get("ordId") or "")
    client_id = str(raw.get("clOrdId") or "") or f"okx-{exchange_id}"
    order_type = (
        OrderType(str(raw.get("ordType") or "limit"))
        if str(raw.get("ordType") or "limit") in {item.value for item in OrderType}
        else OrderType.LIMIT
    )
    created = datetime.fromtimestamp(int(raw.get("cTime") or raw.get("uTime") or 0) / 1000, tz=UTC)
    request = OrderRequest(
        client_id,
        str(raw["instId"]),
        OrderSide(str(raw["side"])),
        order_type,
        Decimal(str(raw.get("sz") or "0")),
        Decimal(str(raw.get("px") or raw.get("avgPx") or "0")),
        "exchange-sync",
        created,
        run_id="reconciliation",
        strategy_name="reconciliation",
        mode="demo",
        bar="unknown",
        order_source=OrderSource.RECONCILIATION,
    )
    average = _optional(raw, "avgPx")
    return Order(
        request,
        states.get(str(raw.get("state") or ""), OrderState.UNKNOWN),
        exchange_id or None,
        Decimal(str(raw.get("accFillSz") or "0")),
        average,
        datetime.fromtimestamp(int(raw.get("uTime") or raw.get("cTime") or 0) / 1000, tz=UTC),
        [states.get(str(raw.get("state") or ""), OrderState.UNKNOWN)],
    )


def parse_cost_fill(raw: dict[str, Any]) -> CostFill:
    timestamp = str(raw.get("fillTime") or raw.get("ts") or "")
    if not timestamp:
        raise ValueError("fill time missing")
    return CostFill(
        OrderSide(str(raw["side"])),
        Decimal(str(raw["fillSz"])),
        Decimal(str(raw["fillPx"])),
        abs(Decimal(str(raw.get("fee") or "0"))),
        str(raw.get("feeCcy") or "") or None,
        datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC),
    )


def parse_derivative_positions(rows: object) -> dict[str, Decimal]:
    if not isinstance(rows, list):
        raise ValueError("positions must be a list")
    return {
        str(row["instId"]): Decimal(str(row.get("pos") or "0"))
        for row in rows
        if isinstance(row, dict)
        and str(row.get("instType") or "").upper() != "SPOT"
        and str(row.get("instId") or "")
        and Decimal(str(row.get("pos") or "0")) != 0
    }


def _event_time(raw: dict[str, Any], rows: list[Any]) -> datetime:
    values = [
        str(raw.get("pTime") or raw.get("uTime") or ""),
        *[str(row.get("uTime") or "") for row in rows if isinstance(row, dict)],
    ]
    stamps = [int(value) for value in values if value]
    if not stamps:
        raise ValueError("private event time missing")
    return datetime.fromtimestamp(max(stamps) / 1000, tz=UTC)


def _timestamp(value: str) -> datetime:
    if not value:
        raise ValueError("timestamp missing")
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _optional(raw: dict[str, Any], field: str) -> Decimal | None:
    value = str(raw.get(field) or "")
    return _required(raw, field) if value else None


def _required(raw: dict[str, Any], field: str) -> Decimal:
    try:
        value = Decimal(str(raw.get(field) or ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {field}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"invalid {field}")
    return value


def _signed(raw: dict[str, Any], field: str) -> Decimal | None:
    value = str(raw.get(field) or "")
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid {field}")
    return parsed
