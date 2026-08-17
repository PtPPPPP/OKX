from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.config.settings import Settings
from app.domain.order import Order
from app.exchange.okx_models import parse_order
from app.market.historical_data import MarketDataError
from app.market.network import NetworkConfiguration
from app.market.websocket import (
    ConnectionFactory,
    ConnectionState,
    WebSocketLike,
    _decode_message,
    default_websocket_connection,
)


class PrivateEventKind(StrEnum):
    CONNECTION = "connection"
    ORDER = "order"
    ACCOUNT = "account"
    POSITION = "position"


@dataclass(frozen=True, slots=True)
class PrivateStreamHealth:
    connected: bool
    authenticated: bool
    subscriptions_ready: bool
    last_message_at: datetime | None
    last_account_message_at: datetime | None
    last_order_message_at: datetime | None
    last_position_message_at: datetime | None
    reconnect_count: int
    stale: bool
    error: str | None
    connect_attempts: int = 0
    connections: int = 0
    tls_ready: bool = False
    handshake_ready: bool = False
    login_sent: bool = False
    subscribe_sent: bool = False
    events_received: int = 0
    unsubscriptions: int = 0
    closed_cleanly: bool = False
    failure_stage: str | None = None
    failure_type: str | None = None


@dataclass(frozen=True, slots=True)
class PrivateEvent:
    kind: PrivateEventKind
    idempotency_key: str
    payload: dict[str, Any]
    order: Order | None = None
    connection_epoch: int | None = None
    sequence: int | None = None


class OKXPrivateEventAdapter:
    """Authentication and parsing foundation; it never sends trading operations."""

    demo_url = "wss://wspap.okx.com:8443/ws/v5/private"

    @staticmethod
    def login_message(settings: Settings, now: datetime | None = None) -> str:
        settings.require_demo_credentials()
        timestamp = str(int((now or datetime.now(UTC)).timestamp()))
        message = f"{timestamp}GET/users/self/verify"
        signature = base64.b64encode(
            hmac.new(
                settings.okx_secret_key.get_secret_value().encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return json.dumps(
            {
                "op": "login",
                "args": [
                    {
                        "apiKey": settings.okx_api_key.get_secret_value(),
                        "passphrase": settings.okx_passphrase.get_secret_value(),
                        "timestamp": timestamp,
                        "sign": signature,
                    }
                ],
            },
            separators=(",", ":"),
        )

    @staticmethod
    def subscription_message() -> str:
        return json.dumps(
            {
                "op": "subscribe",
                "args": [
                    {"channel": "orders", "instType": "SPOT"},
                    {"channel": "account"},
                    {"channel": "balance_and_position"},
                ],
            },
            separators=(",", ":"),
        )

    @staticmethod
    def parse(message: str) -> tuple[PrivateEvent, ...]:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            raise MarketDataError("OKX 私有 WebSocket 返回无效 JSON") from exc
        if payload.get("event") == "error":
            raise MarketDataError(
                f"OKX 私有 WebSocket 错误: {payload.get('code')}: {payload.get('msg')}"
            )
        argument = payload.get("arg")
        rows = payload.get("data", [])
        if not isinstance(argument, dict) or not isinstance(rows, list):
            return ()
        channel = str(argument.get("channel") or "")
        events: list[PrivateEvent] = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise MarketDataError("OKX 私有 WebSocket 事件结构无效")
            if channel == "orders":
                order = parse_order(raw)
                key = _payload_key(
                    f"order:{order.request.client_order_id}",
                    raw,
                )
                events.append(PrivateEvent(PrivateEventKind.ORDER, key, raw, order=order))
            elif channel == "account":
                events.append(
                    PrivateEvent(
                        PrivateEventKind.ACCOUNT,
                        _payload_key("account", raw),
                        raw,
                    )
                )
            elif channel == "balance_and_position":
                events.append(
                    PrivateEvent(
                        PrivateEventKind.POSITION,
                        _payload_key("position", raw),
                        raw,
                    )
                )
        return tuple(events)


class OKXPrivateWebSocketProvider:
    """Demo-only private state stream. It exposes no order operation."""

    demo_url = OKXPrivateEventAdapter.demo_url

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: ConnectionFactory | None = None,
        network: NetworkConfiguration | None = None,
        heartbeat_seconds: float = 20,
        pong_timeout_seconds: float = 10,
        max_reconnect_attempts: int = 5,
        base_reconnect_delay_seconds: float = 1,
    ) -> None:
        settings.require_demo_credentials()
        self.settings = settings
        self.network = network or NetworkConfiguration.from_environment()
        self.connection_factory = connection_factory or (
            lambda url: default_websocket_connection(url, proxy=self.network.websocket_proxy)
        )
        self.heartbeat_seconds = heartbeat_seconds
        self.pong_timeout_seconds = pong_timeout_seconds
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_reconnect_delay_seconds = base_reconnect_delay_seconds
        self.state = ConnectionState.DISCONNECTED
        self.reconnect_count = 0
        self.last_error: str | None = None
        self._stopping = asyncio.Event()
        self._active: WebSocketLike | None = None
        self._ready = threading.Event()
        self._authenticated = False
        self._subscriptions_ready = False
        self._last_message_at: datetime | None = None
        self._last_account_message_at: datetime | None = None
        self._last_order_message_at: datetime | None = None
        self._last_position_message_at: datetime | None = None
        self._connection_epoch = 0
        self.connect_attempts = 0
        self.connection_count = 0
        self.tls_ready = False
        self.handshake_ready = False
        self.login_sent = False
        self.subscribe_sent = False
        self.events_received = 0
        self.unsubscription_count = 0
        self.closed_cleanly = False
        self.failure_stage: str | None = None
        self.failure_type: str | None = None
        self._active_stage: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.state is ConnectionState.CONNECTED and self._ready.is_set()

    @property
    def health(self) -> PrivateStreamHealth:
        now = datetime.now(UTC)
        stale = (not self.is_ready) or (
            self._last_message_at is not None and (now - self._last_message_at).total_seconds() > 60
        )
        return PrivateStreamHealth(
            self.state is ConnectionState.CONNECTED,
            self._authenticated,
            self._subscriptions_ready,
            self._last_message_at,
            self._last_account_message_at,
            self._last_order_message_at,
            self._last_position_message_at,
            self.reconnect_count,
            stale,
            self.last_error,
            self.connect_attempts,
            self.connection_count,
            self.tls_ready,
            self.handshake_ready,
            self.login_sent,
            self.subscribe_sent,
            self.events_received,
            self.unsubscription_count,
            self.closed_cleanly,
            self.failure_stage,
            self.failure_type,
        )

    def wait_until_ready(self, timeout_seconds: float) -> bool:
        return self._ready.wait(timeout_seconds)

    async def stream_events(self) -> AsyncIterator[PrivateEvent]:
        attempt = 0
        while not self._stopping.is_set():
            self.state = (
                ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RECONNECTING
            )
            try:
                self.connect_attempts += 1
                self._active_stage = "connect"
                async with self.connection_factory(self.demo_url) as socket:
                    self._active = socket
                    self.connection_count += 1
                    self.tls_ready = True
                    self.handshake_ready = True
                    self._active_stage = "login"
                    await socket.send(OKXPrivateEventAdapter.login_message(self.settings))
                    self.login_sent = True
                    self._active_stage = "authentication"
                    await self._expect_event(socket, "login")
                    self._authenticated = True
                    self._active_stage = "subscribe"
                    await socket.send(OKXPrivateEventAdapter.subscription_message())
                    self.subscribe_sent = True
                    self._active_stage = "subscription_acknowledgement"
                    await self._expect_subscriptions(socket)
                    self._subscriptions_ready = True
                    self.state = ConnectionState.CONNECTED
                    self._connection_epoch += 1
                    self.last_error = None
                    if self._connection_epoch > 1:
                        yield PrivateEvent(
                            PrivateEventKind.CONNECTION,
                            f"connection:{self._connection_epoch}",
                            {},
                            connection_epoch=self._connection_epoch,
                        )
                    self._ready.set()
                    attempt = 0
                    self.failure_stage = None
                    self.failure_type = None
                    while not self._stopping.is_set():
                        self._active_stage = "receive"
                        message = await self._receive(socket)
                        if message is None:
                            continue
                        for event in OKXPrivateEventAdapter.parse(message):
                            received_at = datetime.now(UTC)
                            self.events_received += 1
                            self._last_message_at = received_at
                            if event.kind is PrivateEventKind.ACCOUNT:
                                self._last_account_message_at = received_at
                            elif event.kind is PrivateEventKind.ORDER:
                                self._last_order_message_at = received_at
                            else:
                                self._last_position_message_at = received_at
                            yield replace(event, connection_epoch=self._connection_epoch)
            except asyncio.CancelledError:
                self.state = ConnectionState.STOPPED
                raise
            except Exception as exc:
                self._ready.clear()
                self._authenticated = False
                self._subscriptions_ready = False
                if self._stopping.is_set():
                    self.closed_cleanly = True
                    break
                self.failure_stage = self._active_stage
                self.failure_type = type(exc).__name__
                self.last_error = f"{type(exc).__name__}: {exc}"
                attempt += 1
                self.reconnect_count += 1
                if attempt > self.max_reconnect_attempts:
                    self.state = ConnectionState.BLOCKED
                    raise MarketDataError(
                        f"OKX 私有 WebSocket 连续重连失败: {self.last_error}"
                    ) from exc
                await asyncio.sleep(min(self.base_reconnect_delay_seconds * 2 ** (attempt - 1), 30))
            finally:
                self._active = None
        self.state = ConnectionState.STOPPED

    async def stop(self) -> None:
        self._stopping.set()
        self._ready.clear()
        self._authenticated = False
        self._subscriptions_ready = False
        if self._active is not None:
            await self._active.close()
            self.closed_cleanly = True
        self.state = ConnectionState.STOPPED

    @staticmethod
    async def _expect_event(socket: WebSocketLike, expected: str) -> None:
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MarketDataError(f"OKX 私有 WebSocket 未确认 {expected}")
            payload = json.loads(_decode_message(await asyncio.wait_for(socket.recv(), remaining)))
            if payload.get("event") == "error" or str(payload.get("code", "0")) != "0":
                raise MarketDataError(
                    f"OKX 私有 WebSocket {expected} 失败: "
                    f"{payload.get('code')}: {payload.get('msg')}"
                )
            if payload.get("event") == expected:
                return

    @staticmethod
    async def _expect_subscriptions(socket: WebSocketLike) -> None:
        expected = {"orders", "account", "balance_and_position"}
        deadline = asyncio.get_running_loop().time() + 10
        while expected:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MarketDataError("OKX 私有 WebSocket 未确认全部状态订阅")
            payload = json.loads(_decode_message(await asyncio.wait_for(socket.recv(), remaining)))
            if payload.get("event") == "error":
                raise MarketDataError(
                    f"OKX 私有 WebSocket 订阅失败: {payload.get('code')}: {payload.get('msg')}"
                )
            if payload.get("event") == "subscribe":
                argument = payload.get("arg", {})
                if isinstance(argument, dict):
                    expected.discard(str(argument.get("channel") or ""))

    async def _receive(self, socket: WebSocketLike) -> str | None:
        try:
            return _decode_message(await asyncio.wait_for(socket.recv(), self.heartbeat_seconds))
        except TimeoutError:
            await socket.send("ping")
            pong = _decode_message(await asyncio.wait_for(socket.recv(), self.pong_timeout_seconds))
            if pong != "pong":
                raise MarketDataError("OKX 私有 WebSocket 心跳响应无效") from None
            return None


def _payload_key(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}:{digest}"
