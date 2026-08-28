from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.market import Candle
from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.services.legacy_quarantine import RuntimeGenerationService
from app.storage.database import Database
from scripts.phase_4a_soak import (
    _WINDOWS_ERROR_ACCESS_DENIED,
    _WINDOWS_ERROR_INVALID_PARAMETER,
    WriteBoundaryCounters,
    _await_startup_evidence,
    _load_json,
    _process_exists,
    _windows_open_error_reports_missing,
    analyze_timeline,
    append_jsonl,
    exchange_write_guard,
    finalize_artifact,
    read_jsonl,
    unexplained_gap_count,
    write_json_atomic,
    write_startup_evidence,
)


@dataclass(frozen=True, slots=True)
class _Configuration:
    strategy_name: str = "vwap_shadow"
    instrument_id: str = "BTC-USDT"
    timeframe: str = "1h"


def test_jsonl_append_is_resume_safe_and_rejects_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "samples.jsonl"
    append_jsonl(path, {"type": "start", "count": 0})
    append_jsonl(path, {"type": "periodic", "count": 1})

    assert [record["count"] for record in read_jsonl(path)] == [0, 1]

    monkeypatch.setenv("OKX_API_KEY", "sensitive-demo-value")
    with pytest.raises(ValueError, match="credential value"):
        append_jsonl(path, {"value": "sensitive-demo-value"})
    with pytest.raises(ValueError, match="sensitive field"):
        write_json_atomic(tmp_path / "unsafe.json", {"api_key": "redacted"})


def test_timeline_analysis_counts_duplicates_gaps_and_out_of_order() -> None:
    start = datetime(2026, 8, 17, tzinfo=UTC)
    timeline = [
        start,
        start + timedelta(hours=1),
        start + timedelta(hours=1),
        start + timedelta(hours=4),
        start + timedelta(hours=3),
    ]

    result = analyze_timeline(timeline, timedelta(hours=1))

    assert result.duplicates == 1
    assert result.missing_intervals == 2
    assert result.out_of_order == 1

    persisted = [start, start + timedelta(hours=1)]
    assert (
        unexplained_gap_count([start, start + timedelta(hours=3)], persisted, timedelta(hours=1))
        == 1
    )


def test_exchange_write_guard_fails_closed_and_counts_attempt() -> None:
    counters = WriteBoundaryCounters()

    with exchange_write_guard(counters), pytest.raises(RuntimeError, match="write guard"):
        from app.exchange.okx_client import OkxClient

        OkxClient.place_order(object(), object())  # type: ignore[arg-type]

    assert counters.place_order_calls == 1
    assert counters.total == 1


def test_finalize_reconciles_artifact_and_database_counters(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    database_path = tmp_path / "soak.db"
    database = Database(f"sqlite:///{database_path}")
    database.initialize()
    generation = RuntimeGenerationService(database)
    generation_id = generation.create_preparing("manifest", "new-database", {"phase": "4A"}, "test")
    generation.activate(generation_id)
    repository = ContinuousShadowRepository(database)
    config = _Configuration()
    run_id = "phase4a-test-run"
    started = datetime(2026, 8, 17, tzinfo=UTC)
    candle = Candle(
        started,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        Decimal("1"),
        True,
    )
    repository.create_run(run_id, config, started)
    assert repository.commit_vwap_shadow_candle(
        run_id=run_id,
        config=config,
        candle=candle,
        strategy_version="test",
        signal_id="signal",
        signal_type="hold",
        signal_value="{}",
        runtime_state="{}",
        warmup_count=1,
        warmup_completed=False,
        proposal_price=None,
        processed_count=1,
        signal_count=1,
        proposal_count=0,
    )
    repository.finish(run_id, "stopped", "test_complete")

    write_json_atomic(
        artifact / "metadata.json",
        {
            "run_id": run_id,
            "database": str(database_path),
            "git_commit": "commit",
            "instrument": "BTC-USDT",
            "bar_interval": "1h",
            "start_utc": started.isoformat(),
            "duration_target_seconds": 30,
            "database_preflight": {"database_bytes": 0},
        },
    )
    for sample_type in ("start", "end"):
        append_jsonl(
            artifact / "samples.jsonl",
            {
                "type": sample_type,
                "public_ws_connects": 1,
                "public_ws_disconnects": 0,
                "public_ws_reconnects": 0,
                "rest_requests": 1,
                "rest_failures": 0,
                "rest_fallbacks": 0,
                "resources": {
                    "rss_bytes": 100,
                    "cpu_seconds": 1.0,
                    "open_handles": 5,
                    "wal_bytes": 0,
                    "log_bytes": 10,
                },
            },
        )
    append_jsonl(
        artifact / "samples.jsonl",
        {
            "type": "confirmed_candle_received",
            "candle_open_time": candle.timestamp.isoformat(),
        },
    )
    write_json_atomic(
        artifact / "runtime_summary.json",
        {
            "end_utc": (started + timedelta(seconds=60)).isoformat(),
            "graceful_shutdown": True,
            "pending_tasks_after_shutdown": 0,
            "uncaught_exceptions": 0,
            "database_locked_events": 0,
            "network_error_classes": {},
            "write_guard": {
                "place_order_calls": 0,
                "cancel_order_calls": 0,
                "controlled_demo_write_calls": 0,
                "demo_authorization_consumed": 0,
                "broker_writes": 0,
            },
        },
    )

    report = finalize_artifact(artifact)

    assert report["correctness_gates_passed"] is True
    assert report["shakedown_passed"] is True
    assert report["candles"] == {
        "confirmed_received": 1,
        "confirmed_processed": 1,
        "duplicates_seen": 0,
        "duplicates_persisted": 0,
        "out_of_order_seen": 0,
        "out_of_order_persisted": 0,
        "unexplained_gaps": 0,
        "missing_persisted": 0,
    }
    assert (artifact / "final_report.json").exists()


def test_finalize_rejects_incomplete_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="incomplete"):
        finalize_artifact(tmp_path)


def _spawn_sleeping_child(seconds: int) -> subprocess.Popen[None]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def test_process_exists_reports_current_process_alive() -> None:
    assert _process_exists(os.getpid()) is True


def test_process_probe_does_not_signal_running_child() -> None:
    child = _spawn_sleeping_child(6)
    try:
        for _ in range(4):
            assert _process_exists(child.pid) is True
            # A probe that delivered Ctrl+C (the old os.kill(pid, 0) path on
            # Windows) would interrupt the sleeper here.
            assert child.poll() is None
            time.sleep(0.2)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_process_exists_reports_reaped_child_dead() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=30) == 0
    assert _process_exists(child.pid) is False


def test_process_exists_reports_missing_pid_dead() -> None:
    # 2**31 - 1 exceeds every supported PID space (Windows PIDs are < 2**31,
    # Linux pid_max <= 2**22), so no process can own it.
    assert _process_exists(2_147_483_647) is False


@pytest.mark.parametrize("process_id", [0, -1])
def test_process_exists_refuses_nonpositive_pid(process_id: int) -> None:
    assert _process_exists(process_id) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows unreaped-handle liveness semantics")
def test_windows_process_exists_reports_unreaped_terminated_child_dead() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 30
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child.poll() is not None  # exited, deliberately left unwaited
    try:
        assert _process_exists(child.pid) is False
    finally:
        child.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Win32 OpenProcess error classification")
def test_windows_open_errors_fail_closed_for_unknown_and_denied() -> None:
    assert _windows_open_error_reports_missing(_WINDOWS_ERROR_INVALID_PARAMETER) is True
    # A live process refusing query access must not be reported dead.
    assert _windows_open_error_reports_missing(_WINDOWS_ERROR_ACCESS_DENIED) is False
    assert _windows_open_error_reports_missing(0) is False


_EVIDENCE_CHILD_SNIPPET = "\n".join(
    [
        "import json, os, sys, time",
        "artifact, run_id, soak_id, config_hash = sys.argv[1:5]",
        "pid = os.getpid()",
        "tmp = os.path.join(artifact, 'metadata.json.tmp')",
        "with open(tmp, 'w', encoding='utf-8') as handle:",
        "    json.dump({'run_id': run_id, 'config_hash': config_hash, 'process_id': pid}, handle)",
        "os.replace(tmp, os.path.join(artifact, 'metadata.json'))",
        "time.sleep(0.3)",
        "payload = {",
        "    'schema_version': 1,",
        "    'run_id': run_id,",
        "    'soak_id': soak_id,",
        "    'process_id': pid,",
        "    'rest_bootstrap': True,",
        "    'public_ws_connected': True,",
        "    'exchange_write_guard_installed': True,",
        "    'config_hash': config_hash,",
        "}",
        "tmp = os.path.join(artifact, 'startup_evidence.json.tmp')",
        "with open(tmp, 'w', encoding='utf-8') as handle:",
        "    json.dump(payload, handle)",
        "os.replace(tmp, os.path.join(artifact, 'startup_evidence.json'))",
        "time.sleep(30)",
    ]
)


def _spawn_evidence_child(artifact: Path, run_id: str, config_hash: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _EVIDENCE_CHILD_SNIPPET,
            str(artifact),
            run_id,
            artifact.name,
            config_hash,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_silent_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _prepare_run_metadata(artifact: Path, run_id: str, config_hash: str = "cfg-hash") -> None:
    artifact.mkdir(parents=True, exist_ok=True)
    write_json_atomic(artifact / "metadata.json", {"run_id": run_id, "config_hash": config_hash})


def test_startup_evidence_proves_run_independent_of_sampling(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _prepare_run_metadata(artifact, "run-abc", "cfg-123")
    child = _spawn_evidence_child(artifact, "run-abc", "cfg-123")
    try:
        started = time.monotonic()
        evidence = _await_startup_evidence(
            artifact_dir=artifact,
            process=child,
            soak_id=artifact.name,
            timeout_seconds=10,
            poll_interval_seconds=0.05,
        )
        elapsed = time.monotonic() - started
        assert evidence["rest_bootstrap"] is True
        assert evidence["public_ws_connected"] is True
        assert evidence["run_id"] == "run-abc"
        # Proof arrived without any periodic sample, far below any cadence.
        assert elapsed < 5
        assert not (artifact / "samples.jsonl").exists()
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.mark.parametrize("sample_interval_seconds", [5, 60, 300, 3600])
def test_startup_proof_identical_for_every_sample_interval(
    tmp_path: Path, sample_interval_seconds: int
) -> None:
    # The sampler cadence must never gate the startup handshake: even with a
    # start sample stuck at zero connections and no periodic sample inside the
    # whole startup window, the evidence channel proves the run.
    artifact = tmp_path / f"artifact-{sample_interval_seconds}"
    _prepare_run_metadata(artifact, "run-xyz")
    append_jsonl(artifact / "samples.jsonl", {"type": "start", "public_ws_connects": 0})
    child = _spawn_evidence_child(artifact, "run-xyz", "cfg-hash")
    try:
        evidence = _await_startup_evidence(
            artifact_dir=artifact,
            process=child,
            soak_id=artifact.name,
            timeout_seconds=10,
            poll_interval_seconds=0.05,
        )
        assert evidence["public_ws_connected"] is True
        assert evidence["run_id"] == "run-xyz"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_startup_wait_times_out_and_requests_graceful_stop(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _prepare_run_metadata(artifact, "run-slow")
    child = _spawn_silent_sleeper()
    try:
        with pytest.raises(TimeoutError, match="startup was not proven"):
            _await_startup_evidence(
                artifact_dir=artifact,
                process=child,
                soak_id=artifact.name,
                timeout_seconds=2,
                poll_interval_seconds=0.05,
            )
        assert (artifact / "stop.requested").exists()
        # The wait never signaled the child: it survived the whole window.
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_startup_wait_fails_fast_when_child_exits_without_evidence(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _prepare_run_metadata(artifact, "run-dead")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="exited during startup"):
        _await_startup_evidence(
            artifact_dir=artifact,
            process=child,
            soak_id=artifact.name,
            timeout_seconds=30,
            poll_interval_seconds=0.05,
        )
    assert time.monotonic() - started < 10
    assert not (artifact / "startup_evidence.json").exists()


def test_startup_wait_rejects_foreign_run_evidence(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _prepare_run_metadata(artifact, "run-real")
    child = _spawn_silent_sleeper()
    try:
        assert (
            write_startup_evidence(
                artifact / "startup_evidence.json",
                run_id="run-other",
                soak_id=artifact.name,
                process_id=child.pid,
                instrument="BTC-USDT",
                bar_interval="1h",
                database_path=artifact / "soak.db",
                git_commit="commit",
                config_hash="cfg-hash",
                runtime_generation="gen",
            )
            is True
        )
        with pytest.raises(TimeoutError, match="run_id"):
            _await_startup_evidence(
                artifact_dir=artifact,
                process=child,
                soak_id=artifact.name,
                timeout_seconds=2,
                poll_interval_seconds=0.05,
            )
        assert (artifact / "stop.requested").exists()
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_startup_wait_rejects_malformed_evidence(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _prepare_run_metadata(artifact, "run-broken")
    child = _spawn_silent_sleeper()
    try:
        (artifact / "startup_evidence.json").write_bytes(b"{not valid json")
        with pytest.raises(TimeoutError, match="invalid startup evidence"):
            _await_startup_evidence(
                artifact_dir=artifact,
                process=child,
                soak_id=artifact.name,
                timeout_seconds=2,
                poll_interval_seconds=0.05,
            )
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_startup_evidence_is_one_shot_and_credential_free(tmp_path: Path) -> None:
    path = tmp_path / "startup_evidence.json"
    base = {
        "run_id": "run-1",
        "soak_id": "soak",
        "process_id": 4242,
        "instrument": "BTC-USDT",
        "bar_interval": "1h",
        "database_path": tmp_path / "soak.db",
        "git_commit": "commit",
        "config_hash": "hash",
        "runtime_generation": "gen",
    }
    assert write_startup_evidence(path, **base) is True
    evidence = _load_json(path)
    assert evidence["rest_bootstrap"] is True
    assert evidence["public_ws_connected"] is True
    assert evidence["exchange_write_guard_installed"] is True
    assert not {"api_key", "secret", "passphrase", "account_id"} & set(evidence)
    assert write_startup_evidence(path, **{**base, "run_id": "run-2"}) is False
    assert _load_json(path) == evidence
