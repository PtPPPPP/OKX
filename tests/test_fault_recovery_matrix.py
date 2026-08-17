from __future__ import annotations

import ast
import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config.settings import Settings, TradingMode
from app.domain.capability import MaxAvailableSize
from app.domain.market import Instrument, TradeMode
from app.domain.order import Order, OrderRequest, OrderState
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.domain.private_state import PrivateStateStatus
from app.exchange.exceptions import NetworkError
from app.exchange.recovery_models import ExchangeOrder, RecoveryQueryEvidence
from app.execution.demo_write_authorization import (
    DemoWriteAuthorization,
    require_place_authorization,
)
from app.market.historical_data import MarketDataError
from app.market.private_websocket import (
    OKXPrivateWebSocketProvider,
    PrivateEvent,
    PrivateEventKind,
)
from app.market.websocket import ConnectionState, WebSocketLike
from app.runtime.clock import BacktestClock
from app.services.controlled_demo_write import ControlledDemoWriteService
from app.services.demo_order_preflight import ProposalStatus
from app.services.private_events import PrivateEventProcessor
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.private_state_monitor import PrivateStateMonitor
from app.services.reconciliation import AccountSync, ReconciliationService
from app.services.unknown_order_recovery import UnknownOrderRecoveryService
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from app.testing.fault_adapters import (
    FaultInjectingRestClient,
    FaultInjectingWebSocketFactory,
)
from app.testing.fault_injection import (
    FaultAction,
    FaultInjector,
    FaultPlan,
    FaultStep,
    VirtualClock,
)
from tests.conftest import make_candles, make_instrument
from tests.programmable_exchange import ProgrammableExchange
from tests.test_demo_write_boundary import _controlled_order


def _ready_state(
    tmp_path: Path,
) -> tuple[TradingRepository, ProgrammableExchange, Instrument]:
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    portfolio = PortfolioSnapshot(
        balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
        positions={instrument.instrument_id: Decimal("0")},
        average_entry_prices={},
    )
    candles = make_candles(["100", "101"])
    exchange = ProgrammableExchange(portfolio, candles)
    database = Database(f"sqlite:///{tmp_path / 'fault-matrix.db'}")
    database.initialize()
    repository = TradingRepository(database)
    AccountSync(exchange, repository, BacktestClock(candles[-1].timestamp)).sync(
        instrument,
        "5m",
        run_id="fault-matrix",
        mode="demo",
        strategy_name="moving_average_cross",
    )
    return repository, exchange, instrument


@pytest.mark.parametrize(
    ("point", "action"),
    [
        ("private_rest.request.before_send", FaultAction.TIMEOUT),
        ("private_rest.request.before_send", FaultAction.RATE_LIMIT),
        ("private_rest.request.before_send", FaultAction.SERVER_ERROR),
        ("private_rest.request.before_send", FaultAction.CONNECTION_RESET),
        ("private_rest.response.before_parse", FaultAction.MALFORMED_JSON),
        ("private_rest.response.before_parse", FaultAction.SCHEMA_INVALID),
        ("private_rest.response.before_parse", FaultAction.SEMANTIC_INVALID),
        ("private_rest.snapshot.before_apply", FaultAction.SEMANTIC_INVALID),
    ],
)
def test_rest_fault_matrix_freezes_then_recovers_with_fresh_reconciliation(
    tmp_path: Path,
    point: str,
    action: FaultAction,
) -> None:
    repository, exchange, instrument = _ready_state(tmp_path)
    adapter = FaultInjectingRestClient(
        exchange,
        FaultPlan("rest-recovery", 11, (FaultStep(point, action),)),
        VirtualClock(),
    )
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(adapter, repository),
        repository,
    )

    try:
        failed = coordinator.reconcile_private_state(instrument, source="fault-matrix")
    except ValueError:
        failed = None

    if failed is not None:
        assert not failed.order_submission_allowed
    assert not repository.private_state_snapshot().submission_allowed

    recovered = coordinator.reconcile_private_state(instrument, source="fault-recovery")

    assert recovered.order_submission_allowed
    assert repository.private_state_snapshot().submission_allowed
    assert adapter.broker_write_calls == 0
    assert adapter.external_network_calls == 0
    adapter.injector.assert_consumed()


def _account_event(sequence: int = 1, *, epoch: int = 1) -> PrivateEvent:
    return PrivateEvent(
        PrivateEventKind.ACCOUNT,
        f"account:{epoch}:{sequence}",
        {
            "uTime": str(1_000 + sequence),
            "details": [
                {
                    "ccy": "USDT",
                    "cashBal": "100",
                    "availBal": "100",
                    "frozenBal": "0",
                    "eq": "100",
                    "uTime": str(1_000 + sequence),
                }
            ],
        },
        connection_epoch=epoch,
        sequence=sequence,
    )


@pytest.mark.parametrize(
    ("point", "reason"),
    [
        ("private_ws.event.before_coordinator", "fault_before_coordinator"),
        ("private_ws.event.during_reconciliation", "fault_during_reconciliation"),
        ("private_ws.replay.before_event", "replay_exception"),
    ],
)
def test_private_event_fault_matrix_freezes_without_state_guessing(
    tmp_path: Path,
    point: str,
    reason: str,
) -> None:
    repository, exchange, instrument = _ready_state(tmp_path)
    injector = FaultInjector(
        FaultPlan("private-event-fault", 13, (FaultStep(point, FaultAction.SEMANTIC_INVALID),)),
        exchange,
        VirtualClock(),
    )
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(exchange, repository),
        repository,
        fault_injector=injector,
    )
    assert coordinator.reconcile_private_state(
        instrument, source="initial"
    ).order_submission_allowed

    if point == "private_ws.event.before_coordinator":
        assert not coordinator.handle_private_ws_event(_account_event())
    else:
        gate = exchange.block_next_pending_orders()
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                coordinator.reconcile_private_state,
                instrument,
                source="fault-matrix",
            )
            assert gate.entered.wait(timeout=2)
            accepted = coordinator.handle_private_ws_event(_account_event())
            gate.release.set()
            result = future.result(timeout=2)
        if point == "private_ws.event.during_reconciliation":
            assert not accepted
        else:
            assert accepted
        assert not result.order_submission_allowed

    snapshot = repository.private_state_snapshot()
    assert not snapshot.submission_allowed
    assert any(reason in item for item in snapshot.dirty_reasons)
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0
    injector.assert_consumed()


class _FakeSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self.messages:
            await asyncio.Future()
        return self.messages.pop(0)

    async def close(self) -> None:
        self.closed = True


class _LocalSocketFactory:
    is_local_adapter = True

    def __init__(self, sockets: list[_FakeSocket]) -> None:
        self.sockets = sockets
        self.calls = 0

    def __call__(self, url: str) -> AbstractAsyncContextManager[WebSocketLike]:
        return self._open(url)

    @asynccontextmanager
    async def _open(self, url: str) -> AsyncIterator[WebSocketLike]:
        self.calls += 1
        if not self.sockets:
            raise RuntimeError(f"no scripted socket for {url}")
        yield self.sockets.pop(0)


def _handshake_messages(*, event: str | None = None) -> list[str]:
    messages = [
        json.dumps({"event": "login", "code": "0"}),
        json.dumps({"event": "subscribe", "arg": {"channel": "orders"}}),
        json.dumps({"event": "subscribe", "arg": {"channel": "account"}}),
        json.dumps(
            {
                "event": "subscribe",
                "arg": {"channel": "balance_and_position"},
            }
        ),
    ]
    if event == "account":
        messages.append(
            json.dumps(
                {
                    "arg": {"channel": "account"},
                    "data": [
                        {
                            "uTime": "1001",
                            "details": [
                                {
                                    "ccy": "USDT",
                                    "cashBal": "100",
                                    "availBal": "100",
                                    "frozenBal": "0",
                                    "eq": "100",
                                    "uTime": "1001",
                                }
                            ],
                        }
                    ],
                }
            )
        )
    return messages


def _settings() -> Settings:
    return Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("fake-key"),
        okx_secret_key=SecretStr("fake-secret"),
        okx_passphrase=SecretStr("fake-passphrase"),
    )


def test_private_websocket_disconnect_reconciles_before_becoming_ready(
    tmp_path: Path,
) -> None:
    repository, exchange, instrument = _ready_state(tmp_path)
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(exchange, repository),
        repository,
    )
    assert coordinator.reconcile_private_state(
        instrument, source="initial"
    ).order_submission_allowed
    factory = _LocalSocketFactory(
        [
            _FakeSocket(_handshake_messages()),
            _FakeSocket(_handshake_messages(event="account")),
        ]
    )
    adapter = FaultInjectingWebSocketFactory(
        factory,
        FaultPlan(
            "disconnect-on-first-stream-read",
            17,
            (
                FaultStep(
                    "private_ws.receive.before",
                    FaultAction.CONNECTION_RESET,
                    occurrence=5,
                ),
            ),
        ),
        VirtualClock(),
    )
    provider = OKXPrivateWebSocketProvider(
        _settings(),
        connection_factory=adapter,
        base_reconnect_delay_seconds=0,
    )
    monitor = PrivateStateMonitor(
        provider,
        coordinator,
        reconciliation_interval_seconds=60,
    )

    asyncio.run(monitor.run(instrument, max_events=1))
    final = coordinator.reconcile_private_state(instrument, source="final")

    assert final.order_submission_allowed
    assert repository.private_state_snapshot().epoch == 2
    assert provider.reconnect_count == 1
    assert factory.calls == 2
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0
    adapter.injector.assert_consumed()


def test_private_websocket_reconnect_exhaustion_freezes_state(
    tmp_path: Path,
) -> None:
    repository, exchange, instrument = _ready_state(tmp_path)
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(exchange, repository),
        repository,
    )
    assert coordinator.reconcile_private_state(
        instrument, source="initial"
    ).order_submission_allowed
    factory = _LocalSocketFactory([])
    adapter = FaultInjectingWebSocketFactory(
        factory,
        FaultPlan(
            "reconnect-exhausted",
            19,
            (
                FaultStep("private_ws.connect.before", FaultAction.CONNECTION_RESET),
                FaultStep(
                    "private_ws.connect.before",
                    FaultAction.CONNECTION_RESET,
                    occurrence=2,
                ),
            ),
        ),
        VirtualClock(),
    )
    provider = OKXPrivateWebSocketProvider(
        _settings(),
        connection_factory=adapter,
        max_reconnect_attempts=1,
        base_reconnect_delay_seconds=0,
    )
    monitor = PrivateStateMonitor(
        provider,
        coordinator,
        reconciliation_interval_seconds=60,
    )

    with pytest.raises(MarketDataError, match="连续重连失败"):
        asyncio.run(monitor.run(instrument, max_events=1))

    assert provider.state is ConnectionState.STOPPED
    assert not repository.private_state_snapshot().submission_allowed
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0
    adapter.injector.assert_consumed()


def test_fault_adapters_reject_non_local_delegates() -> None:
    plan = FaultPlan("guard", 23, ())
    clock = VirtualClock()
    with pytest.raises(ValueError, match="local delegate"):
        FaultInjectingRestClient(object(), plan, clock)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="local delegate"):
        FaultInjectingWebSocketFactory(object(), plan, clock)  # type: ignore[arg-type]


class _RecoveryClient:
    is_local_adapter = True

    def __init__(
        self,
        *,
        local_order: Order,
        authoritative_order: Order,
        query_failure: Exception | None = None,
        candidate_visible: bool = True,
    ) -> None:
        self.clock = BacktestClock(local_order.request.created_at)
        self.instrument = make_instrument(
            "BTC-USDT",
            "BTC",
            "USDT",
            "0.00001",
            "0.1",
        )
        self.portfolio = PortfolioSnapshot(
            {"BTC": Decimal("0"), "USDT": Decimal("0")},
            {"BTC-USDT": Decimal("0")},
            {},
        )
        self.authoritative_order = authoritative_order
        self.query_failure = query_failure
        self.candidate_visible = candidate_visible
        self.query_calls = 0
        self.broker_write_calls = 0
        self.external_network_calls = 0
        self.candidate = ExchangeOrder(
            authoritative_order.exchange_order_id,
            local_order.request.client_order_id,
            local_order.request.instrument_id,
            local_order.request.side.value,
            local_order.request.order_type.value,
            str(local_order.request.price),
            str(local_order.request.quantity),
            authoritative_order.state.value,
            local_order.request.created_at,
            {},
        )

    @staticmethod
    def _evidence(
        endpoint: str,
        begin: datetime,
        end: datetime,
        records: int,
    ) -> RecoveryQueryEvidence:
        return RecoveryQueryEvidence(endpoint, begin, end, 1, records, True)

    def get_recovery_orders_pending(
        self,
        instrument_id: str,
        begin: datetime,
        end: datetime,
    ) -> tuple[list[ExchangeOrder], RecoveryQueryEvidence]:
        assert instrument_id == "BTC-USDT"
        records = [self.candidate] if self.candidate_visible else []
        return records, self._evidence(
            "/api/v5/trade/orders-pending",
            begin,
            end,
            len(records),
        )

    def get_recovery_orders(
        self,
        instrument_id: str,
        begin: datetime,
        end: datetime,
        *,
        archive: bool = False,
    ) -> tuple[list[ExchangeOrder], RecoveryQueryEvidence]:
        assert instrument_id == "BTC-USDT"
        endpoint = (
            "/api/v5/trade/orders-history-archive" if archive else "/api/v5/trade/orders-history"
        )
        return [], self._evidence(endpoint, begin, end, 0)

    def get_recovery_fills(
        self,
        instrument_id: str,
        begin: datetime,
        end: datetime,
    ) -> tuple[list[object], RecoveryQueryEvidence]:
        assert instrument_id == "BTC-USDT"
        return [], self._evidence("/api/v5/trade/fills-history", begin, end, 0)

    def get_recovery_bills(
        self,
        instrument_id: str,
        begin: datetime,
        end: datetime,
    ) -> tuple[list[object], RecoveryQueryEvidence]:
        assert instrument_id == "BTC-USDT"
        return [], self._evidence("/api/v5/account/bills", begin, end, 0)

    def get_instrument(self, instrument_id: str) -> Instrument:
        assert instrument_id == "BTC-USDT"
        return self.instrument

    def get_account_configuration(self) -> AccountConfiguration:
        return AccountConfiguration(
            AccountMode.SPOT,
            None,
            False,
            None,
            self.clock.now(),
        )

    def get_portfolio(
        self,
        instrument: Instrument,
        *,
        configuration: AccountConfiguration | None = None,
    ) -> PortfolioSnapshot:
        assert instrument == self.instrument
        assert configuration is not None
        return self.portfolio

    def get_derivative_positions(self) -> dict[str, Decimal]:
        return {}

    def get_max_available_size(self, instrument_id: str) -> MaxAvailableSize:
        return MaxAvailableSize(
            instrument_id,
            TradeMode.CASH,
            Decimal("0"),
            Decimal("0"),
            self.clock.now(),
        )

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        assert instrument_id == "BTC-USDT"
        assert client_order_id == self.authoritative_order.request.client_order_id
        self.query_calls += 1
        if self.query_failure is not None:
            raise self.query_failure
        return self.authoritative_order


def _unknown_recovery_case(
    tmp_path: Path,
    *,
    exchange_order_id: str = "exchange-accepted-1",
    state: OrderState = OrderState.ACCEPTED,
) -> tuple[TradingRepository, Order, Order]:
    repository, local_order = _controlled_order(tmp_path)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    candles = make_candles(["100", "101"])
    portfolio = PortfolioSnapshot(
        {"BTC": Decimal("0"), "USDT": Decimal("0")},
        {"BTC-USDT": Decimal("0")},
        {},
    )
    AccountSync(
        ProgrammableExchange(portfolio, candles),
        repository,
        BacktestClock(candles[-1].timestamp),
    ).sync(
        instrument,
        "5m",
        run_id="unknown-recovery",
        mode="demo",
        strategy_name="moving_average_cross",
    )
    repository.mark_controlled_demo_submission_unknown(
        local_order.request.signal_id,
        error_category="timeout",
        http_status=None,
    )
    authoritative = Order(
        local_order.request,
        state=state,
        exchange_order_id=exchange_order_id,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return repository, local_order, authoritative


def test_unknown_order_recovery_requires_authoritative_read_then_full_reconciliation(
    tmp_path: Path,
) -> None:
    repository, local_order, authoritative = _unknown_recovery_case(tmp_path)
    client = _RecoveryClient(
        local_order=local_order,
        authoritative_order=authoritative,
    )

    result = UnknownOrderRecoveryService(
        repository,
        client,  # type: ignore[arg-type]
    ).recover(local_order.request.signal_id)

    recovered_order = repository.load_order(local_order.request.client_order_id)
    recovery_state = repository.private_state_snapshot()
    assert result.recovery_status == "confirmed_submitted"
    assert recovered_order is not None
    assert recovered_order.state is OrderState.ACCEPTED
    assert recovered_order.exchange_order_id == "exchange-accepted-1"
    assert recovery_state.status is PrivateStateStatus.RECONCILING_EXPECTED
    assert not recovery_state.submission_allowed

    exchange = ProgrammableExchange(
        client.portfolio,
        make_candles(["100", "101"]),
        pending_orders=[authoritative],
    )
    reconciled = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(exchange, repository),
        repository,
    ).reconcile_private_state(client.instrument, source="unknown-recovery")

    assert reconciled.order_submission_allowed
    assert repository.private_state_snapshot().submission_allowed
    assert client.query_calls == 1
    assert client.broker_write_calls == 0
    assert client.external_network_calls == 0
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0


def test_submission_in_progress_recovery_fences_then_reads_without_resubmitting(
    tmp_path: Path,
) -> None:
    repository, local_order = _controlled_order(tmp_path)
    authoritative = Order(
        local_order.request,
        state=OrderState.ACCEPTED,
        exchange_order_id="exchange-stuck-1",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    client = _RecoveryClient(local_order=local_order, authoritative_order=authoritative)

    result = UnknownOrderRecoveryService(
        repository,
        client,  # type: ignore[arg-type]
    ).recover(local_order.request.signal_id)

    proposal = repository.load_demo_order_proposal(local_order.request.signal_id)
    with repository.database.connect() as connection:
        events = connection.execute(
            "SELECT event_type FROM demo_order_proposal_events "
            "WHERE proposal_id=? ORDER BY event_id",
            (local_order.request.signal_id,),
        ).fetchall()
    assert result.recovery_status == "confirmed_submitted"
    assert proposal is not None and proposal.status is ProposalStatus.SUBMITTED
    assert "submission_unknown" in {str(row[0]) for row in events}
    assert client.query_calls == 1
    assert client.broker_write_calls == 0
    assert client.external_network_calls == 0


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (TimeoutError("authoritative read timed out"), "recoverable_failure"),
        (ValueError("authoritative response conflicted"), "terminal_failure"),
    ],
)
def test_unknown_order_recovery_failure_remains_frozen(
    tmp_path: Path,
    failure: Exception,
    expected_status: str,
) -> None:
    repository, local_order, authoritative = _unknown_recovery_case(tmp_path)
    client = _RecoveryClient(
        local_order=local_order,
        authoritative_order=authoritative,
        query_failure=failure,
    )

    result = UnknownOrderRecoveryService(
        repository,
        client,  # type: ignore[arg-type]
    ).recover(local_order.request.signal_id)

    unresolved = repository.load_order(local_order.request.client_order_id)
    assert result.recovery_status == expected_status
    assert any(item.startswith("authoritative_order_query_failed:") for item in result.blockers)
    assert unresolved is not None
    assert unresolved.state is OrderState.UNKNOWN
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN
    assert not repository.private_state_snapshot().submission_allowed
    assert client.broker_write_calls == 0
    assert client.external_network_calls == 0


@pytest.mark.parametrize(
    ("exchange_order_id", "state"),
    [
        ("", OrderState.ACCEPTED),
        ("exchange-unknown", OrderState.UNKNOWN),
        ("exchange-submitted", OrderState.SUBMITTED),
    ],
)
def test_unknown_order_repository_rejects_inconclusive_authoritative_state(
    tmp_path: Path,
    exchange_order_id: str,
    state: OrderState,
) -> None:
    repository, local_order, authoritative = _unknown_recovery_case(
        tmp_path,
        exchange_order_id=exchange_order_id,
        state=state,
    )

    with pytest.raises(ValueError, match="not conclusive"):
        repository.resolve_unknown_order_from_authoritative_read(
            local_order.request.signal_id,
            authoritative,
        )

    unresolved = repository.load_order(local_order.request.client_order_id)
    assert unresolved is not None
    assert unresolved.state is OrderState.UNKNOWN
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN


class _ResponseLostWriteClient:
    """Local exchange: accept one order, persist it, then lose the response."""

    is_local_adapter = True

    def __init__(self, *, response_lost: bool, accept_before_error: bool = True) -> None:
        self.response_lost = response_lost
        self.accept_before_error = accept_before_error
        self.orders: dict[str, Order] = {}
        self.place_calls = 0
        self.broker_write_calls = 0
        self.external_network_calls = 0

    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        require_place_authorization(authorization, request)
        self.place_calls += 1
        if self.response_lost and not self.accept_before_error:
            raise NetworkError("local connection failed before exchange acceptance")
        accepted = self.orders.get(request.client_order_id)
        if accepted is None:
            accepted = Order(request, exchange_order_id="local-accepted-1")
            accepted.transition(OrderState.SUBMITTED, at=request.created_at)
            accepted.transition(OrderState.ACCEPTED, at=request.created_at)
            self.orders[request.client_order_id] = accepted
        if self.response_lost:
            raise NetworkError("local response lost after exchange acceptance")
        return accepted

    def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        raise AssertionError("response-lost recovery must not cancel an order")


def test_response_lost_harness_has_no_broker_or_network_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.execution.demo_broker as demo_broker

    class BrokerSentinel:
        objects_created = 0

        def __init__(self, *_: object, **__: object) -> None:
            type(self).objects_created += 1
            raise AssertionError("response-lost harness must not construct a Broker")

    network_calls = 0

    def reject_network(*_: object, **__: object) -> None:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("response-lost harness must not access the network")

    monkeypatch.setattr(demo_broker, "OKXDemoBroker", BrokerSentinel)
    monkeypatch.setattr(socket, "create_connection", reject_network)

    tree = ast.parse(Path("app/services/controlled_demo_write.py").read_text(encoding="utf8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert all("demo_broker" not in item and "okx_client" not in item for item in imported)

    repository, local_order = _controlled_order(tmp_path)
    with pytest.raises(NetworkError):
        ControlledDemoWriteService(
            repository,
            _ResponseLostWriteClient(response_lost=True),
        ).place_order(local_order)

    assert BrokerSentinel.objects_created == 0
    assert network_calls == 0


def test_local_submission_distinguishes_normal_acceptance_from_pre_send_failure(
    tmp_path: Path,
) -> None:
    repository, local_order = _controlled_order(tmp_path)
    normal_exchange = _ResponseLostWriteClient(response_lost=False)
    accepted = ControlledDemoWriteService(repository, normal_exchange).place_order(local_order)
    repository.complete_controlled_demo_submission(
        local_order.request.signal_id,
        accepted,
        event_type="submission_succeeded",
        proposal_status=ProposalStatus.SUBMITTED,
    )
    assert accepted.state is OrderState.ACCEPTED
    assert len(normal_exchange.orders) == 1
    assert normal_exchange.place_calls == 1

    failed_repository, failed_order = _controlled_order(tmp_path / "pre-send")
    pre_send_exchange = _ResponseLostWriteClient(
        response_lost=True,
        accept_before_error=False,
    )
    with pytest.raises(NetworkError, match="before exchange acceptance"):
        ControlledDemoWriteService(failed_repository, pre_send_exchange).place_order(failed_order)
    assert pre_send_exchange.orders == {}
    assert pre_send_exchange.place_calls == 1
    failed_local = failed_repository.load_order(failed_order.request.client_order_id)
    assert failed_local is not None and failed_local.state is OrderState.UNKNOWN
    assert failed_repository.private_state_snapshot().status is PrivateStateStatus.FROZEN


def test_response_lost_after_acceptance_freezes_and_recovers_one_order(tmp_path: Path) -> None:
    repository, local_order = _controlled_order(tmp_path)
    exchange = _ResponseLostWriteClient(response_lost=True)
    service = ControlledDemoWriteService(repository, exchange)

    with pytest.raises(NetworkError, match="response lost"):
        service.place_order(local_order)

    proposal = repository.load_demo_order_proposal(local_order.request.signal_id)
    unknown = repository.load_order(local_order.request.client_order_id)
    assert proposal is not None and proposal.status is ProposalStatus.UNKNOWN
    assert unknown is not None and unknown.state is OrderState.UNKNOWN
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN
    assert list(exchange.orders) == [local_order.request.client_order_id]
    assert exchange.place_calls == 1

    with pytest.raises(PermissionError, match="already issued"):
        service.place_order(local_order)
    assert exchange.place_calls == 1

    accepted = exchange.orders[local_order.request.client_order_id]
    recovery_client = _RecoveryClient(local_order=local_order, authoritative_order=accepted)
    result = UnknownOrderRecoveryService(repository, recovery_client).recover(  # type: ignore[arg-type]
        local_order.request.signal_id
    )
    assert result.recovery_status == "confirmed_submitted"

    reconciled_exchange = ProgrammableExchange(
        recovery_client.portfolio,
        make_candles(["100", "101"]),
        pending_orders=[accepted],
    )
    AccountSync(
        reconciled_exchange,
        repository,
        BacktestClock(make_candles(["100", "101"])[-1].timestamp),
    ).sync(
        recovery_client.instrument,
        "5m",
        run_id="response-lost-recovery",
        mode="demo",
        strategy_name="moving_average_cross",
    )
    reconciled = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(reconciled_exchange, repository),
        repository,
    ).reconcile_private_state(recovery_client.instrument, source="response-lost-recovery")

    recovered = repository.load_order(local_order.request.client_order_id)
    assert reconciled.order_submission_allowed
    assert recovered is not None and recovered.exchange_order_id == "local-accepted-1"
    assert len(exchange.orders) == 1
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0
    assert reconciled_exchange.broker_write_calls == 0
    assert reconciled_exchange.external_network_calls == 0


def test_response_lost_recovery_query_failure_and_temporary_not_found_stay_closed(
    tmp_path: Path,
) -> None:
    repository, local_order = _controlled_order(tmp_path)
    exchange = _ResponseLostWriteClient(response_lost=True)
    with pytest.raises(NetworkError):
        ControlledDemoWriteService(repository, exchange).place_order(local_order)
    accepted = exchange.orders[local_order.request.client_order_id]

    first_client = _RecoveryClient(
        local_order=local_order,
        authoritative_order=accepted,
        query_failure=TimeoutError("local recovery query timeout"),
    )
    first = UnknownOrderRecoveryService(repository, first_client).recover(  # type: ignore[arg-type]
        local_order.request.signal_id
    )
    assert first.recovery_status == "recoverable_failure"
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN
    assert exchange.place_calls == 1

    not_found_client = _RecoveryClient(
        local_order=local_order,
        authoritative_order=accepted,
        candidate_visible=False,
    )
    not_found = UnknownOrderRecoveryService(
        repository,
        not_found_client,  # type: ignore[arg-type]
    ).recover(local_order.request.signal_id)
    proposal = repository.load_demo_order_proposal(local_order.request.signal_id)
    assert not_found.recovery_status in {
        "recoverable_failure",
        "confirmed_not_submitted",
    }
    assert proposal is not None and proposal.status is ProposalStatus.UNKNOWN
    assert exchange.place_calls == 1

    recovered_client = _RecoveryClient(local_order=local_order, authoritative_order=accepted)
    recovered = UnknownOrderRecoveryService(repository, recovered_client).recover(  # type: ignore[arg-type]
        local_order.request.signal_id
    )
    assert recovered.recovery_status == "confirmed_submitted"
    assert exchange.place_calls == 1
