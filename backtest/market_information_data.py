"""Public-only OKX derivatives information download and durable raw cache."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.market.historical_data import MarketDataError
from app.market.network import NetworkConfiguration, NetworkMode

BASE_URL = "https://www.okx.com"
SWAP_INSTRUMENT = "BTC-USDT-SWAP"
SPOT_INSTRUMENT = "BTC-USDT"
RESEARCH_CUTOFF_MS = 1786550399999
RESEARCH_START_MS = 1690074000000


class PublicTransport(Protocol):
    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DownloadStats:
    source: str
    rows: int
    requests: int
    retries: int
    failures: int
    duplicates: int
    source_revisions: int
    actual_start: str | None
    actual_end: str | None
    dataset_hash: str
    retention_limited: bool


class ImmutableJsonRecords:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records = root / "records"
        self.pages = root / "pages"
        self.records.mkdir(parents=True, exist_ok=True)
        self.pages.mkdir(parents=True, exist_ok=True)

    def commit(self, timestamp_ms: int, row: object) -> str:
        canonical = _canonical(row)
        path = self.records / f"{timestamp_ms}.json"
        if path.exists():
            return "duplicate" if path.read_text(encoding="utf-8") == canonical else "revision"
        _atomic_write(path, canonical)
        return "new"

    def save_page(self, request_id: str, payload: object) -> None:
        path = self.pages / f"{hashlib.sha256(request_id.encode()).hexdigest()[:24]}.json"
        if not path.exists():
            _atomic_write(path, _canonical(payload))

    def rows(self) -> list[Any]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.records.glob("*.json"))
        ]

    def checkpoint(self) -> dict[str, Any]:
        path = self.root / "checkpoint.json"
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def save_checkpoint(self, *, cursor: str, complete: bool) -> None:
        _atomic_write(
            self.root / "checkpoint.json",
            _canonical({"cursor": cursor, "complete": complete}),
        )


class OKXMarketInformationClient:
    def __init__(
        self,
        network: NetworkConfiguration,
        *,
        transport: PublicTransport | None = None,
        retry_limit: int = 2,
        throttle_seconds: float = 0.12,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if network.mode is not NetworkMode.PROXY:
            raise ValueError("market information download requires explicit proxy mode")
        self.network = network
        self.transport = transport or network.create_http_client(
            timeout=httpx.Timeout(20.0, connect=10.0)
        )
        self.retry_limit = retry_limit
        self.throttle_seconds = throttle_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.requests = 0
        self.retries = 0
        self.failures = 0

    def close(self) -> None:
        self.transport.close()

    def get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self.retry_limit + 1):
            self.requests += 1
            try:
                response = self.transport.get(
                    BASE_URL + endpoint,
                    params=params,
                    headers={"Accept": "application/json"},
                )
                if response.status_code != 200:
                    raise MarketDataError(f"OKX public HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("code") != "0":
                    raise MarketDataError("OKX public response code is not zero")
                self.sleep(self.throttle_seconds)
                return payload
            except (httpx.HTTPError, ValueError, MarketDataError):
                self.failures += 1
                if attempt >= self.retry_limit:
                    raise
                self.retries += 1
                self.sleep(0.5 * 2**attempt)
        raise AssertionError("bounded retry loop exhausted")


def download_funding(client: OKXMarketInformationClient, root: Path) -> DownloadStats:
    cache = ImmutableJsonRecords(root / "raw" / "funding")
    checkpoint = cache.checkpoint()
    cursor = str(checkpoint.get("cursor", RESEARCH_CUTOFF_MS + 1))
    duplicates = revisions = 0
    start_requests, start_retries, start_failures = client.requests, client.retries, client.failures
    while not checkpoint.get("complete", False):
        params = {"instId": SWAP_INSTRUMENT, "limit": "400", "after": cursor}
        payload = client.get("/api/v5/public/funding-rate-history", params)
        cache.save_page(
            json.dumps(params, sort_keys=True),
            {
                "endpoint": "/api/v5/public/funding-rate-history",
                "params": params,
                "downloaded_at": client.clock().astimezone(UTC).isoformat(),
                "response": payload,
            },
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            cache.save_checkpoint(cursor=cursor, complete=True)
            break
        oldest = min(int(row["fundingTime"]) for row in rows)
        for row in rows:
            stamp = int(row["fundingTime"])
            if stamp <= RESEARCH_CUTOFF_MS:
                status = cache.commit(stamp, row)
                duplicates += status == "duplicate"
                revisions += status == "revision"
        if oldest >= int(cursor) or len(rows) < 400:
            cache.save_checkpoint(cursor=str(oldest), complete=True)
            break
        cursor = str(oldest)
        cache.save_checkpoint(cursor=cursor, complete=False)
    return _stats(
        "funding",
        cache,
        "fundingTime",
        client,
        start_requests,
        start_retries,
        start_failures,
        duplicates,
        revisions,
        True,
    )


def download_open_interest(client: OKXMarketInformationClient, root: Path) -> DownloadStats:
    cache = ImmutableJsonRecords(root / "raw" / "open_interest")
    checkpoint = cache.checkpoint()
    cursor = str(checkpoint.get("cursor", RESEARCH_CUTOFF_MS))
    duplicates = revisions = 0
    start_requests, start_retries, start_failures = client.requests, client.retries, client.failures
    while not checkpoint.get("complete", False):
        params = {"instId": SWAP_INSTRUMENT, "period": "1H", "limit": "100", "end": cursor}
        payload = client.get("/api/v5/rubik/stat/contracts/open-interest-history", params)
        cache.save_page(
            json.dumps(params, sort_keys=True),
            {
                "endpoint": "/api/v5/rubik/stat/contracts/open-interest-history",
                "params": params,
                "downloaded_at": client.clock().astimezone(UTC).isoformat(),
                "response": payload,
            },
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            cache.save_checkpoint(cursor=cursor, complete=True)
            break
        oldest = min(int(row[0]) for row in rows)
        for row in rows:
            stamp = int(row[0])
            if stamp <= RESEARCH_CUTOFF_MS:
                status = cache.commit(stamp, row)
                duplicates += status == "duplicate"
                revisions += status == "revision"
        if oldest >= int(cursor) or len(rows) < 100:
            cache.save_checkpoint(cursor=str(oldest), complete=True)
            break
        cursor = str(oldest - 1)
        cache.save_checkpoint(cursor=cursor, complete=False)
    return _stats(
        "open_interest",
        cache,
        0,
        client,
        start_requests,
        start_retries,
        start_failures,
        duplicates,
        revisions,
        True,
    )


def download_swap_candles(client: OKXMarketInformationClient, root: Path) -> DownloadStats:
    cache = ImmutableJsonRecords(root / "raw" / "perp")
    checkpoint = cache.checkpoint()
    cursor = str(checkpoint.get("cursor", RESEARCH_CUTOFF_MS + 1))
    duplicates = revisions = 0
    start_requests, start_retries, start_failures = client.requests, client.retries, client.failures
    while not checkpoint.get("complete", False):
        params = {"instId": SWAP_INSTRUMENT, "bar": "1H", "limit": "300", "after": cursor}
        payload = client.get("/api/v5/market/history-candles", params)
        cache.save_page(
            json.dumps(params, sort_keys=True),
            {
                "endpoint": "/api/v5/market/history-candles",
                "params": params,
                "downloaded_at": client.clock().astimezone(UTC).isoformat(),
                "response": payload,
            },
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            cache.save_checkpoint(cursor=cursor, complete=True)
            break
        oldest = min(int(row[0]) for row in rows)
        for row in rows:
            stamp = int(row[0])
            if RESEARCH_START_MS <= stamp <= RESEARCH_CUTOFF_MS and str(row[8]) == "1":
                status = cache.commit(stamp, row)
                duplicates += status == "duplicate"
                revisions += status == "revision"
        if oldest >= int(cursor) or len(rows) < 300 or oldest <= RESEARCH_START_MS:
            cache.save_checkpoint(cursor=str(oldest), complete=True)
            break
        cursor = str(oldest)
        cache.save_checkpoint(cursor=cursor, complete=False)
    return _stats(
        "perp",
        cache,
        0,
        client,
        start_requests,
        start_retries,
        start_failures,
        duplicates,
        revisions,
        False,
    )


def fetch_metadata(client: OKXMarketInformationClient, root: Path) -> dict[str, Any]:
    endpoints = {
        "instrument": (
            "/api/v5/public/instruments",
            {"instType": "SWAP", "instId": SWAP_INSTRUMENT},
        ),
        "open_interest_current": (
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": SWAP_INSTRUMENT},
        ),
        "funding_current": ("/api/v5/public/funding-rate", {"instId": SWAP_INSTRUMENT}),
        "mark_current": (
            "/api/v5/public/mark-price",
            {"instType": "SWAP", "instId": SWAP_INSTRUMENT},
        ),
        "index_current": ("/api/v5/market/index-tickers", {"instId": SPOT_INSTRUMENT}),
    }
    result: dict[str, Any] = {}
    for name, (endpoint, params) in endpoints.items():
        payload = client.get(endpoint, params)
        result[name] = {"endpoint": endpoint, "params": params, "response": payload}
    _atomic_write(root / "raw" / "metadata" / "public_metadata.json", _canonical(result))
    return result


def _stats(
    source: str,
    cache: ImmutableJsonRecords,
    timestamp_key: str | int,
    client: OKXMarketInformationClient,
    request_start: int,
    retry_start: int,
    failure_start: int,
    duplicates: int,
    revisions: int,
    retention_limited: bool,
) -> DownloadStats:
    rows = cache.rows()
    stamps = sorted(int(row[timestamp_key]) for row in rows)
    return DownloadStats(
        source,
        len(rows),
        client.requests - request_start,
        client.retries - retry_start,
        client.failures - failure_start,
        duplicates,
        revisions,
        datetime.fromtimestamp(stamps[0] / 1000, tz=UTC).isoformat() if stamps else None,
        datetime.fromtimestamp(stamps[-1] / 1000, tz=UTC).isoformat() if stamps else None,
        hashlib.sha256(_canonical(rows).encode()).hexdigest(),
        retention_limited,
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
