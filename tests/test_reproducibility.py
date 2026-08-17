from __future__ import annotations

from pathlib import Path

import pytest

from app.config.run_config import RunConfig
from app.domain.market import Instrument
from app.reproducibility import (
    InstrumentSnapshotStore,
    InstrumentSpecSnapshot,
    candles_hash,
    config_hash,
)
from tests.conftest import make_candles


def test_instrument_snapshot_priority_and_integrity(
    tmp_path: Path, btc_instrument: Instrument
) -> None:
    configured = tmp_path / "configured.json"
    local_dir = tmp_path / "local"
    snapshot = InstrumentSpecSnapshot.capture(
        btc_instrument, source="test", fetched_at=make_candles(["100"])[0].timestamp
    )
    InstrumentSnapshotStore.save(snapshot, configured)
    calls = 0

    def fetch(_instrument_id: str) -> Instrument:
        nonlocal calls
        calls += 1
        return btc_instrument

    resolved = InstrumentSnapshotStore(local_dir).resolve(
        "BTC-USDT", configured_path=configured, fetch=fetch
    )
    assert resolved.snapshot_hash == snapshot.snapshot_hash
    assert calls == 0

    payload = configured.read_text(encoding="utf-8").replace("BTC-USDT", "ETH-USDT")
    configured.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        InstrumentSnapshotStore.load(configured)


def test_hashes_are_deterministic(btc_instrument: Instrument) -> None:
    config = RunConfig()
    candles = make_candles(["100", "101"])
    assert config_hash(config) == config_hash(config.model_copy(deep=True))
    assert candles_hash(candles) == candles_hash(list(candles))

    first = InstrumentSpecSnapshot.capture(
        btc_instrument, source="test", fetched_at=candles[0].timestamp
    )
    second = InstrumentSpecSnapshot.capture(
        btc_instrument, source="test", fetched_at=candles[0].timestamp
    )
    assert first.snapshot_hash == second.snapshot_hash
