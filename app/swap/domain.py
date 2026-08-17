from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class PositionMode(StrEnum):
    NET = "net"
    LONG_SHORT = "long_short"


class MarginMode(StrEnum):
    ISOLATED = "isolated"
    CROSS = "cross"


class TradeAction(StrEnum):
    OPEN_LONG = "open_long"
    CLOSE_LONG = "close_long"
    OPEN_SHORT = "open_short"
    CLOSE_SHORT = "close_short"
    NO_TRADE = "no_trade"
    DATA_INCOMPLETE = "data_incomplete"


class DataStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    STALE = "stale"


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ContractSpecification:
    instrument_id: str
    contract_value: Decimal
    contract_multiplier: Decimal
    tick_size: Decimal
    lot_size: Decimal
    minimum_order_size: Decimal
    maximum_order_size: Decimal
    minimum_notional: Decimal
    base_currency: str = "BTC"
    quote_currency: str = "USDT"
    settlement_currency: str = "USDT"
    contract_type: str = "linear_perpetual"
    state: str = "live"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "fixture"
    schema_version: str = "swap-v1"

    def __post_init__(self) -> None:
        if not self.instrument_id.endswith("-USDT-SWAP"):
            raise ValueError("only USDT linear perpetual contracts are supported")
        if self.settlement_currency != "USDT" or self.contract_type != "linear_perpetual":
            raise ValueError("unsupported contract specification")
        if self.state != "live" or any(
            value <= 0 or not value.is_finite()
            for value in (
                self.contract_value,
                self.contract_multiplier,
                self.tick_size,
                self.lot_size,
                self.minimum_order_size,
                self.maximum_order_size,
                self.minimum_notional,
            )
        ):
            raise ValueError("contract_specification_incomplete")
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at, "fetched_at"))

    @property
    def base_per_contract(self) -> Decimal:
        return self.contract_value * self.contract_multiplier

    def quantize_contracts(self, contracts: Decimal) -> Decimal:
        value = (contracts / self.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.lot_size
        return value if value >= self.minimum_order_size else Decimal("0")

    def contracts_to_base_quantity(self, contracts: Decimal) -> Decimal:
        return contracts * self.base_per_contract

    def base_quantity_to_contracts(self, quantity: Decimal) -> Decimal:
        return self.quantize_contracts(quantity / self.base_per_contract)

    def contracts_to_notional(self, contracts: Decimal, price: Decimal) -> Decimal:
        return self.contracts_to_base_quantity(contracts) * price

    def notional_to_contracts(self, notional: Decimal, price: Decimal) -> Decimal:
        return self.quantize_contracts(notional / (self.base_per_contract * price))

    def required_initial_margin(
        self, contracts: Decimal, price: Decimal, leverage: Decimal = Decimal("1")
    ) -> Decimal:
        if leverage <= 0:
            raise ValueError("leverage must be positive")
        return self.contracts_to_notional(contracts, price) / leverage


@dataclass(frozen=True, slots=True)
class SwapCandle:
    instrument_id: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal | None
    confirmed: bool
    source: str = "fixture"
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completeness_status: DataStatus = DataStatus.COMPLETE
    data_quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_time", _utc(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", _utc(self.close_time, "close_time"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if self.close_time <= self.open_time or not self.confirmed:
            raise ValueError("unconfirmed_or_invalid_candle")
        values = (self.open, self.high, self.low, self.close, self.base_volume)
        if any(not value.is_finite() for value in values) or self.base_volume <= 0:
            raise ValueError("candle_data_incomplete")
        if self.quote_volume is not None and (
            not self.quote_volume.is_finite() or self.quote_volume < 0
        ):
            raise ValueError("quote_volume_invalid")


@dataclass(frozen=True, slots=True)
class OpenInterestPoint:
    instrument_id: str
    timestamp: datetime
    open_interest_contracts: Decimal
    open_interest_base: Decimal | None
    open_interest_usd: Decimal | None
    source: str = "fixture"
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completeness_status: DataStatus = DataStatus.COMPLETE

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if self.open_interest_contracts <= 0 or not self.open_interest_contracts.is_finite():
            raise ValueError("oi_missing_or_stale")


@dataclass(frozen=True, slots=True)
class FundingRatePoint:
    instrument_id: str
    funding_time: datetime
    funding_rate: Decimal
    source: str = "fixture"
    completeness_status: DataStatus = DataStatus.COMPLETE

    def __post_init__(self) -> None:
        object.__setattr__(self, "funding_time", _utc(self.funding_time, "funding_time"))
        if not self.funding_rate.is_finite():
            raise ValueError("funding rate is invalid")


@dataclass(slots=True)
class SwapPosition:
    instrument_id: str
    position_side: PositionSide
    contracts: Decimal
    entry_price: Decimal
    opened_at: datetime
    margin_mode: MarginMode = MarginMode.ISOLATED
    realized_pnl: Decimal = Decimal("0")
    accumulated_fees: Decimal = Decimal("0")
    accumulated_funding: Decimal = Decimal("0")

    def unrealized_pnl(self, specification: ContractSpecification, mark_price: Decimal) -> Decimal:
        signed = Decimal("1") if self.position_side is PositionSide.LONG else Decimal("-1")
        return (
            signed
            * specification.contracts_to_base_quantity(self.contracts)
            * (mark_price - self.entry_price)
        )
