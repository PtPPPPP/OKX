"""Bounded, read-only OKX proxy readiness stability audit.

This command intentionally has no bounded-demo, broker, or order dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config.run_config import load_run_config
from app.config.settings import Settings
from app.exchange.okx_client import OkxClient
from app.market.network import NetworkConfiguration, NetworkMode
from app.market.websocket import OKXPublicWebSocketProvider, PublicWebSocketEventType
from app.services.demo_session import DemoTradingSession
from app.storage.database import Database
from app.storage.repositories import TradingRepository


@dataclass(frozen=True, slots=True)
class IterationResult:
    iteration_id: int
    proxy_listener_ready: bool
    public_rest_ready: bool = False
    public_rest_attempts: int = 0
    public_rest_latency_ms: int | None = None
    public_ws_connected: bool = False
    public_ws_subscribed: bool = False
    public_ws_events_received: int = 0
    public_ws_closed_cleanly: bool = False
    private_ws_authenticated: bool = False
    private_ws_subscriptions_ready: bool = False
    private_account_state_received: bool = False
    private_position_state_received: bool = False
    private_state_reconciled: bool = False
    private_clean_shutdown: bool = False
    private_api_write_calls: int = 0
    broker_write_calls: int = 0
    place_order_calls: int = 0
    cancel_order_calls: int = 0
    pending_tasks: int = 0
    failure_stage: str | None = None
    failure_type: str | None = None
    socket_error_code: int | None = None


def _exception_details(error: BaseException) -> tuple[str, int | None]:
    current: BaseException | None = error
    while current is not None:
        code = getattr(current, "winerror", None) or getattr(current, "errno", None)
        if isinstance(code, int):
            return type(current).__name__, code
        current = current.__cause__ or current.__context__
    return type(error).__name__, None


async def _public_websocket_iteration(network: NetworkConfiguration) -> dict[str, Any]:
    stream = OKXPublicWebSocketProvider(
        network=network,
        max_reconnect_attempts=0,
        stale_after_seconds=7200,
    )
    event_received = False
    try:
        async with asyncio.timeout(20):
            async for event in stream.stream_events("BTC-USDT", "1h"):
                if event.event_type is PublicWebSocketEventType.CANDLE:
                    event_received = True
                    await stream.stop()
                if event.event_type is PublicWebSocketEventType.CLOSED:
                    break
    finally:
        await stream.stop()
    return {
        "connected": stream.connection_count == 1,
        "subscribed": stream.subscription_count == 1,
        "events": stream.live_event_count,
        "event_received": event_received,
        "closed_cleanly": stream.state.value == "stopped" and stream.unsubscription_count == 1,
    }


def public_iteration(iteration_id: int, network: NetworkConfiguration) -> IterationResult:
    listener = network.probe_proxy_listener()
    if not listener:
        return IterationResult(iteration_id, False, failure_stage="proxy_listener")
    started = time.perf_counter()
    try:
        with network.create_http_client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = client.get("https://www.okx.com/api/v5/public/time")
        latency = round((time.perf_counter() - started) * 1000)
        payload = response.json()
        if response.status_code != 200 or payload.get("code") != "0":
            return IterationResult(
                iteration_id,
                True,
                public_rest_attempts=1,
                public_rest_latency_ms=latency,
                failure_stage="public_rest_response",
                failure_type=f"HTTP_{response.status_code}",
            )
    except (httpx.HTTPError, ValueError) as error:
        error_type, code = _exception_details(error)
        return IterationResult(
            iteration_id,
            True,
            public_rest_attempts=1,
            public_rest_latency_ms=round((time.perf_counter() - started) * 1000),
            failure_stage="public_rest_connect",
            failure_type=error_type,
            socket_error_code=code,
        )
    try:
        websocket = asyncio.run(_public_websocket_iteration(network))
    except Exception as error:
        error_type, code = _exception_details(error)
        return IterationResult(
            iteration_id,
            True,
            public_rest_ready=True,
            public_rest_attempts=1,
            public_rest_latency_ms=latency,
            failure_stage="public_ws",
            failure_type=error_type,
            socket_error_code=code,
        )
    if not all(
        (
            websocket["connected"],
            websocket["subscribed"],
            websocket["event_received"],
            websocket["closed_cleanly"],
        )
    ):
        return IterationResult(
            iteration_id,
            True,
            public_rest_ready=True,
            public_rest_attempts=1,
            public_rest_latency_ms=latency,
            public_ws_connected=websocket["connected"],
            public_ws_subscribed=websocket["subscribed"],
            public_ws_events_received=websocket["events"],
            public_ws_closed_cleanly=websocket["closed_cleanly"],
            failure_stage="public_ws_lifecycle",
        )
    return IterationResult(
        iteration_id,
        True,
        public_rest_ready=True,
        public_rest_attempts=1,
        public_rest_latency_ms=latency,
        public_ws_connected=True,
        public_ws_subscribed=True,
        public_ws_events_received=websocket["events"],
        public_ws_closed_cleanly=True,
    )


def private_iteration(
    iteration_id: int,
    *,
    network: NetworkConfiguration,
    settings: Settings,
    database: Database,
) -> IterationResult:
    listener = network.probe_proxy_listener()
    if not listener:
        return IterationResult(iteration_id, False, failure_stage="proxy_listener")
    client = OkxClient(settings, network=network)
    session: DemoTradingSession | None = None
    try:
        config = load_run_config(Path("configs/btc_vwap_bounded_acceptance.yaml"), environ={})
        session = DemoTradingSession(config, settings, client, TradingRepository(database))
        started = session.start(timeout_seconds=15)
        readiness = session.readiness_snapshot
        ready_health = session.stream.health
        monitor = session.monitor
        account_state_received = bool(monitor and monitor.account_snapshot_received)
        position_state_received = bool(monitor and monitor.position_snapshot_received)
        if not all(
            (
                started.reconciliation_status.value == "healthy",
                ready_health.authenticated,
                ready_health.subscriptions_ready,
                account_state_received,
                position_state_received,
                readiness.private_state_received,
                readiness.private_state_reconciled,
            )
        ):
            return IterationResult(
                iteration_id,
                True,
                failure_stage="private_reconciliation",
                private_ws_authenticated=ready_health.authenticated,
                private_ws_subscriptions_ready=ready_health.subscriptions_ready,
                private_account_state_received=account_state_received,
                private_position_state_received=position_state_received,
                private_state_reconciled=readiness.private_state_reconciled,
            )
        session.close()
        closed_health = session.stream.health
        closed_readiness = session.readiness_snapshot
        pending_tasks = int(closed_readiness.monitor_thread_alive)
        shutdown_failure = closed_readiness.monitor_error_type
        return IterationResult(
            iteration_id,
            True,
            private_ws_authenticated=ready_health.authenticated,
            private_ws_subscriptions_ready=ready_health.subscriptions_ready,
            private_account_state_received=account_state_received,
            private_position_state_received=position_state_received,
            private_state_reconciled=True,
            private_clean_shutdown=closed_health.closed_cleanly,
            private_api_write_calls=client.private_api_write_calls,
            place_order_calls=client.place_order_calls,
            cancel_order_calls=client.cancel_order_calls,
            pending_tasks=pending_tasks,
            failure_stage="private_clean_shutdown" if shutdown_failure else None,
            failure_type=shutdown_failure,
        )
    except Exception as error:
        error_type, code = _exception_details(error)
        failed_health = session.stream.health if session is not None else None
        return IterationResult(
            iteration_id,
            True,
            failure_stage="private_readiness",
            failure_type=error_type,
            socket_error_code=code,
            private_ws_authenticated=bool(failed_health and failed_health.authenticated),
            private_ws_subscriptions_ready=bool(
                failed_health and failed_health.subscriptions_ready
            ),
            private_state_reconciled=bool(
                session and session.readiness_snapshot.private_state_reconciled
            ),
            private_api_write_calls=client.private_api_write_calls,
            place_order_calls=client.place_order_calls,
            cancel_order_calls=client.cancel_order_calls,
        )
    finally:
        if session is not None:
            session.close()
        client.close()


def _summary(results: list[IterationResult], *, private: bool) -> dict[str, int]:
    if private:
        successful = sum(
            result.failure_stage is None
            and result.private_ws_authenticated
            and result.private_ws_subscriptions_ready
            and result.private_account_state_received
            and result.private_position_state_received
            and result.private_state_reconciled
            and result.private_clean_shutdown
            and result.private_api_write_calls == 0
            and result.broker_write_calls == 0
            and result.place_order_calls == 0
            and result.cancel_order_calls == 0
            and result.pending_tasks == 0
            for result in results
        )
    else:
        successful = sum(
            result.failure_stage is None
            and result.proxy_listener_ready
            and result.public_rest_ready
            and result.public_rest_attempts == 1
            and result.public_ws_connected
            and result.public_ws_subscribed
            and result.public_ws_events_received > 0
            and result.public_ws_closed_cleanly
            and result.pending_tasks == 0
            for result in results
        )
    prefix = "private" if private else "public"
    return {
        f"{prefix}_iterations": len(results),
        f"{prefix}_successful": successful,
        f"{prefix}_failed": len(results) - successful,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only proxy readiness stability audit")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    network = NetworkConfiguration.from_environment()
    if network.mode is not NetworkMode.PROXY:
        raise ValueError("readiness stability requires OKX_NETWORK_MODE=proxy")
    output = arguments.output
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"audit output already exists: {output}")
    public = [public_iteration(index, network) for index in range(1, 21)]
    result: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "network_mode": network.mode.value,
        "proxy_url_redacted": network.redacted_proxy_url,
        "proxy_listener_ready": network.probe_proxy_listener(),
        "public_iteration_results": [asdict(item) for item in public],
        **_summary(public, private=False),
        "private_iteration_results": [],
    }
    if result["public_failed"] == 0:
        settings = Settings()
        database = Database(settings.database_url)
        private = [
            private_iteration(index, network=network, settings=settings, database=database)
            for index in range(1, 11)
        ]
        result["private_iteration_results"] = [asdict(item) for item in private]
        result.update(_summary(private, private=True))
    else:
        result.update({"private_iterations": 0, "private_successful": 0, "private_failed": 0})
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in result.items() if not isinstance(value, list)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
