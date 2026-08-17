from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.market.network import NetworkConfiguration
from backtest.prospective_collector import (
    CollectorTelemetry,
    FetchBatch,
    ProspectiveCollectorRunner,
)
from backtest.prospective_oos import PROSPECTIVE_START, ProspectiveOOSStore
from backtest.vwap_signal_edge_data import RawCandle


def _network() -> NetworkConfiguration:
    return NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self, rows: tuple[RawCandle, ...]) -> None:
        self.rows = rows
        self.closed = False
        self.requests = 0
        self.successful_requests = 0
        self.failed_requests = 0

    def fetch(self, *, start: datetime, end: datetime) -> FetchBatch:
        self.requests += 1
        self.successful_requests += 1
        selected = tuple(
            row
            for row in self.rows
            if start <= datetime.fromtimestamp(row.timestamp_ms / 1000, tz=UTC) <= end
        )
        return FetchBatch(selected, 1, 1, 0, 0, start.isoformat(), end.isoformat())

    def close(self) -> None:
        self.closed = True


class InvalidClient(FakeClient):
    def fetch(self, *, start: datetime, end: datetime) -> FetchBatch:
        batch = super().fetch(start=start, end=end)
        invalid = replace(batch.rows[0], high="50")
        return replace(batch, rows=(invalid,))


def _raw(hour: int) -> RawCandle:
    stamp = PROSPECTIVE_START + timedelta(hours=hour)
    payload = (
        str(int(stamp.timestamp() * 1000)),
        "100",
        "102",
        "99",
        "101",
        "10",
        "1000",
        "1000",
        "1",
    )
    return RawCandle(
        int(payload[0]),
        *payload[1:8],
        payload[8],
        "BTC-USDT",
        "1h",
        "OKX_PUBLIC_API",
        stamp.isoformat(),
        payload,
    )


def test_proxy_failure_stops_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NetworkConfiguration, "probe_proxy_listener", lambda self: False)
    clock = Clock(datetime(2026, 8, 13, 2, tzinfo=UTC))
    store = ProspectiveOOSStore(tmp_path, clock=clock)
    result = ProspectiveCollectorRunner(store, _network(), clock=clock).run(max_runtime_seconds=0)
    assert result.network_failures == 1
    assert result.last_network_failure == "PROXY_LISTENER_UNAVAILABLE"
    assert result.graceful_shutdown is True
    assert result.pending_tasks == 0
    assert result.manifest_flushed is True


def test_internal_deadline_and_backfill_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NetworkConfiguration, "probe_proxy_listener", lambda self: True)
    clock = Clock(datetime(2026, 8, 13, 2, 5, tzinfo=UTC))
    store = ProspectiveOOSStore(tmp_path, clock=clock)
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient((_raw(0), _raw(1)))
        clients.append(client)
        return client

    result = ProspectiveCollectorRunner(
        store,
        _network(),
        client_factory=factory,
        clock=clock,
        sleep=clock.sleep,
        poll_seconds=60,
    ).run(max_runtime_seconds=120)
    assert result.runtime_seconds == 120
    assert result.new_confirmed_candles == 2
    assert result.polls == 2
    assert result.pending_tasks == 0
    assert all(client.closed for client in clients)


def test_collector_rejects_direct_or_env_network(tmp_path: Path) -> None:
    store = ProspectiveOOSStore(tmp_path)
    with pytest.raises(ValueError, match="forbids direct and env"):
        ProspectiveCollectorRunner(
            store, NetworkConfiguration.from_environment({"OKX_NETWORK_MODE": "direct"})
        )


def test_source_integrity_failure_is_not_mislabeled_as_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(NetworkConfiguration, "probe_proxy_listener", lambda self: True)
    clock = Clock(datetime(2026, 8, 13, 1, 5, tzinfo=UTC))
    store = ProspectiveOOSStore(tmp_path, clock=clock)
    result = ProspectiveCollectorRunner(
        store,
        _network(),
        client_factory=lambda: InvalidClient((_raw(0),)),
        clock=clock,
    ).run(max_runtime_seconds=0)
    assert result.network_failures == 0
    assert store.integrity_report()["integrity_check"] == "failed"


def test_telemetry_contract_has_no_strategy_metrics() -> None:
    fields = set(CollectorTelemetry.__dataclass_fields__)
    assert not fields.intersection({"pnl", "sharpe", "win_rate", "strategy_return"})
