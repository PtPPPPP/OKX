"""Read-only Public REST and WebSocket preflight for Continuous VWAP Shadow."""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import asdict, dataclass
from itertools import pairwise

from app.market.network import NetworkConfiguration, NetworkMode
from app.market.okx_public import OKXPublicHistoricalDataProvider
from app.market.websocket import OKXPublicWebSocketProvider, PublicWebSocketEventType


@dataclass(frozen=True, slots=True)
class PublicNetworkPreflightResult:
    network_mode: str
    proxy_configured: bool
    proxy_listener_ready: bool
    direct_okx_tcp_ready: bool
    proxy_okx_rest_ready: bool
    proxy_okx_ws_ready: bool
    public_rest_calls: int
    public_ws_connections: int
    subscriptions: int
    unsubscriptions: int
    live_events_received: int
    unconfirmed_events_received: int
    confirmed_history_bars: int
    canonical_parse: bool
    strict_chronology: bool
    vwap_bootstrap_ready: bool
    closed_cleanly: bool


def _direct_tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


async def _websocket_preflight(network: NetworkConfiguration) -> dict[str, int | bool]:
    stream = OKXPublicWebSocketProvider(network=network, stale_after_seconds=7200)
    async with asyncio.timeout(15):
        async for event in stream.stream_events("BTC-USDT", "1h"):
            if event.event_type is PublicWebSocketEventType.CANDLE:
                await stream.stop()
            elif event.event_type is PublicWebSocketEventType.CLOSED:
                break
    return {
        "proxy_okx_ws_ready": stream.connection_count == 1
        and stream.subscription_count == 1
        and stream.live_event_count > 0
        and stream.unsubscription_count == 1
        and stream.state.value == "stopped",
        "public_ws_connections": stream.connection_count,
        "subscriptions": stream.subscription_count,
        "unsubscriptions": stream.unsubscription_count,
        "live_events_received": stream.live_event_count,
        "unconfirmed_events_received": stream.unconfirmed_event_count,
        "closed_cleanly": stream.state.value == "stopped",
    }


def main() -> None:
    network = NetworkConfiguration.from_environment()
    direct_tcp = _direct_tcp_ready("www.okx.com", 443)
    history = OKXPublicHistoricalDataProvider(network=network)
    try:
        candles = history.get_historical_bars("BTC-USDT", "1h", limit=30)
        rest_ready = (
            history.public_rest_calls == 1
            and len(candles) >= 29
            and all(candle.confirmed for candle in candles)
            and all(left.timestamp < right.timestamp for left, right in pairwise(candles))
        )
        websocket = asyncio.run(_websocket_preflight(network)) if rest_ready else {}
        result = PublicNetworkPreflightResult(
            network_mode=network.mode.value,
            proxy_configured=network.proxy_url is not None,
            proxy_listener_ready=network.probe_proxy_listener()
            if network.mode is NetworkMode.PROXY
            else False,
            direct_okx_tcp_ready=direct_tcp,
            proxy_okx_rest_ready=rest_ready,
            proxy_okx_ws_ready=bool(websocket.get("proxy_okx_ws_ready", False)),
            public_rest_calls=history.public_rest_calls,
            public_ws_connections=int(websocket.get("public_ws_connections", 0)),
            subscriptions=int(websocket.get("subscriptions", 0)),
            unsubscriptions=int(websocket.get("unsubscriptions", 0)),
            live_events_received=int(websocket.get("live_events_received", 0)),
            unconfirmed_events_received=int(websocket.get("unconfirmed_events_received", 0)),
            confirmed_history_bars=len(candles),
            canonical_parse=all(candle.confirmed for candle in candles),
            strict_chronology=all(
                left.timestamp < right.timestamp for left, right in pairwise(candles)
            ),
            vwap_bootstrap_ready=len(candles) >= 29,
            closed_cleanly=bool(websocket.get("closed_cleanly", False)),
        )
    finally:
        history.close()
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
