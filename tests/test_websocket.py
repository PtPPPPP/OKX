from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

from app.market.websocket import (
    ConnectionState,
    OKXPublicWebSocketProvider,
    PublicWebSocketEventType,
    WebSocketLike,
)


class FakeSocket:
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


def _row(timestamp: datetime, confirmed: bool = True) -> list[str]:
    return [
        str(int(timestamp.timestamp() * 1000)),
        "100",
        "101",
        "99",
        "100.5",
        "12",
        "1200",
        "1200",
        "1" if confirmed else "0",
    ]


def _data(row: list[str]) -> str:
    return json.dumps({"arg": {"channel": "candle5m", "instId": "BTC-USDT"}, "data": [row]})


def test_public_websocket_filters_and_deduplicates() -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    minute = now.minute - now.minute % 5
    current = now.replace(minute=minute) - timedelta(minutes=10)
    following = current + timedelta(minutes=5)
    socket = FakeSocket(
        [
            json.dumps({"event": "subscribe"}),
            _data(_row(current, confirmed=False)),
            _data(_row(current)),
            _data(_row(current)),
            _data(_row(current - timedelta(minutes=5))),
            _data(_row(following)),
        ]
    )

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    provider = OKXPublicWebSocketProvider(
        connection_factory=factory,
        stale_after_seconds=3600,
        base_reconnect_delay_seconds=0,
    )

    async def collect() -> list[datetime]:
        timestamps: list[datetime] = []
        async for candle in provider.stream_confirmed_candles("BTC-USDT", "5m"):
            timestamps.append(candle.timestamp)
            if len(timestamps) == 2:
                await provider.stop()
        return timestamps

    timestamps = asyncio.run(collect())
    assert timestamps == [current, following]
    assert provider.duplicate_count == 1
    assert provider.out_of_order_count == 1
    assert provider.connection_count == provider.subscription_count == 1
    assert provider.live_event_count == 5
    assert provider.unconfirmed_event_count == 1
    assert provider.state is ConnectionState.STOPPED
    assert '"channel":"candle5m"' in socket.sent[0]


def test_public_websocket_reconnects_after_disconnect() -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    current = now.replace(minute=now.minute - now.minute % 5) - timedelta(minutes=5)
    socket = FakeSocket([json.dumps({"event": "subscribe"}), _data(_row(current))])
    attempts = 0

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("fixture disconnect")
        yield socket

    provider = OKXPublicWebSocketProvider(
        connection_factory=factory,
        stale_after_seconds=3600,
        base_reconnect_delay_seconds=0,
    )

    async def collect_one() -> None:
        async for _candle in provider.stream_confirmed_candles("BTC-USDT", "5m"):
            await provider.stop()

    asyncio.run(collect_one())
    assert attempts == 2
    assert provider.reconnect_count == 1
    assert provider.connection_count == provider.subscription_count == 1


def test_factory_protocol_accepts_async_context_manager() -> None:
    socket = FakeSocket([])

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    context: AbstractAsyncContextManager[WebSocketLike] = factory("wss://example")
    assert context is not None


def test_public_websocket_1h_never_sends_login_or_private_subscription() -> None:
    current = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    socket = FakeSocket([json.dumps({"event": "subscribe"}), _data(_row(current))])

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    provider = OKXPublicWebSocketProvider(
        connection_factory=factory, stale_after_seconds=7200, base_reconnect_delay_seconds=0
    )

    async def collect() -> None:
        async for _ in provider.stream_confirmed_candles("BTC-USDT", "1h"):
            await provider.stop()

    asyncio.run(collect())
    assert socket.sent == [
        '{"id":"observecandles","op":"subscribe","args":[{"channel":"candle1H","instId":"BTC-USDT"}]}',
        '{"id":"observecandles","op":"unsubscribe","args":[{"channel":"candle1H","instId":"BTC-USDT"}]}',
    ]
    assert provider.unsubscription_count == 1


def test_public_websocket_emits_disconnect_and_reconnect_subscription_events() -> None:
    current = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)

    class DisconnectingSocket(FakeSocket):
        async def recv(self) -> str | bytes:
            if not self.messages:
                raise ConnectionError("fixture disconnect")
            return self.messages.pop(0)

    sockets = [
        DisconnectingSocket([json.dumps({"event": "subscribe"}), _data(_row(current))]),
        DisconnectingSocket(
            [
                json.dumps({"event": "subscribe"}),
                _data(_row(current + timedelta(hours=1))),
            ]
        ),
    ]

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield sockets.pop(0)

    provider = OKXPublicWebSocketProvider(
        connection_factory=factory,
        stale_after_seconds=10800,
        base_reconnect_delay_seconds=0,
    )

    async def collect() -> list[PublicWebSocketEventType]:
        event_types: list[PublicWebSocketEventType] = []
        async for event in provider.stream_events("BTC-USDT", "1h"):
            event_types.append(event.event_type)
            if event.event_type is PublicWebSocketEventType.CANDLE and event.generation == 2:
                await provider.stop()
        return event_types

    assert asyncio.run(collect()) == [
        PublicWebSocketEventType.CONNECTED,
        PublicWebSocketEventType.CANDLE,
        PublicWebSocketEventType.DISCONNECTED,
        PublicWebSocketEventType.RECONNECTED,
        PublicWebSocketEventType.CANDLE,
        PublicWebSocketEventType.CLOSED,
    ]
