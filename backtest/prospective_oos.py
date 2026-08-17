"""Append-only prospective market-data storage and research firewall."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.domain.market import Candle
from app.market.historical_data import MarketDataError
from backtest.vwap_signal_edge_data import RawCandle

SCHEMA_VERSION = 1
COLLECTOR_VERSION = "PROSPECTIVE_OOS_COLLECTOR_V1"
RESEARCH_CUTOFF = datetime(2026, 8, 12, 23, 59, 59, 999999, tzinfo=UTC)
PROSPECTIVE_START = datetime(2026, 8, 13, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)


class DatasetPurpose(StrEnum):
    HISTORICAL_RESEARCH = "historical_research"
    PROSPECTIVE_VALIDATION = "prospective_validation"


class PartitionState(StrEnum):
    OPEN = "OPEN"
    SEALED = "SEALED"


@dataclass(frozen=True, slots=True)
class IngestResult:
    new_confirmed_candles: int
    duplicates_ignored: int
    source_revisions: int
    unconfirmed_rejected: int
    missing_source_candles: int
    sealed_partitions: int
    open_partitions: int


def latest_closed_hour(now: datetime) -> datetime:
    current = _utc(now).replace(minute=0, second=0, microsecond=0)
    return current - ONE_HOUR


def load_governed_candles(
    path: Path,
    *,
    purpose: DatasetPurpose = DatasetPurpose.HISTORICAL_RESEARCH,
    frozen_candidate: bool = False,
) -> list[Candle]:
    """Load normalized candles while enforcing the historical/prospective firewall."""
    candles: list[Candle] = []
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row.get("confirm") != "1":
                raise MarketDataError("governed data contains unconfirmed candle")
            stamp = _utc(datetime.fromisoformat(str(row["timestamp"])))
            if purpose is DatasetPurpose.HISTORICAL_RESEARCH and stamp > RESEARCH_CUTOFF:
                raise MarketDataError("historical research reader rejected prospective data")
            if purpose is DatasetPurpose.PROSPECTIVE_VALIDATION and not frozen_candidate:
                raise MarketDataError("prospective validation requires a frozen candidate")
            candles.append(
                Candle(
                    timestamp=stamp,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=Decimal(str(row["volume"])),
                    confirmed=True,
                )
            )
    if any(later.timestamp <= earlier.timestamp for earlier, later in pairwise(candles)):
        raise MarketDataError("governed candles are not strictly ordered")
    return candles


class ProspectiveOOSStore:
    """Immutable confirmed-candle records with atomic materialized partition views."""

    def __init__(
        self,
        root: Path,
        *,
        instrument: str = "BTC-USDT",
        timeframe: str = "1h",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self.instrument = instrument
        self.timeframe = timeframe.lower()
        self.clock = clock or (lambda: datetime.now(UTC))
        if self.instrument != "BTC-USDT" or self.timeframe != "1h":
            raise ValueError("collector V1 supports BTC-USDT 1H only")
        self.dataset_root = root / instrument / self.timeframe
        self.audit_path = root / "collector_audit.jsonl"
        self.manifest_path = root / "prospective_oos_manifest.json"
        self.dataset_root.mkdir(parents=True, exist_ok=True)

    def ingest(self, rows: Sequence[RawCandle], *, latest_closed: datetime) -> IngestResult:
        latest_closed = _utc(latest_closed)
        grouped: dict[date, list[RawCandle]] = {}
        unconfirmed = 0
        for row in rows:
            stamp = _timestamp(row)
            if stamp < PROSPECTIVE_START:
                raise ValueError("historical candle cannot enter prospective storage")
            if stamp > latest_closed:
                raise ValueError("unclosed candle cannot enter prospective storage")
            if row.instrument != self.instrument or row.bar.lower() != self.timeframe:
                raise ValueError("candle identity does not match prospective dataset")
            if not row.confirmed:
                unconfirmed += 1
                self._audit("UNCONFIRMED_CANDLE_REJECTED", row=row)
                continue
            _require_valid(row)
            grouped.setdefault(stamp.date(), []).append(row)

        new_rows = duplicates = revisions = 0
        affected = set(grouped)
        for day, candidates in sorted(grouped.items()):
            partition = self._partition(day)
            manifest = self._read_partition_manifest(partition)
            if manifest and manifest["state"] == PartitionState.SEALED:
                existing = self._records(partition)
                for row in candidates:
                    previous = existing.get(row.timestamp_ms)
                    if previous is None or _payload_identity(previous) != _payload_identity(row):
                        revisions += 1
                        self._audit("SOURCE_REVISION_DETECTED", row=row)
                    else:
                        duplicates += 1
                continue
            for row in candidates:
                record_path = self._record_path(partition, row.timestamp_ms)
                if record_path.exists():
                    previous = _read_record(record_path)
                    if _payload_identity(previous) == _payload_identity(row):
                        duplicates += 1
                    else:
                        revisions += 1
                        self._audit("SOURCE_REVISION_DETECTED", row=row)
                    continue
                record_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(record_path, _record_json(row))
                new_rows += 1

        all_days = affected | set(self._partition_days())
        if latest_closed >= PROSPECTIVE_START:
            cursor = PROSPECTIVE_START.date()
            while cursor <= latest_closed.date():
                all_days.add(cursor)
                cursor += timedelta(days=1)
        missing = sealed = opened = 0
        for day in sorted(all_days):
            report = self._materialize(day, latest_closed=latest_closed)
            missing += int(report["missing_count"])
            sealed += int(report["state"] == PartitionState.SEALED)
            opened += int(report["state"] == PartitionState.OPEN)
        self.write_root_manifest()
        return IngestResult(
            new_rows,
            duplicates,
            revisions,
            unconfirmed,
            missing,
            sealed,
            opened,
        )

    def recover(self, *, latest_closed: datetime) -> IngestResult:
        """Rebuild materialized views from immutable record files after interruption."""
        return self.ingest((), latest_closed=latest_closed)

    def missing_timestamps(self, *, latest_closed: datetime) -> tuple[datetime, ...]:
        latest_closed = _utc(latest_closed)
        if latest_closed < PROSPECTIVE_START:
            return ()
        existing = {
            _timestamp(row)
            for day in self._partition_days()
            for row in self._records(self._partition(day)).values()
        }
        return tuple(
            stamp for stamp in _hours(PROSPECTIVE_START, latest_closed) if stamp not in existing
        )

    def write_root_manifest(self) -> dict[str, Any]:
        partitions = [
            manifest
            for day in self._partition_days()
            if (manifest := self._read_partition_manifest(self._partition(day))) is not None
        ]
        total_rows = sum(int(item["row_count"]) for item in partitions)
        content = {
            "schema_version": SCHEMA_VERSION,
            "research_cutoff": RESEARCH_CUTOFF.isoformat(),
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "prospective_start": PROSPECTIVE_START.isoformat(),
            "latest_confirmed_timestamp": max(
                (str(item["last_timestamp"]) for item in partitions if item["last_timestamp"]),
                default=None,
            ),
            "partition_count": len(partitions),
            "sealed_partition_count": sum(
                item["state"] == PartitionState.SEALED for item in partitions
            ),
            "open_partition_count": sum(
                item["state"] == PartitionState.OPEN for item in partitions
            ),
            "total_rows": total_rows,
            "duplicates": 0,
            "missing": sum(int(item["missing_count"]) for item in partitions),
            "invalid": sum(int(item["invalid_count"]) for item in partitions),
            "source_revisions": self._audit_count("SOURCE_REVISION_DETECTED"),
            "dataset_root_hash": _hash_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "partitions": sorted(
                        (str(item["partition_date"]), str(item["content_hash"]), str(item["state"]))
                        for item in partitions
                    ),
                }
            ),
            "collector_version": COLLECTOR_VERSION,
            "generated_at": _utc(self.clock()).isoformat(),
            "append_only": True,
            "confirmed_only": True,
            "utc_partitioned": True,
        }
        _atomic_write(self.manifest_path, _json(content))
        return content

    def integrity_report(self) -> dict[str, Any]:
        partition_reports: list[dict[str, Any]] = []
        sealed_mutations = 0
        for day in self._partition_days():
            partition = self._partition(day)
            manifest = self._read_partition_manifest(partition)
            if manifest is None:
                continue
            rows = tuple(self._records(partition).values())
            current = _partition_content(
                rows, day=day, state=PartitionState(str(manifest["state"]))
            )
            materialized_hashes = {
                name: _sha256((partition / name).read_bytes())
                for name in ("raw.jsonl", "normalized.csv")
                if (partition / name).exists()
            }
            valid = (
                current["content_hash"] == manifest["content_hash"]
                and materialized_hashes == manifest["file_hashes"]
                and _hash_json({**manifest, "manifest_hash": None}) == manifest["manifest_hash"]
            )
            if manifest["state"] == PartitionState.SEALED and not valid:
                sealed_mutations += 1
            partition_reports.append(
                {
                    "partition_date": day.isoformat(),
                    "state": manifest["state"],
                    "integrity_ok": valid,
                    "row_count": len(rows),
                    "first_timestamp": current["first_timestamp"],
                    "last_timestamp": current["last_timestamp"],
                    "missing_count": current["missing_count"],
                    "invalid_count": current["invalid_count"],
                }
            )
        manifest = self.write_root_manifest()
        data_integrity_failures = self._audit_count("DATA_INTEGRITY_FAILURE")
        return {
            "integrity_check": "ok"
            if sealed_mutations == 0
            and int(manifest["invalid"]) == 0
            and data_integrity_failures == 0
            else "failed",
            "duplicates": 0,
            "missing": manifest["missing"],
            "invalid": manifest["invalid"],
            "unconfirmed_in_sealed": 0,
            "source_revisions": manifest["source_revisions"],
            "sealed_partition_mutations": sealed_mutations,
            "data_integrity_failures": data_integrity_failures,
            "dataset_root_hash": manifest["dataset_root_hash"],
            "partitions": partition_reports,
            "no_interpolation": True,
        }

    def record_audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self._audit(event_type, details=details)

    def _materialize(self, day: date, *, latest_closed: datetime) -> dict[str, Any]:
        partition = self._partition(day)
        existing = self._read_partition_manifest(partition)
        if existing and existing["state"] == PartitionState.SEALED:
            return existing
        rows = tuple(self._records(partition).values())
        day_end = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23)
        provisional = _partition_content(
            rows,
            day=day,
            state=PartitionState.OPEN,
            latest_expected=min(latest_closed, day_end),
        )
        state = (
            PartitionState.SEALED
            if latest_closed >= day_end
            and provisional["row_count"] == 24
            and provisional["missing_count"] == 0
            and provisional["invalid_count"] == 0
            else PartitionState.OPEN
        )
        content = _partition_content(
            rows,
            day=day,
            state=state,
            latest_expected=min(latest_closed, day_end),
        )
        partition.mkdir(parents=True, exist_ok=True)
        raw = "".join(_record_json(row) for row in rows)
        normalized = _normalized_csv(rows, day)
        _atomic_write(partition / "raw.jsonl", raw)
        _atomic_write(partition / "normalized.csv", normalized)
        manifest_base = {
            **content,
            "file_hash": _hash_json(
                {
                    "raw.jsonl": _sha256(raw.encode()),
                    "normalized.csv": _sha256(normalized.encode()),
                }
            ),
            "file_hashes": {
                "raw.jsonl": _sha256(raw.encode()),
                "normalized.csv": _sha256(normalized.encode()),
            },
        }
        manifest = {
            **manifest_base,
            "manifest_hash": _hash_json({**manifest_base, "manifest_hash": None}),
        }
        _atomic_write(partition / "manifest.json", _json(manifest))
        previous_missing = existing.get("missing_timestamps", []) if existing else []
        if content["missing_count"] and content["missing_timestamps"] != previous_missing:
            self._audit(
                "MISSING_SOURCE_CANDLE",
                details={
                    "partition_date": day.isoformat(),
                    "timestamps": content["missing_timestamps"],
                },
            )
        filled = sorted(set(previous_missing) - set(content["missing_timestamps"]))
        if filled:
            self._audit(
                "MISSING_SOURCE_CANDLE_FILLED",
                details={"partition_date": day.isoformat(), "timestamps": filled},
            )
        return manifest

    def _partition(self, day: date) -> Path:
        return self.dataset_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"

    def _partition_days(self) -> Iterable[date]:
        for path in sorted(self.dataset_root.glob("????/??/??")):
            if path.is_dir():
                yield date(int(path.parts[-3]), int(path.parts[-2]), int(path.parts[-1]))

    def _records(self, partition: Path) -> dict[int, RawCandle]:
        result: dict[int, RawCandle] = {}
        for path in sorted((partition / "records").glob("*.json")):
            row = _read_record(path)
            if row.timestamp_ms in result:
                raise MarketDataError("duplicate prospective record key")
            result[row.timestamp_ms] = row
        return dict(sorted(result.items()))

    @staticmethod
    def _record_path(partition: Path, timestamp_ms: int) -> Path:
        return partition / "records" / f"{timestamp_ms}.json"

    @staticmethod
    def _read_partition_manifest(partition: Path) -> dict[str, Any] | None:
        path = partition / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _audit(
        self,
        event_type: str,
        *,
        row: RawCandle | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "event_id": hashlib.sha256(
                f"{event_type}|{row.timestamp_ms if row else ''}|{_utc(self.clock()).isoformat()}".encode()
            ).hexdigest()[:24],
            "event_type": event_type,
            "timestamp": _utc(self.clock()).isoformat(),
            "instrument": row.instrument if row else self.instrument,
            "timeframe": row.bar if row else self.timeframe,
            "candle_timestamp": _timestamp(row).isoformat() if row else None,
            "details": details or {},
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def _audit_count(self, event_type: str) -> int:
        if not self.audit_path.exists():
            return 0
        return sum(
            json.loads(line)["event_type"] == event_type
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line
        )


def _partition_content(
    rows: Sequence[RawCandle],
    *,
    day: date,
    state: PartitionState,
    latest_expected: datetime | None = None,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row.timestamp_ms)
    stamps = [_timestamp(row) for row in ordered]
    expected_start = max(datetime.combine(day, datetime.min.time(), tzinfo=UTC), PROSPECTIVE_START)
    expected_end = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23)
    expected = set(_hours(expected_start, expected_end))
    actual = set(stamps)
    if state is PartitionState.SEALED:
        missing = sorted(expected - actual)
    else:
        open_end = latest_expected or (stamps[-1] if stamps else expected_start - ONE_HOUR)
        missing = sorted(stamp for stamp in _hours(expected_start, open_end) if stamp not in actual)
    canonical = [_payload_identity(row) for row in ordered]
    content = {
        "schema_version": SCHEMA_VERSION,
        "partition_date": day.isoformat(),
        "state": state,
        "immutable_partition": state is PartitionState.SEALED,
        "row_count": len(ordered),
        "first_timestamp": stamps[0].isoformat() if stamps else None,
        "last_timestamp": stamps[-1].isoformat() if stamps else None,
        "missing_count": len(missing),
        "missing_timestamps": [stamp.isoformat() for stamp in missing],
        "invalid_count": sum(not _valid(row) for row in ordered),
        "out_of_order_count": 0,
        "unconfirmed_count": sum(not row.confirmed for row in ordered),
        "content_hash": _hash_json(canonical),
    }
    return content


def _normalized_csv(rows: Sequence[RawCandle], day: date) -> str:
    fields = (
        "instrument",
        "timeframe",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "volume_ccy",
        "volume_quote",
        "confirm",
        "source",
        "downloaded_at",
        "partition_date",
    )
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda item: item.timestamp_ms):
        writer.writerow(
            {
                "instrument": row.instrument,
                "timeframe": row.bar,
                "timestamp": _timestamp(row).isoformat(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "volume_ccy": row.volume_ccy,
                "volume_quote": row.volume_quote,
                "confirm": row.confirm,
                "source": row.source,
                "downloaded_at": row.downloaded_at,
                "partition_date": day.isoformat(),
            }
        )
    return output.getvalue()


def _require_valid(row: RawCandle) -> None:
    if not _valid(row):
        raise MarketDataError("prospective candle failed OHLC or volume validation")


def _valid(row: RawCandle) -> bool:
    try:
        open_, high, low, close, volume = map(
            Decimal, (row.open, row.high, row.low, row.close, row.volume)
        )
    except InvalidOperation:
        return False
    return bool(
        all(value.is_finite() for value in (open_, high, low, close, volume))
        and low > 0
        and volume >= 0
        and low <= open_ <= high
        and low <= close <= high
    )


def _record_json(row: RawCandle) -> str:
    payload = asdict(row)
    payload["payload"] = list(row.payload)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"


def _read_record(path: Path) -> RawCandle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["payload"] = tuple(payload["payload"])
    return RawCandle(**payload)


def _payload_identity(row: RawCandle) -> dict[str, Any]:
    return {
        "instrument": row.instrument,
        "timeframe": row.bar,
        "timestamp_ms": row.timestamp_ms,
        "payload": list(row.payload),
    }


def _timestamp(row: RawCandle) -> datetime:
    return datetime.fromtimestamp(row.timestamp_ms / 1000, tz=UTC)


def _hours(start: datetime, end: datetime) -> Iterable[datetime]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += ONE_HOUR


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


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _hash_json(value: object) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)
