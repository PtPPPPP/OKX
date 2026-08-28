"""Operate and audit a real-time, public-market-only Phase 4A Shadow soak."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast
from unittest.mock import patch
from uuid import uuid4

from app.config.run_config import RunConfig, load_run_config
from app.domain.market import Candle
from app.exchange.okx_client import OkxClient
from app.execution.demo_broker import OKXDemoBroker
from app.execution.demo_write_authorization import DemoWriteAuthorization
from app.market.network import NetworkConfiguration, NetworkMode
from app.market.okx_public import OKXPublicHistoricalDataProvider
from app.market.providers import MarketDataProvider
from app.market.websocket import (
    OKXPublicWebSocketProvider,
    PublicWebSocketEvent,
    PublicWebSocketEventType,
)
from app.reproducibility import InstrumentSnapshotStore
from app.services.controlled_demo_write import ControlledDemoWriteService
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.vwap_continuous_shadow import ContinuousVWAPShadowRunner
from app.storage.database import Database
from app.storage.migrations import MigrationManager

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPOSITORY_ROOT / "configs" / "btc_vwap_shadow.yaml"
DEFAULT_ARTIFACT_ROOT = REPOSITORY_ROOT / "artifacts" / "soak" / "phase_4a"
DEFAULT_DATABASE_ROOT = REPOSITORY_ROOT / "data" / "soak" / "phase_4a"
INSTRUMENT_ID = "BTC-USDT"
BAR_INTERVAL = "1h"
BAR_DELTA = timedelta(hours=1)
LOCK_RENEW_INTERVAL_SECONDS = 10.0

_FORBIDDEN_ARTIFACT_KEYS = (
    "api_key",
    "secret",
    "passphrase",
    "credential",
    "account_id",
    "order_id",
    "trade_id",
    "ip_whitelist",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _sensitive_environment_values() -> tuple[str, ...]:
    values: list[str] = []
    for key in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        value = os.environ.get(key, "")
        if len(value) >= 8:
            values.append(value)
    return tuple(values)


def _validate_artifact_payload(payload: object) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    lowered = serialized.lower()
    if any(value in serialized for value in _sensitive_environment_values()):
        raise ValueError("artifact contains an exact credential value")

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).lower().replace("-", "_")
                if any(fragment in normalized for fragment in _FORBIDDEN_ARTIFACT_KEYS):
                    raise ValueError(f"artifact contains forbidden sensitive field: {key}")
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                visit(child)

    visit(payload)
    if "-----begin private key-----" in lowered:
        raise ValueError("artifact contains a private key marker")


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _validate_artifact_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    _validate_artifact_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL record at line {line_number}: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL record must be an object at line {line_number}: {path}")
        records.append(value)
    return records


@dataclass(frozen=True, slots=True)
class TimelineAnalysis:
    duplicates: int
    out_of_order: int
    missing_intervals: int


def analyze_timeline(timestamps: Sequence[datetime], interval: timedelta) -> TimelineAnalysis:
    seen: set[datetime] = set()
    previous: datetime | None = None
    duplicates = out_of_order = missing = 0
    for raw in timestamps:
        timestamp = raw.astimezone(UTC)
        if timestamp in seen:
            duplicates += 1
            continue
        seen.add(timestamp)
        if previous is not None:
            if timestamp < previous:
                out_of_order += 1
                continue
            if timestamp > previous + interval:
                missing += int((timestamp - previous) / interval) - 1
        previous = timestamp
    return TimelineAnalysis(duplicates, out_of_order, missing)


def unexplained_gap_count(
    received: Sequence[datetime], persisted: Sequence[datetime], interval: timedelta
) -> int:
    persisted_set = {timestamp.astimezone(UTC) for timestamp in persisted}
    unexplained = 0
    previous: datetime | None = None
    for raw in received:
        timestamp = raw.astimezone(UTC)
        if previous is not None and timestamp > previous + interval:
            expected = previous + interval
            while expected < timestamp:
                unexplained += int(expected not in persisted_set)
                expected += interval
        if previous is None or timestamp > previous:
            previous = timestamp
    return unexplained


def classify_network_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}".lower()
    if "name or service" in text or "getaddrinfo" in text or "dns" in text:
        return "DNS"
    if "proxy" in text:
        return "PROXY"
    if "certificate" in text or "tls" in text or "ssl" in text:
        return "TLS"
    if "429" in text:
        return "HTTP_429"
    if "http status: 4" in text:
        return "HTTP_4XX"
    if "http status: 5" in text:
        return "HTTP_5XX"
    if "timeout" in text or "timed out" in text:
        return "TIMEOUT"
    if "websocket" in text or "connectionclosed" in text:
        return "WS_CLOSE"
    if "connect" in text or "network" in text or "10065" in text:
        return "CONNECT"
    return "UNKNOWN"


@dataclass(slots=True)
class NetworkEvidence:
    rest_requests: int = 0
    rest_failures: int = 0
    network_error_classes: dict[str, int] = field(default_factory=dict)
    confirmed_received: int = 0
    received_timestamps: list[datetime] = field(default_factory=list)
    ws_disconnects: int = 0
    ws_reconnects: int = 0

    def record_error(self, error: BaseException) -> None:
        category = classify_network_error(error)
        self.network_error_classes[category] = self.network_error_classes.get(category, 0) + 1


class ObservedHistoricalProvider(MarketDataProvider):
    def __init__(
        self, delegate: OKXPublicHistoricalDataProvider, evidence: NetworkEvidence
    ) -> None:
        self.delegate = delegate
        self.evidence = evidence

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        self.evidence.rest_requests += 1
        try:
            return self.delegate.get_historical_bars(instrument_id, bar, start, end, limit)
        except Exception as exc:
            self.evidence.rest_failures += 1
            self.evidence.record_error(exc)
            raise

    def close(self) -> None:
        self.delegate.close()


@dataclass(slots=True)
class WriteBoundaryCounters:
    place_order_calls: int = 0
    cancel_order_calls: int = 0
    controlled_demo_write_calls: int = 0
    demo_authorization_consumed: int = 0
    broker_writes: int = 0

    def _blocked(self, counter: str) -> NoReturn:
        setattr(self, counter, int(getattr(self, counter)) + 1)
        raise RuntimeError(f"Phase 4A exchange write guard blocked {counter}")

    def block_place(self, *_args: object, **_kwargs: object) -> NoReturn:
        self._blocked("place_order_calls")

    def block_cancel(self, *_args: object, **_kwargs: object) -> NoReturn:
        self._blocked("cancel_order_calls")

    def block_controlled(self, *_args: object, **_kwargs: object) -> NoReturn:
        self._blocked("controlled_demo_write_calls")

    def block_authorization(self, *_args: object, **_kwargs: object) -> NoReturn:
        self._blocked("demo_authorization_consumed")

    def block_broker(self, *_args: object, **_kwargs: object) -> NoReturn:
        self._blocked("broker_writes")

    @property
    def total(self) -> int:
        return sum(asdict(self).values())


@contextmanager
def exchange_write_guard(counters: WriteBoundaryCounters) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(OkxClient, "place_order", side_effect=counters.block_place)
        )
        stack.enter_context(
            patch.object(OkxClient, "cancel_order", side_effect=counters.block_cancel)
        )
        stack.enter_context(
            patch.object(
                ControlledDemoWriteService, "place_order", side_effect=counters.block_controlled
            )
        )
        stack.enter_context(
            patch.object(
                ControlledDemoWriteService, "cancel_order", side_effect=counters.block_controlled
            )
        )
        stack.enter_context(
            patch.object(
                DemoWriteAuthorization, "consume_place", side_effect=counters.block_authorization
            )
        )
        stack.enter_context(
            patch.object(
                DemoWriteAuthorization, "consume_cancel", side_effect=counters.block_authorization
            )
        )
        stack.enter_context(
            patch.object(OKXDemoBroker, "submit_order", side_effect=counters.block_broker)
        )
        stack.enter_context(
            patch.object(OKXDemoBroker, "place_order", side_effect=counters.block_broker)
        )
        stack.enter_context(
            patch.object(OKXDemoBroker, "cancel_order", side_effect=counters.block_broker)
        )
        yield


def _git_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, text=True, encoding="utf-8"
    ).strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rss_bytes() -> int | str:
    if os.name == "nt":
        try:

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            windll: Any = cast(Any, ctypes).windll
            process = windll.kernel32.GetCurrentProcess()
            if windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except (AttributeError, OSError, ValueError):
            return "NOT_MEASURED"
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return "NOT_MEASURED"


def _open_handles() -> int | str:
    if os.name == "nt":
        try:
            count = ctypes.c_ulong()
            windll: Any = cast(Any, ctypes).windll
            process = windll.kernel32.GetCurrentProcess()
            if windll.kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
                return int(count.value)
        except (AttributeError, OSError, ValueError):
            return "NOT_MEASURED"
    descriptors = Path("/proc/self/fd")
    if descriptors.exists():
        return len(list(descriptors.iterdir()))
    return "NOT_MEASURED"


def _database_snapshot(database_path: Path, run_id: str | None) -> dict[str, object]:
    if not database_path.exists():
        return {"database_exists": False}
    database = Database(f"sqlite:///{database_path}")
    with database.connect() as connection:
        snapshot: dict[str, object] = {
            "database_exists": True,
            "schema_version": MigrationManager(database_path).status().current_version,
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
            "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "quick_check": str(connection.execute("PRAGMA quick_check").fetchone()[0]),
            "database_bytes": database_path.stat().st_size,
            "wal_bytes": database_path.with_name(database_path.name + "-wal").stat().st_size
            if database_path.with_name(database_path.name + "-wal").exists()
            else 0,
        }
        if run_id is None:
            return snapshot
        run = connection.execute(
            "SELECT * FROM continuous_demo_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        lock = connection.execute(
            "SELECT * FROM continuous_run_locks WHERE run_id=?", (run_id,)
        ).fetchone()
        processed = connection.execute(
            "SELECT candle_open_time FROM processed_candles WHERE run_id=? ORDER BY processed_at,candle_open_time",
            (run_id,),
        ).fetchall()
        snapshot.update(
            {
                "run": dict(run) if run is not None else None,
                "lock": dict(lock) if lock is not None else None,
                "persisted_timestamps": [str(row[0]) for row in processed],
                "processed_rows": len(processed),
                "signal_rows": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM strategy_signal_events WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                ),
                "proposal_rows": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM shadow_order_proposals WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                ),
                "submitted_shadow_proposals": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM shadow_order_proposals WHERE run_id=? AND submission_performed=1",
                        (run_id,),
                    ).fetchone()[0]
                ),
                "orders": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM orders WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                ),
                "fills": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM fills WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                ),
            }
        )
        return snapshot


def _resource_snapshot(database_path: Path, log_path: Path) -> dict[str, object]:
    wal_path = database_path.with_name(database_path.name + "-wal")
    return {
        "rss_bytes": _rss_bytes(),
        "cpu_seconds": time.process_time(),
        "open_handles": _open_handles(),
        "database_bytes": database_path.stat().st_size if database_path.exists() else 0,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "log_bytes": log_path.stat().st_size if log_path.exists() else 0,
    }


def _sample_payload(
    *,
    database_path: Path,
    log_path: Path,
    runner: ContinuousVWAPShadowRunner,
    stream: OKXPublicWebSocketProvider,
    network: NetworkEvidence,
    writes: WriteBoundaryCounters,
    sample_type: str,
) -> dict[str, object]:
    session = runner.session
    run_id = session.run_id if session is not None else None
    database = _database_snapshot(database_path, run_id)
    run_value = database.get("run")
    run = run_value if isinstance(run_value, dict) else {}
    observed_timeline = analyze_timeline(network.received_timestamps, BAR_DELTA)
    return {
        "type": sample_type,
        "observed_at": _iso(_utc_now()),
        "run_id": run_id,
        "public_ws_connects": stream.connection_count,
        "public_ws_disconnects": network.ws_disconnects,
        "public_ws_reconnects": stream.reconnect_count,
        "rest_requests": network.rest_requests,
        "rest_failures": network.rest_failures,
        "rest_fallbacks": max(0, network.rest_requests - 1),
        "backfill_requests": max(0, network.rest_requests - 1),
        "confirmed_received": network.confirmed_received,
        "confirmed_processed": session.processed if session is not None else 0,
        "duplicates_seen": observed_timeline.duplicates,
        "out_of_order_seen": observed_timeline.out_of_order,
        "unexplained_gaps": session.gaps if session is not None else 0,
        "signals_generated": (session.holds + session.buys) if session is not None else 0,
        "shadow_proposals_generated": session.proposals if session is not None else 0,
        "last_heartbeat_utc": run.get("last_heartbeat_at"),
        "write_guard": asdict(writes),
        "exchange_write_attempts": writes.total,
        "network_error_classes": dict(network.network_error_classes),
        "database": database,
        "resources": _resource_snapshot(database_path, log_path),
    }


async def _observed_events(
    stream: OKXPublicWebSocketProvider,
    evidence: NetworkEvidence,
    samples_path: Path,
) -> AsyncIterator[PublicWebSocketEvent]:
    async for event in stream.stream_events(INSTRUMENT_ID, BAR_INTERVAL):
        if event.event_type is PublicWebSocketEventType.DISCONNECTED:
            evidence.ws_disconnects += 1
        elif event.event_type is PublicWebSocketEventType.RECONNECTED:
            evidence.ws_reconnects += 1
        elif (
            event.event_type is PublicWebSocketEventType.CANDLE
            and event.candle is not None
            and event.candle.confirmed
        ):
            evidence.confirmed_received += 1
            evidence.received_timestamps.append(event.candle.timestamp)
            append_jsonl(
                samples_path,
                {
                    "type": "confirmed_candle_received",
                    "observed_at": _iso(_utc_now()),
                    "candle_open_time": _iso(event.candle.timestamp),
                    "generation": event.generation,
                },
            )
        yield event


def _initialize_database(database_path: Path) -> tuple[Database, str]:
    if database_path.exists():
        raise FileExistsError(f"Phase 4A database already exists: {database_path}")
    database = Database(f"sqlite:///{database_path}")
    database.initialize()
    generation = RuntimeGenerationService(database)
    generation_id = generation.create_preparing(
        "phase4a-public-shadow",
        "new-dedicated-soak-database",
        {"phase": "4A", "public_market_only": True},
        "Phase 4A Continuous Shadow operational soak",
    )
    generation.activate(generation_id)
    return database, generation_id


def _network_metadata(network: NetworkConfiguration) -> dict[str, object]:
    dns = socket.getaddrinfo("www.okx.com", 443, type=socket.SOCK_STREAM)
    return {
        "network_mode": network.mode.value,
        "proxy_configured": network.proxy_url is not None,
        "dns_resolved": bool(dns),
        "dns_result_count": len(dns),
        "system_clock_utc": _iso(_utc_now()),
    }


async def _operate(
    *,
    artifact_dir: Path,
    database_path: Path,
    config_path: Path,
    duration_seconds: int,
    sample_interval_seconds: int,
    network_mode: str,
    proxy_url: str | None,
) -> dict[str, object]:
    start = _utc_now()
    target_end = start + timedelta(seconds=duration_seconds)
    samples_path = artifact_dir / "samples.jsonl"
    metadata_path = artifact_dir / "metadata.json"
    log_path = artifact_dir / "runtime.log"
    stop_path = artifact_dir / "stop.requested"
    network_config = NetworkConfiguration(NetworkMode(network_mode), proxy_url)
    config: RunConfig = load_run_config(config_path, environ={})
    if config.data.instrument_snapshot is None:
        raise ValueError("Phase 4A requires a tracked instrument snapshot")
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    if (instrument.instrument_id, config.market.bar.lower(), config.strategy.name) != (
        INSTRUMENT_ID,
        BAR_INTERVAL,
        "vwap_shadow",
    ):
        raise ValueError("Phase 4A is fixed to the tracked BTC-USDT 1H VWAP Shadow config")

    database, generation_id = _initialize_database(database_path)
    database_preflight = _database_snapshot(database_path, None)
    if (
        database_preflight.get("journal_mode") != "wal"
        or database_preflight.get("synchronous") != 2
        or database_preflight.get("integrity") != "ok"
    ):
        raise RuntimeError("Phase 4A database preflight did not prove WAL + FULL + integrity")

    network_evidence = NetworkEvidence()
    history = ObservedHistoricalProvider(
        OKXPublicHistoricalDataProvider(network=network_config), network_evidence
    )
    stream = OKXPublicWebSocketProvider(network=network_config)
    runner = ContinuousVWAPShadowRunner(database, config, instrument, history)
    write_counters = WriteBoundaryCounters()
    metadata: dict[str, object] = {
        "phase": "4A",
        "run_id": None,
        "process_id": os.getpid(),
        "start_utc": _iso(start),
        "target_end_utc": _iso(target_end),
        "duration_target_seconds": duration_seconds,
        "sample_interval_seconds": sample_interval_seconds,
        "git_commit": _git_commit(),
        "config": str(config_path.relative_to(REPOSITORY_ROOT)),
        "config_hash": _file_sha256(config_path),
        "instrument": INSTRUMENT_ID,
        "bar_interval": BAR_INTERVAL,
        "vwap_window": int(config.strategy.parameters["vwap_window"]),
        "buy_deviation_bps": str(config.strategy.parameters["buy_deviation_bps"]),
        "database": str(database_path),
        "artifact_path": str(artifact_dir),
        "runtime_generation": generation_id,
        "network": _network_metadata(network_config),
        "database_preflight": database_preflight,
        "exchange_write_guard": "installed",
    }
    write_json_atomic(metadata_path, metadata)

    task: asyncio.Task[object] | None = None
    graceful_shutdown = False
    pending_tasks = 0
    uncaught_exceptions = 0
    database_errors = 0
    database_locked_events = 0
    result_payload: dict[str, object] = {}
    try:
        with exchange_write_guard(write_counters):
            task = asyncio.create_task(
                runner.run_events(_observed_events(stream, network_evidence, samples_path))
            )
            while runner.session is None and not task.done():
                await asyncio.sleep(0.1)
            if task.done():
                await task
                raise RuntimeError("Continuous Shadow ended before session initialization")
            assert runner.session is not None
            metadata["run_id"] = runner.session.run_id
            write_json_atomic(metadata_path, metadata)
            append_jsonl(
                samples_path,
                _sample_payload(
                    database_path=database_path,
                    log_path=log_path,
                    runner=runner,
                    stream=stream,
                    network=network_evidence,
                    writes=write_counters,
                    sample_type="start",
                ),
            )
            startup_evidence_written = False
            next_renew = time.monotonic()
            next_sample = time.monotonic() + sample_interval_seconds
            while not task.done():
                now_monotonic = time.monotonic()
                if not startup_evidence_written and stream.connection_count >= 1:
                    session = runner.session
                    if (
                        session is not None
                        and network_evidence.rest_requests >= 1
                        and network_evidence.rest_failures == 0
                    ):
                        startup_evidence_written = write_startup_evidence(
                            artifact_dir / "startup_evidence.json",
                            run_id=session.run_id,
                            soak_id=artifact_dir.name,
                            process_id=os.getpid(),
                            instrument=INSTRUMENT_ID,
                            bar_interval=BAR_INTERVAL,
                            database_path=database_path,
                            git_commit=str(metadata["git_commit"]),
                            config_hash=str(metadata["config_hash"]),
                            runtime_generation=generation_id,
                        )
                if stop_path.exists() or _utc_now() >= target_end:
                    await stream.stop()
                    break
                if now_monotonic >= next_renew:
                    try:
                        session = runner.session
                        session.lock.renew(session.run_id)
                        runner.repository.heartbeat(
                            session.run_id,
                            session.processed,
                            session.holds + session.buys,
                            session.proposals,
                            private_status="not_created",
                            public_status=stream.state.value,
                        )
                    except Exception as exc:
                        database_errors += 1
                        if "locked" in str(exc).lower():
                            database_locked_events += 1
                        raise
                    next_renew = now_monotonic + LOCK_RENEW_INTERVAL_SECONDS
                if now_monotonic >= next_sample:
                    append_jsonl(
                        samples_path,
                        _sample_payload(
                            database_path=database_path,
                            log_path=log_path,
                            runner=runner,
                            stream=stream,
                            network=network_evidence,
                            writes=write_counters,
                            sample_type="periodic",
                        ),
                    )
                    next_sample = now_monotonic + sample_interval_seconds
                await asyncio.sleep(1)
            result = await task
            result_payload = asdict(result)
            graceful_shutdown = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        uncaught_exceptions += 1
        if network_evidence.rest_failures == 0:
            network_evidence.record_error(exc)
        raise
    finally:
        with contextlib.suppress(Exception):
            await stream.stop()
        history.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending_tasks = sum(
            item is not asyncio.current_task() and not item.done() for item in asyncio.all_tasks()
        )
        if runner.session is not None:
            append_jsonl(
                samples_path,
                _sample_payload(
                    database_path=database_path,
                    log_path=log_path,
                    runner=runner,
                    stream=stream,
                    network=network_evidence,
                    writes=write_counters,
                    sample_type="end",
                ),
            )
        runtime_summary = {
            "graceful_shutdown": graceful_shutdown,
            "pending_tasks_after_shutdown": pending_tasks,
            "uncaught_exceptions": uncaught_exceptions,
            "database_errors": database_errors,
            "database_locked_events": database_locked_events,
            "write_guard": asdict(write_counters),
            "network_error_classes": network_evidence.network_error_classes,
            "result": result_payload,
            "end_utc": _iso(_utc_now()),
        }
        write_json_atomic(artifact_dir / "runtime_summary.json", runtime_summary)
    return finalize_artifact(artifact_dir)


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_STILL_ACTIVE_EXIT_CODE = 259
_WINDOWS_ERROR_ACCESS_DENIED = 5
_WINDOWS_ERROR_INVALID_PARAMETER = 87


def _process_exists(process_id: int) -> bool:
    """Probe whether one process is running, without ever signaling it.

    PID values <= 0 address process groups or special system contexts, not a
    single probeable process, and are refused: on POSIX ``kill(0, sig)``
    targets the caller's whole process group, which is never what a liveness
    probe means here.
    """
    if process_id <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_exists(process_id)
    try:
        # POSIX signal 0 is answered by the kernel without delivery.
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_process_exists(process_id: int) -> bool:
    """Windows liveness via query-only handles; never os.kill(pid, 0).

    On Windows ``os.kill(pid, 0)`` aliases CTRL_C_EVENT, so a probe can raise
    Ctrl+C inside the target console process. OpenProcess with
    PROCESS_QUERY_LIMITED_INFORMATION only reads state. A terminated process
    reports its real exit code instead of STILL_ACTIVE, so recently stopped
    (even not-yet-reaped) processes read as dead, which signal-based probing
    got wrong.
    """
    if sys.platform != "win32":
        # The ctypes.WinDLL symbols below are declared Windows-only in
        # typeshed, so this early exit also keeps Linux-platform mypy analysis
        # out of the Win32 body instead of failing on them.
        raise RuntimeError("Windows process probe used on non-Windows platform")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return not _windows_open_error_reports_missing(ctypes.get_last_error())
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        # A process that exits with code 259 is indistinguishable from a live
        # one; that is an inherent GetExitCodeProcess limitation.
        return exit_code.value == _WINDOWS_STILL_ACTIVE_EXIT_CODE
    finally:
        kernel32.CloseHandle(handle)


def _windows_open_error_reports_missing(error_code: int) -> bool:
    """Map a failed OpenProcess to a not-found verdict, failing closed.

    ERROR_INVALID_PARAMETER is how Windows reports a PID with no process.
    Anything else (notably ERROR_ACCESS_DENIED, a live process refusing query
    access) must not be read as dead: claiming a running soak is gone is the
    unsafe mistake, so unknown errors keep reporting the process as alive.
    """
    return error_code == _WINDOWS_ERROR_INVALID_PARAMETER


def _parse_timestamps(values: Sequence[object]) -> list[datetime]:
    result: list[datetime] = []
    for value in values:
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            raise ValueError("artifact timestamp is missing timezone")
        result.append(timestamp.astimezone(UTC))
    return result


def _as_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _rss_summary(samples: Sequence[dict[str, Any]]) -> dict[str, object]:
    values = [
        resources["rss_bytes"]
        for sample in samples
        if isinstance((resources := sample.get("resources")), dict)
        and isinstance(resources.get("rss_bytes"), int)
    ]
    if not values:
        return {
            "rss_start": "NOT_MEASURED",
            "rss_median": "NOT_MEASURED",
            "rss_max": "NOT_MEASURED",
            "rss_end": "NOT_MEASURED",
            "rss_classification": "NOT_MEASURED",
        }
    growth = values[-1] - values[0]
    threshold = max(32 * 1024 * 1024, int(values[0] * 0.25))
    classification = "POSSIBLE_GROWTH" if growth > threshold else "STABLE"
    return {
        "rss_start": values[0],
        "rss_median": int(statistics.median(values)),
        "rss_max": max(values),
        "rss_end": values[-1],
        "rss_classification": classification,
    }


def _log_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"bytes": 0, "warnings": 0, "errors": 0, "sensitive_value_matches": 0}
    text = path.read_text(encoding="utf-8", errors="ignore")
    lowered = text.lower()
    return {
        "bytes": path.stat().st_size,
        "warnings": lowered.count("warning"),
        "errors": lowered.count("error"),
        "sensitive_value_matches": sum(
            text.count(value) for value in _sensitive_environment_values()
        ),
    }


def finalize_artifact(artifact_dir: Path) -> dict[str, object]:
    metadata_path = artifact_dir / "metadata.json"
    summary_path = artifact_dir / "runtime_summary.json"
    if not metadata_path.exists() or not summary_path.exists():
        raise ValueError("Phase 4A run is incomplete: metadata or runtime summary is missing")
    metadata = _load_json(metadata_path)
    runtime_summary = _load_json(summary_path)
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Phase 4A run metadata has no runtime run_id")
    database_path = Path(str(metadata["database"]))
    samples = read_jsonl(artifact_dir / "samples.jsonl")
    database = _database_snapshot(database_path, run_id)
    received = _parse_timestamps(
        [
            sample["candle_open_time"]
            for sample in samples
            if sample.get("type") == "confirmed_candle_received"
        ]
    )
    persisted_value = database.get("persisted_timestamps")
    persisted_values: list[object] = persisted_value if isinstance(persisted_value, list) else []
    persisted = _parse_timestamps(persisted_values)
    received_analysis = analyze_timeline(received, BAR_DELTA)
    persisted_analysis = analyze_timeline(persisted, BAR_DELTA)
    received_set = set(received)
    persisted_set = set(persisted)
    missing_persisted = len(received_set - persisted_set)
    unexplained_gaps = unexplained_gap_count(received, persisted, BAR_DELTA)
    run_value = database.get("run")
    run: dict[str, Any] = run_value if isinstance(run_value, dict) else {}
    write_guard = runtime_summary.get("write_guard")
    write_values = list(write_guard.values()) if isinstance(write_guard, dict) else [1]
    write_attempts = sum(_as_int(value) for value in write_values)
    submitted = _as_int(database.get("submitted_shadow_proposals"))
    orders = _as_int(database.get("orders"))
    fills = _as_int(database.get("fills"))
    processed_rows = _as_int(database.get("processed_rows"))
    signal_rows = _as_int(database.get("signal_rows"))
    proposal_rows = _as_int(database.get("proposal_rows"))
    counter_reconciliation = (
        _as_int(run.get("processed_candle_count"), default=-1) == processed_rows
        and _as_int(run.get("generated_signal_count"), default=-1) == signal_rows
        and _as_int(run.get("shadow_proposal_count"), default=-1) == proposal_rows
    )
    start = datetime.fromisoformat(str(metadata["start_utc"])).astimezone(UTC)
    end = datetime.fromisoformat(str(runtime_summary["end_utc"])).astimezone(UTC)
    duration_seconds = max(0.0, (end - start).total_seconds())
    target_seconds = int(metadata["duration_target_seconds"])
    periodic = [sample for sample in samples if sample.get("type") in {"start", "periodic", "end"}]
    latest = periodic[-1] if periodic else {}
    resources = _rss_summary(periodic)
    heartbeat = run.get("last_heartbeat_at")
    heartbeat_time = (
        datetime.fromisoformat(heartbeat).astimezone(UTC)
        if isinstance(heartbeat, str) and heartbeat
        else None
    )
    heartbeat_not_stuck = heartbeat_time is not None and end - heartbeat_time <= timedelta(
        seconds=30
    )
    log_summary = _log_summary(artifact_dir / "runtime.log")
    correctness = {
        "exchange_write_attempts": write_attempts + submitted + orders + fills,
        "database_integrity": database.get("integrity") == "ok"
        and database.get("quick_check") == "ok",
        "duplicate_persisted_candles": persisted_analysis.duplicates,
        "out_of_order_persisted_candles": persisted_analysis.out_of_order,
        "unexplained_missing_persisted_candles": missing_persisted,
        "uncaught_exceptions": int(runtime_summary.get("uncaught_exceptions", 1)),
        "heartbeat_not_stuck": heartbeat_not_stuck,
        "runtime_state_consistent": counter_reconciliation,
        "graceful_shutdown": bool(runtime_summary.get("graceful_shutdown")),
        "pending_tasks_after_shutdown": int(
            runtime_summary.get("pending_tasks_after_shutdown", -1)
        ),
        "sensitive_log_values": _as_int(log_summary.get("sensitive_value_matches")),
    }
    gates_passed = (
        correctness["exchange_write_attempts"] == 0
        and correctness["database_integrity"] is True
        and correctness["duplicate_persisted_candles"] == 0
        and correctness["out_of_order_persisted_candles"] == 0
        and correctness["unexplained_missing_persisted_candles"] == 0
        and correctness["uncaught_exceptions"] == 0
        and correctness["heartbeat_not_stuck"] is True
        and correctness["runtime_state_consistent"] is True
        and correctness["graceful_shutdown"] is True
        and correctness["pending_tasks_after_shutdown"] == 0
        and correctness["sensitive_log_values"] == 0
    )
    report: dict[str, object] = {
        "phase": "4A",
        "run_id": run_id,
        "git_commit": metadata["git_commit"],
        "instrument": metadata["instrument"],
        "bar_interval": metadata["bar_interval"],
        "start_utc": metadata["start_utc"],
        "end_utc": runtime_summary["end_utc"],
        "duration_seconds": duration_seconds,
        "duration_target_seconds": target_seconds,
        "duration_target_met": duration_seconds >= target_seconds,
        "network": {
            "public_ws_connects": latest.get("public_ws_connects", 0),
            "disconnects": latest.get("public_ws_disconnects", 0),
            "reconnects": latest.get("public_ws_reconnects", 0),
            "rest_requests": latest.get("rest_requests", 0),
            "rest_failures": latest.get("rest_failures", 0),
            "fallbacks": latest.get("rest_fallbacks", 0),
            "network_error_classes": runtime_summary.get("network_error_classes", {}),
        },
        "candles": {
            "confirmed_received": len(received),
            "confirmed_processed": processed_rows,
            "duplicates_seen": received_analysis.duplicates,
            "duplicates_persisted": persisted_analysis.duplicates,
            "out_of_order_seen": received_analysis.out_of_order,
            "out_of_order_persisted": persisted_analysis.out_of_order,
            "unexplained_gaps": unexplained_gaps,
            "missing_persisted": missing_persisted,
        },
        "strategy_state": {
            "processed_count": processed_rows,
            "signals": signal_rows,
            "shadow_proposals": proposal_rows,
            "runtime_generation": run.get("generation_id"),
            "last_confirmed_timestamp": persisted[-1].isoformat() if persisted else None,
            "state_consistent": counter_reconciliation,
        },
        "exchange_safety": {
            "guard_counters": write_guard,
            "submitted_shadow_proposals": submitted,
            "orders": orders,
            "fills": fills,
        },
        "database": {
            "journal_mode": database.get("journal_mode"),
            "synchronous": database.get("synchronous"),
            "integrity": database.get("integrity"),
            "size_start": metadata.get("database_preflight", {}).get("database_bytes")
            if isinstance(metadata.get("database_preflight"), dict)
            else None,
            "size_end": database.get("database_bytes"),
            "wal_max": max(
                (
                    int(resources_value.get("wal_bytes", 0))
                    for sample in periodic
                    if isinstance((resources_value := sample.get("resources")), dict)
                ),
                default=0,
            ),
            "locked_events": runtime_summary.get("database_locked_events", 0),
        },
        "resources": {
            **resources,
            "cpu_seconds_end": (
                latest.get("resources", {}).get("cpu_seconds")
                if isinstance(latest.get("resources"), dict)
                else "NOT_MEASURED"
            ),
            "open_handles_end": (
                latest.get("resources", {}).get("open_handles")
                if isinstance(latest.get("resources"), dict)
                else "NOT_MEASURED"
            ),
            "log_bytes_end": (
                latest.get("resources", {}).get("log_bytes")
                if isinstance(latest.get("resources"), dict)
                else 0
            ),
            "log_summary": log_summary,
        },
        "runtime_health": {
            "heartbeat": heartbeat,
            "uncaught_exceptions": runtime_summary.get("uncaught_exceptions"),
            "natural_reconnect_exercised": bool(int(latest.get("public_ws_reconnects", 0))),
            "graceful_shutdown": runtime_summary.get("graceful_shutdown"),
            "pending_tasks": runtime_summary.get("pending_tasks_after_shutdown"),
        },
        "correctness": correctness,
        "correctness_gates_passed": gates_passed,
        "shakedown_passed": gates_passed
        and int(latest.get("public_ws_connects", 0)) >= 1
        and len(periodic) >= 2,
        "continuous_shadow_24h_ready": gates_passed and duration_seconds >= 24 * 60 * 60,
        "what_was_not_tested": [
            "forced_network_outage",
            "process_kill_recovery",
            "database_lock_injection",
            "os_crash",
            "power_loss",
            "continuous_demo_execution",
        ],
    }
    write_json_atomic(artifact_dir / "final_report.json", report)
    return report


def run_foreground(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir).resolve()
    database_path = Path(args.database).resolve()
    config_path = Path(args.config).resolve()
    if artifact_dir.exists():
        unexpected = {path.name for path in artifact_dir.iterdir()} - {"runtime.log"}
        if unexpected:
            raise FileExistsError(f"artifact directory is not empty: {artifact_dir}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(
            _operate(
                artifact_dir=artifact_dir,
                database_path=database_path,
                config_path=config_path,
                duration_seconds=args.duration_seconds,
                sample_interval_seconds=args.sample_interval_seconds,
                network_mode=args.network_mode,
                proxy_url=args.proxy_url,
            )
        )
    except KeyboardInterrupt:
        print(json.dumps({"status": "keyboard_interrupt_received", "artifact": str(artifact_dir)}))
        return 130
    except Exception as exc:
        failure = {
            "status": "failed",
            "error_class": type(exc).__name__,
            "error_category": classify_network_error(exc),
            "occurred_at": _iso(_utc_now()),
        }
        write_json_atomic(artifact_dir / "startup_failure.json", failure)
        print(json.dumps(failure))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _automatic_paths() -> tuple[str, Path, Path]:
    soak_id = _utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    return (
        soak_id,
        DEFAULT_ARTIFACT_ROOT / soak_id,
        DEFAULT_DATABASE_ROOT / f"{soak_id}.db",
    )


_STARTUP_EVIDENCE_SCHEMA_VERSION = 1


def write_startup_evidence(
    path: Path,
    *,
    run_id: str,
    soak_id: str,
    process_id: int,
    instrument: str,
    bar_interval: str,
    database_path: Path,
    git_commit: str,
    config_hash: str,
    runtime_generation: str,
) -> bool:
    """Record one-shot startup proof: REST bootstrap + real public WS connect.

    The runner writes this the moment both real startup conditions hold, so
    the parent's startup gate never depends on the periodic sampling cadence.
    The file is immutable evidence: a second write is a no-op, and the payload
    carries run identity only — never credentials.
    """
    if path.exists():
        return False
    write_json_atomic(
        path,
        {
            "schema_version": _STARTUP_EVIDENCE_SCHEMA_VERSION,
            "run_id": run_id,
            "soak_id": soak_id,
            "process_id": process_id,
            "observed_at_utc": _iso(_utc_now()),
            "rest_bootstrap": True,
            "public_ws_connected": True,
            "instrument": instrument,
            "bar_interval": bar_interval,
            "database_path": str(database_path),
            "git_commit": git_commit,
            "config_hash": config_hash,
            "runtime_generation": runtime_generation,
            "exchange_write_guard_installed": True,
        },
    )
    return True


def _validate_startup_evidence(
    evidence: Mapping[str, object],
    *,
    metadata: Mapping[str, object],
    soak_id: str,
) -> None:
    """Fail closed unless the evidence proves this run's real startup.

    The evidence must carry this run's identity — run_id, soak_id, the
    runner's self-recorded process_id from metadata.json and the config hash —
    plus positive proof of REST bootstrap, a real public WebSocket connection
    and the exchange write guard. The runner's own PID is the canonical run
    identity (status/stop/finalize operate on it too); the direct child of
    `start` can be a venv launcher that re-execs the interpreter. Anything
    stale, malformed or foreign is rejected so it can never pass as success.
    """
    if evidence.get("schema_version") != _STARTUP_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("startup evidence schema_version mismatch")
    for required_field in (
        "rest_bootstrap",
        "public_ws_connected",
        "exchange_write_guard_installed",
    ):
        if evidence.get(required_field) is not True:
            raise ValueError(f"startup evidence {required_field} is not proven")
    if not evidence.get("run_id") or evidence.get("run_id") != metadata.get("run_id"):
        raise ValueError("startup evidence run_id does not match run metadata")
    if _as_int(evidence.get("process_id"), default=-1) != _as_int(
        metadata.get("process_id"), default=-2
    ):
        raise ValueError("startup evidence process_id does not match run metadata")
    if evidence.get("soak_id") != soak_id:
        raise ValueError("startup evidence soak_id does not match the artifact")
    metadata_config_hash = metadata.get("config_hash")
    if metadata_config_hash is not None and evidence.get("config_hash") != metadata_config_hash:
        raise ValueError("startup evidence config_hash does not match run metadata")


def _await_startup_evidence(
    *,
    artifact_dir: Path,
    process: subprocess.Popen[bytes],
    soak_id: str,
    timeout_seconds: int,
    poll_interval_seconds: float = 0.5,
) -> dict[str, object]:
    """Wait for the child's one-shot startup evidence, independent of sampling.

    Success requires evidence written by the runner itself, bound to this
    run's identity, while the spawned process is still alive. A child that
    exits first fails immediately; invalid evidence never counts as success
    and the deadline still fails closed with a graceful stop request.
    """
    evidence_path = artifact_dir / "startup_evidence.json"
    metadata_path = artifact_dir / "metadata.json"
    failure_path = artifact_dir / "startup_failure.json"
    deadline = time.monotonic() + timeout_seconds
    last_rejection = "startup evidence not observed"
    while time.monotonic() < deadline:
        if evidence_path.exists():
            try:
                evidence = _load_json(evidence_path)
                _validate_startup_evidence(
                    evidence,
                    metadata=_load_json(metadata_path) if metadata_path.exists() else {},
                    soak_id=soak_id,
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                last_rejection = f"invalid startup evidence: {error}"
            else:
                if process.poll() is not None:
                    raise RuntimeError("Phase 4A process exited right after startup evidence")
                return evidence
        if process.poll() is not None:
            failure = _load_json(failure_path) if failure_path.exists() else {}
            raise RuntimeError(f"Phase 4A process exited during startup: {failure}")
        time.sleep(poll_interval_seconds)
    (artifact_dir / "stop.requested").touch(exist_ok=True)
    raise TimeoutError(
        f"Phase 4A startup was not proven within {timeout_seconds}s "
        f"(last rejection: {last_rejection}): {artifact_dir}"
    )


def start_detached(args: argparse.Namespace) -> int:
    soak_id, artifact_dir, database_path = _automatic_paths()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    log_path = artifact_dir / "runtime.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--artifact-dir",
        str(artifact_dir),
        "--database",
        str(database_path),
        "--config",
        str(Path(args.config).resolve()),
        "--duration-seconds",
        str(args.duration_seconds),
        "--sample-interval-seconds",
        str(args.sample_interval_seconds),
        "--network-mode",
        str(args.network_mode),
    ]
    if args.proxy_url is not None:
        command.extend(("--proxy-url", str(args.proxy_url)))
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    creation_flags = 0
    if os.name == "nt":
        subprocess_module = cast(Any, subprocess)
        creation_flags = int(subprocess_module.CREATE_NEW_PROCESS_GROUP) | int(
            subprocess_module.CREATE_NO_WINDOW
        )
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
    evidence = _await_startup_evidence(
        artifact_dir=artifact_dir,
        process=process,
        soak_id=soak_id,
        timeout_seconds=args.startup_wait_seconds,
    )
    metadata = _load_json(artifact_dir / "metadata.json")
    metadata.update({"soak_id": soak_id, "process_status": "running", "startup_evidence": evidence})
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


def status_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir).resolve()
    metadata = _load_json(artifact_dir / "metadata.json")
    process_id = int(metadata["process_id"])
    samples = read_jsonl(artifact_dir / "samples.jsonl")
    periodic = [sample for sample in samples if sample.get("type") in {"start", "periodic", "end"}]
    payload = {
        "run_id": metadata.get("run_id"),
        "process_id": process_id,
        "process_alive": _process_exists(process_id),
        "start_utc": metadata.get("start_utc"),
        "target_end_utc": metadata.get("target_end_utc"),
        "artifact_path": str(artifact_dir),
        "latest_sample": periodic[-1] if periodic else None,
        "final_report": _load_json(artifact_dir / "final_report.json")
        if (artifact_dir / "final_report.json").exists()
        else None,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


def stop_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir).resolve()
    metadata = _load_json(artifact_dir / "metadata.json")
    stop_path = artifact_dir / "stop.requested"
    stop_path.touch(exist_ok=True)
    process_id = int(metadata["process_id"])
    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline and _process_exists(process_id):
        time.sleep(0.5)
    payload = {
        "run_id": metadata.get("run_id"),
        "stop_requested": True,
        "process_alive": _process_exists(process_id),
        "final_report_present": (artifact_dir / "final_report.json").exists(),
    }
    print(json.dumps(payload))
    return 0 if not payload["process_alive"] else 2


def finalize_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir).resolve()
    metadata = _load_json(artifact_dir / "metadata.json")
    if _process_exists(int(metadata["process_id"])):
        raise RuntimeError("cannot finalize a running Phase 4A process")
    report = finalize_artifact(artifact_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="run one foreground soak or shakedown")
    run.add_argument("--artifact-dir", required=True)
    run.add_argument("--database", required=True)
    run.add_argument("--config", default=str(DEFAULT_CONFIG))
    run.add_argument("--duration-seconds", type=int, required=True)
    run.add_argument("--sample-interval-seconds", type=int, default=300)
    run.add_argument("--network-mode", choices=[mode.value for mode in NetworkMode], default="env")
    run.add_argument("--proxy-url")
    run.set_defaults(handler=run_foreground)

    start = subcommands.add_parser("start", help="start a detached soak process")
    start.add_argument("--config", default=str(DEFAULT_CONFIG))
    start.add_argument("--duration-seconds", type=int, default=24 * 60 * 60)
    start.add_argument("--sample-interval-seconds", type=int, default=300)
    start.add_argument("--startup-wait-seconds", type=int, default=60)
    start.add_argument(
        "--network-mode", choices=[mode.value for mode in NetworkMode], default="env"
    )
    start.add_argument("--proxy-url")
    start.set_defaults(handler=start_detached)

    status = subcommands.add_parser("status", help="read current evidence without mutation")
    status.add_argument("--artifact-dir", required=True)
    status.set_defaults(handler=status_command)

    stop = subcommands.add_parser("stop", help="request and await graceful shutdown")
    stop.add_argument("--artifact-dir", required=True)
    stop.add_argument("--wait-seconds", type=int, default=60)
    stop.set_defaults(handler=stop_command)

    finalize = subcommands.add_parser("finalize", help="rebuild the final report from evidence")
    finalize.add_argument("--artifact-dir", required=True)
    finalize.set_defaults(handler=finalize_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if getattr(args, "duration_seconds", 1) <= 0:
        raise ValueError("duration must be positive")
    if getattr(args, "sample_interval_seconds", 1) <= 0:
        raise ValueError("sample interval must be positive")
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "error_category": classify_network_error(exc),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
