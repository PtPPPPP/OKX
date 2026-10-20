from __future__ import annotations

import hashlib
import json
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class DomainEvent:
    run_id: str
    timestamp: datetime
    event_type: str
    instrument_id: str
    strategy_name: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid4().hex)
    idempotency_key: str | None = None


class EventStore(Protocol):
    def claim_event(self, idempotency_key: str, event_type: str, payload_hash: str) -> bool: ...


@runtime_checkable
class DurableEventStore(EventStore, Protocol):
    def append_event(self, event: DomainEvent) -> bool: ...


class EventBus:
    def __init__(
        self, store: EventStore | None = None, *, retention_limit: int | None = None
    ) -> None:
        if retention_limit is not None and (
            retention_limit < 1 or not isinstance(store, DurableEventStore)
        ):
            raise ValueError(
                "bounded EventBus requires a durable append_event store and positive limit"
            )
        self._events: deque[DomainEvent] = deque(maxlen=retention_limit)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._store = store
        self.retention_limit = retention_limit
        self.durable = retention_limit is not None

    def publish(self, event: DomainEvent) -> bool:
        key = event.idempotency_key or event.event_id
        previous_hash = self._seen.get(key)
        if previous_hash is not None and not self.durable:
            if self._store is None:
                return False
            payload_hash = self._payload_hash(event)
            if previous_hash != payload_hash:
                raise ValueError("相同事件幂等键对应了不同负载")
            return False
        if self._store is not None:
            payload_hash = self._payload_hash(event)
            accepted = (
                self._store.append_event(event)
                if self.durable and isinstance(self._store, DurableEventStore)
                else self._store.claim_event(key, event.event_type, payload_hash)
            )
            if not accepted:
                # No cache growth from arbitrary duplicate/replayed keys.
                return False
            self._seen[key] = payload_hash
        else:
            # Store-less buses (backtest/audit) do not need payload hashes; the
            # seen-mark alone preserves idempotent de-duplication semantics.
            self._seen[key] = ""
        self._events.append(event)
        if self.retention_limit is not None and len(self._seen) > self.retention_limit:
            self._seen.popitem(last=False)
        return True

    @staticmethod
    def _payload_hash(event: DomainEvent) -> str:
        return hashlib.sha256(
            json.dumps(event.payload, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def events(self) -> tuple[DomainEvent, ...]:
        return tuple(self._events)
