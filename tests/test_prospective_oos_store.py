from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.market.historical_data import MarketDataError
from backtest.prospective_oos import (
    PROSPECTIVE_START,
    DatasetPurpose,
    PartitionState,
    ProspectiveOOSStore,
    load_governed_candles,
)
from backtest.vwap_signal_edge_data import RawCandle


def _raw(hour: int, *, confirm: str = "1", close: str = "101") -> RawCandle:
    stamp = PROSPECTIVE_START + timedelta(hours=hour)
    payload = (
        str(int(stamp.timestamp() * 1000)),
        "100",
        "102",
        "99",
        close,
        "10",
        "1000",
        "1000",
        confirm,
    )
    return RawCandle(
        int(payload[0]),
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
        "2026-08-13T01:01:00+00:00",
        payload,
    )


def _store(tmp_path: Path) -> ProspectiveOOSStore:
    return ProspectiveOOSStore(
        tmp_path / "prospective",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
    )


def test_research_cutoff_boundary_and_prospective_only_backfill(tmp_path: Path) -> None:
    store = _store(tmp_path)
    historical = replace(
        _raw(0), timestamp_ms=int((PROSPECTIVE_START - timedelta(hours=1)).timestamp() * 1000)
    )
    with pytest.raises(ValueError, match="historical candle"):
        store.ingest((historical,), latest_closed=PROSPECTIVE_START)
    result = store.ingest((_raw(0),), latest_closed=PROSPECTIVE_START)
    assert result.new_confirmed_candles == 1


def test_confirmed_only_and_invalid_ohlc_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.ingest((_raw(0, confirm="0"),), latest_closed=PROSPECTIVE_START)
    assert result.unconfirmed_rejected == 1
    assert store.write_root_manifest()["total_rows"] == 0
    with pytest.raises(MarketDataError, match="OHLC"):
        store.ingest((_raw(0, close="1000"),), latest_closed=PROSPECTIVE_START)


def test_duplicate_noop_and_conflicting_duplicate_audited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest((_raw(0),), latest_closed=PROSPECTIVE_START)
    duplicate = store.ingest((_raw(0),), latest_closed=PROSPECTIVE_START)
    conflict = store.ingest((_raw(0, close="100.5"),), latest_closed=PROSPECTIVE_START)
    assert duplicate.duplicates_ignored == 1
    assert conflict.source_revisions == 1
    assert store.write_root_manifest()["source_revisions"] == 1
    normalized = next(store.root.rglob("normalized.csv")).read_text(encoding="utf-8")
    assert "100.5" not in normalized


def test_utc_partition_open_then_sealed_and_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest(
        tuple(_raw(hour) for hour in range(12)),
        latest_closed=PROSPECTIVE_START + timedelta(hours=11),
    )
    partition = store.dataset_root / "2026" / "08" / "13"
    opened = json.loads((partition / "manifest.json").read_text(encoding="utf-8"))
    assert opened["state"] == PartitionState.OPEN
    store.ingest(
        tuple(_raw(hour) for hour in range(12, 24)),
        latest_closed=PROSPECTIVE_START + timedelta(hours=23),
    )
    sealed_bytes = (partition / "raw.jsonl").read_bytes()
    sealed = json.loads((partition / "manifest.json").read_text(encoding="utf-8"))
    assert sealed["state"] == PartitionState.SEALED
    assert sealed["row_count"] == 24
    store.ingest((_raw(0, close="100.5"),), latest_closed=PROSPECTIVE_START + timedelta(hours=23))
    assert (partition / "raw.jsonl").read_bytes() == sealed_bytes


def test_partition_and_root_hashes_are_deterministic(tmp_path: Path) -> None:
    rows = tuple(_raw(hour) for hour in range(24))
    first = _store(tmp_path / "a")
    second = _store(tmp_path / "b")
    first.ingest(rows, latest_closed=PROSPECTIVE_START + timedelta(hours=23))
    second.ingest(tuple(reversed(rows)), latest_closed=PROSPECTIVE_START + timedelta(hours=23))
    first_manifest = first.write_root_manifest()
    second_manifest = second.write_root_manifest()
    assert first_manifest["dataset_root_hash"] == second_manifest["dataset_root_hash"]
    assert first.integrity_report()["integrity_check"] == "ok"


def test_resume_rebuilds_views_after_interruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest((_raw(0), _raw(1)), latest_closed=PROSPECTIVE_START + timedelta(hours=1))
    partition = store.dataset_root / "2026" / "08" / "13"
    (partition / "raw.jsonl").unlink()
    (partition / "normalized.csv").unlink()
    (partition / "manifest.json").unlink()
    recovered = store.recover(latest_closed=PROSPECTIVE_START + timedelta(hours=1))
    assert recovered.open_partitions == 1
    assert (partition / "raw.jsonl").exists()
    assert (partition / "normalized.csv").exists()
    assert store.integrity_report()["integrity_check"] == "ok"


def test_missing_candle_detected_without_interpolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.ingest((_raw(0), _raw(2)), latest_closed=PROSPECTIVE_START + timedelta(hours=2))
    assert result.missing_source_candles == 1
    assert store.write_root_manifest()["total_rows"] == 2
    audit = store.audit_path.read_text(encoding="utf-8")
    assert "MISSING_SOURCE_CANDLE" in audit
    assert PROSPECTIVE_START.replace(hour=1).isoformat() in audit
    store.ingest((_raw(1),), latest_closed=PROSPECTIVE_START + timedelta(hours=2))
    assert "MISSING_SOURCE_CANDLE_FILLED" in store.audit_path.read_text(encoding="utf-8")


def test_missing_trailing_candle_is_reported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest((_raw(0),), latest_closed=PROSPECTIVE_START + timedelta(hours=2))
    manifest = store.write_root_manifest()
    assert manifest["missing"] == 2


def test_research_firewall_requires_explicit_frozen_validation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest((_raw(0),), latest_closed=PROSPECTIVE_START)
    normalized = next(store.root.rglob("normalized.csv"))
    with pytest.raises(MarketDataError, match="rejected prospective"):
        load_governed_candles(normalized)
    with pytest.raises(MarketDataError, match="frozen candidate"):
        load_governed_candles(normalized, purpose=DatasetPurpose.PROSPECTIVE_VALIDATION)
    candles = load_governed_candles(
        normalized,
        purpose=DatasetPurpose.PROSPECTIVE_VALIDATION,
        frozen_candidate=True,
    )
    assert len(candles) == 1


def test_existing_historical_loader_rejects_prospective_path(tmp_path: Path) -> None:
    from backtest.vwap_signal_edge_data import load_normalized_candles

    path = tmp_path / "prospective_oos" / "normalized.csv"
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    with pytest.raises(MarketDataError, match="rejects prospective_oos"):
        load_normalized_candles(path, bar="1h")


def test_integrity_detects_silent_sealed_materialization_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest(
        tuple(_raw(hour) for hour in range(24)),
        latest_closed=PROSPECTIVE_START + timedelta(hours=23),
    )
    normalized = next(store.root.rglob("normalized.csv"))
    normalized.write_text("corrupted", encoding="utf-8")
    report = store.integrity_report()
    assert report["integrity_check"] == "failed"
    assert report["sealed_partition_mutations"] == 1
