from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from app.config.settings import Settings, TradingMode
from app.domain.order import OrderState
from app.exchange.okx_models import parse_candle, parse_instrument, parse_order
from app.market.historical_data import MarketDataError
from app.market.private_websocket import (
    OKXPrivateEventAdapter,
    OKXPrivateWebSocketProvider,
    PrivateEvent,
    PrivateEventKind,
)
from app.market.websocket import WebSocketLike

FIXTURE_PATH = Path("tests/fixtures/okx/contracts.json")


def fixtures() -> dict[str, object]:
    return cast(dict[str, object], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def test_rest_contract_fixtures_parse() -> None:
    payload = fixtures()
    instrument = parse_instrument(payload["instrument"]["data"][0])  # type: ignore[index]
    candle = parse_candle(payload["candle_history"]["data"][0])  # type: ignore[index]
    partial = parse_order(payload["partial_fill"])  # type: ignore[arg-type]
    assert instrument.instrument_id == "BTC-USDT"
    assert candle.confirmed
    assert partial.state is OrderState.PARTIALLY_FILLED
    assert str(partial.filled_quantity) == "0.0004"


def test_private_websocket_contracts_have_stable_keys() -> None:
    payload = fixtures()
    message = json.dumps(payload["ws_order_partial"])
    first = OKXPrivateEventAdapter.parse(message)[0]
    second = OKXPrivateEventAdapter.parse(message)[0]
    account = OKXPrivateEventAdapter.parse(json.dumps(payload["ws_account"]))[0]
    position = OKXPrivateEventAdapter.parse(json.dumps(payload["ws_position"]))[0]
    assert first.kind is PrivateEventKind.ORDER
    assert first.order is not None
    assert first.order.state is OrderState.PARTIALLY_FILLED
    assert first.idempotency_key == second.idempotency_key
    assert account.kind is PrivateEventKind.ACCOUNT
    assert position.kind is PrivateEventKind.POSITION


def test_private_login_contract_does_not_expose_secret() -> None:
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("api-key"),
        okx_secret_key=SecretStr("never-emit-this-secret"),
        okx_passphrase=SecretStr("passphrase"),
    )
    message = OKXPrivateEventAdapter.login_message(settings, datetime(2026, 1, 1, tzinfo=UTC))
    assert "never-emit-this-secret" not in message
    assert json.loads(message)["op"] == "login"


class FakePrivateSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self.messages:
            await asyncio.Future()
        return self.messages.pop(0)

    async def close(self) -> None:
        return None


def test_private_websocket_provider_login_subscribe_and_parse() -> None:
    fixture = fixtures()
    socket = FakePrivateSocket(
        [
            json.dumps({"event": "login", "code": "0"}),
            json.dumps({"event": "subscribe", "arg": {"channel": "orders"}}),
            json.dumps({"event": "subscribe", "arg": {"channel": "account"}}),
            json.dumps({"event": "subscribe", "arg": {"channel": "balance_and_position"}}),
            json.dumps(fixture["ws_order_partial"]),
        ]
    )

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    settings = Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("api-key"),
        okx_secret_key=SecretStr("secret"),
        okx_passphrase=SecretStr("passphrase"),
    )
    provider = OKXPrivateWebSocketProvider(
        settings,
        connection_factory=factory,
        base_reconnect_delay_seconds=0,
    )

    async def collect_one() -> PrivateEvent:
        async for event in provider.stream_events():
            assert provider.is_ready
            await provider.stop()
            return event
        raise AssertionError("没有收到私有事件")

    event = asyncio.run(collect_one())
    assert event.kind is PrivateEventKind.ORDER
    assert json.loads(socket.sent[0])["op"] == "login"
    assert json.loads(socket.sent[1])["op"] == "subscribe"
    health = provider.health
    assert health.connect_attempts == health.connections == 1
    assert health.tls_ready and health.handshake_ready
    assert health.login_sent and health.subscribe_sent
    assert health.events_received == 1
    assert health.closed_cleanly
    assert health.failure_stage is None
    assert health.failure_type is None


def test_private_websocket_heartbeat_timeout_blocks_after_bounded_reconnects() -> None:
    socket = FakePrivateSocket(
        [
            json.dumps({"event": "login", "code": "0"}),
            json.dumps({"event": "subscribe", "arg": {"channel": "orders"}}),
            json.dumps({"event": "subscribe", "arg": {"channel": "account"}}),
            json.dumps({"event": "subscribe", "arg": {"channel": "balance_and_position"}}),
        ]
    )

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    provider = OKXPrivateWebSocketProvider(
        Settings(
            trading_mode=TradingMode.DEMO,
            okx_api_key=SecretStr("api-key"),
            okx_secret_key=SecretStr("secret"),
            okx_passphrase=SecretStr("passphrase"),
        ),
        connection_factory=factory,
        heartbeat_seconds=0.005,
        pong_timeout_seconds=0.005,
        max_reconnect_attempts=0,
        base_reconnect_delay_seconds=0,
    )

    async def exhaust() -> None:
        async for _ in provider.stream_events():
            raise AssertionError("timeout stream must not emit an event")

    with pytest.raises(MarketDataError, match="连续重连失败"):
        asyncio.run(exhaust())
    assert provider.reconnect_count == 1
    assert socket.sent[-1] == "ping"
