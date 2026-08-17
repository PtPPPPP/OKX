from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from app.domain.market import Instrument
from app.domain.private_state import PrivateStateStatus
from app.market.private_websocket import PrivateEvent, PrivateEventKind
from app.services.private_events import PrivateEventProcessor
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.reconciliation import ReconciliationResult, ReconciliationStatus
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from tests.conftest import make_instrument


class BlockingHealthyReconciler:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def reconcile(
        self, instrument: Instrument, *, persist_remote_state: bool = False
    ) -> ReconciliationResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        return ReconciliationResult(ReconciliationStatus.HEALTHY, "rest baseline", 0, 0)


def _event(
    key: str,
    *,
    amount: str,
    sequence: int,
    epoch: int = 1,
) -> PrivateEvent:
    return PrivateEvent(
        PrivateEventKind.ACCOUNT,
        key,
        {
            "uTime": str(1_000 + sequence),
            "details": [
                {
                    "ccy": "USDT",
                    "cashBal": amount,
                    "availBal": amount,
                    "frozenBal": "0",
                    "eq": amount,
                    "uTime": str(1_000 + sequence),
                }
            ],
        },
        connection_epoch=epoch,
        sequence=sequence,
    )


def _coordinator(
    tmp_path: Path,
) -> tuple[TradingRepository, PrivateStateCoordinator, BlockingHealthyReconciler]:
    database = Database(f"sqlite:///{tmp_path / 'watermark.db'}")
    database.initialize()
    repository = TradingRepository(database)
    reconciler = BlockingHealthyReconciler()
    coordinator = PrivateStateCoordinator(PrivateEventProcessor(repository), reconciler, repository)
    return repository, coordinator, reconciler


def test_rest_baseline_buffers_and_replays_ordered_private_events(tmp_path: Path) -> None:
    repository, coordinator, reconciler = _coordinator(tmp_path)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(coordinator.reconcile_private_state, instrument, source="test")
        assert reconciler.started.wait(timeout=2)
        token = coordinator.active_reconciliation
        assert token is not None
        assert token.starting_ws_watermark == 0
        assert coordinator.handle_private_ws_event(_event("account:1", amount="100", sequence=1))
        assert coordinator.handle_private_ws_event(_event("account:2", amount="120", sequence=2))
        assert repository.private_state_snapshot().status is PrivateStateStatus.RECONCILING_EXPECTED
        reconciler.release.set()
        result = future.result(timeout=2)

    assert result.status is ReconciliationStatus.HEALTHY
    snapshot = repository.private_state_snapshot()
    assert snapshot.status is PrivateStateStatus.HEALTHY
    assert snapshot.ws_watermark == 2
    with repository.database.connect() as connection:
        payload = connection.execute(
            "SELECT normalized_json FROM private_state_snapshots WHERE scope_key='account:USDT'"
        ).fetchone()[0]
    assert '"cash_balance": "120"' in payload


def test_duplicate_event_is_idempotent_and_sequence_gap_freezes(tmp_path: Path) -> None:
    repository, coordinator, _ = _coordinator(tmp_path)

    assert coordinator.handle_private_ws_event(_event("account:1", amount="100", sequence=1))
    assert not coordinator.handle_private_ws_event(_event("account:1", amount="100", sequence=1))
    assert not coordinator.handle_private_ws_event(_event("account:3", amount="130", sequence=3))

    snapshot = repository.private_state_snapshot()
    assert snapshot.ws_watermark == 1
    assert snapshot.status is PrivateStateStatus.FROZEN
    assert snapshot.version > 0


def test_sequence_gap_during_rest_reconciliation_stays_frozen(tmp_path: Path) -> None:
    repository, coordinator, reconciler = _coordinator(tmp_path)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(coordinator.reconcile_private_state, instrument, source="test")
        assert reconciler.started.wait(timeout=2)
        assert not coordinator.handle_private_ws_event(
            _event("account:gap", amount="100", sequence=2)
        )
        reconciler.release.set()
        result = future.result(timeout=2)

    assert result.status is ReconciliationStatus.BLOCKED
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN


def test_stale_connection_epoch_is_rejected_without_contaminating_state(tmp_path: Path) -> None:
    repository, coordinator, _ = _coordinator(tmp_path)

    assert coordinator.handle_private_ws_event(
        _event("account:new", amount="200", sequence=1, epoch=2)
    )
    assert not coordinator.handle_private_ws_event(
        _event("account:old", amount="20", sequence=2, epoch=1)
    )

    with repository.database.connect() as connection:
        payload = connection.execute(
            "SELECT normalized_json FROM private_state_snapshots WHERE scope_key='account:USDT'"
        ).fetchone()[0]
    assert '"cash_balance": "200"' in payload
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN


def test_startup_demo_order_and_doctor_requests_share_one_reconciliation_token(
    tmp_path: Path,
) -> None:
    repository, coordinator, reconciler = _coordinator(tmp_path)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        startup = pool.submit(
            coordinator.reconcile_private_state, instrument, source="startup_recovery"
        )
        assert reconciler.started.wait(timeout=2)
        demo_order = coordinator.reconcile_private_state(instrument, source="demo_order_service")
        doctor = coordinator.reconcile_private_state(instrument, source="demo_doctor")
        reconciler.release.set()
        startup_result = startup.result(timeout=2)

    assert demo_order.status is ReconciliationStatus.BLOCKED
    assert doctor.status is ReconciliationStatus.BLOCKED
    assert startup_result.status is ReconciliationStatus.BLOCKED
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN


def test_private_ws_buffer_overflow_freezes_without_replay(tmp_path: Path) -> None:
    repository, _, reconciler = _coordinator(tmp_path)
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository), reconciler, repository, max_buffered_events=1
    )
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(coordinator.reconcile_private_state, instrument, source="buffer-test")
        assert reconciler.started.wait(timeout=2)
        assert coordinator.handle_private_ws_event(_event("account:1", amount="100", sequence=1))
        assert not coordinator.handle_private_ws_event(
            _event("account:2", amount="120", sequence=2)
        )
        reconciler.release.set()
        result = future.result(timeout=2)

    assert result.status is ReconciliationStatus.BLOCKED
    assert repository.private_state_snapshot().status is PrivateStateStatus.FROZEN
