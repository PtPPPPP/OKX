from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.domain.market import Candle, Instrument
from app.domain.order import Order, OrderSource, OrderState
from app.domain.position import PortfolioSnapshot
from app.exchange.exceptions import ExchangeError, OrderNotFound
from app.portfolio.cost_basis import CostFill, recover_average_cost
from app.runtime.clock import Clock


class ReconciliationStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: ReconciliationStatus
    message: str
    remote_order_count: int
    recovered_order_count: int
    unresolved_order_ids: tuple[str, ...] = ()

    @property
    def order_submission_allowed(self) -> bool:
        return self.status is ReconciliationStatus.HEALTHY


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    portfolio: PortfolioSnapshot
    mark_price: Decimal
    captured_at: datetime
    open_orders: tuple[Order, ...]


class ReconciliationClient(Protocol):
    def get_portfolio(self, instrument: Instrument) -> PortfolioSnapshot: ...

    def get_pending_orders(self, instrument_id: str) -> list[Order]: ...

    def get_history_candles(
        self, instrument_id: str, bar: str = "5m", limit: int = 300
    ) -> list[Candle]: ...

    def query_order(self, instrument_id: str, client_order_id: str) -> Order: ...

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]: ...

    def get_derivative_positions(self) -> dict[str, Decimal]: ...


class ReconciliationRepository(Protocol):
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
    ) -> None: ...

    def save_order(self, order: Order) -> None: ...

    def load_open_orders(self) -> list[Order]: ...

    def load_latest_portfolio_snapshot(
        self, instrument: Instrument
    ) -> tuple[PortfolioSnapshot, Decimal, datetime] | None: ...

    def load_cost_fills(self, instrument_id: str) -> list[CostFill]: ...

    def has_nonzero_private_derivative_position(self) -> bool: ...


class AccountSync:
    def __init__(
        self,
        client: ReconciliationClient,
        repository: ReconciliationRepository,
        clock: Clock,
    ) -> None:
        self.client = client
        self.repository = repository
        self.clock = clock

    def sync(
        self,
        instrument: Instrument,
        bar: str,
        *,
        run_id: str,
        mode: str,
        strategy_name: str,
    ) -> AccountSnapshot:
        portfolio = self._recover_cost(self.client.get_portfolio(instrument), instrument)
        open_orders = tuple(
            _with_order_context(
                order,
                run_id=run_id,
                mode=mode,
                strategy_name=strategy_name,
                bar=bar,
            )
            for order in self.client.get_pending_orders(instrument.instrument_id)
        )
        candles = self.client.get_history_candles(instrument.instrument_id, bar, 2)
        confirmed = [candle for candle in candles if candle.confirmed]
        if not confirmed:
            raise ValueError("账户同步无法获得最新已确认收盘价格")
        mark_price = confirmed[-1].close
        captured_at = self.clock.now()
        self.repository.save_portfolio_snapshot(
            portfolio,
            instrument,
            mark_price,
            captured_at,
            run_id=run_id,
            mode=mode,
            strategy_name=strategy_name,
            bar=bar,
        )
        for order in open_orders:
            self.repository.save_order(order)
        return AccountSnapshot(portfolio, mark_price, captured_at, open_orders)

    def _recover_cost(
        self, portfolio: PortfolioSnapshot, instrument: Instrument
    ) -> PortfolioSnapshot:
        quantity = portfolio.position(instrument.instrument_id)
        if quantity <= 0:
            return portfolio
        sources = (
            (self.repository.load_cost_fills(instrument.instrument_id), "local_demo_fills"),
            (None, "okx_fill_history"),
        )
        for configured_fills, source in sources:
            fills = (
                configured_fills
                if configured_fills is not None
                else self.client.get_trade_fills(instrument.instrument_id)
            )
            cost = recover_average_cost(
                fills,
                current_quantity=quantity,
                base_currency=instrument.base_currency,
                quote_currency=instrument.quote_currency,
                quantity_tolerance=instrument.quantity_step,
                source=source,
            )
            if cost.cost_is_reliable:
                if cost.average_entry_price is None:
                    raise RuntimeError("可靠成本缺少平均开仓价格")
                return replace(
                    portfolio,
                    average_entry_prices={
                        **portfolio.average_entry_prices,
                        instrument.instrument_id: cost.average_entry_price,
                    },
                    position_costs={**portfolio.position_costs, instrument.instrument_id: cost},
                )
        return portfolio


class ReconciliationService:
    def __init__(
        self,
        client: ReconciliationClient,
        repository: ReconciliationRepository,
    ) -> None:
        self.client = client
        self.repository = repository

    def reconcile(
        self, instrument: Instrument, *, persist_remote_state: bool = False
    ) -> ReconciliationResult:
        """Read and compare REST state.

        Only ``PrivateStateCoordinator`` may persist a REST baseline and
        confirm private state after buffering and replaying concurrent private
        WebSocket events. Direct callers remain read-only.
        """
        try:
            remote_orders = self.client.get_pending_orders(instrument.instrument_id)
            remote_portfolio = self.client.get_portfolio(instrument)
            remote_derivative_positions = self.client.get_derivative_positions()
        except ExchangeError as exc:
            return ReconciliationResult(
                ReconciliationStatus.UNKNOWN,
                f"远端账户状态不可确认: {exc}",
                0,
                0,
            )
        if persist_remote_state:
            for remote in remote_orders:
                self.repository.save_order(remote)
        local_open = [
            order
            for order in self.repository.load_open_orders()
            if order.request.instrument_id == instrument.instrument_id
        ]
        remote_ids = {order.request.client_order_id for order in remote_orders}
        recovered = 0
        unresolved: list[str] = []
        for local in local_open:
            client_id = local.request.client_order_id
            if client_id in remote_ids:
                continue
            try:
                authoritative = self.client.query_order(instrument.instrument_id, client_id)
            except (ExchangeError, OrderNotFound):
                unresolved.append(client_id)
                continue
            if persist_remote_state:
                self.repository.save_order(authoritative)
            recovered += 1
            if authoritative.state is OrderState.UNKNOWN:
                unresolved.append(client_id)
        if unresolved:
            return ReconciliationResult(
                ReconciliationStatus.BLOCKED,
                "存在无法确认的本地订单，禁止继续下单",
                len(remote_orders),
                recovered,
                tuple(unresolved),
            )
        local_snapshot = self.repository.load_latest_portfolio_snapshot(instrument)
        if local_snapshot is None:
            return ReconciliationResult(
                ReconciliationStatus.DEGRADED,
                "缺少已持久化账户快照",
                len(remote_orders),
                recovered,
            )
        local_portfolio = local_snapshot[0]
        if remote_derivative_positions:
            return ReconciliationResult(
                ReconciliationStatus.BLOCKED,
                "REST 检测到非零衍生品持仓，现货模拟订单已禁止",
                len(remote_orders),
                recovered,
            )
        if any(
            (
                remote_portfolio.cash_balance(instrument.base_currency)
                != local_portfolio.cash_balance(instrument.base_currency),
                remote_portfolio.cash_balance(instrument.quote_currency)
                != local_portfolio.cash_balance(instrument.quote_currency),
                remote_portfolio.position(instrument.instrument_id)
                != local_portfolio.position(instrument.instrument_id),
            )
        ):
            return ReconciliationResult(
                ReconciliationStatus.BLOCKED,
                "本地账户快照与模拟账户余额或持仓不一致",
                len(remote_orders),
                recovered,
            )
        for currency in (instrument.base_currency, instrument.quote_currency):
            remote_asset = remote_portfolio.asset_balances.get(currency)
            local_asset = local_portfolio.asset_balances.get(currency)
            if remote_asset is None and local_asset is None:
                continue
            if (
                remote_asset is None
                or local_asset is None
                or any(
                    (
                        remote_asset.cash_balance != local_asset.cash_balance,
                        remote_asset.available_balance != local_asset.available_balance,
                        remote_asset.frozen_balance != local_asset.frozen_balance,
                        remote_asset.equity != local_asset.equity,
                    )
                )
            ):
                return ReconciliationResult(
                    ReconciliationStatus.BLOCKED,
                    "本地账户资产明细与模拟账户不一致",
                    len(remote_orders),
                    recovered,
                )
        return ReconciliationResult(
            ReconciliationStatus.HEALTHY,
            "本地状态与模拟账户已完成对账",
            len(remote_orders),
            recovered,
        )


def _with_order_context(
    order: Order,
    *,
    run_id: str,
    mode: str,
    strategy_name: str,
    bar: str,
) -> Order:
    order.request = replace(
        order.request,
        run_id=run_id,
        mode=mode,
        strategy_name=strategy_name,
        bar=bar,
        order_source=OrderSource.RECONCILIATION,
    )
    return order
