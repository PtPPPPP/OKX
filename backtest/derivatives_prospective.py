"""Append-only prospective storage for public derivatives observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.market.historical_data import MarketDataError
from backtest.prospective_oos import (
    PROSPECTIVE_START,
    RESEARCH_CUTOFF,
    DatasetPurpose,
    PartitionState,
    _atomic_write,
    _hash_json,
    _utc,
)

SCHEMA_VERSION = 1
COLLECTOR_VERSION = "DERIVATIVES_PROSPECTIVE_COLLECTOR_V1"
QUALITY_STATES = frozenset({"READY", "LIMITED", "STALE", "MISSING", "INVALID"})


@dataclass(frozen=True, slots=True)
class ProspectiveObservation:
    source: str
    instrument: str
    source_timestamp: datetime
    observed_at: datetime
    downloaded_at: datetime
    collection_mode: str
    unique_key: str
    values: Mapping[str, Any]
    raw: object

    def __post_init__(self) -> None:
        for value in (self.source_timestamp, self.observed_at, self.downloaded_at):
            _utc(value)
        if self.source_timestamp < PROSPECTIVE_START:
            raise ValueError("historical observation cannot enter prospective storage")
        if self.source_timestamp > self.observed_at:
            raise ValueError("source timestamp cannot be later than observation time")
        if self.collection_mode not in {"backfill", "live_snapshot"}:
            raise ValueError("invalid collection mode")

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "instrument": self.instrument,
            "source_timestamp": _utc(self.source_timestamp).isoformat(),
            "observed_at": _utc(self.observed_at).isoformat(),
            "downloaded_at": _utc(self.downloaded_at).isoformat(),
            "collection_mode": self.collection_mode,
            "unique_key": self.unique_key,
            "values": dict(self.values),
            "raw": self.raw,
        }


@dataclass(frozen=True, slots=True)
class ObservationIngestResult:
    new_records: int
    duplicates: int
    source_revisions: int
    sealed_partitions: int
    open_partitions: int


class ProspectiveObservationStore:
    """Immutable records plus atomic daily materializations for one public source."""

    def __init__(self, root: Path, source: str, instrument: str) -> None:
        self.root = root
        self.source = source
        self.instrument = instrument
        self.dataset_root = root / source / instrument
        self.audit_path = self.dataset_root / "audit.jsonl"
        self.manifest_path = self.dataset_root / "source_manifest.json"
        self.dataset_root.mkdir(parents=True, exist_ok=True)

    def ingest(
        self, rows: Sequence[ProspectiveObservation], *, now: datetime
    ) -> ObservationIngestResult:
        now = _utc(now)
        affected: set[date] = set()
        new = duplicates = revisions = 0
        for row in rows:
            if row.source != self.source or row.instrument != self.instrument:
                raise ValueError("observation identity does not match store")
            day = row.source_timestamp.date()
            affected.add(day)
            partition = self._partition(day)
            manifest = self._partition_manifest(partition)
            path = partition / "records" / f"{_safe_key(row.unique_key)}.json"
            if manifest and manifest["state"] == PartitionState.SEALED:
                if path.exists() and path.read_text(encoding="utf-8") == _json(row.canonical()):
                    duplicates += 1
                    self._audit("DUPLICATE_IGNORED", row, "sealed partition")
                else:
                    revisions += 1
                    self._audit("SOURCE_REVISION_DETECTED", row, "sealed partition")
                continue
            encoded = _json(row.canonical())
            if path.exists():
                if path.read_text(encoding="utf-8") == encoded:
                    duplicates += 1
                    self._audit("DUPLICATE_IGNORED", row, "same observation")
                else:
                    revisions += 1
                    self._audit("SOURCE_REVISION_DETECTED", row, "conflicting unique key")
                continue
            _atomic_write(path, encoded)
            new += 1
        affected.update(self.partition_days())
        sealed = opened = 0
        for day in sorted(affected):
            manifest = self._materialize(day, now)
            sealed += manifest["state"] == PartitionState.SEALED
            opened += manifest["state"] == PartitionState.OPEN
        self.write_manifest()
        return ObservationIngestResult(new, duplicates, revisions, sealed, opened)

    def recover(self, *, now: datetime) -> ObservationIngestResult:
        return self.ingest((), now=now)

    def observations(self) -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.dataset_root.glob("????/??/??/records/*.json"))
        ]

    def write_manifest(self) -> dict[str, Any]:
        partitions = [
            item
            for day in self.partition_days()
            if (item := self._partition_manifest(self._partition(day))) is not None
        ]
        rows = self.observations()
        content = {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "instrument": self.instrument,
            "research_cutoff": RESEARCH_CUTOFF.isoformat(),
            "prospective_start": PROSPECTIVE_START.isoformat(),
            "rows": len(rows),
            "first_timestamp": min((row["source_timestamp"] for row in rows), default=None),
            "last_timestamp": max((row["source_timestamp"] for row in rows), default=None),
            "sealed_partitions": sum(item["state"] == PartitionState.SEALED for item in partitions),
            "open_partitions": sum(item["state"] == PartitionState.OPEN for item in partitions),
            "duplicates": self._audit_count("DUPLICATE_IGNORED"),
            "conflicts": self._audit_count("SOURCE_REVISION_DETECTED"),
            "missing": 0,
            "file_hashes": {
                str(path.relative_to(self.dataset_root)).replace("\\", "/"): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(self.dataset_root.glob("????/??/??/records/*.json"))
            },
        }
        content["source_hash"] = _hash_json(
            {key: content[key] for key in ("source", "instrument", "rows", "file_hashes")}
        )
        _atomic_write(self.manifest_path, _json(content))
        return content

    def integrity_report(self) -> dict[str, Any]:
        failures = 0
        for day in self.partition_days():
            partition = self._partition(day)
            manifest = self._partition_manifest(partition)
            if manifest is None:
                failures += 1
                continue
            rows = self._day_rows(partition)
            expected = _hash_json([row for row in rows])
            if expected != manifest["content_hash"]:
                failures += 1
        return {
            "source": self.source,
            "integrity_check": "ok" if failures == 0 else "failed",
            "partition_failures": failures,
            "source_revisions": self._audit_count("SOURCE_REVISION_DETECTED"),
            "no_interpolation": True,
        }

    def sampling_gaps(self, *, threshold_seconds: int = 900) -> list[dict[str, Any]]:
        rows = sorted(
            (row for row in self.observations() if row["collection_mode"] == "live_snapshot"),
            key=lambda row: row["observed_at"],
        )
        gaps = []
        for earlier, later in pairwise(rows):
            seconds = (
                datetime.fromisoformat(later["observed_at"])
                - datetime.fromisoformat(earlier["observed_at"])
            ).total_seconds()
            if seconds > threshold_seconds:
                gaps.append(
                    {
                        "event": "COLLECTION_GAP",
                        "start": earlier["source_timestamp"],
                        "end": later["source_timestamp"],
                        "seconds": seconds,
                    }
                )
        return gaps

    def partition_days(self) -> Iterable[date]:
        for path in sorted(self.dataset_root.glob("????/??/??")):
            if path.is_dir():
                yield date(int(path.parts[-3]), int(path.parts[-2]), int(path.parts[-1]))

    def _partition(self, day: date) -> Path:
        return self.dataset_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"

    @staticmethod
    def _partition_manifest(partition: Path) -> dict[str, Any] | None:
        path = partition / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    @staticmethod
    def _day_rows(partition: Path) -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((partition / "records").glob("*.json"))
        ]

    def _materialize(self, day: date, now: datetime) -> dict[str, Any]:
        partition = self._partition(day)
        existing = self._partition_manifest(partition)
        if existing and existing["state"] == PartitionState.SEALED:
            return existing
        rows = self._day_rows(partition)
        state = PartitionState.SEALED if day < now.date() else PartitionState.OPEN
        content = {
            "schema_version": SCHEMA_VERSION,
            "partition_date": day.isoformat(),
            "state": state,
            "immutable_partition": state is PartitionState.SEALED,
            "row_count": len(rows),
            "first_timestamp": min((row["source_timestamp"] for row in rows), default=None),
            "last_timestamp": max((row["source_timestamp"] for row in rows), default=None),
            "content_hash": _hash_json(rows),
        }
        _atomic_write(partition / "normalized.jsonl", "".join(_json_line(row) for row in rows))
        _atomic_write(partition / "manifest.json", _json(content))
        return content

    def _audit(self, event: str, row: ProspectiveObservation, reason: str) -> None:
        payload = {
            "event_type": event,
            "timestamp": _utc(row.downloaded_at).isoformat(),
            "unique_key": row.unique_key,
            "reason": reason,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")
            file.flush()

    def _audit_count(self, event: str) -> int:
        if not self.audit_path.exists():
            return 0
        return sum(
            json.loads(line)["event_type"] == event
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line
        )


def load_prospective_observations(
    store: ProspectiveObservationStore,
    *,
    purpose: DatasetPurpose = DatasetPurpose.HISTORICAL_RESEARCH,
    frozen_candidate: bool = False,
) -> list[dict[str, Any]]:
    if purpose is DatasetPurpose.HISTORICAL_RESEARCH:
        raise MarketDataError("historical research reader rejected prospective derivatives")
    if not frozen_candidate:
        raise MarketDataError("prospective validation requires a frozen candidate")
    return store.observations()


def backward_asof(
    rows: Sequence[dict[str, Any]], target: datetime
) -> tuple[dict[str, Any] | None, float | None]:
    target = _utc(target)
    eligible = [
        row
        for row in rows
        if datetime.fromisoformat(row["source_timestamp"]) <= target
        and datetime.fromisoformat(str(row.get("observed_at", row["source_timestamp"]))) <= target
    ]
    if not eligible:
        return None, None
    selected = max(eligible, key=lambda row: row["source_timestamp"])
    age = (target - datetime.fromisoformat(selected["source_timestamp"])).total_seconds()
    return selected, age


def build_basis_observation(
    spot: Sequence[dict[str, Any]],
    mark: Sequence[dict[str, Any]],
    target: datetime,
    *,
    observed_at: datetime,
    stale_seconds: int = 7_200,
) -> ProspectiveObservation | None:
    spot_row, spot_age = backward_asof(spot, target)
    mark_row, mark_age = backward_asof(mark, target)
    if spot_row is None or mark_row is None or spot_age is None or mark_age is None:
        return None
    spot_price = float(spot_row["values"]["close"])
    mark_price = float(mark_row["values"]["close"])
    quality = "STALE" if max(spot_age, mark_age) > stale_seconds else "READY"
    return ProspectiveObservation(
        "derived/basis_mark_spot",
        "BTC-USDT-SWAP",
        _utc(target),
        _utc(observed_at),
        _utc(observed_at),
        "backfill" if target < observed_at else "live_snapshot",
        f"BTC-USDT-SWAP|observed|{_utc(target).isoformat()}",
        {
            "basis_pct": mark_price / spot_price - 1,
            "spot_reference_price": spot_price,
            "mark_price": mark_price,
            "spot_age_seconds": spot_age,
            "mark_age_seconds": mark_age,
            "perp_age_seconds": None,
            "basis_quality": quality,
            "definition": "mark_price / spot_reference_price - 1",
        },
        {"spot_key": spot_row["unique_key"], "mark_key": mark_row["unique_key"]},
    )


def unified_manifest(
    root: Path, stores: Mapping[str, ProspectiveObservationStore], spot_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    sources = {name: store.write_manifest() for name, store in stores.items()}
    rows_by_source = {
        "spot": int(spot_manifest.get("total_rows", 0)),
        **{name: int(item["rows"]) for name, item in sources.items()},
    }
    source_hashes = {name: item["source_hash"] for name, item in sources.items()}
    source_hashes["spot"] = str(spot_manifest.get("dataset_root_hash", ""))
    latest = {name: item["last_timestamp"] for name, item in sources.items()}
    latest["spot"] = spot_manifest.get("latest_confirmed_timestamp")
    content = {
        "schema_version": SCHEMA_VERSION,
        "research_cutoff": RESEARCH_CUTOFF.isoformat(),
        "prospective_start": PROSPECTIVE_START.isoformat(),
        "latest_by_source": latest,
        "rows_by_source": rows_by_source,
        "partition_counts": {
            name: {"sealed": item["sealed_partitions"], "open": item["open_partitions"]}
            for name, item in sources.items()
        },
        "duplicates": sum(int(item["duplicates"]) for item in sources.values()),
        "source_revisions": sum(int(item["conflicts"]) for item in sources.values()),
        "missing": int(spot_manifest.get("missing", 0)),
        "stale_observations": _stale_count(stores),
        "dataset_root_hash": _hash_json(dict(sorted(source_hashes.items()))),
        "collector_version": COLLECTOR_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write(root / "derivatives_prospective_manifest.json", _json(content))
    return content


def _stale_count(stores: Mapping[str, ProspectiveObservationStore]) -> int:
    return sum(
        row.get("values", {}).get("basis_quality") == "STALE"
        for store in stores.values()
        for row in store.observations()
    )


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
