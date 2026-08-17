"""CLI surface for read-only public-market Continuous VWAP Shadow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated

import typer

from app.config.run_config import load_run_config
from app.market.network import NetworkConfiguration
from app.market.okx_public import OKXPublicHistoricalDataProvider
from app.market.websocket import (
    OKXPublicWebSocketProvider,
    PublicWebSocketEvent,
    PublicWebSocketEventType,
)
from app.reproducibility import InstrumentSnapshotStore
from app.services.shadow_smoke_recovery import ShadowSmokeRecoveryService
from app.services.vwap_continuous_shadow import ContinuousVWAPShadowRunner
from app.storage.database import Database

app = typer.Typer(help="READ-ONLY PUBLIC MARKET SHADOW. NO ORDER EXECUTION. NO PRIVATE API.")


@dataclass(slots=True)
class _BoundedFeedState:
    runtime_deadline_reached: bool = False


async def _bounded_feed(
    provider: OKXPublicWebSocketProvider, seconds: int, state: _BoundedFeedState
) -> AsyncIterator[PublicWebSocketEvent]:
    events = provider.stream_events("BTC-USDT", "1h")
    iterator = events.__aiter__()
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                state.runtime_deadline_reached = True
                yield PublicWebSocketEvent(
                    PublicWebSocketEventType.CLOSED, provider.connection_count
                )
                return
            try:
                yield await asyncio.wait_for(anext(iterator), timeout=remaining)
            except TimeoutError:
                state.runtime_deadline_reached = True
                yield PublicWebSocketEvent(
                    PublicWebSocketEventType.CLOSED, provider.connection_count
                )
                return
    finally:
        try:
            await provider.stop()
        finally:
            close = getattr(events, "aclose", None)
            if callable(close):
                await close()


@app.command("run-vwap-continuous-shadow")
def run_vwap_continuous_shadow(
    database_path: Annotated[Path, typer.Option("--database")],
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/btc_vwap_shadow.yaml"),
    instrument: Annotated[str, typer.Option("--instrument")] = "BTC-USDT",
    bar_interval: Annotated[str, typer.Option("--bar-interval")] = "1H",
    max_runtime_seconds: Annotated[int, typer.Option("--max-runtime-seconds", min=1, max=90)] = 30,
    max_confirmed_bars: Annotated[int, typer.Option("--max-confirmed-bars", min=1, max=6)] = 1,
    resume_run_id: Annotated[str | None, typer.Option("--resume-run-id")] = None,
) -> None:
    """READ-ONLY VWAP SHADOW. PUBLIC MARKET DATA ONLY. NO PRIVATE API. NO ORDER EXECUTION."""
    if instrument != "BTC-USDT" or bar_interval.lower() != "1h":
        raise typer.BadParameter("only BTC-USDT and 1H are supported")
    config = load_run_config(config_path, environ={})
    if config.data.instrument_snapshot is None:
        raise typer.BadParameter("instrument snapshot is required")
    resolved = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    database = Database(f"sqlite:///{database_path}")
    network = NetworkConfiguration.from_environment()
    history = OKXPublicHistoricalDataProvider(network=network)
    stream = OKXPublicWebSocketProvider(network=network)
    try:
        runner = ContinuousVWAPShadowRunner(database, config, resolved, history)
        feed_state = _BoundedFeedState()
        result = asyncio.run(
            runner.run_events(
                _bounded_feed(stream, max_runtime_seconds, feed_state),
                maximum_confirmed_bars=max_confirmed_bars,
                resume_run_id=resume_run_id,
            )
        )
        if runner.session is None:
            raise RuntimeError("Shadow Smoke session is unavailable after completion")
        observation = {
            "confirmed_history_bars": runner.session.bootstrap_bars,
            "bootstrap_latest_confirmed_timestamp": runner.session.bootstrap_latest_confirmed_timestamp.isoformat(),
            "public_rest_calls": history.public_rest_calls,
            "public_ws_connections": stream.connection_count,
            "subscriptions": stream.subscription_count,
            "live_events_received": stream.live_event_count,
            "unconfirmed_events_received": stream.unconfirmed_event_count,
            "unsubscriptions": stream.unsubscription_count,
            "closed_cleanly": stream.state.value == "stopped",
            "runtime_deadline_reached": feed_state.runtime_deadline_reached,
        }
        runner.repository.record_run_event(
            result.run_id, "shadow_smoke_observation_completed", observation
        )
        payload = asdict(result)
        payload.update(observation)
        payload["observation_outcome"] = (
            "confirmed_candles_processed"
            if result.confirmed_bars_processed
            else "healthy_no_confirmed_event"
        )
        typer.echo(json.dumps(payload, default=str))
    finally:
        history.close()


@app.command("recover-vwap-continuous-shadow")
def recover_vwap_continuous_shadow(
    database_path: Annotated[Path, typer.Option("--database")],
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Locally finalize one dead-owner Shadow Smoke run; no network or Broker is used."""
    result = ShadowSmokeRecoveryService(Database(f"sqlite:///{database_path}")).recover(
        run_id, "external_process_termination_recovered"
    )
    typer.echo(json.dumps(asdict(result), default=str))


if __name__ == "__main__":
    app()
