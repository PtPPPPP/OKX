from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, cast

from websockets.asyncio.client import connect

from app.domain.market import Candle
from app.exchange.okx_models import parse_candle
from app.market.historical_data import BAR_INTERVALS, MarketDataError, normalize_candles, okx_bar
from app.market.network import NetworkConfiguration
from app.market.providers import MarketDataProvider


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class PublicWebSocketEventType(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTED = "reconnected"
    CANDLE = "candle"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PublicWebSocketEvent:
    """One public socket lifecycle or market-data event."""

    event_type: PublicWebSocketEventType
    generation: int
    candle: Candle | None = None


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]


@asynccontextmanager
async def default_websocket_connection(
    url: str, *, proxy: str | Literal[True] | None = True
) -> AsyncIterator[WebSocketLike]:
    async with connect(
        url,
        proxy=proxy,
        ping_interval=None,
        open_timeout=10,
        close_timeout=5,
        max_size=2**20,
    ) as socket:
        yield cast(WebSocketLike, socket)


class OKXPublicWebSocketProvider:
    """Streams confirmed OKX spot candles; it has no private or order capability."""

    url = "wss://ws.okx.com:8443/ws/v5/business"

    def __init__(
        self,
        *,
        gap_provider: MarketDataProvider | None = None,
        connection_factory: ConnectionFactory | None = None,
        network: NetworkConfiguration | None = None,
        heartbeat_seconds: float = 20,
        pong_timeout_seconds: float = 10,
        stale_after_seconds: int = 120,
        max_reconnect_attempts: int = 5,
        base_reconnect_delay_seconds: float = 1,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        self.gap_provider = gap_provider
        self.network = network or NetworkConfiguration.from_environment()
        self.connection_factory = connection_factory or (
            lambda url: default_websocket_connection(url, proxy=self.network.websocket_proxy)
        )
        self.heartbeat_seconds = heartbeat_seconds
        self.pong_timeout_seconds = pong_timeout_seconds
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_reconnect_delay_seconds = base_reconnect_delay_seconds
        if shutdown_timeout_seconds < 0:
            raise ValueError("WebSocket shutdown timeout cannot be negative")
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.state = ConnectionState.DISCONNECTED
        self.last_message_at: datetime | None = None
        self.last_candle_at: datetime | None = None
        self.reconnect_count = 0
        self.connection_count = 0
        self.subscription_count = 0
        self.unsubscription_count = 0
        self.live_event_count = 0
        self.unconfirmed_event_count = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.connected_once = False
        self.last_error: str | None = None
        self._stopping = asyncio.Event()
        self._active: WebSocketLike | None = None
        self._active_subscription: tuple[str, str] | None = None

    async def stream_confirmed_candles(self, instrument_id: str, bar: str) -> AsyncIterator[Candle]:
        """Compatibility candle stream for existing non-Shadow consumers."""
        interval = BAR_INTERVALS.get(bar.lower())
        if interval is None:
            raise MarketDataError(f"不支持的 K 线周期: {bar}")
        channel = f"candle{okx_bar(bar)}"
        attempt = 0
        while not self._stopping.is_set():
            self.state = (
                ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RECONNECTING
            )
            try:
                async with self.connection_factory(self.url) as socket:
                    self._active = socket
                    await self._subscribe(socket, channel, instrument_id)
                    self._active_subscription = (channel, instrument_id)
                    self.connection_count += 1
                    self.subscription_count += 1
                    self.state = ConnectionState.CONNECTED
                    self.connected_once = True
                    self.last_error = None
                    attempt = 0
                    while not self._stopping.is_set():
                        message = await self._receive(socket)
                        if message is None:
                            continue
                        for candle in self._parse_candles(message, bar):
                            self.live_event_count += 1
                            self.unconfirmed_event_count += int(not candle.confirmed)
                            if not candle.confirmed:
                                continue
                            if self._is_stale(candle, interval):
                                self.state = ConnectionState.BLOCKED
                                raise MarketDataError("WebSocket 已确认 K 线过期")
                            if self.last_candle_at is not None:
                                if candle.timestamp == self.last_candle_at:
                                    self.duplicate_count += 1
                                    continue
                                if candle.timestamp < self.last_candle_at:
                                    self.out_of_order_count += 1
                                    continue
                                if candle.timestamp > self.last_candle_at + interval:
                                    for missing in self._fill_gap(
                                        instrument_id, bar, candle.timestamp, interval
                                    ):
                                        self.last_candle_at = missing.timestamp
                                        yield missing
                            self.last_candle_at = candle.timestamp
                            yield candle
            except asyncio.CancelledError:
                self.state = ConnectionState.STOPPED
                raise
            except Exception as exc:
                self._active = None
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._stopping.is_set():
                    break
                attempt += 1
                self.reconnect_count += 1
                if attempt > self.max_reconnect_attempts:
                    self.state = ConnectionState.BLOCKED
                    raise MarketDataError(
                        f"WebSocket 连续重连失败: {type(exc).__name__}: {exc}"
                    ) from exc
                await asyncio.sleep(min(self.base_reconnect_delay_seconds * 2 ** (attempt - 1), 30))
            finally:
                self._active = None
                self._active_subscription = None
        self.state = ConnectionState.STOPPED

    async def stream_events(
        self, instrument_id: str, bar: str
    ) -> AsyncIterator[PublicWebSocketEvent]:
        """Emit subscription lifecycle and candle events without REST reconciliation."""
        interval = BAR_INTERVALS.get(bar.lower())
        if interval is None:
            raise MarketDataError(f"unsupported candle interval: {bar}")
        channel = f"candle{okx_bar(bar)}"
        attempt = 0
        generation = 0
        subscribed_once = False
        while not self._stopping.is_set():
            self.state = (
                ConnectionState.CONNECTING if not subscribed_once else ConnectionState.RECONNECTING
            )
            subscribed = False
            try:
                async with self.connection_factory(self.url) as socket:
                    self._active = socket
                    await self._subscribe(socket, channel, instrument_id)
                    self._active_subscription = (channel, instrument_id)
                    self.connection_count += 1
                    self.subscription_count += 1
                    subscribed = True
                    generation += 1
                    self.state = ConnectionState.CONNECTED
                    self.connected_once = True
                    self.last_error = None
                    attempt = 0
                    event_type = (
                        PublicWebSocketEventType.RECONNECTED
                        if subscribed_once
                        else PublicWebSocketEventType.CONNECTED
                    )
                    subscribed_once = True
                    yield PublicWebSocketEvent(event_type, generation)
                    while not self._stopping.is_set():
                        message = await self._receive(socket)
                        if message is None:
                            continue
                        for candle in self._parse_candles(message, bar):
                            self.live_event_count += 1
                            self.unconfirmed_event_count += int(not candle.confirmed)
                            if candle.confirmed and self._is_stale(candle, interval):
                                self.state = ConnectionState.BLOCKED
                                raise MarketDataError("confirmed WebSocket candle is stale")
                            yield PublicWebSocketEvent(
                                PublicWebSocketEventType.CANDLE, generation, candle
                            )
            except asyncio.CancelledError:
                self.state = ConnectionState.STOPPED
                raise
            except Exception as exc:
                self._active = None
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._stopping.is_set():
                    break
                if subscribed:
                    self.state = ConnectionState.DISCONNECTED
                    yield PublicWebSocketEvent(PublicWebSocketEventType.DISCONNECTED, generation)
                attempt += 1
                self.reconnect_count += 1
                if attempt > self.max_reconnect_attempts:
                    self.state = ConnectionState.BLOCKED
                    raise MarketDataError(
                        f"WebSocket reconnect limit exceeded: {type(exc).__name__}: {exc}"
                    ) from exc
                await asyncio.sleep(min(self.base_reconnect_delay_seconds * 2 ** (attempt - 1), 30))
            finally:
                self._active = None
                self._active_subscription = None
        self.state = ConnectionState.STOPPED
        yield PublicWebSocketEvent(PublicWebSocketEventType.CLOSED, generation)

    async def stop(self) -> None:
        self._stopping.set()
        shutdown_error: MarketDataError | None = None
        if self._active is not None:
            try:
                if self._active_subscription is not None:
                    try:
                        await asyncio.wait_for(
                            self._unsubscribe(self._active, *self._active_subscription),
                            timeout=self.shutdown_timeout_seconds,
                        )
                    except TimeoutError:
                        shutdown_error = MarketDataError("WebSocket unsubscribe timed out")
                    except Exception as exc:
                        shutdown_error = MarketDataError(
                            f"WebSocket unsubscribe failed: {type(exc).__name__}: {exc}"
                        )
            finally:
                try:
                    await asyncio.wait_for(
                        self._active.close(), timeout=self.shutdown_timeout_seconds
                    )
                except TimeoutError:
                    if shutdown_error is None:
                        shutdown_error = MarketDataError("WebSocket close timed out")
                except Exception as exc:
                    if shutdown_error is None:
                        shutdown_error = MarketDataError(
                            f"WebSocket close failed: {type(exc).__name__}: {exc}"
                        )
                finally:
                    self._active = None
                    self._active_subscription = None
        self.state = ConnectionState.STOPPED
        if shutdown_error is not None:
            raise shutdown_error

    async def _subscribe(self, socket: WebSocketLike, channel: str, instrument_id: str) -> None:
        await socket.send(
            json.dumps(
                {
                    "id": "observecandles",
                    "op": "subscribe",
                    "args": [{"channel": channel, "instId": instrument_id}],
                },
                separators=(",", ":"),
            )
        )
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MarketDataError("OKX WebSocket 未确认订阅")
            raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            payload = json.loads(_decode_message(raw))
            if payload.get("event") == "error":
                raise MarketDataError(
                    f"OKX WebSocket 订阅失败: {payload.get('code')}: {payload.get('msg')}"
                )
            if payload.get("event") == "subscribe":
                return

    async def _unsubscribe(self, socket: WebSocketLike, channel: str, instrument_id: str) -> None:
        await socket.send(
            json.dumps(
                {
                    "id": "observecandles",
                    "op": "unsubscribe",
                    "args": [{"channel": channel, "instId": instrument_id}],
                },
                separators=(",", ":"),
            )
        )
        self.unsubscription_count += 1

    async def _receive(self, socket: WebSocketLike) -> str | None:
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=self.heartbeat_seconds)
        except TimeoutError:
            await socket.send("ping")
            pong = await asyncio.wait_for(socket.recv(), timeout=self.pong_timeout_seconds)
            if _decode_message(pong) != "pong":
                raise MarketDataError("OKX WebSocket 心跳响应无效") from None
            self.last_message_at = datetime.now(UTC)
            return None
        self.last_message_at = datetime.now(UTC)
        return _decode_message(raw)

    @staticmethod
    def _parse_candles(message: str, bar: str) -> list[Candle]:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            raise MarketDataError("OKX WebSocket 返回无效 JSON") from exc
        if payload.get("event") == "error":
            raise MarketDataError(
                f"OKX WebSocket 错误: {payload.get('code')}: {payload.get('msg')}"
            )
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise MarketDataError("OKX WebSocket K 线结构无效")
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, list) or not all(isinstance(item, str) for item in row):
                raise MarketDataError("OKX WebSocket K 线字段无效")
            candle = parse_candle(row)
            candles.extend(normalize_candles([candle], bar=bar))
        return candles

    def _fill_gap(
        self,
        instrument_id: str,
        bar: str,
        current_timestamp: datetime,
        interval: timedelta,
    ) -> list[Candle]:
        if self.gap_provider is None or self.last_candle_at is None:
            self.state = ConnectionState.DEGRADED
            raise MarketDataError("WebSocket K 线出现缺口且没有补数数据源")
        gap_count = (
            int(
                (current_timestamp - self.last_candle_at).total_seconds()
                // interval.total_seconds()
            )
            - 1
        )
        if gap_count <= 0:
            return []
        if gap_count > 300:
            raise MarketDataError("WebSocket K 线缺口超过 300 根，拒绝继续")
        candidates = self.gap_provider.get_historical_bars(instrument_id, bar, limit=gap_count + 2)
        missing = [
            candle
            for candle in candidates
            if candle.confirmed and self.last_candle_at < candle.timestamp < current_timestamp
        ]
        expected = [self.last_candle_at + interval * index for index in range(1, gap_count + 1)]
        if [candle.timestamp for candle in missing] != expected:
            self.state = ConnectionState.DEGRADED
            raise MarketDataError("WebSocket K 线缺口补数不完整")
        return missing

    def _is_stale(self, candle: Candle, interval: timedelta) -> bool:
        return datetime.now(UTC) - (candle.timestamp + interval) > self.stale_after


def _decode_message(raw: str | bytes) -> str:
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw
