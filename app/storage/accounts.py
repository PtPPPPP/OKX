from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from app.domain.market import Instrument
from app.domain.order import (
    OrderSide,
)
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
from app.storage._codec import _decimal_text, _json, _now, _optional_decimal
from app.storage.database import Database


class AccountRepository:
    """Portfolio snapshots, risk metrics and strategy-owned inventory."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize_inventory_scope(
        self,
        *,
        strategy_name: str,
        run_id: str,
        instrument_id: str,
        scope: str,
        quantity: Decimal,
        average_cost: Decimal | None,
    ) -> None:
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO managed_inventory (strategy_name,run_id,instrument_id,inventory_scope,acquired_quantity,disposed_quantity,reserved_quantity,average_cost,realized_pnl,created_at,updated_at) VALUES (?,?,?,?,?,'0','0',?,'0',?,?)",
                (
                    strategy_name,
                    run_id,
                    instrument_id,
                    scope,
                    str(quantity),
                    _decimal_text(average_cost),
                    now,
                    now,
                ),
            )

    def managed_strategy_quantity(
        self, strategy_name: str, run_id: str, instrument_id: str
    ) -> Decimal:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT acquired_quantity,disposed_quantity,reserved_quantity FROM managed_inventory WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (strategy_name, run_id, instrument_id),
            ).fetchone()
        return (
            Decimal("0")
            if row is None
            else Decimal(str(row[0])) - Decimal(str(row[1])) - Decimal(str(row[2]))
        )

    def managed_strategy_position(
        self, strategy_name: str, run_id: str, instrument_id: str
    ) -> tuple[Decimal, Decimal | None]:
        """Return only inventory owned by this strategy run, never account inventory."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT acquired_quantity,disposed_quantity,reserved_quantity,average_cost FROM managed_inventory WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (strategy_name, run_id, instrument_id),
            ).fetchone()
        if row is None:
            return Decimal("0"), None
        quantity = Decimal(str(row[0])) - Decimal(str(row[1])) - Decimal(str(row[2]))
        return quantity, Decimal(str(row[3])) if row[3] is not None else None

    def managed_strategy_quantity_for_generation(
        self, strategy_name: str, run_id: str, instrument_id: str, generation_id: str
    ) -> Decimal:
        quantity, _ = self.managed_strategy_position_for_generation(
            strategy_name, run_id, instrument_id, generation_id
        )
        return quantity

    def managed_strategy_position_for_generation(
        self, strategy_name: str, run_id: str, instrument_id: str, generation_id: str
    ) -> tuple[Decimal, Decimal | None]:
        """Return inventory only when its source run belongs to the requested generation."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT mi.acquired_quantity,mi.disposed_quantity,mi.reserved_quantity,mi.average_cost
                FROM managed_inventory AS mi
                JOIN continuous_demo_runs AS run ON run.run_id=mi.run_id
                WHERE mi.strategy_name=? AND mi.run_id=? AND mi.instrument_id=?
                  AND mi.inventory_scope='strategy_managed' AND run.generation_id=?""",
                (strategy_name, run_id, instrument_id, generation_id),
            ).fetchone()
        if row is None:
            return Decimal("0"), None
        quantity = Decimal(str(row[0])) - Decimal(str(row[1])) - Decimal(str(row[2]))
        return quantity, Decimal(str(row[3])) if row[3] is not None else None

    def apply_managed_fill(
        self,
        *,
        strategy_name: str,
        run_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
    ) -> None:
        if quantity <= 0 or price <= 0:
            raise ValueError("managed fill quantity and price must be positive")
        now = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT acquired_quantity,disposed_quantity,reserved_quantity,average_cost,realized_pnl FROM managed_inventory WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (strategy_name, run_id, instrument_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO managed_inventory(strategy_name,run_id,instrument_id,inventory_scope,acquired_quantity,disposed_quantity,reserved_quantity,average_cost,realized_pnl,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        strategy_name,
                        run_id,
                        instrument_id,
                        "strategy_managed",
                        "0",
                        "0",
                        "0",
                        None,
                        "0",
                        now,
                        now,
                    ),
                )
                acquired = disposed = reserved = Decimal("0")
                average = None
                realized = Decimal("0")
            else:
                acquired = Decimal(str(row[0]))
                disposed = Decimal(str(row[1]))
                reserved = Decimal(str(row[2]))
                average = Decimal(str(row[3])) if row[3] is not None else None
                realized = Decimal(str(row[4]))
            if side is OrderSide.BUY:
                current = acquired - disposed
                total_cost = (average or Decimal("0")) * current + price * quantity + fee
                acquired += quantity
                average = total_cost / (current + quantity)
            else:
                available = acquired - disposed - reserved
                if quantity > available:
                    raise ValueError("sell fill exceeds strategy-managed inventory")
                disposed += quantity
                realized += (price - (average or price)) * quantity - fee
            connection.execute(
                "UPDATE managed_inventory SET acquired_quantity=?,disposed_quantity=?,reserved_quantity=?,average_cost=?,realized_pnl=?,updated_at=? WHERE strategy_name=? AND run_id=? AND instrument_id=? AND inventory_scope='strategy_managed'",
                (
                    str(acquired),
                    str(disposed),
                    str(reserved),
                    str(average) if average is not None else None,
                    str(realized),
                    now,
                    strategy_name,
                    run_id,
                    instrument_id,
                ),
            )

    def save_portfolio_snapshot(
        self,
        portfolio: PortfolioSnapshot,
        instrument: Instrument,
        mark_price: Decimal,
        captured_at: datetime,
        *,
        run_id: str,
        mode: str,
        strategy_name: str,
        bar: str,
    ) -> None:
        quote_balance = portfolio.cash_balance(instrument.quote_currency) or Decimal("0")
        base_quantity = portfolio.position(instrument.instrument_id)
        managed_equity = quote_balance + base_quantity * mark_price
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO portfolio_snapshots
                (run_id, mode, strategy_name, instrument_id, bar, balances_json,
                 positions_json, average_entry_prices_json, equity, managed_equity, captured_at,
                 asset_balances_json, position_costs_json, balance_model_version,
                 field_contract_version, account_configuration_json, account_equity_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    mode,
                    strategy_name,
                    instrument.instrument_id,
                    bar,
                    _json(dict(portfolio.balances)),
                    _json(dict(portfolio.positions)),
                    _json(dict(portfolio.average_entry_prices)),
                    str(managed_equity),
                    str(managed_equity),
                    captured_at.isoformat(),
                    _json(
                        {
                            currency: {
                                "currency": asset.currency,
                                "cash_balance": _decimal_text(asset.cash_balance),
                                "available_balance": _decimal_text(asset.available_balance),
                                "frozen_balance": _decimal_text(asset.frozen_balance),
                                "equity": _decimal_text(asset.equity),
                                "equity_usd": _decimal_text(asset.equity_usd),
                                "discount_equity": _decimal_text(asset.discount_equity),
                                "liabilities": _decimal_text(asset.liabilities),
                                "unrealized_pnl": _decimal_text(asset.unrealized_pnl),
                                "holding_quantity": _decimal_text(asset.holding_quantity),
                                "spendable_quantity": _decimal_text(asset.spendable_quantity),
                                "raw_field_presence": sorted(asset.raw_field_presence),
                                "fetched_at": asset.fetched_at.isoformat(),
                                "account_mode": asset.account_mode.value,
                                "validation_status": asset.validation_status.value,
                            }
                            for currency, asset in portfolio.asset_balances.items()
                        }
                    ),
                    _json(
                        {
                            instrument_id: {
                                "average_entry_price": (
                                    str(cost.average_entry_price)
                                    if cost.average_entry_price is not None
                                    else None
                                ),
                                "cost_source": cost.cost_source,
                                "cost_is_reliable": cost.cost_is_reliable,
                            }
                            for instrument_id, cost in portfolio.position_costs.items()
                        }
                    ),
                    portfolio.model_version,
                    "okx-v5-2026-07",
                    _json(
                        {
                            "account_mode": portfolio.account_configuration.account_mode.value,
                            "position_mode": portfolio.account_configuration.position_mode,
                            "auto_loan_enabled": portfolio.account_configuration.auto_loan_enabled,
                            "greeks_type": portfolio.account_configuration.greeks_type,
                            "fetched_at": portfolio.account_configuration.fetched_at.isoformat(),
                        }
                        if portfolio.account_configuration
                        else {}
                    ),
                    _json(
                        {
                            "okx_total_equity": _decimal_text(
                                portfolio.account_equity.okx_total_equity
                            ),
                            "okx_adjusted_equity": _decimal_text(
                                portfolio.account_equity.okx_adjusted_equity
                            ),
                            "okx_equity_usd": _decimal_text(
                                portfolio.account_equity.okx_equity_usd
                            ),
                            "account_mode": portfolio.account_equity.account_mode.value,
                        }
                        if portfolio.account_equity
                        else {}
                    ),
                ),
            )

    def load_latest_portfolio_snapshot(
        self, instrument: Instrument
    ) -> tuple[PortfolioSnapshot, Decimal, datetime] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM portfolio_snapshots
                WHERE instrument_id = ?
                  AND balance_model_version IN ('v4', 'backtest-v1')
                ORDER BY captured_at DESC LIMIT 1""",
                (instrument.instrument_id,),
            ).fetchone()
        if row is None:
            return None
        balances = json.loads(str(row["balances_json"]))
        positions = json.loads(str(row["positions_json"]))
        average_entry_prices = json.loads(str(row["average_entry_prices_json"]))
        asset_balances = json.loads(str(row["asset_balances_json"]))
        position_costs = json.loads(str(row["position_costs_json"]))
        configuration_data = json.loads(str(row["account_configuration_json"]))
        account_equity_data = json.loads(str(row["account_equity_json"]))
        return (
            PortfolioSnapshot(
                balances={key: Decimal(str(value)) for key, value in balances.items()},
                positions={key: Decimal(str(value)) for key, value in positions.items()},
                average_entry_prices={
                    key: Decimal(str(value)) for key, value in average_entry_prices.items()
                },
                asset_balances={
                    key: AssetBalance(
                        currency=str(value["currency"]),
                        cash_balance=_optional_decimal(value.get("cash_balance")),
                        available_balance=_optional_decimal(value.get("available_balance")),
                        frozen_balance=_optional_decimal(value.get("frozen_balance")),
                        equity=_optional_decimal(value.get("equity")),
                        equity_usd=_optional_decimal(value.get("equity_usd")),
                        discount_equity=_optional_decimal(value.get("discount_equity")),
                        liabilities=_optional_decimal(value.get("liabilities")),
                        unrealized_pnl=_optional_decimal(value.get("unrealized_pnl")),
                        holding_quantity=_optional_decimal(value.get("holding_quantity")),
                        spendable_quantity=_optional_decimal(value.get("spendable_quantity")),
                        account_mode=AccountMode(str(value.get("account_mode") or "unknown")),
                        source=BalanceSource.REST,
                        fetched_at=datetime.fromisoformat(str(value["fetched_at"])),
                        raw_field_presence=frozenset(value.get("raw_field_presence") or ()),
                        is_authoritative=True,
                        validation_status=BalanceValidationStatus(
                            str(value.get("validation_status") or "insufficient_data")
                        ),
                    )
                    for key, value in asset_balances.items()
                },
                position_costs={
                    key: PositionCost(
                        average_entry_price=(
                            Decimal(str(value["average_entry_price"]))
                            if value.get("average_entry_price") is not None
                            else None
                        ),
                        cost_source=str(value["cost_source"]),
                        cost_is_reliable=bool(value["cost_is_reliable"]),
                    )
                    for key, value in position_costs.items()
                },
                account_configuration=(
                    AccountConfiguration(
                        AccountMode(str(configuration_data["account_mode"])),
                        configuration_data.get("position_mode"),
                        configuration_data.get("auto_loan_enabled"),
                        configuration_data.get("greeks_type"),
                        datetime.fromisoformat(
                            str(configuration_data.get("fetched_at") or row["captured_at"])
                        ),
                    )
                    if configuration_data.get("account_mode")
                    else None
                ),
                account_equity=(
                    AccountEquitySnapshot(
                        _optional_decimal(account_equity_data.get("okx_total_equity")),
                        _optional_decimal(account_equity_data.get("okx_adjusted_equity")),
                        _optional_decimal(account_equity_data.get("okx_equity_usd")),
                        AccountMode(str(account_equity_data["account_mode"])),
                    )
                    if account_equity_data.get("account_mode")
                    else None
                ),
                model_version=str(row["balance_model_version"]),
                trusted_for_trading=(
                    str(row["balance_model_version"]) == "v4"
                    and configuration_data.get("account_mode") == AccountMode.SPOT.value
                ),
            ),
            Decimal(str(row["managed_equity"])),
            datetime.fromisoformat(str(row["captured_at"])),
        )

    def daily_risk_metrics(
        self, instrument_id: str, now: datetime, current_equity: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT managed_equity FROM portfolio_snapshots
                WHERE instrument_id = ? AND mode = 'demo' AND captured_at >= ?
                ORDER BY captured_at""",
                (instrument_id, day_start.isoformat()),
            ).fetchall()
        if not rows:
            return None, None
        equities = [Decimal(str(row["managed_equity"])) for row in rows]
        first = equities[0]
        peak = max([current_equity, *equities])
        daily_pnl = current_equity - first
        drawdown = (peak - current_equity) / peak * Decimal("100") if peak > 0 else Decimal("0")
        return daily_pnl, drawdown
