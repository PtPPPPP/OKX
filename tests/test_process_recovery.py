from __future__ import annotations

import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from app.config.run_config import load_run_config
from app.domain.position import PortfolioSnapshot
from app.market.synthetic_candles import SyntheticCandleRequest
from app.runtime.clock import BacktestClock
from app.services.private_events import PrivateEventProcessor
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.reconciliation import AccountSync, ReconciliationService, ReconciliationStatus
from app.services.vwap_shadow_soak import (
    build_synthetic_soak_source,
    read_soak_snapshot,
    run_vwap_shadow_soak,
)
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from tests.conftest import make_candles, make_instrument
from tests.programmable_exchange import ProgrammableExchange

_CHILD_MARKER_SCRIPT = """
from pathlib import Path
import sys

from app.storage.database import Database
from app.storage.repositories import TradingRepository

database = Database(f"sqlite:///{sys.argv[1]}")
database.initialize()
TradingRepository(database).begin_private_reconciliation("os-process-kill")
Path(sys.argv[2]).write_text("ready", encoding="ascii")
sys.stdin.buffer.read()
"""

_CHILD_STATUS_SCRIPT = """
import sys

from app.storage.database import Database
from app.storage.repositories import TradingRepository

database = Database(f"sqlite:///{sys.argv[1]}")
print(TradingRepository(database).private_state_snapshot().status.value, flush=True)
"""

_CHILD_CHECKPOINT_FAILURE_SCRIPT = """
import sqlite3
import sys
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.synthetic_candles import SyntheticCandleRequest
from app.services.vwap_shadow_soak import build_synthetic_soak_source, run_vwap_shadow_soak
from app.storage.database import StorageError
from app.testing.fault_injection import FaultAction, FaultInjector, FaultPlan, FaultStep, VirtualClock


class LocalAdapter:
    is_local_adapter = True


database_path = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
marker = Path(sys.argv[3])
config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
source = build_synthetic_soak_source(
    SyntheticCandleRequest(count=160, seed=271828, bar_interval="1h")
)
injector = FaultInjector(
    FaultPlan(
        "process-checkpoint-failure",
        20260809,
        (FaultStep("shadow_soak.checkpoint.before_insert", FaultAction.STORAGE_ERROR),),
    ),
    LocalAdapter(),
    VirtualClock(),
)
try:
    run_vwap_shadow_soak(
        database_path=database_path,
        output_dir=output_dir,
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=100,
        fault_injector=injector,
    )
except StorageError:
    injector.assert_consumed()
    with sqlite3.connect(database_path) as connection:
        run_id = connection.execute("SELECT run_id FROM shadow_soak_runs").fetchone()[0]
    marker.write_text(str(run_id), encoding="ascii")
    sys.stdin.buffer.read()
    raise SystemExit(0)
raise AssertionError("checkpoint fault did not occur")
"""

_CHILD_SOAK_RESUME_SCRIPT = """
import sys
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.synthetic_candles import SyntheticCandleRequest
from app.services.vwap_shadow_soak import build_synthetic_soak_source, run_vwap_shadow_soak

config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
source = build_synthetic_soak_source(
    SyntheticCandleRequest(count=160, seed=271828, bar_interval="1h")
)
run_vwap_shadow_soak(
    database_path=Path(sys.argv[1]),
    output_dir=Path(sys.argv[2]),
    config=config,
    source=source,
    bar_interval="1h",
    checkpoint_every=100,
    resume_run_id=sys.argv[3],
)
print(sys.argv[3], flush=True)
"""


def _wait_for_marker(marker: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"child process exited before checkpoint: {stderr}")
        time.sleep(0.01)
    raise AssertionError("child process did not reach the durable checkpoint")


def test_os_process_kill_and_restart_recovers_temporary_private_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "process-recovery.db"
    marker = tmp_path / "child-ready.txt"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD_MARKER_SCRIPT, str(database_path), str(marker)],
        cwd=root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(marker, child)
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    restarted = subprocess.run(
        [sys.executable, "-c", _CHILD_STATUS_SCRIPT, str(database_path)],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert restarted.stdout.strip() == "reconciling_expected"

    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    candles = make_candles(["100", "101"])
    exchange = ProgrammableExchange(
        PortfolioSnapshot(
            balances={"BTC": Decimal("0"), "USDT": Decimal("100")},
            positions={instrument.instrument_id: Decimal("0")},
            average_entry_prices={},
        ),
        candles,
    )
    database = Database(f"sqlite:///{database_path}")
    repository = TradingRepository(database)
    AccountSync(exchange, repository, BacktestClock(candles[-1].timestamp)).sync(
        instrument,
        "5m",
        run_id="process-recovery",
        mode="demo",
        strategy_name="moving_average_cross",
    )
    coordinator = PrivateStateCoordinator(
        PrivateEventProcessor(repository),
        ReconciliationService(exchange, repository),
        repository,
    )

    recovered = coordinator.reconcile_private_state(instrument, source="os-process-restart")

    assert recovered.status is ReconciliationStatus.HEALTHY
    assert repository.private_state_snapshot().submission_allowed
    assert exchange.broker_write_calls == 0
    assert exchange.external_network_calls == 0


def test_os_kill_after_checkpoint_failure_restarts_to_baseline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    source = build_synthetic_soak_source(
        SyntheticCandleRequest(count=160, seed=271828, bar_interval="1h")
    )
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    baseline_database = tmp_path / "baseline.db"
    baseline_result = run_vwap_shadow_soak(
        database_path=baseline_database,
        output_dir=tmp_path / "baseline-output",
        config=config,
        source=source,
        bar_interval="1h",
        checkpoint_every=100,
    )
    baseline = read_soak_snapshot(baseline_database, str(baseline_result["run_id"]))

    database_path = tmp_path / "checkpoint-failure.db"
    output_dir = tmp_path / "output"
    marker = tmp_path / "checkpoint-failed.txt"
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            _CHILD_CHECKPOINT_FAILURE_SCRIPT,
            str(database_path),
            str(output_dir),
            str(marker),
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(marker, child)
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    run_id = marker.read_text(encoding="ascii")
    restarted = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD_SOAK_RESUME_SCRIPT,
            str(database_path),
            str(output_dir),
            run_id,
        ],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert restarted.stdout.strip() == run_id
    assert read_soak_snapshot(database_path, run_id) == baseline
