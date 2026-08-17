from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings, TradingMode
from app.domain.environment import TradingEnvironment
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import OrderRequest, OrderSide, OrderSource, OrderState, OrderType
from app.exchange.exceptions import ExchangeError, OrderRejected
from app.exchange.okx_client import DEMO_ENVIRONMENT, OkxClient, OkxEnvironment
from app.exchange.okx_models import parse_candle, parse_order
from app.execution.demo_write_authorization import (
    DemoWriteAuthorization,
    DemoWriteOperation,
    _issue_demo_write_authorization,
)
from tests.conftest import make_instrument


def demo_settings() -> Settings:
    return Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("demo-key"),
        okx_secret_key=SecretStr("demo-secret"),
        okx_passphrase=SecretStr("demo-passphrase"),
    )


def client_with(handler: httpx.MockTransport) -> OkxClient:
    return OkxClient(demo_settings(), httpx.Client(transport=handler))


def response(data: list[object], *, code: str = "0", message: str = "") -> httpx.Response:
    return httpx.Response(200, json={"code": code, "msg": message, "data": data})


def test_parse_candle() -> None:
    candle = parse_candle(["1767225600000", "100", "102", "99", "101", "10", "0", "0", "1"])
    assert candle.close == Decimal("101")
    assert candle.confirmed
    assert candle.timestamp.tzinfo is UTC


def test_parse_order_state() -> None:
    order = parse_order(
        {
            "clOrdId": "client-1",
            "ordId": "exchange-1",
            "instId": "BTC-USDT",
            "side": "buy",
            "sz": "0.001",
            "px": "100",
            "state": "partially_filled",
            "accFillSz": "0.0005",
            "avgPx": "100.1",
            "cTime": "1767225600000",
            "uTime": "1767225601000",
        }
    )
    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("0.0005")


def test_server_time_normal_response() -> None:
    transport = httpx.MockTransport(lambda request: response([{"ts": "1767225600000"}]))
    client = client_with(transport)
    assert client.get_server_time() == 1767225600000
    client.close()


def test_rest_attempt_counters_separate_public_from_private_reads() -> None:
    client = client_with(httpx.MockTransport(lambda request: response([{"ts": "1767225600000"}])))

    client.get_server_time()
    client._request("GET", "/api/v5/account/config", private=True)

    assert client.public_rest_calls == 1
    assert client.private_rest_calls == 1
    assert client.private_api_write_calls == 0
    client.close()


def test_business_error_is_converted() -> None:
    transport = httpx.MockTransport(
        lambda request: response([], code="50001", message="business failure")
    )
    client = client_with(transport)
    with pytest.raises(ExchangeError, match="50001"):
        client.get_server_time()
    client.close()


def test_empty_data_is_rejected() -> None:
    transport = httpx.MockTransport(lambda request: response([]))
    client = client_with(transport)
    with pytest.raises(ExchangeError, match="空数据"):
        client.get_server_time()
    client.close()


def test_instrument_rules_are_parsed() -> None:
    transport = httpx.MockTransport(
        lambda request: response(
            [
                {
                    "instId": "BTC-USDT",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                    "state": "live",
                    "minSz": "0.00001",
                    "lotSz": "0.00001",
                    "tickSz": "0.1",
                }
            ]
        )
    )
    client = client_with(transport)
    rules = client.get_instrument_rules("BTC-USDT")
    assert rules.price_tick == Decimal("0.1")
    client.close()


def test_suspended_instrument_is_rejected_before_run() -> None:
    transport = httpx.MockTransport(
        lambda request: response(
            [
                {
                    "instId": "ETH-USDT",
                    "baseCcy": "ETH",
                    "quoteCcy": "USDT",
                    "state": "suspend",
                    "minSz": "0.0001",
                    "lotSz": "0.0001",
                    "tickSz": "0.01",
                }
            ]
        )
    )
    client = client_with(transport)
    with pytest.raises(ExchangeError, match="不可交易"):
        client.get_instrument("ETH-USDT")
    client.close()


def test_unknown_instrument_is_rejected_before_run() -> None:
    client = client_with(httpx.MockTransport(lambda request: response([])))
    with pytest.raises(ExchangeError, match="空数据"):
        client.get_instrument("UNKNOWN-USDT")
    client.close()


def test_private_request_uses_simulated_header() -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.headers))
        return response(
            [
                {
                    "details": [
                        {
                            "ccy": currency,
                            "cashBal": "0",
                            "availBal": "0",
                            "frozenBal": "0",
                            "eq": "0",
                            "eqUsd": "0",
                            "uTime": "1700000000000",
                        }
                        for currency in ("BTC", "USDT")
                    ]
                }
            ]
        )

    client = client_with(httpx.MockTransport(handler))
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    client.get_portfolio(instrument)
    assert observed["x-simulated-trading"] == "1"
    assert observed["ok-access-key"] == "demo-key"
    assert "ok-access-sign" in observed
    client.close()


@pytest.mark.parametrize(
    "environment",
    [
        None,
        OkxEnvironment(
            TradingEnvironment.LIVE,
            "https://www.okx.com",
            "wss://ws.okx.com/ws/v5/public",
            "wss://ws.okx.com/ws/v5/private",
            False,
        ),
        OkxEnvironment(
            "unknown",  # type: ignore[arg-type]
            "https://www.okx.com",
            DEMO_ENVIRONMENT.public_ws_url,
            DEMO_ENVIRONMENT.private_ws_url,
            True,
        ),
    ],
)
def test_missing_live_and_unknown_client_environments_are_blocked(
    environment: OkxEnvironment | None,
) -> None:
    with pytest.raises(PermissionError, match=r"environment|live|simulated"):
        OkxClient(
            demo_settings(),
            httpx.Client(transport=httpx.MockTransport(lambda _: response([]))),
            environment=environment,  # type: ignore[arg-type]
        )


def test_write_is_blocked_if_simulated_environment_binding_is_removed() -> None:
    transport_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return response([])

    client = client_with(httpx.MockTransport(handler))
    client.environment = OkxEnvironment(
        TradingEnvironment.DEMO,
        DEMO_ENVIRONMENT.rest_base_url,
        DEMO_ENVIRONMENT.public_ws_url,
        DEMO_ENVIRONMENT.private_ws_url,
        False,
    )

    with pytest.raises(PermissionError, match="environment binding"):
        request = _request()
        client.place_order(request, authorization=_authorization(request))

    assert transport_calls == 0
    client.close()


def test_demo_probe_and_write_both_use_simulated_header() -> None:
    observed: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            (request.method, request.url.path, request.headers.get("x-simulated-trading"))
        )
        if request.method == "GET":
            return response([{"acctLv": "1", "posMode": "net_mode"}])
        return response([{"sCode": "0", "ordId": "exchange-1", "clOrdId": "client-1"}])

    client = client_with(httpx.MockTransport(handler))
    request = _request()

    assert (
        client.place_order(request, authorization=_authorization(request)).state
        is OrderState.ACCEPTED
    )
    assert observed == [
        ("GET", "/api/v5/account/config", "1"),
        ("POST", "/api/v5/trade/order", "1"),
    ]
    client.close()


def test_failed_demo_credential_probe_blocks_write() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return response([], code="50101", message="environment mismatch")

    client = client_with(httpx.MockTransport(handler))

    with pytest.raises(ExchangeError, match="authentication failed"):
        request = _request()
        client.place_order(request, authorization=_authorization(request))

    assert methods == ["GET"]
    assert client.private_api_write_calls == 0
    client.close()


def test_order_item_error_is_rejected() -> None:
    transport = httpx.MockTransport(
        lambda request: response(
            [{"sCode": "51011", "sMsg": "duplicate", "ordId": "", "clOrdId": "client-1"}]
        )
    )
    client = client_with(transport)
    with pytest.raises(OrderRejected, match="51011"):
        request = _request()
        client.place_order(request, authorization=_authorization(request))
    client.close()


def test_order_write_without_authorization_never_reaches_transport() -> None:
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return response([])

    client = client_with(httpx.MockTransport(handler))
    with pytest.raises(PermissionError, match="one-use authorization"):
        client.place_order(_request())
    assert transport_calls == 0
    client.close()


def test_cancel_write_without_authorization_never_reaches_transport() -> None:
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return response([])

    client = client_with(httpx.MockTransport(handler))
    with pytest.raises(PermissionError, match="one-use authorization"):
        client.cancel_order("BTC-USDT", "client-1")
    assert transport_calls == 0
    client.close()


def test_raw_post_without_authorization_never_reaches_transport() -> None:
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return response([])

    client = client_with(httpx.MockTransport(handler))
    with pytest.raises(PermissionError, match="raw OKX write"):
        client._request(
            "POST",
            "/api/v5/trade/order",
            body={"instId": "BTC-USDT"},
            private=True,
        )
    assert transport_calls == 0
    client.close()


def test_timeout_reconciles_by_client_order_id_without_resubmit() -> None:
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            raise httpx.ReadTimeout("timeout", request=request)
        return response(
            [
                {
                    "clOrdId": "client-1",
                    "ordId": "exchange-1",
                    "instId": "BTC-USDT",
                    "side": "buy",
                    "sz": "0.001",
                    "px": "100",
                    "state": "live",
                    "accFillSz": "0",
                    "avgPx": "",
                    "cTime": "1767225600000",
                    "uTime": "1767225600000",
                }
            ]
        )

    client = client_with(httpx.MockTransport(handler))
    request = _request()
    order = client.place_order(request, authorization=_authorization(request))
    assert post_count == 1
    assert order.state is OrderState.ACCEPTED
    client.close()


def test_timeout_and_failed_query_returns_unknown_without_resubmit() -> None:
    post_count = 0
    probe_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count, probe_count
        if request.url.path == "/api/v5/account/config" and probe_count == 0:
            probe_count += 1
            return response([{"acctLv": "1", "posMode": "net_mode"}])
        if request.method == "POST":
            post_count += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = client_with(httpx.MockTransport(handler))
    request = _request()
    order = client.place_order(request, authorization=_authorization(request))
    assert post_count == 1
    assert order.state is OrderState.UNKNOWN
    client.close()


def _request() -> OrderRequest:
    return OrderRequest(
        client_order_id="client-1",
        instrument_id="BTC-USDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        price=Decimal("100"),
        signal_id="proposal-1",
        created_at=datetime.now(UTC),
        run_id="run-1",
        strategy_name="moving_average_cross",
        mode="demo",
        order_source=OrderSource.MANUAL_DEMO_TEST,
    )


def _authorization(request: OrderRequest) -> DemoWriteAuthorization:
    return _issue_demo_write_authorization(
        operation=DemoWriteOperation.PLACE,
        proposal_id=request.signal_id,
        request=request,
        instrument_type=InstrumentType.SPOT,
        trade_mode=TradeMode.CASH,
        private_state_version=1,
        environment=TradingEnvironment.DEMO,
    )
