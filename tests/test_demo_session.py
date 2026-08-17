from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from app.domain.market import Instrument
from app.market.network import NetworkConfiguration
from app.market.private_websocket import PrivateStreamHealth
from app.services.demo_session import (
    DemoTradingSession,
    PrivateReadinessStage,
    _new_monitor_event_loop,
)
from app.services.reconciliation import ReconciliationResult, ReconciliationStatus
from app.storage.database import Database
from app.storage.repositories import TradingRepository


class _Coordinator:
    def __init__(self) -> None:
        self.calls = 0

    def reconcile_private_state(
        self, instrument: Instrument, *, source: str
    ) -> ReconciliationResult:
        self.calls += 1
        return ReconciliationResult(ReconciliationStatus.HEALTHY, "ok", 0, 0)


class _Repository:
    def __init__(self) -> None:
        self.responses = [True, False]

    def has_unreconciled_private_state(self) -> bool:
        return self.responses.pop(0) if self.responses else False


class _Stream:
    def __init__(self) -> None:
        self.network = NetworkConfiguration.from_environment(
            {
                "OKX_NETWORK_MODE": "proxy",
                "OKX_PROXY_URL": "http://user:password@127.0.0.1:7890",
            }
        )
        self.is_ready = False
        self.health = PrivateStreamHealth(
            False, False, False, None, None, None, None, 0, True, None
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows event loop regression")
def test_private_monitor_uses_selector_loop_on_windows() -> None:
    loop = _new_monitor_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_final_reconciliation_fails_closed_when_private_push_arrives_after_rest_check(
    btc_instrument: Instrument,
) -> None:
    session = object.__new__(DemoTradingSession)
    coordinator = _Coordinator()
    cast(Any, session).private_state_coordinator = coordinator
    session.repository = cast(TradingRepository, _Repository())
    session.instrument = btc_instrument
    with pytest.raises(RuntimeError, match="private state changed during final reconciliation"):
        session._reconcile_private_state(timeout_seconds=1)

    assert coordinator.calls == 1


def test_private_readiness_audit_persists_redacted_structured_telemetry(tmp_path: Path) -> None:
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'audit.db'}"))
    repository.database.initialize()
    session = object.__new__(DemoTradingSession)
    session.repository = repository
    session.stream = cast(Any, _Stream())
    session.monitor = None
    session.start_snapshot = None
    session._thread = None
    session._monitor_error = None
    session._stage = PrivateReadinessStage.DISCONNECTED
    session._readiness_id = "readiness-audit"
    session._last_reconciliation = None

    session._record_readiness_event("private_readiness_test")

    with repository.database.connect() as connection:
        row = connection.execute(
            "SELECT details_json FROM system_events WHERE event_type='private_readiness_test'"
        ).fetchone()
    assert row is not None
    assert '"private_ws_connect_attempts"' in str(row["details_json"])
    assert "password" not in str(row["details_json"])
    assert "http://***@127.0.0.1:7890" in str(row["details_json"])
