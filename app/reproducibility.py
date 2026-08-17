from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config.run_config import RunConfig
from app.domain.market import (
    Candle,
    Instrument,
    InstrumentStatus,
    InstrumentType,
)
from app.market.providers import MarketDataProvider
from app.version import APP_VERSION

SNAPSHOT_SCHEMA_VERSION = 1


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CodeVersion:
    app_version: str
    git_commit: str
    git_dirty: bool


@dataclass(frozen=True, slots=True)
class InstrumentSpecSnapshot:
    schema_version: int
    fetched_at: datetime
    source: str
    raw: dict[str, Any]
    raw_hash: str
    snapshot_hash: str

    @property
    def instrument(self) -> Instrument:
        return _instrument_from_raw(self.raw)

    @classmethod
    def capture(
        cls,
        instrument: Instrument,
        *,
        source: str,
        fetched_at: datetime | None = None,
    ) -> InstrumentSpecSnapshot:
        raw = _instrument_to_raw(instrument)
        timestamp = fetched_at or datetime.now(UTC)
        raw_hash = canonical_hash(raw)
        identity = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "fetched_at": timestamp.isoformat(),
            "source": source,
            "raw": raw,
            "raw_hash": raw_hash,
        }
        return cls(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            fetched_at=timestamp,
            source=source,
            raw=raw,
            raw_hash=raw_hash,
            snapshot_hash=canonical_hash(identity),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fetched_at": self.fetched_at.isoformat(),
            "source": self.source,
            "raw": self.raw,
            "raw_hash": self.raw_hash,
            "snapshot_hash": self.snapshot_hash,
        }


class InstrumentSnapshotStore:
    def __init__(self, directory: Path = Path("data/instruments")) -> None:
        self.directory = directory

    def resolve(
        self,
        instrument_id: str,
        *,
        configured_path: Path | None,
        fetch: Callable[[str], Instrument],
    ) -> InstrumentSpecSnapshot:
        if configured_path is not None:
            snapshot = self.load(configured_path)
        else:
            local = self.path_for(instrument_id)
            if local.is_file():
                snapshot = self.load(local)
            else:
                snapshot = InstrumentSpecSnapshot.capture(
                    fetch(instrument_id), source="okx_rest_normalized"
                )
                self.save(snapshot, local)
        if snapshot.instrument.instrument_id != instrument_id:
            raise ValueError("交易规则快照与配置品种不一致")
        return snapshot

    def path_for(self, instrument_id: str) -> Path:
        safe_name = instrument_id.replace("/", "_").replace("\\", "_")
        return self.directory / f"{safe_name}.json"

    @staticmethod
    def load(path: Path) -> InstrumentSpecSnapshot:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot = InstrumentSpecSnapshot(
                schema_version=int(payload["schema_version"]),
                fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
                source=str(payload["source"]),
                raw=dict(payload["raw"]),
                raw_hash=str(payload["raw_hash"]),
                snapshot_hash=str(payload["snapshot_hash"]),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"交易规则快照无效: {path}") from exc
        expected_raw = canonical_hash(snapshot.raw)
        identity = {
            "schema_version": snapshot.schema_version,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "source": snapshot.source,
            "raw": snapshot.raw,
            "raw_hash": snapshot.raw_hash,
        }
        if (
            snapshot.schema_version != SNAPSHOT_SCHEMA_VERSION
            or snapshot.raw_hash != expected_raw
            or snapshot.snapshot_hash != canonical_hash(identity)
        ):
            raise ValueError("交易规则快照校验失败")
        return snapshot

    @staticmethod
    def save(snapshot: InstrumentSpecSnapshot, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class RecordingMarketDataProvider:
    def __init__(self, inner: MarketDataProvider) -> None:
        self.inner = inner
        self.candles: tuple[Candle, ...] = ()

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        candles = self.inner.get_historical_bars(
            instrument_id, bar, start=start, end=end, limit=limit
        )
        self.candles = tuple(candles)
        return candles

    @property
    def data_hash(self) -> str:
        return candles_hash(self.candles)


def candles_hash(candles: tuple[Candle, ...] | list[Candle]) -> str:
    return canonical_hash(
        [
            {
                "timestamp": candle.timestamp.astimezone(UTC).isoformat(),
                "open": str(candle.open),
                "high": str(candle.high),
                "low": str(candle.low),
                "close": str(candle.close),
                "volume": str(candle.volume),
                "confirmed": candle.confirmed,
            }
            for candle in candles
        ]
    )


def config_hash(config: RunConfig) -> str:
    return canonical_hash(config.model_dump(mode="json"))


def code_version(repository_root: Path = Path(".")) -> CodeVersion:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unborn"
    try:
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        dirty = True
    return CodeVersion(APP_VERSION, commit, dirty)


def build_run_manifest(
    *,
    run_id: str,
    config: RunConfig,
    instrument_snapshot: InstrumentSpecSnapshot,
    provider: RecordingMarketDataProvider,
    data_started_at: datetime,
    data_completed_at: datetime,
    run_started_at: datetime,
    start_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = dict(
        start_manifest
        or build_run_start_manifest(
            run_id=run_id,
            config=config,
            instrument_snapshot=instrument_snapshot,
            run_started_at=run_started_at,
        )
    )
    run_completed_at = datetime.now(UTC)
    manifest.update(
        {
            "data_hash": provider.data_hash,
            "completed_at": run_completed_at.isoformat(),
            "data_started_at": data_started_at.isoformat(),
            "data_completed_at": data_completed_at.isoformat(),
            "candle_count": len(provider.candles),
        }
    )
    return manifest


def build_run_start_manifest(
    *,
    run_id: str,
    config: RunConfig,
    instrument_snapshot: InstrumentSpecSnapshot,
    run_started_at: datetime,
) -> dict[str, Any]:
    version = code_version()
    return {
        "run_id": run_id,
        "mode": config.mode.value,
        "app_version": version.app_version,
        "git_commit": version.git_commit,
        "git_dirty": version.git_dirty,
        "config_hash": config_hash(config),
        "data_hash": "",
        "instrument_snapshot_hash": instrument_snapshot.snapshot_hash,
        "instrument_snapshot": instrument_snapshot.to_dict(),
        "config_snapshot": config.model_dump(mode="json"),
        "strategy_name": config.strategy.name,
        "strategy_parameters": config.strategy.parameters,
        "instrument_id": config.market.instrument_id,
        "bar": config.market.bar,
        "started_at": run_started_at.isoformat(),
        "completed_at": None,
        "data_started_at": None,
        "data_completed_at": None,
        "candle_count": 0,
        "data_source": config.data.source,
        "cost_parameters": {
            "fee_rate": str(config.backtest.fee_rate),
            "slippage_rate": str(config.backtest.slippage_rate),
        },
        "seed": config.backtest.seed,
    }


def write_run_manifest(manifest: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"run_manifest_{manifest['run_id']}.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _instrument_to_raw(instrument: Instrument) -> dict[str, Any]:
    raw = asdict(instrument)
    return {
        key: value.value
        if isinstance(value, (InstrumentType, InstrumentStatus))
        else str(value)
        if isinstance(value, Decimal)
        else value
        for key, value in raw.items()
    }


def _instrument_from_raw(raw: dict[str, Any]) -> Instrument:
    return Instrument(
        instrument_id=str(raw["instrument_id"]),
        base_currency=str(raw["base_currency"]),
        quote_currency=str(raw["quote_currency"]),
        instrument_type=InstrumentType(str(raw["instrument_type"])),
        price_tick=Decimal(str(raw["price_tick"])),
        quantity_step=Decimal(str(raw["quantity_step"])),
        minimum_quantity=Decimal(str(raw["minimum_quantity"])),
        minimum_notional=Decimal(str(raw["minimum_notional"])),
        status=InstrumentStatus(str(raw["status"])),
        contract_value=Decimal(str(raw["contract_value"]))
        if raw.get("contract_value") is not None
        else None,
        settlement_currency=str(raw["settlement_currency"])
        if raw.get("settlement_currency") is not None
        else None,
    )
