"""Bounded public-only OKX collection orchestration for prospective OOS data."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.market.historical_data import MarketDataError
from app.market.network import NetworkConfiguration, NetworkMode
from backtest.prospective_oos import (
    COLLECTOR_VERSION,
    PROSPECTIVE_START,
    ProspectiveOOSStore,
    latest_closed_hour,
)
from backtest.vwap_signal_edge_data import RawCandle

OKX_HISTORY_ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"
PAGE_LIMIT = 300


class PublicTransport(Protocol):
    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response: ...

    def close(self) -> None: ...


class ProspectivePublicClient(Protocol):
    requests: int
    successful_requests: int
    failed_requests: int

    def fetch(self, *, start: datetime, end: datetime) -> FetchBatch: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FetchBatch:
    rows: tuple[RawCandle, ...]
    requests: int
    successful_requests: int
    failed_requests: int
    retries: int
    request_started_at: str | None
    response_finished_at: str | None


@dataclass(frozen=True, slots=True)
class CollectorTelemetry:
    started_at: str
    finished_at: str
    runtime_seconds: float
    polls: int
    requests: int
    successful_requests: int
    failed_requests: int
    new_confirmed_candles: int
    duplicates_ignored: int
    source_revisions: int
    network_failures: int
    last_network_failure: str | None
    graceful_shutdown: bool
    pending_tasks: int
    manifest_flushed: bool
    proxy_listener_ready: bool


class OKXProspectivePublicClient:
    """Credential-free, proxy-only backward pagination over public candle history."""

    def __init__(
        self,
        network: NetworkConfiguration,
        *,
        transport: PublicTransport | None = None,
        retry_limit: int = 2,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if network.mode is not NetworkMode.PROXY:
            raise ValueError("prospective collector requires explicit proxy mode")
        if retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        self.network = network
        self.transport = transport or network.create_http_client(
            timeout=httpx.Timeout(20.0, connect=10.0)
        )
        self.retry_limit = retry_limit
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.requests = 0
        self.successful_requests = 0
        self.failed_requests = 0

    def close(self) -> None:
        self.transport.close()

    def fetch(self, *, start: datetime, end: datetime) -> FetchBatch:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        if start < PROSPECTIVE_START or end < start:
            raise ValueError("fetch range must be prospective and ordered")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        after = end_ms + 3_600_000
        rows: list[RawCandle] = []
        requests = successes = failures = retries = 0
        first_request: datetime | None = None
        last_response: datetime | None = None
        while after > start_ms:
            payload: dict[str, Any] | None = None
            for attempt in range(self.retry_limit + 1):
                requests += 1
                self.requests += 1
                first_request = first_request or self.clock().astimezone(UTC)
                try:
                    response = self.transport.get(
                        OKX_HISTORY_ENDPOINT,
                        params={
                            "instId": "BTC-USDT",
                            "bar": "1H",
                            "after": str(after),
                            "limit": str(PAGE_LIMIT),
                        },
                        headers={"Accept": "application/json"},
                    )
                    if response.status_code != 200:
                        raise MarketDataError(f"OKX public history HTTP {response.status_code}")
                    decoded = response.json()
                    if not isinstance(decoded, dict) or decoded.get("code") != "0":
                        raise MarketDataError("OKX public history response code is not zero")
                    payload = decoded
                    successes += 1
                    self.successful_requests += 1
                    last_response = self.clock().astimezone(UTC)
                    break
                except (httpx.HTTPError, ValueError, MarketDataError):
                    failures += 1
                    self.failed_requests += 1
                    if attempt >= self.retry_limit:
                        raise
                    retries += 1
                    self.sleep(0.5 * 2**attempt)
            assert payload is not None
            raw = payload.get("data")
            if not isinstance(raw, list):
                raise MarketDataError("OKX public history data is not a list")
            if not raw:
                break
            downloaded_at = self.clock().astimezone(UTC).isoformat()
            page = tuple(_parse_row(item, downloaded_at=downloaded_at) for item in raw)
            oldest = min(row.timestamp_ms for row in page)
            if oldest >= after:
                raise MarketDataError("OKX public history pagination did not move backward")
            for row in page:
                if start_ms <= row.timestamp_ms <= end_ms:
                    rows.append(row)
            after = oldest
            if len(raw) < PAGE_LIMIT or oldest <= start_ms:
                break
        return FetchBatch(
            tuple(sorted(rows, key=lambda row: row.timestamp_ms)),
            requests,
            successes,
            failures,
            retries,
            first_request.isoformat() if first_request else None,
            last_response.isoformat() if last_response else None,
        )


class ProspectiveCollectorRunner:
    def __init__(
        self,
        store: ProspectiveOOSStore,
        network: NetworkConfiguration,
        *,
        client_factory: Callable[[], ProspectivePublicClient] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: int = 600,
    ) -> None:
        if network.mode is not NetworkMode.PROXY:
            raise ValueError("prospective collector forbids direct and env network modes")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.store = store
        self.network = network
        self.client_factory = client_factory or (lambda: OKXProspectivePublicClient(network))
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.poll_seconds = poll_seconds

    def run(self, *, max_runtime_seconds: float) -> CollectorTelemetry:
        if max_runtime_seconds < 0:
            raise ValueError("max_runtime_seconds cannot be negative")
        started = self.clock().astimezone(UTC)
        deadline = started + timedelta(seconds=max_runtime_seconds)
        counters = {
            "polls": 0,
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "new_confirmed_candles": 0,
            "duplicates_ignored": 0,
            "source_revisions": 0,
            "network_failures": 0,
        }
        last_failure: str | None = None
        proxy_ready = self.network.probe_proxy_listener()
        if not proxy_ready:
            last_failure = "PROXY_LISTENER_UNAVAILABLE"
            counters["network_failures"] = 1
            self.store.record_audit_event(last_failure, self._provenance())
        else:
            while True:
                now = self.clock().astimezone(UTC)
                closed = latest_closed_hour(now)
                counters["polls"] += 1
                missing = self.store.missing_timestamps(latest_closed=closed)
                if missing:
                    client = self.client_factory()
                    try:
                        batch = client.fetch(start=missing[0], end=missing[-1])
                        counters["requests"] += batch.requests
                        counters["successful_requests"] += batch.successful_requests
                        counters["failed_requests"] += batch.failed_requests
                        try:
                            result = self.store.ingest(batch.rows, latest_closed=closed)
                        except (MarketDataError, ValueError) as exc:
                            self.store.record_audit_event(
                                "DATA_INTEGRITY_FAILURE",
                                {**self._provenance(), "error": f"{type(exc).__name__}: {exc}"},
                            )
                            break
                        counters["new_confirmed_candles"] += result.new_confirmed_candles
                        counters["duplicates_ignored"] += result.duplicates_ignored
                        counters["source_revisions"] += result.source_revisions
                        self.store.record_audit_event(
                            "PUBLIC_FETCH_COMPLETED",
                            {
                                **self._provenance(),
                                "request_time": batch.request_started_at,
                                "response_time": batch.response_finished_at,
                                "requested_start": missing[0].isoformat(),
                                "requested_end": missing[-1].isoformat(),
                            },
                        )
                    except (httpx.HTTPError, MarketDataError, OSError, ValueError) as exc:
                        counters["network_failures"] += 1
                        counters["requests"] += client.requests
                        counters["successful_requests"] += client.successful_requests
                        counters["failed_requests"] += client.failed_requests
                        last_failure = f"{type(exc).__name__}: {exc}"
                        self.store.record_audit_event(
                            "PUBLIC_NETWORK_FAILURE",
                            {**self._provenance(), "error": last_failure},
                        )
                        break
                    finally:
                        client.close()
                else:
                    self.store.recover(latest_closed=closed)
                if max_runtime_seconds == 0 or self.clock().astimezone(UTC) >= deadline:
                    break
                remaining = (deadline - self.clock().astimezone(UTC)).total_seconds()
                self.sleep(min(float(self.poll_seconds), max(remaining, 0.0)))
                if self.clock().astimezone(UTC) >= deadline:
                    break
        self.store.write_root_manifest()
        finished = self.clock().astimezone(UTC)
        return CollectorTelemetry(
            started.isoformat(),
            finished.isoformat(),
            max((finished - started).total_seconds(), 0.0),
            counters["polls"],
            counters["requests"],
            counters["successful_requests"],
            counters["failed_requests"],
            counters["new_confirmed_candles"],
            counters["duplicates_ignored"],
            counters["source_revisions"],
            counters["network_failures"],
            last_failure,
            True,
            0,
            self.store.manifest_path.exists(),
            proxy_ready,
        )

    def _provenance(self) -> dict[str, Any]:
        return {
            "source": "OKX_PUBLIC_API",
            "endpoint": OKX_HISTORY_ENDPOINT,
            "network_mode": self.network.mode,
            "proxy_url": self.network.redacted_proxy_url,
            "collector_version": COLLECTOR_VERSION,
            "private_credentials_used": False,
        }


def telemetry_dict(value: CollectorTelemetry) -> dict[str, Any]:
    return asdict(value)


def default_store(path: Path) -> ProspectiveOOSStore:
    return ProspectiveOOSStore(path)


def _parse_row(value: object, *, downloaded_at: str) -> RawCandle:
    if not isinstance(value, list) or len(value) < 9:
        raise MarketDataError("OKX public history candle row is invalid")
    payload = tuple(str(item) for item in value)
    try:
        timestamp_ms = int(payload[0])
    except ValueError as exc:
        raise MarketDataError("OKX public history timestamp is invalid") from exc
    if payload[8] not in {"0", "1"}:
        raise MarketDataError("OKX public history confirmation flag is invalid")
    return RawCandle(
        timestamp_ms,
        payload[1],
        payload[2],
        payload[3],
        payload[4],
        payload[5],
        payload[6],
        payload[7],
        payload[8],
        "BTC-USDT",
        "1h",
        "OKX_PUBLIC_API",
        downloaded_at,
        payload,
    )
