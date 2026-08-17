from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.market.historical_data import MarketDataError
from backtest.derivatives_prospective import (
    ProspectiveObservation,
    ProspectiveObservationStore,
    backward_asof,
    build_basis_observation,
    load_prospective_observations,
    unified_manifest,
)
from backtest.prospective_oos import PROSPECTIVE_START, DatasetPurpose


def _row(
    hour: int = 0, *, source: str = "open_interest", key: str | None = None, value: str = "100"
) -> ProspectiveObservation:
    stamp = PROSPECTIVE_START + timedelta(hours=hour)
    return ProspectiveObservation(
        source,
        "BTC-USDT-SWAP",
        stamp,
        stamp + timedelta(minutes=5),
        stamp + timedelta(minutes=5),
        "live_snapshot",
        key or f"k-{hour}",
        {
            "oi_contracts": value,
            "oi_ccy": "1",
            "oi_usd": "100",
            "unit_metadata": {"oi_contracts": "contracts", "oi_ccy": "BTC", "oi_usd": "USD"},
        },
        {"raw": value},
    )


def test_cutoff_and_utc_enforced() -> None:
    with pytest.raises(ValueError, match="historical"):
        replace(_row(), source_timestamp=PROSPECTIVE_START - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="timezone"):
        replace(_row(), observed_at=datetime(2026, 8, 13))


def test_duplicate_noop_and_conflict_audit(tmp_path: Path) -> None:
    store = ProspectiveObservationStore(tmp_path, "open_interest", "BTC-USDT-SWAP")
    assert store.ingest((_row(),), now=PROSPECTIVE_START).new_records == 1
    assert store.ingest((_row(),), now=PROSPECTIVE_START).duplicates == 1
    assert store.ingest((_row(value="101"),), now=PROSPECTIVE_START).source_revisions == 1
    assert store.observations()[0]["values"]["oi_contracts"] == "100"


def test_later_same_value_observation_is_preserved(tmp_path: Path) -> None:
    store = ProspectiveObservationStore(tmp_path, "open_interest", "BTC-USDT-SWAP")
    result = store.ingest(
        (_row(0), _row(1, value="100")), now=PROSPECTIVE_START + timedelta(hours=1)
    )
    assert result.new_records == 2


def test_snapshot_gap_is_detected_without_interpolation(tmp_path: Path) -> None:
    store = ProspectiveObservationStore(tmp_path, "open_interest", "BTC-USDT-SWAP")
    store.ingest((_row(0), _row(1)), now=PROSPECTIVE_START + timedelta(hours=1))
    assert store.sampling_gaps(threshold_seconds=900)[0]["event"] == "COLLECTION_GAP"
    assert len(store.observations()) == 2


def test_unit_metadata_preserved(tmp_path: Path) -> None:
    store = ProspectiveObservationStore(tmp_path, "open_interest", "BTC-USDT-SWAP")
    store.ingest((_row(),), now=PROSPECTIVE_START)
    assert store.observations()[0]["values"]["unit_metadata"]["oi_contracts"] == "contracts"


def test_open_partition_recovery_and_deterministic_hash(tmp_path: Path) -> None:
    stores = []
    for name in ("a", "b"):
        store = ProspectiveObservationStore(tmp_path / name, "open_interest", "BTC-USDT-SWAP")
        store.ingest((_row(1), _row(0)), now=PROSPECTIVE_START + timedelta(hours=1))
        stores.append(store)
    assert stores[0].write_manifest()["source_hash"] == stores[1].write_manifest()["source_hash"]
    partition = next((tmp_path / "a").glob("open_interest/BTC-USDT-SWAP/????/??/??"))
    (partition / "normalized.jsonl").unlink()
    stores[0].recover(now=PROSPECTIVE_START + timedelta(hours=1))
    assert (partition / "normalized.jsonl").exists()


def test_sealed_partition_is_immutable(tmp_path: Path) -> None:
    store = ProspectiveObservationStore(tmp_path, "open_interest", "BTC-USDT-SWAP")
    store.ingest((_row(),), now=PROSPECTIVE_START + timedelta(days=1))
    assert (
        store.ingest(
            (_row(value="101"),), now=PROSPECTIVE_START + timedelta(days=1)
        ).source_revisions
        == 1
    )
    assert store.observations()[0]["values"]["oi_contracts"] == "100"


def test_backward_asof_never_joins_future() -> None:
    rows = [_row(0).canonical(), _row(2).canonical()]
    selected, age = backward_asof(rows, PROSPECTIVE_START + timedelta(hours=1))
    assert selected is not None and selected["unique_key"] == "k-0"
    assert age == 3600


def test_basis_formula_and_stale_quality() -> None:
    spot = [_priced("spot", 100, 0)]
    mark = [_priced("mark", 101, 0)]
    ready = build_basis_observation(spot, mark, PROSPECTIVE_START, observed_at=PROSPECTIVE_START)
    assert ready is not None and ready.values["basis_pct"] == pytest.approx(0.01)
    stale = build_basis_observation(
        spot,
        mark,
        PROSPECTIVE_START + timedelta(hours=3),
        observed_at=PROSPECTIVE_START + timedelta(hours=3),
    )
    assert stale is not None and stale.values["basis_quality"] == "STALE"


def test_missing_input_creates_no_fake_basis() -> None:
    assert build_basis_observation([], [], PROSPECTIVE_START, observed_at=PROSPECTIVE_START) is None


def test_research_firewall_blocks_all_derivatives(tmp_path: Path) -> None:
    for source in (
        "open_interest",
        "funding/events",
        "funding/snapshots",
        "derived/basis_mark_spot",
    ):
        store = ProspectiveObservationStore(tmp_path, source, "BTC-USDT-SWAP")
        with pytest.raises(MarketDataError, match="rejected"):
            load_prospective_observations(store)
        with pytest.raises(MarketDataError, match="frozen"):
            load_prospective_observations(store, purpose=DatasetPurpose.PROSPECTIVE_VALIDATION)


def test_funding_event_and_snapshot_are_separate(tmp_path: Path) -> None:
    event = ProspectiveObservationStore(tmp_path, "funding/events", "BTC-USDT-SWAP")
    snapshot = ProspectiveObservationStore(tmp_path, "funding/snapshots", "BTC-USDT-SWAP")
    event.ingest((_row(source="funding/events"),), now=PROSPECTIVE_START)
    snapshot.ingest((_row(source="funding/snapshots"),), now=PROSPECTIVE_START)
    assert event.dataset_root != snapshot.dataset_root


def test_unified_root_hash_is_deterministic(tmp_path: Path) -> None:
    store = ProspectiveObservationStore(tmp_path, "open_interest", "BTC-USDT-SWAP")
    store.ingest((_row(),), now=PROSPECTIVE_START)
    spot = {
        "total_rows": 0,
        "missing": 0,
        "dataset_root_hash": "spot",
        "latest_confirmed_timestamp": None,
    }
    assert (
        unified_manifest(tmp_path, {"open_interest": store}, spot)["dataset_root_hash"]
        == unified_manifest(tmp_path, {"open_interest": store}, spot)["dataset_root_hash"]
    )


def _priced(source: str, price: float, hour: int) -> dict[str, object]:
    stamp = (PROSPECTIVE_START + timedelta(hours=hour)).isoformat()
    return {
        "source_timestamp": stamp,
        "observed_at": stamp,
        "unique_key": f"{source}-{hour}",
        "values": {"close": price},
    }
