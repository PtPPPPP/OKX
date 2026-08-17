"""Proxy-only public OKX adapters and bounded derivatives collector runner."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.market.historical_data import MarketDataError
from app.market.network import NetworkConfiguration, NetworkMode
from backtest.derivatives_prospective import (
    ProspectiveObservation,
    ProspectiveObservationStore,
    build_basis_observation,
    unified_manifest,
)
from backtest.prospective_collector import ProspectiveCollectorRunner
from backtest.prospective_oos import PROSPECTIVE_START, ProspectiveOOSStore, latest_closed_hour

SOURCES = (
    "perp",
    "mark",
    "index",
    "open_interest",
    "funding/events",
    "funding/snapshots",
    "derived/basis_mark_spot",
)


@dataclass(frozen=True, slots=True)
class DerivativesTelemetry:
    started_at: str
    finished_at: str
    runtime_seconds: float
    polls: int
    rows_by_source: dict[str, int]
    duplicates: int
    source_revisions: int
    network_failures: int
    failures: tuple[dict[str, str], ...]
    collection_gaps: int
    graceful_shutdown: bool
    pending_tasks: int
    all_manifests_flushed: bool
    proxy_listener_ready: bool


class OKXDerivativesPublicClient:
    def __init__(
        self,
        network: NetworkConfiguration,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if network.mode is not NetworkMode.PROXY:
            raise ValueError("derivatives collector requires explicit proxy mode")
        self.network = network
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.client = network.create_http_client(timeout=httpx.Timeout(20, connect=10))
        self.failures: list[dict[str, str]] = []

    def close(self) -> None:
        self.client.close()

    def metadata(self) -> dict[str, Any]:
        result = {}
        for name, params in (
            ("spot", {"instType": "SPOT", "instId": "BTC-USDT"}),
            ("perp", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}),
        ):
            result[name] = self._get("/api/v5/public/instruments", params)["data"][0]
        return result

    def collect(self, *, start: datetime, end: datetime) -> dict[str, list[ProspectiveObservation]]:
        observed = self.clock().astimezone(UTC)
        result = {
            "perp": self._candles(
                "/api/v5/market/history-candles", "BTC-USDT-SWAP", start, end, observed
            ),
            "mark": self._candles(
                "/api/v5/market/history-mark-price-candles", "BTC-USDT-SWAP", start, end, observed
            ),
            "index": self._candles(
                "/api/v5/market/history-index-candles", "BTC-USDT", start, end, observed
            ),
            "open_interest": self._oi(start, end, observed),
            "funding/events": self._funding_events(start, end, observed),
            "funding/snapshots": self._funding_snapshot(observed),
        }
        result["mark"].extend(
            self._price_snapshot(
                "mark",
                "/api/v5/public/mark-price",
                {"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
                observed,
            )
        )
        result["index"].extend(
            self._price_snapshot(
                "index", "/api/v5/market/index-tickers", {"instId": "BTC-USDT"}, observed
            )
        )
        result["open_interest"].extend(self._oi_snapshot(observed))
        return result

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = self.client.get(
                    "https://www.okx.com" + endpoint,
                    params=params,
                    headers={"Accept": "application/json"},
                )
                if response.status_code != 200:
                    raise MarketDataError(f"HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("code") != "0":
                    raise MarketDataError("public response code is not zero")
                self.sleep(0.12)
                return payload
            except (httpx.HTTPError, ValueError, MarketDataError) as exc:
                self.failures.append(
                    {
                        "endpoint": endpoint,
                        "stage": "public_fetch",
                        "exception": f"{type(exc).__name__}: {exc}",
                        "timestamp": self.clock().astimezone(UTC).isoformat(),
                    }
                )
                if attempt == 2:
                    raise
                self.sleep(0.5 * 2**attempt)
        raise AssertionError("bounded retry exhausted")

    def _candles(
        self, endpoint: str, instrument: str, start: datetime, end: datetime, observed: datetime
    ) -> list[ProspectiveObservation]:
        cursor = int(end.timestamp() * 1000) + 3_600_000
        start_ms = int(start.timestamp() * 1000)
        rows: list[ProspectiveObservation] = []
        limit = 100 if "index" in endpoint or "mark" in endpoint else 300
        while cursor > start_ms:
            raw = self._get(
                endpoint,
                {"instId": instrument, "bar": "1H", "after": str(cursor), "limit": str(limit)},
            )["data"]
            if not raw:
                break
            oldest = min(int(row[0]) for row in raw)
            for row in raw:
                stamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
                confirm = str(row[8]) if len(row) > 8 else "1"
                if start <= stamp <= end and confirm == "1":
                    values = {
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "volume": row[5] if len(row) > 5 else None,
                        "volume_ccy": row[6] if len(row) > 6 else None,
                        "volume_quote": row[7] if len(row) > 7 else None,
                        "confirm": confirm,
                    }
                    rows.append(
                        ProspectiveObservation(
                            endpoint_source(endpoint),
                            instrument,
                            stamp,
                            observed,
                            observed,
                            "backfill",
                            f"{instrument}|1H|{stamp.isoformat()}",
                            values,
                            row,
                        )
                    )
            if len(raw) < limit or oldest <= start_ms:
                break
            cursor = oldest
        return rows

    def _oi(
        self, start: datetime, end: datetime, observed: datetime
    ) -> list[ProspectiveObservation]:
        cursor = int(end.timestamp() * 1000)
        start_ms = int(start.timestamp() * 1000)
        result: list[ProspectiveObservation] = []
        while cursor >= start_ms:
            raw = self._get(
                "/api/v5/rubik/stat/contracts/open-interest-history",
                {"instId": "BTC-USDT-SWAP", "period": "5m", "end": str(cursor), "limit": "100"},
            )["data"]
            if not raw:
                break
            oldest = min(int(row[0]) for row in raw)
            for row in raw:
                stamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
                if start <= stamp <= end:
                    result.append(
                        ProspectiveObservation(
                            "open_interest",
                            "BTC-USDT-SWAP",
                            stamp,
                            observed,
                            observed,
                            "backfill",
                            f"BTC-USDT-SWAP|{stamp.isoformat()}",
                            {
                                "oi_contracts": row[1],
                                "oi_ccy": row[2],
                                "oi_usd": row[3],
                                "unit_metadata": {
                                    "oi_contracts": "contracts",
                                    "oi_ccy": "BTC",
                                    "oi_usd": "USD",
                                },
                            },
                            row,
                        )
                    )
            if len(raw) < 100 or oldest <= start_ms:
                break
            cursor = oldest - 1
        return result

    def _funding_events(
        self, start: datetime, end: datetime, observed: datetime
    ) -> list[ProspectiveObservation]:
        raw = self._get(
            "/api/v5/public/funding-rate-history",
            {
                "instId": "BTC-USDT-SWAP",
                "after": str(int(end.timestamp() * 1000) + 1),
                "limit": "400",
            },
        )["data"]
        result = []
        for row in raw:
            stamp = datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=UTC)
            if start <= stamp <= end:
                result.append(
                    ProspectiveObservation(
                        "funding/events",
                        "BTC-USDT-SWAP",
                        stamp,
                        observed,
                        observed,
                        "backfill",
                        f"BTC-USDT-SWAP|{stamp.isoformat()}",
                        {
                            "realized_funding": row.get("realizedRate") or row.get("fundingRate"),
                            "funding_time": stamp.isoformat(),
                            "estimated": False,
                        },
                        row,
                    )
                )
        return result

    def _funding_snapshot(self, observed: datetime) -> list[ProspectiveObservation]:
        row = self._get("/api/v5/public/funding-rate", {"instId": "BTC-USDT-SWAP"})["data"][0]
        stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, tz=UTC)
        if stamp < PROSPECTIVE_START:
            return []
        completed = max(observed, self.clock().astimezone(UTC), stamp)
        return [
            ProspectiveObservation(
                "funding/snapshots",
                "BTC-USDT-SWAP",
                stamp,
                completed,
                completed,
                "live_snapshot",
                f"BTC-USDT-SWAP|{stamp.isoformat()}",
                {
                    "current_funding_estimate": row.get("fundingRate"),
                    "next_funding_time": row.get("nextFundingTime"),
                    "funding_time": row.get("fundingTime"),
                    "estimated": True,
                },
                row,
            )
        ]

    def _price_snapshot(
        self, source: str, endpoint: str, params: dict[str, str], observed: datetime
    ) -> list[ProspectiveObservation]:
        row = self._get(endpoint, params)["data"][0]
        stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, tz=UTC)
        if stamp < PROSPECTIVE_START:
            return []
        completed = max(observed, self.clock().astimezone(UTC), stamp)
        price = row.get("markPx") if source == "mark" else row.get("idxPx")
        return [
            ProspectiveObservation(
                source,
                "BTC-USDT-SWAP" if source == "mark" else "BTC-USDT",
                stamp,
                completed,
                completed,
                "live_snapshot",
                f"{source}|{stamp.isoformat()}",
                {"close": price, "snapshot": True},
                row,
            )
        ]

    def _oi_snapshot(self, observed: datetime) -> list[ProspectiveObservation]:
        row = self._get(
            "/api/v5/public/open-interest", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}
        )["data"][0]
        stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, tz=UTC)
        if stamp < PROSPECTIVE_START:
            return []
        completed = max(observed, self.clock().astimezone(UTC), stamp)
        return [
            ProspectiveObservation(
                "open_interest",
                "BTC-USDT-SWAP",
                stamp,
                completed,
                completed,
                "live_snapshot",
                f"BTC-USDT-SWAP|snapshot|{stamp.isoformat()}",
                {
                    "oi_contracts": row.get("oi"),
                    "oi_ccy": row.get("oiCcy"),
                    "oi_usd": row.get("oiUsd"),
                    "unit_metadata": {
                        "oi_contracts": "contracts",
                        "oi_ccy": "BTC",
                        "oi_usd": "USD",
                    },
                },
                row,
            )
        ]


def endpoint_source(endpoint: str) -> str:
    if "mark-price" in endpoint:
        return "mark"
    if "index" in endpoint:
        return "index"
    return "perp"


class DerivativesCollectorRunner:
    def __init__(
        self,
        root: Path,
        network: NetworkConfiguration,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: int = 300,
    ) -> None:
        if network.mode is not NetworkMode.PROXY:
            raise ValueError("collector forbids direct and env network modes")
        self.root, self.network = root, network
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep, self.poll_seconds = sleep, poll_seconds
        instruments = {
            "perp": "BTC-USDT-SWAP",
            "mark": "BTC-USDT-SWAP",
            "index": "BTC-USDT",
            "open_interest": "BTC-USDT-SWAP",
            "funding/events": "BTC-USDT-SWAP",
            "funding/snapshots": "BTC-USDT-SWAP",
            "derived/basis_mark_spot": "BTC-USDT-SWAP",
        }
        self.stores = {
            source: ProspectiveObservationStore(root, source, instrument)
            for source, instrument in instruments.items()
        }

    def run(
        self, *, max_runtime_seconds: float
    ) -> tuple[DerivativesTelemetry, dict[str, Any], dict[str, Any]]:
        if not 0 <= max_runtime_seconds <= 28_800:
            raise ValueError("runtime must be between zero and eight hours")
        started = self.clock().astimezone(UTC)
        deadline = started + timedelta(seconds=max_runtime_seconds)
        counters = {"spot": 0, **{source: 0 for source in SOURCES}}
        duplicates = revisions = polls = gaps = 0
        failures: list[dict[str, str]] = []
        ready = self.network.probe_proxy_listener()
        metadata: dict[str, Any] = {}
        while ready:
            polls += 1
            now = self.clock().astimezone(UTC)
            closed = latest_closed_hour(now)
            spot_telemetry = ProspectiveCollectorRunner(
                ProspectiveOOSStore(self.root, clock=self.clock),
                self.network,
                clock=self.clock,
                sleep=self.sleep,
                poll_seconds=self.poll_seconds,
            ).run(max_runtime_seconds=0)
            counters["spot"] += spot_telemetry.new_confirmed_candles
            if spot_telemetry.network_failures:
                failures.append(
                    {
                        "endpoint": "spot/history-candles",
                        "stage": "spot_collection",
                        "exception": str(spot_telemetry.last_network_failure),
                        "timestamp": now.isoformat(),
                    }
                )
                break
            client = OKXDerivativesPublicClient(self.network, clock=self.clock, sleep=self.sleep)
            try:
                metadata = metadata or client.metadata()
                if closed >= PROSPECTIVE_START:
                    batches = client.collect(start=PROSPECTIVE_START, end=closed)
                    for source, rows in batches.items():
                        result = self.stores[source].ingest(rows, now=now)
                        counters[source] += result.new_records
                        duplicates += result.duplicates
                        revisions += result.source_revisions
                    basis_rows = self._basis(now)
                    result = self.stores["derived/basis_mark_spot"].ingest(basis_rows, now=now)
                    counters["derived/basis_mark_spot"] += result.new_records
            except (httpx.HTTPError, MarketDataError, OSError, ValueError) as exc:
                failures.extend(
                    client.failures
                    or [
                        {
                            "endpoint": "unknown",
                            "stage": "collection",
                            "exception": f"{type(exc).__name__}: {exc}",
                            "timestamp": now.isoformat(),
                        }
                    ]
                )
                break
            finally:
                client.close()
            if max_runtime_seconds == 0 or self.clock().astimezone(UTC) >= deadline:
                break
            remaining = (deadline - self.clock().astimezone(UTC)).total_seconds()
            self.sleep(min(self.poll_seconds, max(remaining, 0)))
            if self.clock().astimezone(UTC) >= deadline:
                break
        spot_manifest = ProspectiveOOSStore(self.root).write_root_manifest()
        gaps = sum(
            len(self.stores[source].sampling_gaps())
            for source in ("open_interest", "mark", "index")
        )
        manifest = unified_manifest(self.root, self.stores, spot_manifest)
        finished = self.clock().astimezone(UTC)
        telemetry = DerivativesTelemetry(
            started.isoformat(),
            finished.isoformat(),
            max(0.0, (finished - started).total_seconds()),
            polls,
            counters,
            duplicates,
            revisions,
            len(failures) + (not ready),
            tuple(failures),
            gaps,
            True,
            0,
            True,
            ready,
        )
        return telemetry, manifest, metadata

    def _basis(self, observed: datetime) -> list[ProspectiveObservation]:
        spot = _spot_rows(self.root)
        mark = self.stores["mark"].observations()
        value = build_basis_observation(spot, mark, observed, observed_at=observed)
        return [value] if value is not None else []


def _spot_rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / "BTC-USDT" / "1h").glob("????/??/??/records/*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromtimestamp(int(raw["timestamp_ms"]) / 1000, tz=UTC).isoformat()
        rows.append(
            {
                "source_timestamp": stamp,
                "observed_at": raw["downloaded_at"],
                "unique_key": f"BTC-USDT|1H|{stamp}",
                "values": {"close": raw["close"]},
            }
        )
    return rows


def telemetry_dict(value: DerivativesTelemetry) -> dict[str, Any]:
    return asdict(value)
