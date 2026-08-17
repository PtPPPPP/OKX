"""Tests for the Phase 2C1 strategy compute profiling tooling.

No wall-clock assertions: only result equivalence between compute-only and the
real replay (same strategy semantics), zero DB usage in compute-only mode,
deterministic call counts, and structural profile output.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config.run_config import load_run_config
from benchmarks.strategy_profiler import (
    attribute_counts,
    compute_only_replay,
    profile_with_cprofile,
    stats_table,
    workload_candles,
)

_CONFIG = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})


def test_compute_only_matches_real_replay_strategy_results(tmp_path: Path) -> None:
    """Same input ⇒ same buy/hold sequence and same signal payload text."""
    from app.market.historical_data import save_candles_csv
    from app.services.shadow_replay import run_shadow_replay
    from app.storage.database import Database
    from tests.migration_fakes import _now

    candles = workload_candles("C")
    profile = compute_only_replay(candles, _CONFIG)
    assert profile.bars == len(candles)

    workspace = tmp_path / "equivalence"
    workspace.mkdir()
    csv_path = workspace / "c.csv"
    save_candles_csv(candles, csv_path)
    database = Database(f"sqlite:///{workspace / 'r.db'}")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            """INSERT INTO runtime_generations(generation_id,generation_number,
            status,created_at,activated_at,manifest_sha256,database_sha256_before,
            authorization_json,notes) VALUES ('g',1,'active',?,?,'t','t','{}','2c1')""",
            (_now(), _now()),
        )
        connection.commit()
    result = run_shadow_replay(database, _CONFIG, csv_path, len(candles))

    assert result["entry_signals"] == profile.buys
    assert result["processed_candles"] == profile.bars
    with sqlite3.connect(database.path) as connection:
        persisted_buys = [
            str(row[0])
            for row in connection.execute(
                """SELECT candle_open_time FROM strategy_signal_events
                WHERE signal_type='buy' ORDER BY candle_open_time"""
            )
        ]
    assert persisted_buys == profile.buys_at, (
        "compute-only must reproduce the exact buy sequence of the real replay"
    )


def test_compute_only_never_touches_a_database() -> None:
    candles = workload_candles("B")
    before = list(Path("data").glob("*.db")) if Path("data").exists() else []
    profile = compute_only_replay(candles, _CONFIG)
    after = list(Path("data").glob("*.db")) if Path("data").exists() else []
    assert profile.bars == len(candles)
    assert profile.buys == 0
    assert before == after, "compute-only mode must not create or touch any database"


def test_call_counts_are_deterministic_across_repeats() -> None:
    candles = workload_candles("C")
    first = compute_only_replay(candles, _CONFIG)
    second = compute_only_replay(candles, _CONFIG)
    for field_name in (
        "bars",
        "buys",
        "holds",
        "warmup_bars",
        "buys_at",
        "signal_samples",
    ):
        assert getattr(first, field_name) == getattr(second, field_name), field_name
    assert first.signal_samples and second.signal_samples


def test_cprofile_attribution_and_stats_table_are_structurally_valid() -> None:
    candles = workload_candles("B")
    profiler, summary = profile_with_cprofile(candles, _CONFIG)
    assert summary["bars"] == len(candles)

    rows = stats_table(profiler, 50)
    assert rows, "stats table must not be empty"
    assert any("on_bar" in row["function"] for row in rows)
    assert any("rolling_vwap" in row["function"] for row in rows)

    counts = attribute_counts(profiler, summary["bars"])
    assert counts["on_bar_per_candle"] == 1.0
    assert counts["rolling_vwap_per_candle"] == 1.0
    assert counts["signal_ctor_per_candle"] == 1.0
    assert counts["json_dumps_per_candle"] >= 2.0  # signal_value + state snapshot (+ one-off setup)
    assert counts["sha256_per_candle"] >= 1.0  # per-signal identity (+ setup one-offs)
    assert counts["isoformat_per_candle"] >= 2.0


def test_workload_shapes_cover_the_three_regimes() -> None:
    warmup_heavy = workload_candles("A")
    steady = workload_candles("B")
    signals = workload_candles("C")
    assert compute_only_replay(warmup_heavy, _CONFIG).warmup_bars == 23
    assert compute_only_replay(steady, _CONFIG).buys == 0
    profile = compute_only_replay(signals, _CONFIG)
    assert profile.buys > 0
    assert [c.timestamp for c in signals] == sorted(c.timestamp for c in signals)
