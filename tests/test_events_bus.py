from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.events import DomainEvent, EventBus


class FakeStore:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.claimed: list[tuple[str, str]] = []

    def claim_event(self, idempotency_key: str, event_type: str, payload_hash: str) -> bool:
        if not self.accept:
            return False
        self.claimed.append((idempotency_key, payload_hash))
        return True


def _event(key: str, payload: dict[str, object] | None = None) -> DomainEvent:
    return DomainEvent(
        run_id="run-1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        event_type="TEST",
        instrument_id="BTC-USDT",
        strategy_name="test",
        payload=payload or {"v": 1},
        idempotency_key=key,
    )


def test_storeless_bus_deduplicates_by_idempotency_key() -> None:
    bus = EventBus()
    assert bus.publish(_event("k1")) is True
    assert bus.publish(_event("k1")) is False
    assert len(bus.events) == 1


def test_storeless_bus_accepts_distinct_keys() -> None:
    bus = EventBus()
    assert bus.publish(_event("k1")) is True
    assert bus.publish(_event("k2")) is True
    assert len(bus.events) == 2


def test_stored_bus_conflicting_payload_raises() -> None:
    store = FakeStore(accept=True)
    bus = EventBus(store)
    assert bus.publish(_event("k1", {"v": 1})) is True
    with pytest.raises(ValueError, match="不同负载"):
        bus.publish(_event("k1", {"v": 2}))
    assert len(bus.events) == 1


def test_stored_bus_rejects_event_when_store_denies() -> None:
    store = FakeStore(accept=False)
    bus = EventBus(store)
    assert bus.publish(_event("k1")) is False
    assert len(bus.events) == 0
    assert store.claimed == []
