from __future__ import annotations

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
    WriteBoundaryCounters,
    analyze_timeline,
    append_jsonl,
    exchange_write_guard,
    finalize_artifact,
    read_jsonl,
    unexplained_gap_count,
    write_json_atomic,
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
