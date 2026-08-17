from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings, TradingMode
from app.exchange.exceptions import NetworkError
from app.exchange.okx_client import OkxClient
from app.market.historical_data import MarketDataError
from app.market.network import NetworkConfiguration, NetworkMode
from app.market.okx_public import OKXPublicHistoricalDataProvider
from app.market.private_websocket import OKXPrivateWebSocketProvider
from app.market.websocket import OKXPublicWebSocketProvider, WebSocketLike


def test_network_configuration_defaults_to_environment_mode() -> None:
    config = NetworkConfiguration.from_environment({})
    assert config.mode is NetworkMode.ENV
    assert config.proxy_url is None
    assert config.websocket_proxy is True


@pytest.mark.parametrize(
    ("environment", "mode", "websocket_proxy"),
    (
        ({"OKX_NETWORK_MODE": "direct"}, NetworkMode.DIRECT, None),
        ({"OKX_NETWORK_MODE": "env"}, NetworkMode.ENV, True),
        (
            {
                "OKX_NETWORK_MODE": "proxy",
                "OKX_PROXY_URL": "http://127.0.0.1:7890",
            },
            NetworkMode.PROXY,
            "http://127.0.0.1:7890",
        ),
    ),
)
def test_network_configuration_selects_one_shared_rest_and_ws_mode(
    environment: dict[str, str], mode: NetworkMode, websocket_proxy: str | bool | None
) -> None:
    config = NetworkConfiguration.from_environment(environment)
    assert config.mode is mode
    assert config.websocket_proxy == websocket_proxy


@pytest.mark.parametrize(
    "environment",
    (
        {"OKX_NETWORK_MODE": "unexpected"},
        {"OKX_NETWORK_MODE": "proxy"},
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "socks5://127.0.0.1:7890"},
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:bad"},
        {"OKX_NETWORK_MODE": "direct", "OKX_PROXY_URL": "http://127.0.0.1:7890"},
    ),
)
def test_network_configuration_rejects_ambiguous_or_unsupported_proxy(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        NetworkConfiguration.from_environment(environment)


def test_proxy_url_is_redacted_before_it_can_be_logged() -> None:
    config = NetworkConfiguration.from_environment(
        {
            "OKX_NETWORK_MODE": "proxy",
            "OKX_PROXY_URL": "http://user:password@127.0.0.1:7890/path",
        }
    )
    assert config.redacted_proxy_url == "http://***@127.0.0.1:7890/path"
    assert "password" not in config.redacted_proxy_url


@pytest.mark.parametrize(
    ("environment", "expected"),
    (
        ({"OKX_NETWORK_MODE": "direct"}, {"trust_env": False}),
        ({"OKX_NETWORK_MODE": "env"}, {"trust_env": True}),
        (
            {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"},
            {"trust_env": False, "proxy": "http://127.0.0.1:7890"},
        ),
    ),
)
def test_http_client_modes_keep_tls_verification_enabled(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], expected: dict[str, object]
) -> None:
    received: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            received.update(kwargs)

    monkeypatch.setattr("app.market.network.httpx.Client", _Client)
    config = NetworkConfiguration.from_environment(environment)
    client = config.create_http_client(timeout=httpx.Timeout(1))
    assert isinstance(client, _Client)
    assert {key: received[key] for key in expected} == expected
    assert received.get("verify", True) is True


def test_proxy_listener_absence_is_reported_without_network_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )

    def denied(*_: object, **__: object) -> object:
        raise OSError("listener unavailable")

    monkeypatch.setattr("app.market.network.socket.create_connection", denied)
    assert not config.probe_proxy_listener()


def test_proxy_listener_success_uses_configured_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    config = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )
    received: dict[str, object] = {}

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def connected(address: object, timeout: object) -> _Connection:
        received["address"] = address
        received["timeout"] = timeout
        return _Connection()

    monkeypatch.setattr("app.market.network.socket.create_connection", connected)
    assert config.probe_proxy_listener(timeout_seconds=3)
    assert received == {"address": ("127.0.0.1", 7890), "timeout": 3}


def test_proxy_rest_success_keeps_public_endpoint_contract() -> None:
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )

    class _Transport:
        def get(self, *_: object, **__: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [["1767225600000", "100", "101", "99", "100", "1", "0", "0", "1"]],
                },
            )

    provider = OKXPublicHistoricalDataProvider(_Transport(), network=network)
    candles = provider.get_historical_bars("BTC-USDT", "1h", limit=1)
    assert len(candles) == 1
    assert provider.network is network


def test_proxy_rest_failure_fails_closed_without_direct_fallback() -> None:
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )

    class _Transport:
        def get(self, *_: object, **__: object) -> httpx.Response:
            raise httpx.ConnectError("proxy failed")

    provider = OKXPublicHistoricalDataProvider(_Transport(), network=network)
    with pytest.raises(NetworkError, match="public historical candle request failed"):
        provider.get_historical_bars("BTC-USDT", "1h", limit=1)


def test_public_websocket_uses_explicit_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    @asynccontextmanager
    async def connection(_url: str) -> Any:
        yield _Socket()

    def factory(url: str, *, proxy: str | bool | None = True) -> Any:
        received["url"] = url
        received["proxy"] = proxy
        return connection(url)

    monkeypatch.setattr("app.market.websocket.default_websocket_connection", factory)
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )
    provider = OKXPublicWebSocketProvider(network=network)

    async def use_factory() -> None:
        async with provider.connection_factory(provider.url):
            return None

    asyncio.run(use_factory())
    assert received == {"url": provider.url, "proxy": "http://127.0.0.1:7890"}


def test_private_websocket_uses_the_same_explicit_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    @asynccontextmanager
    async def connection(_url: str) -> Any:
        yield _Socket()

    def factory(url: str, *, proxy: str | bool | None = True) -> Any:
        received["url"] = url
        received["proxy"] = proxy
        return connection(url)

    monkeypatch.setattr("app.market.private_websocket.default_websocket_connection", factory)
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("api-key"),
        okx_secret_key=SecretStr("secret"),
        okx_passphrase=SecretStr("passphrase"),
    )
    provider = OKXPrivateWebSocketProvider(settings, network=network)

    async def use_factory() -> None:
        async with provider.connection_factory(provider.demo_url):
            return None

    asyncio.run(use_factory())
    assert received == {"url": provider.demo_url, "proxy": "http://127.0.0.1:7890"}


def test_private_rest_client_uses_the_same_explicit_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            received.update(kwargs)

    monkeypatch.setattr("app.market.network.httpx.Client", _Client)
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("api-key"),
        okx_secret_key=SecretStr("secret"),
        okx_passphrase=SecretStr("passphrase"),
    )

    client = OkxClient(settings, network=network)

    assert isinstance(client.client, _Client)
    assert client.network is network
    assert received["proxy"] == "http://127.0.0.1:7890"
    assert received["trust_env"] is False
    assert received.get("verify", True) is True


def test_proxy_websocket_success_stays_on_public_subscription_path() -> None:
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    socket = _Socket(
        [
            json.dumps({"event": "subscribe"}),
            json.dumps(
                {
                    "arg": {"channel": "candle1H", "instId": "BTC-USDT"},
                    "data": [
                        [
                            str(timestamp),
                            "100",
                            "101",
                            "99",
                            "100",
                            "1",
                            "0",
                            "0",
                            "0",
                        ]
                    ],
                }
            ),
        ]
    )

    @asynccontextmanager
    async def factory(_url: str) -> Any:
        yield socket

    provider = OKXPublicWebSocketProvider(network=network, connection_factory=factory)

    async def collect() -> None:
        async for event in provider.stream_events("BTC-USDT", "1h"):
            if event.event_type.value == "candle":
                await provider.stop()

    asyncio.run(collect())
    assert provider.connection_count == provider.subscription_count == 1
    assert provider.unsubscription_count == 1
    assert all(
        '"op":"subscribe"' in message or '"op":"unsubscribe"' in message for message in socket.sent
    )


def test_proxy_websocket_failure_fails_closed_without_direct_fallback() -> None:
    network = NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )

    @asynccontextmanager
    async def failed_factory(_url: str) -> Any:
        raise ConnectionError("proxy websocket failed")
        yield _Socket([])

    provider = OKXPublicWebSocketProvider(
        network=network,
        connection_factory=failed_factory,
        max_reconnect_attempts=0,
        base_reconnect_delay_seconds=0,
    )

    async def collect() -> None:
        async for _ in provider.stream_events("BTC-USDT", "1h"):
            return None

    with pytest.raises(MarketDataError, match="reconnect limit exceeded"):
        asyncio.run(collect())


class _Socket(WebSocketLike):
    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self.messages:
            await asyncio.Future()
        return self.messages.pop(0)

    async def close(self) -> None:
        return None
