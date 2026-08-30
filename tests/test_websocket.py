from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.client import ClientProtocol
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close
from websockets.uri import parse_uri

from app.market.websocket import (
    ConnectionState,
    HTTPProxyClientConnection,
    OKXPublicWebSocketProvider,
    PublicWebSocketEventType,
    WebSocketLike,
    default_websocket_connection,
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


class ClosingSocket(FakeSocket):
    def __init__(self, messages: list[str], error: Exception) -> None:
        super().__init__(messages)
        self.error = error

    async def recv(self) -> str | bytes:
        if not self.messages:
            raise self.error
        return self.messages.pop(0)


def _closing_factory(
    sockets: list[FakeSocket],
) -> tuple[list[FakeSocket], object]:
    all_sockets = list(sockets)

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        socket = sockets.pop(0)
        try:
            yield socket
        finally:
            await socket.close()

    return all_sockets, factory


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


def test_upstream_client_reproduces_pre_connection_loss_regression() -> None:
    async def exercise() -> None:
        connection = ClientConnection(ClientProtocol(parse_uri("wss://example.com")))
        try:
            connection.connection_lost(ConnectionResetError("pre-connection reset"))
        except AttributeError as error:
            assert "recv_messages" in str(error)
        else:
            raise AssertionError("upstream regression is no longer reproducible")

    asyncio.run(exercise())


def test_http_proxy_connection_adapter_ignores_only_pre_connection_loss() -> None:
    async def exercise() -> None:
        connection = HTTPProxyClientConnection(ClientProtocol(parse_uri("wss://example.com")))
        connection.connection_lost(ConnectionResetError("TLS reset before connection_made"))
        assert not connection.connection_established

    asyncio.run(exercise())


def test_http_proxy_tls_reset_does_not_raise_internal_callback_error() -> None:
    async def exercise() -> list[BaseException]:
        callback_errors: list[BaseException] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()

        def capture(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            error = context.get("exception")
            if isinstance(error, BaseException):
                callback_errors.append(error)

        async def reset_after_connect(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            writer.transport.abort()

        server = await asyncio.start_server(reset_after_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loop.set_exception_handler(capture)
        try:
            with pytest.raises((ConnectionResetError, OSError, TimeoutError)):
                async with default_websocket_connection(
                    "wss://example.com", proxy=f"http://127.0.0.1:{port}"
                ):
                    raise AssertionError("proxy TLS reset unexpectedly connected")
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
            server.close()
            await server.wait_closed()
        return callback_errors

    errors = asyncio.run(exercise())
    assert not [error for error in errors if isinstance(error, AttributeError)]


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


def test_public_websocket_reconnects_after_clean_remote_close() -> None:
    current = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    sockets = [
        ClosingSocket(
            [json.dumps({"event": "subscribe"})],
            ConnectionClosedOK(Close(1000, "normal closure"), Close(1000, ""), True),
        ),
        FakeSocket([json.dumps({"event": "subscribe"}), _data(_row(current))]),
    ]
    all_sockets, factory = _closing_factory(sockets)
    provider = OKXPublicWebSocketProvider(
        connection_factory=factory, stale_after_seconds=7200, base_reconnect_delay_seconds=0
    )

    async def exercise() -> list[PublicWebSocketEventType]:
        events: list[PublicWebSocketEventType] = []
        async for event in provider.stream_events("BTC-USDT", "1h"):
            events.append(event.event_type)
            if event.event_type is PublicWebSocketEventType.CANDLE:
                await provider.stop()
        return events

    assert asyncio.run(exercise()) == [
        PublicWebSocketEventType.CONNECTED,
        PublicWebSocketEventType.DISCONNECTED,
        PublicWebSocketEventType.RECONNECTED,
        PublicWebSocketEventType.CANDLE,
        PublicWebSocketEventType.CLOSED,
    ]
    assert provider.reconnect_count == 1
    assert all(socket.closed for socket in all_sockets)


def test_public_websocket_reconnects_after_connection_reset() -> None:
    current = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    sockets = [
        ClosingSocket(
            [json.dumps({"event": "subscribe"})], ConnectionResetError("transport reset")
        ),
        FakeSocket([json.dumps({"event": "subscribe"}), _data(_row(current))]),
    ]
    _, factory = _closing_factory(sockets)
    provider = OKXPublicWebSocketProvider(
        connection_factory=factory, stale_after_seconds=7200, base_reconnect_delay_seconds=0
    )

    async def exercise() -> None:
        async for event in provider.stream_events("BTC-USDT", "1h"):
            if event.event_type is PublicWebSocketEventType.CANDLE:
                await provider.stop()

    asyncio.run(exercise())
    assert provider.connection_count == 2
    assert provider.reconnect_count == 1
    assert provider.last_error is None


def test_public_websocket_unknown_code_error_fails_closed_without_retry() -> None:
    attempts = 0

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        nonlocal attempts
        attempts += 1
        raise AttributeError("fixture code defect")
        yield FakeSocket([])  # pragma: no cover

    provider = OKXPublicWebSocketProvider(
        connection_factory=factory, base_reconnect_delay_seconds=0
    )

    async def exercise() -> None:
        async for _event in provider.stream_events("BTC-USDT", "1h"):
            raise AssertionError("unexpected event")

    with pytest.raises(AttributeError, match="fixture code defect"):
        asyncio.run(exercise())
    assert attempts == 1
    assert provider.reconnect_count == 0
    assert provider.state is ConnectionState.BLOCKED


def test_public_websocket_survives_ten_reconnect_cycles_without_task_leak() -> None:
    current = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    sockets: list[FakeSocket] = [
        ClosingSocket([json.dumps({"event": "subscribe"})], ConnectionResetError(f"reset-{index}"))
        for index in range(10)
    ]
    sockets.append(FakeSocket([json.dumps({"event": "subscribe"}), _data(_row(current))]))
    all_sockets, factory = _closing_factory(sockets)
    provider = OKXPublicWebSocketProvider(
        connection_factory=factory, stale_after_seconds=7200, base_reconnect_delay_seconds=0
    )

    async def exercise() -> int:
        async for event in provider.stream_events("BTC-USDT", "1h"):
            if event.event_type is PublicWebSocketEventType.CANDLE:
                await provider.stop()
        return sum(
            task is not asyncio.current_task() and not task.done() for task in asyncio.all_tasks()
        )

    assert asyncio.run(exercise()) == 0
    assert provider.connection_count == 11
    assert provider.reconnect_count == 10
    assert all(socket.closed for socket in all_sockets)


def test_public_websocket_shutdown_closes_a_pending_receive() -> None:
    class PendingSocket(FakeSocket):
        def __init__(self) -> None:
            super().__init__([json.dumps({"event": "subscribe"})])
            self.receiving = asyncio.Event()
            self.closed_event = asyncio.Event()

        async def recv(self) -> str | bytes:
            if self.messages:
                return self.messages.pop(0)
            self.receiving.set()
            await self.closed_event.wait()
            raise ConnectionResetError("closed while recv pending")

        async def close(self) -> None:
            self.closed = True
            self.closed_event.set()

    socket = PendingSocket()

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    provider = OKXPublicWebSocketProvider(connection_factory=factory)

    async def exercise() -> PublicWebSocketEventType:
        events = provider.stream_events("BTC-USDT", "1h")
        assert (await anext(events)).event_type is PublicWebSocketEventType.CONNECTED
        pending = asyncio.create_task(anext(events))
        await socket.receiving.wait()
        await provider.stop()
        closed = await asyncio.wait_for(pending, timeout=1)
        await events.aclose()
        return closed.event_type

    assert asyncio.run(exercise()) is PublicWebSocketEventType.CLOSED
    assert provider.state is ConnectionState.STOPPED


def test_public_websocket_shutdown_interrupts_reconnect_backoff() -> None:
    socket = ClosingSocket(
        [json.dumps({"event": "subscribe"})], ConnectionResetError("fixture reset")
    )

    @asynccontextmanager
    async def factory(_url: str) -> AsyncIterator[WebSocketLike]:
        yield socket

    provider = OKXPublicWebSocketProvider(
        connection_factory=factory, base_reconnect_delay_seconds=10
    )

    async def exercise() -> PublicWebSocketEventType:
        events = provider.stream_events("BTC-USDT", "1h")
        assert (await anext(events)).event_type is PublicWebSocketEventType.CONNECTED
        assert (await anext(events)).event_type is PublicWebSocketEventType.DISCONNECTED
        await provider.stop()
        closed = await asyncio.wait_for(anext(events), timeout=1)
        await events.aclose()
        return closed.event_type

    assert asyncio.run(exercise()) is PublicWebSocketEventType.CLOSED
    assert provider.state is ConnectionState.STOPPED
