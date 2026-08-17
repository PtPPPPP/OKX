"""Resumable, public-only OKX candle cache for VWAP signal research."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.domain.market import Candle
from app.market.historical_data import BAR_INTERVALS, MarketDataError, okx_bar
from app.market.network import NetworkConfiguration, NetworkMode

_ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"
_SOURCE = "OKX_PUBLIC_REST_HISTORY_CANDLES"
_PAGE_LIMIT = 300


class PublicCandleTransport(Protocol):
    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class RawCandle:
    timestamp_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    volume_ccy: str
    volume_quote: str
    confirm: str
    instrument: str
    bar: str
    source: str
    downloaded_at: str
    payload: tuple[str, ...]

    @property
    def confirmed(self) -> bool:
        return self.confirm == "1"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    instrument: str
    timeframe: str
    requested_start: str
    requested_end: str
    actual_start: str | None
    actual_end: str | None
    raw_rows: int
    normalized_rows: int
    duplicate_count: int
    missing_count: int
    invalid_ohlc_count: int
    out_of_order_count: int
    unconfirmed_count: int
    source: str
    download_batches: int
    download_failures: int
    retry_count: int
    dataset_hash: str
    generated_at: str
    confirmed_candle_only: bool
    resume: bool
    status: str
    missing_timestamps: tuple[str, ...]


class HistoricalCandleCache:
    """Durable raw cache. A page and its checkpoint commit atomically."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.database_path = directory / "cache.sqlite3"
        self.raw_path = directory / "raw" / "candles.jsonl"
        self.normalized_path = directory / "normalized" / "candles.csv"
        self.manifest_path = directory / "data_manifest.json"
        directory.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS raw_candles (
                    timestamp_ms INTEGER PRIMARY KEY,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    volume_ccy TEXT NOT NULL,
                    volume_quote TEXT NOT NULL,
                    confirm_value TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    source TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS download_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def reset(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM raw_candles")
            connection.execute("DELETE FROM download_state")

    def commit_page(self, rows: Sequence[RawCandle], *, next_after: int) -> int:
        duplicates = 0
        with self._connect() as connection:
            for row in rows:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO raw_candles(
                       timestamp_ms,open,high,low,close,volume,volume_ccy,volume_quote,
                       confirm_value,instrument,bar,source,downloaded_at,payload_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row.timestamp_ms,
                        row.open,
                        row.high,
                        row.low,
                        row.close,
                        row.volume,
                        row.volume_ccy,
                        row.volume_quote,
                        row.confirm,
                        row.instrument,
                        row.bar,
                        row.source,
                        row.downloaded_at,
                        json.dumps(row.payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                duplicates += int(cursor.rowcount == 0)
            connection.execute(
                "INSERT OR REPLACE INTO download_state(key,value) VALUES('next_after',?)",
                (str(next_after),),
            )
        return duplicates

    def state(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM download_state WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_state(self, key: str, value: int | str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO download_state(key,value) VALUES(?,?)",
                (key, str(value)),
            )

    def rows(self, *, start_ms: int, end_ms: int) -> list[RawCandle]:
        with self._connect() as connection:
            records = connection.execute(
                "SELECT * FROM raw_candles WHERE timestamp_ms BETWEEN ? AND ? "
                "ORDER BY timestamp_ms",
                (start_ms, end_ms),
            ).fetchall()
        return [
            RawCandle(
                timestamp_ms=int(row["timestamp_ms"]),
                open=str(row["open"]),
                high=str(row["high"]),
                low=str(row["low"]),
                close=str(row["close"]),
                volume=str(row["volume"]),
                volume_ccy=str(row["volume_ccy"]),
                volume_quote=str(row["volume_quote"]),
                confirm=str(row["confirm_value"]),
                instrument=str(row["instrument"]),
                bar=str(row["bar"]),
                source=str(row["source"]),
                downloaded_at=str(row["downloaded_at"]),
                payload=tuple(json.loads(str(row["payload_json"]))),
            )
            for row in records
        ]

    def export(self, rows: Sequence[RawCandle], manifest: DownloadResult) -> None:
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_path.open("w", encoding="utf-8", newline="\n") as file:
            for row in rows:
                payload = asdict(row)
                payload["payload"] = list(row.payload)
                file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self.normalized_path.parent.mkdir(parents=True, exist_ok=True)
        fields = (
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "volume_ccy",
            "volume_quote",
            "confirm",
            "instrument",
            "bar",
            "source",
            "downloaded_at",
        )
        with self.normalized_path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                if not row.confirmed:
                    continue
                writer.writerow(
                    {
                        "timestamp": datetime.fromtimestamp(
                            row.timestamp_ms / 1000, tz=UTC
                        ).isoformat(),
                        "open": row.open,
                        "high": row.high,
                        "low": row.low,
                        "close": row.close,
                        "volume": row.volume,
                        "volume_ccy": row.volume_ccy,
                        "volume_quote": row.volume_quote,
                        "confirm": row.confirm,
                        "instrument": row.instrument,
                        "bar": row.bar,
                        "source": row.source,
                        "downloaded_at": row.downloaded_at,
                    }
                )
        self.manifest_path.write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
        )


class OKXHistoricalCandleDownloader:
    """Backward paginator for the credential-free OKX history endpoint."""

    def __init__(
        self,
        cache: HistoricalCandleCache,
        *,
        network: NetworkConfiguration,
        transport: PublicCandleTransport | None = None,
        retry_limit: int = 3,
        retry_delay_seconds: float = 0.5,
        page_limit: int = _PAGE_LIMIT,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if network.mode is not NetworkMode.PROXY:
            raise ValueError("long-running research download requires explicit proxy mode")
        if retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        if not 1 <= page_limit <= _PAGE_LIMIT:
            raise ValueError("page_limit must be in 1..300")
        self.cache = cache
        self.network = network
        self.transport = transport or network.create_http_client(
            timeout=httpx.Timeout(20.0, connect=10.0)
        )
        self.retry_limit = retry_limit
        self.retry_delay_seconds = retry_delay_seconds
        self.page_limit = page_limit
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    def download(
        self,
        *,
        instrument: str,
        bar: str,
        start: datetime,
        end: datetime,
        resume: bool = True,
    ) -> DownloadResult:
        interval = BAR_INTERVALS.get(bar.lower())
        if interval is None:
            raise MarketDataError(f"unsupported bar: {bar}")
        start = _utc(start)
        end = _utc(end)
        if start >= end:
            raise ValueError("requested history start must be before end")
        if not resume:
            self.cache.reset()
        interval_ms = int(interval.total_seconds() * 1000)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        identity = json.dumps(
            {
                "instrument": instrument,
                "bar": bar.lower(),
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        stored_identity = self.cache.state("request_identity")
        if resume and stored_identity is not None and stored_identity != identity:
            raise ValueError("resume cache belongs to a different history request")
        self.cache.set_state("request_identity", identity)
        after = _state_int(self.cache, "next_after", end_ms + interval_ms)
        batches = _state_int(self.cache, "download_batches")
        failures = _state_int(self.cache, "download_failures")
        retries = _state_int(self.cache, "retry_count")
        duplicates = _state_int(self.cache, "duplicate_count")
        while after > start_ms:
            payload: dict[str, Any] | None = None
            for attempt in range(self.retry_limit + 1):
                try:
                    response = self.transport.get(
                        _ENDPOINT,
                        params={
                            "instId": instrument,
                            "bar": okx_bar(bar),
                            "after": str(after),
                            "limit": str(self.page_limit),
                        },
                        headers={"Accept": "application/json"},
                    )
                    if response.status_code != 200:
                        raise MarketDataError(f"OKX history HTTP {response.status_code}")
                    decoded = response.json()
                    if not isinstance(decoded, dict) or decoded.get("code") != "0":
                        raise MarketDataError("OKX history response code is not zero")
                    payload = decoded
                    break
                except (httpx.HTTPError, ValueError, MarketDataError):
                    failures += 1
                    self.cache.set_state("download_failures", failures)
                    if attempt >= self.retry_limit:
                        raise
                    retries += 1
                    self.cache.set_state("retry_count", retries)
                    self.sleep(self.retry_delay_seconds * 2**attempt)
            assert payload is not None
            raw = payload.get("data")
            if not isinstance(raw, list):
                raise MarketDataError("OKX history data is not a list")
            if not raw:
                break
            downloaded_at = self.clock().astimezone(UTC).isoformat()
            page = [
                _parse_raw(row, instrument=instrument, bar=bar, downloaded_at=downloaded_at)
                for row in raw
            ]
            oldest = min(row.timestamp_ms for row in page)
            if oldest >= after:
                raise MarketDataError("OKX history pagination did not move backward")
            in_range = [row for row in page if start_ms <= row.timestamp_ms <= end_ms]
            duplicates += self.cache.commit_page(in_range, next_after=oldest)
            batches += 1
            self.cache.set_state("download_batches", batches)
            self.cache.set_state("duplicate_count", duplicates)
            after = oldest
            if len(raw) < self.page_limit or oldest <= start_ms:
                break
        rows = self.cache.rows(start_ms=start_ms, end_ms=end_ms)
        result = _manifest(
            rows,
            instrument=instrument,
            bar=bar,
            start=start,
            end=end,
            interval=interval,
            duplicate_count=duplicates,
            batches=batches,
            failures=failures,
            retries=retries,
            generated_at=self.clock().astimezone(UTC),
            resume=resume,
        )
        self.cache.export(rows, result)
        return result


def load_normalized_candles(path: Path, *, bar: str) -> list[Candle]:
    if "prospective_oos" in {part.lower() for part in path.parts}:
        raise MarketDataError(
            "historical research reader rejects prospective_oos; "
            "use explicit frozen-candidate validation access"
        )
    interval = BAR_INTERVALS.get(bar.lower())
    if interval is None:
        raise MarketDataError(f"unsupported bar: {bar}")
    candles: list[Candle] = []
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row["confirm"] != "1":
                raise MarketDataError("normalized research data contains unconfirmed candle")
            candles.append(
                Candle(
                    timestamp=_utc(datetime.fromisoformat(row["timestamp"])),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                    confirmed=True,
                )
            )
    if any(b.timestamp <= a.timestamp for a, b in pairwise(candles)):
        raise MarketDataError("normalized research candles are not strictly ordered")
    return candles


def _parse_raw(value: object, *, instrument: str, bar: str, downloaded_at: str) -> RawCandle:
    if not isinstance(value, list) or len(value) < 9:
        raise MarketDataError("OKX history candle row is invalid")
    payload = tuple(str(item) for item in value)
    try:
        timestamp_ms = int(payload[0])
        numeric = tuple(Decimal(item) for item in payload[1:8])
    except (ValueError, InvalidOperation) as exc:
        raise MarketDataError("OKX history candle value is invalid") from exc
    if timestamp_ms <= 0 or any(not number.is_finite() for number in numeric):
        raise MarketDataError("OKX history candle contains non-finite value")
    if payload[8] not in {"0", "1"}:
        raise MarketDataError("OKX history candle confirmation flag is invalid")
    return RawCandle(
        timestamp_ms=timestamp_ms,
        open=payload[1],
        high=payload[2],
        low=payload[3],
        close=payload[4],
        volume=payload[5],
        volume_ccy=payload[6],
        volume_quote=payload[7],
        confirm=payload[8],
        instrument=instrument,
        bar=bar.lower(),
        source=_SOURCE,
        downloaded_at=downloaded_at,
        payload=payload,
    )


def _manifest(
    rows: Sequence[RawCandle],
    *,
    instrument: str,
    bar: str,
    start: datetime,
    end: datetime,
    interval: timedelta,
    duplicate_count: int,
    batches: int,
    failures: int,
    retries: int,
    generated_at: datetime,
    resume: bool,
) -> DownloadResult:
    confirmed = [row for row in rows if row.confirmed]
    invalid = sum(not _valid_ohlc(row) for row in confirmed)
    timestamps = [row.timestamp_ms for row in confirmed]
    out_of_order = sum(b <= a for a, b in pairwise(timestamps))
    interval_ms = int(interval.total_seconds() * 1000)
    missing: list[str] = []
    for earlier, later in pairwise(timestamps):
        cursor = earlier + interval_ms
        while cursor < later:
            missing.append(datetime.fromtimestamp(cursor / 1000, tz=UTC).isoformat())
            cursor += interval_ms
    canonical = [
        {
            "timestamp_ms": row.timestamp_ms,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "volume_ccy": row.volume_ccy,
            "volume_quote": row.volume_quote,
            "confirm": row.confirm,
        }
        for row in confirmed
    ]
    dataset_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    status = "complete"
    if invalid or out_of_order or missing or not confirmed:
        status = "quality_failed"
    return DownloadResult(
        instrument=instrument,
        timeframe=bar.lower(),
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
        actual_start=(
            datetime.fromtimestamp(timestamps[0] / 1000, tz=UTC).isoformat() if timestamps else None
        ),
        actual_end=(
            datetime.fromtimestamp(timestamps[-1] / 1000, tz=UTC).isoformat()
            if timestamps
            else None
        ),
        raw_rows=len(rows),
        normalized_rows=len(confirmed),
        duplicate_count=duplicate_count,
        missing_count=len(missing),
        invalid_ohlc_count=invalid,
        out_of_order_count=out_of_order,
        unconfirmed_count=len(rows) - len(confirmed),
        source=_SOURCE,
        download_batches=batches,
        download_failures=failures,
        retry_count=retries,
        dataset_hash=dataset_hash,
        generated_at=generated_at.isoformat(),
        confirmed_candle_only=True,
        resume=resume,
        status=status,
        missing_timestamps=tuple(missing),
    )


def _valid_ohlc(row: RawCandle) -> bool:
    try:
        open_, high, low, close, volume = map(
            Decimal, (row.open, row.high, row.low, row.close, row.volume)
        )
    except InvalidOperation:
        return False
    return bool(
        all(value.is_finite() for value in (open_, high, low, close, volume))
        and low > 0
        and volume > 0
        and low <= open_
        and low <= close
        and high >= open_
        and high >= close
        and low <= high
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("research timestamps must include timezone")
    return value.astimezone(UTC)


def _state_int(cache: HistoricalCandleCache, key: str, default: int = 0) -> int:
    value = cache.state(key)
    return int(value) if value is not None else default
