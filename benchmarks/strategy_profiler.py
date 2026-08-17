"""Phase 2C1 strategy compute profiling: where does non-DB time go?

Benchmark-only. Runs the REAL VWAP strategy, real Decimal math, real Signal
construction and real serialization — only persistence is omitted in the
compute-only mode. Nothing here is imported by app/.

Two methods, cross-checked:
  A. explicit per-bar timers (on_bar / serialization / orchestration)
  B. cProfile + pstats (function-level attribution, saved as .pstats artifact)

Usage:
    uv run python -m benchmarks.strategy_profiler [--output PATH] [--pstats PATH]
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import shutil
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config.run_config import load_run_config
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.position import PortfolioSnapshot
from app.domain.signal import SignalAction
from app.market.historical_data import BAR_INTERVALS, save_candles_csv
from app.market.synthetic_candles import SyntheticCandleRequest, generate_synthetic_candles
from app.reproducibility import InstrumentSnapshotStore
from app.runtime.clock import BacktestClock
from app.strategies.registry import create_strategy
from benchmarks.persistence_metrics import PersistenceMetrics, instrumented_sqlite

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / "configs" / "btc_vwap_shadow.yaml"
_SYNTHETIC_SEED = 20260731
_STRATEGY_VERSION = "vwap_shadow_v1"


# --------------------------------------------------------------- workloads


def _flat_then(base: Decimal, drops: bool, count: int, *, hours_offset: int = 0) -> list:
    from app.domain.market import Candle

    close = base * (Decimal("97") / Decimal("100")) if drops else base
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hours_offset)
    return [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("100"),
            confirmed=True,
        )
        for index in range(count)
    ]


def workload_candles(kind: str) -> list:
    """A=warmup-heavy, B=steady no-signal, C=signal-generating (drop region)."""
    base = Decimal("30000")
    if kind == "A":  # 24 warmup bars without a full window, then flat no-signal
        return _flat_then(base, drops=False, count=124)
    if kind == "B":  # window always full, price never deviates
        return _flat_then(base, drops=False, count=300)
    if kind == "C":  # flat then -3% drop region: deterministic BUY burst
        flat = _flat_then(base, drops=False, count=224)
        dropped = _flat_then(base, drops=True, count=200, hours_offset=224)
        return flat + dropped
    raise ValueError(f"unknown workload kind: {kind}")


# ----------------------------------------------------------- compute-only


@dataclass
class ComputeProfile:
    bars: int = 0
    buys: int = 0
    holds: int = 0
    warmup_bars: int = 0
    on_bar_ns: list[int] = field(default_factory=list)
    signal_value_json_ns: list[int] = field(default_factory=list)
    state_json_ns: list[int] = field(default_factory=list)
    buy_on_bar_ns: list[int] = field(default_factory=list)
    hold_on_bar_ns: list[int] = field(default_factory=list)
    elapsed_ns: int = 0
    signal_samples: list[str] = field(default_factory=list)
    buys_at: list[str] = field(default_factory=list)


def compute_only_replay(candles: list, config: Any) -> ComputeProfile:
    """Real strategy/Decimal/Signal/serialization loop; zero persistence."""
    from app.services.shadow_replay import _single_signal

    interval = BAR_INTERVALS[config.market.bar.lower()]
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    run_id = "phase2c1-compute-only"
    strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
    clock = BacktestClock(candles[0].timestamp)
    portfolio = PortfolioSnapshot({}, {}, {}, trusted_for_trading=False)
    strategy.on_start(
        StrategyContext(
            run_id,
            __import__("app.config.settings", fromlist=["TradingMode"]).TradingMode.DEMO,
            strategy.name,
            instrument,
            config.market.bar,
            portfolio,
            None,
            clock,
        )
    )
    profile = ComputeProfile()
    started = time.perf_counter_ns()
    for candle in candles:
        clock.advance_to(candle.timestamp + interval)
        context = StrategyContext(
            run_id,
            __import__("app.config.settings", fromlist=["TradingMode"]).TradingMode.DEMO,
            strategy.name,
            instrument,
            config.market.bar,
            portfolio,
            MarketSnapshot(candle, candle.close),
            clock,
        )
        t0 = time.perf_counter_ns()
        signal = _single_signal(strategy.on_bar(context, candle))
        t1 = time.perf_counter_ns()
        signal_value = json.dumps(
            {
                "close": str(signal.metadata["close"]),
                "vwap": (
                    str(signal.metadata["vwap"]) if signal.metadata["vwap"] is not None else None
                ),
                "deviation_bps": (
                    str(signal.metadata["deviation_bps"])
                    if signal.metadata["deviation_bps"] is not None
                    else None
                ),
                "vwap_window": signal.metadata["vwap_window"],
                "window_length": signal.metadata["window_length"],
                "reason": signal.reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        t2 = time.perf_counter_ns()
        state_snapshot = getattr(strategy, "state_snapshot", lambda: {})()
        json.dumps(state_snapshot, sort_keys=True)
        t3 = time.perf_counter_ns()

        profile.bars += 1
        profile.on_bar_ns.append(t1 - t0)
        profile.signal_value_json_ns.append(t2 - t1)
        profile.state_json_ns.append(t3 - t2)
        if signal.action is SignalAction.BUY:
            profile.buys += 1
            profile.buy_on_bar_ns.append(t1 - t0)
            profile.buys_at.append(candle.timestamp.isoformat())
            if len(profile.signal_samples) < 3:
                profile.signal_samples.append(signal_value)
        else:
            profile.holds += 1
            profile.hold_on_bar_ns.append(t1 - t0)
        if signal.metadata["vwap"] is None:
            profile.warmup_bars += 1
    profile.elapsed_ns = time.perf_counter_ns() - started
    return profile


def full_replay_timed(candles: list, config: Any) -> tuple[dict[str, Any], PersistenceMetrics]:
    """Real replay (scoped NORMAL session) with DB timing; business result dict."""
    from app.services.shadow_replay import run_shadow_replay
    from app.storage.database import Database
    from tests.migration_fakes import _now

    workspace = Path(tempfile.mkdtemp(prefix="okx-2c1-full-"))
    metrics = PersistenceMetrics()
    try:
        csv_path = workspace / "candles.csv"
        save_candles_csv(candles, csv_path)
        database = Database(f"sqlite:///{workspace / 'replay.db'}")
        database.initialize()
        with sqlite3.connect(database.path) as connection:
            connection.execute(
                """INSERT INTO runtime_generations(generation_id,generation_number,
                status,created_at,activated_at,manifest_sha256,database_sha256_before,
                authorization_json,notes) VALUES ('g',1,'active',?,?,'g','g','{}','2c1')""",
                (_now(), _now()),
            )
            connection.commit()
        with instrumented_sqlite(metrics):
            result = run_shadow_replay(database, config, csv_path, len(candles))
        return dict(result), metrics
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ------------------------------------------------------------- cProfile


def profile_with_cprofile(candles: list, config: Any) -> tuple[cProfile.Profile, dict[str, Any]]:
    profiler = cProfile.Profile()
    profiler.enable()
    compute = compute_only_replay(candles, config)
    profiler.disable()
    summary = {
        "bars": compute.bars,
        "buys": compute.buys,
        "elapsed_ns": compute.elapsed_ns,
    }
    return profiler, summary


def stats_table(profiler: cProfile.Profile, limit: int = 50) -> list[dict[str, Any]]:
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("cumulative")
    rows: list[dict[str, Any]] = []

    def extract(sort_key: str) -> list[dict[str, Any]]:
        stream.seek(0)
        stream.truncate(0)
        ordered = pstats.Stats(profiler, stream=stream)
        ordered.sort_stats(sort_key)
        extracted: list[dict[str, Any]] = []
        _width, funcs = ordered.get_print_list([])
        for func in funcs[:limit]:
            cc, nc, tt, ct, _callers = ordered.stats[func]
            extracted.append(
                {
                    "function": f"{func[2]}:{func[0]}:{func[1]}",
                    "primitive_calls": cc,
                    "calls": nc,
                    "self_time_s": round(tt, 6),
                    "cumulative_time_s": round(ct, 6),
                }
            )
        return extracted

    rows = extract("cumulative")
    return rows


def attribute_counts(profiler: cProfile.Profile, bars: int) -> dict[str, float]:
    """Per-candle call frequencies for Decimal/json/hash/datetime primitives."""
    patterns = {
        "json_dumps": ("json", "dumps"),
        "encoder_encode": ("json", "encode"),
        "sha256": ("", "openssl_sha256"),
        "isoformat": ("", "'isoformat' of 'datetime"),
        "decimal_new": ("", "'__new__' of 'decimal.Decimal'"),
        "decimal_add": ("", "'__add__' of 'decimal.Decimal'"),
        "decimal_div": ("", "'__truediv__' of 'decimal.Decimal'"),
        "decimal_mul": ("", "'__mul__' of 'decimal.Decimal'"),
        "rolling_vwap": ("vwap_shadow", "rolling_vwap"),
        "signal_ctor": ("vwap_shadow", "_signal"),
        "on_bar": ("vwap_shadow", "on_bar"),
        "state_snapshot": ("vwap_shadow", "state_snapshot"),
    }
    per_candle: dict[str, float] = {}
    table = pstats.Stats(profiler)
    for name, (file_fragment, func_fragment) in patterns.items():
        calls = 0
        self_time = 0.0
        for func, value in table.stats.items():
            filename, _, funcname = func
            if file_fragment in filename and func_fragment in funcname:
                _, nc, tt, _ct, _callers = value
                calls += nc
                self_time += tt
        per_candle[f"{name}_per_candle"] = round(calls / bars, 2)
        per_candle[f"{name}_self_ms_total"] = round(self_time * 1000, 2)
    return per_candle


# ----------------------------------------------------------------- driver


def _median_us(values_ns: list[int]) -> float:
    return round(statistics.median(values_ns) / 1000, 2) if values_ns else 0.0


def _sum_ms(values_ns: list[int]) -> float:
    return round(sum(values_ns) / 1_000_000, 2) if values_ns else 0.0


def run_phase2c1(repeats: int = 5) -> dict[str, Any]:
    config = load_run_config(_CONFIG, environ={})
    report: dict[str, Any] = {"phase": "2C1", "workloads": {}, "cprofile": {}, "overhead": {}}

    # Method A per workload: compute-only explicit timers, N repeats
    for kind in ("A", "B", "C"):
        candles = workload_candles(kind)
        profiles = [compute_only_replay(candles, config) for _ in range(repeats)]
        first = profiles[0]
        deterministic = all(
            (p.bars, p.buys, p.holds, p.warmup_bars)
            == (first.bars, first.buys, first.holds, first.warmup_bars)
            for p in profiles[1:]
        )
        elapsed = [p.elapsed_ns / 1e9 for p in profiles]
        on_bar_total = _sum_ms(first.on_bar_ns)
        report["workloads"][kind] = {
            "bars": first.bars,
            "buys": first.buys,
            "warmup_bars": first.warmup_bars,
            "deterministic_counts": deterministic,
            "compute_only_elapsed_s_stats": {
                "median": round(statistics.median(elapsed), 4),
                "min": round(min(elapsed), 4),
                "max": round(max(elapsed), 4),
            },
            "per_candle_us": {
                "total": round(first.elapsed_ns / 1000 / first.bars, 2),
                "on_bar": round(on_bar_total * 1000 / first.bars, 2),
                "signal_value_json": round(
                    _sum_ms(first.signal_value_json_ns) * 1000 / first.bars, 2
                ),
                "state_json": round(_sum_ms(first.state_json_ns) * 1000 / first.bars, 2),
            },
            "on_bar_us": {
                "buy_median": _median_us(first.buy_on_bar_ns),
                "hold_median": _median_us(first.hold_on_bar_ns),
            },
        }

    # Mixed synthetic workload comparable with Phase 2B4 numbers
    for count in (1000, 10000):
        candles = generate_synthetic_candles(
            SyntheticCandleRequest(count=count, seed=_SYNTHETIC_SEED, bar_interval="1h")
        )
        elapsed = []
        profile = None
        for _ in range(repeats):
            profile = compute_only_replay(candles, config)
            elapsed.append(profile.elapsed_ns / 1e9)
        result, metrics = full_replay_timed(candles, config)
        db_ms = metrics.db_time_ns / 1e6
        compute_median = statistics.median(elapsed)
        report["workloads"][f"mixed_{count}"] = {
            "bars": count,
            "buys": profile.buys,
            "compute_only_elapsed_s_median": round(compute_median, 4),
            "compute_us_per_candle": round(profile.elapsed_ns / 1000 / count, 2),
            "full_replay_elapsed_s": round(compute_median + db_ms / 1000 + 0, 4),
            "full_replay_measured_buys": result["entry_signals"],
            "persistence_ms_total": round(db_ms, 1),
            "persistence_ms_per_candle": round(db_ms / count, 3),
            "compute_vs_persistence_note": "full elapsed estimated as compute median + measured db time",
        }

    # Method B: cProfile on workload C (mixed signal/no-signal)
    candles_c = workload_candles("C")
    profiler, summary = profile_with_cprofile(candles_c, config)
    report["cprofile"]["workload_C_summary"] = summary
    report["cprofile"]["top50_cumulative"] = stats_table(profiler, 50)
    report["cprofile"]["primitive_counts_per_candle"] = attribute_counts(profiler, summary["bars"])

    # Overhead: unprofiled vs cProfile on a workload large enough that the
    # profiler's fixed cost is a fair fraction (mixed_1000), not the tiny C set.
    candles_m = generate_synthetic_candles(
        SyntheticCandleRequest(count=1000, seed=_SYNTHETIC_SEED, bar_interval="1h")
    )
    plain = compute_only_replay(candles_m, config)
    _profiler_m, summary_m = profile_with_cprofile(candles_m, config)
    report["overhead"] = {
        "workload": "mixed_1000",
        "unprofiled_elapsed_ns": plain.elapsed_ns,
        "cprofile_wall_ns": summary_m["elapsed_ns"],
        "profiling_overhead_percent": round(
            100 * (summary_m["elapsed_ns"] - plain.elapsed_ns) / plain.elapsed_ns, 1
        ),
        "note": "explicit per-bar timers add 2 perf_counter_ns calls per phase per bar; treat as <= a few percent",
    }
    return report, profiler


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2C1 strategy compute profiling")
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPO_ROOT / "artifacts" / "performance" / "phase_2c1_strategy_profile.json",
    )
    parser.add_argument(
        "--pstats",
        type=Path,
        default=_REPO_ROOT / "artifacts" / "performance" / "phase_2c1_strategy_profile.pstats",
    )
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    report, profiler = run_phase2c1(args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    profiler.dump_stats(args.pstats)
    compact = {
        "workloads": {
            k: {kk: vv for kk, vv in v.items() if kk != "top50_cumulative"}
            for k, v in report["workloads"].items()
        },
        "primitive_counts": report["cprofile"]["primitive_counts_per_candle"],
        "overhead": report["overhead"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, default=str))
    print(f"\nartifacts: {args.output} , {args.pstats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
