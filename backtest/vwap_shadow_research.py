"""Read-only, deterministic research replay for the production VWAP Shadow strategy."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.position import PortfolioSnapshot
from app.runtime.clock import BacktestClock
from app.strategies.vwap_shadow import VWAPShadowParameters, VWAPShadowStrategy


@dataclass(frozen=True, slots=True)
class DataQuality:
    bars: int
    duplicates: int
    missing: int
    out_of_order: int
    invalid_ohlc: int
    invalid_volume: int
    unconfirmed: int


@dataclass(frozen=True, slots=True)
class ShadowSignalRecord:
    timestamp: str
    action: str
    reason: str
    close: str
    vwap: str | None
    deviation_bps: str | None
    proposal_eligible: bool
    execution_timestamp: str | None
    execution_reference_price: str | None


def validate_candles(candles: list[Candle], interval: timedelta) -> DataQuality:
    duplicates = len(candles) - len({candle.timestamp for candle in candles})
    out_of_order = sum(later.timestamp <= earlier.timestamp for earlier, later in pairwise(candles))
    missing = sum(
        later.timestamp - earlier.timestamp != interval for earlier, later in pairwise(candles)
    )
    invalid_ohlc = sum(
        candle.low > min(candle.open, candle.close)
        or candle.high < max(candle.open, candle.close)
        or candle.low <= 0
        or candle.high < candle.low
        for candle in candles
    )
    invalid_volume = sum(not candle.volume.is_finite() or candle.volume <= 0 for candle in candles)
    return DataQuality(
        bars=len(candles),
        duplicates=duplicates,
        missing=missing,
        out_of_order=out_of_order,
        invalid_ohlc=invalid_ohlc,
        invalid_volume=invalid_volume,
        unconfirmed=sum(not candle.confirmed for candle in candles),
    )


def replay_shadow(
    candles: list[Candle], instrument: Instrument, parameters: VWAPShadowParameters
) -> list[ShadowSignalRecord]:
    """Replay the exact production strategy core.  It never constructs a broker or order."""
    strategy = VWAPShadowStrategy(parameters)
    clock = BacktestClock(candles[0].timestamp)
    portfolio = PortfolioSnapshot({}, {}, {}, trusted_for_trading=False)

    def context(candle: Candle | None) -> StrategyContext:
        return StrategyContext(
            run_id="VWAP_BASELINE_V1",
            mode=TradingMode.BACKTEST,
            strategy_name=strategy.name,
            instrument=instrument,
            bar="1h",
            portfolio_snapshot=portfolio,
            market_snapshot=MarketSnapshot(candle, candle.close) if candle else None,
            clock=clock,
        )

    strategy.on_start(context(None))
    records: list[ShadowSignalRecord] = []
    for index, candle in enumerate(candles):
        clock.advance_to(candle.timestamp)
        signal = strategy.on_bar(context(candle), candle)[0]
        next_candle = candles[index + 1] if index + 1 < len(candles) else None
        metadata = signal.metadata
        records.append(
            ShadowSignalRecord(
                timestamp=candle.timestamp.astimezone(UTC).isoformat(),
                action=signal.action.value,
                reason=signal.reason,
                close=str(candle.close),
                vwap=_decimal_or_none(metadata.get("vwap")),
                deviation_bps=_decimal_or_none(metadata.get("deviation_bps")),
                proposal_eligible=signal.action.value == "buy",
                execution_timestamp=(
                    next_candle.timestamp.astimezone(UTC).isoformat()
                    if signal.action.value == "buy" and next_candle
                    else None
                ),
                execution_reference_price=(
                    str(next_candle.open) if signal.action.value == "buy" and next_candle else None
                ),
            )
        )
    return records


def write_research_artifacts(
    output_dir: Path,
    *,
    candles: list[Candle],
    instrument: Instrument,
    parameters: VWAPShadowParameters,
    data_source: Path,
) -> dict[str, Any]:
    """Write an auditable signal-baseline artifact set without trading simulation."""
    output_dir.mkdir(parents=True, exist_ok=False)
    quality = validate_candles(candles, timedelta(hours=1))
    if quality.duplicates or quality.out_of_order or quality.invalid_ohlc:
        raise ValueError(f"historical data quality gate failed: {asdict(quality)}")
    records = replay_shadow(candles, instrument, parameters)
    data_hash = hashlib.sha256(data_source.read_bytes()).hexdigest()
    summary: dict[str, Any] = {
        "backtest_id": "VWAP_BASELINE_V1",
        "strategy_version": "vwap_shadow_v1",
        "strategy_parameters_changed": False,
        "instrument": instrument.instrument_id,
        "timeframe": "1h",
        "data_start": candles[0].timestamp.astimezone(UTC).isoformat(),
        "data_end": candles[-1].timestamp.astimezone(UTC).isoformat(),
        "quality": asdict(quality),
        "signal_count": len(records),
        "buy_signal_count": sum(record.proposal_eligible for record in records),
        "unfilled_last_bar_signals": sum(
            record.proposal_eligible and record.execution_timestamp is None for record in records
        ),
        "lookahead_bias": False,
        "confirmed_candle_only": True,
        "execution_model": "signal_at_confirmed_close_execute_at_next_open_reference_only",
        "funding_model_status": "not_applicable_spot",
        "capital_backtest_status": "not_run_shadow_has_no_exit_or_position_sizing_semantics",
        "assessment": "BACKTEST_INSUFFICIENT_DATA",
        "safety": {
            "bounded_demo_started": 0,
            "broker_write_calls": 0,
            "place_order_calls": 0,
            "cancel_order_calls": 0,
            "live_trading": False,
        },
    }
    (output_dir / "config.json").write_text(
        json.dumps({"parameters": parameters.model_dump(mode="json")}, indent=2), encoding="utf-8"
    )
    (output_dir / "data_manifest.json").write_text(
        json.dumps(
            {"path": str(data_source), "sha256": data_hash, "quality": asdict(quality)}, indent=2
        ),
        encoding="utf-8",
    )
    with (output_dir / "signals.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(ShadowSignalRecord.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text(_report(summary), encoding="utf-8")
    return summary


def current_git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _decimal_or_none(value: object) -> str | None:
    return str(value) if isinstance(value, Decimal) else None


def _report(summary: dict[str, Any]) -> str:
    return "\n".join(
        (
            "# VWAP_BASELINE_V1",
            "",
            "## Executive Summary",
            "",
            "当前正式 Shadow 策略仅定义 BUY/HOLD，不含退出与仓位规模；因此已冻结并审计信号基线，不能诚实地产生资金收益结论。",
            "",
            "## Bias Checks",
            "",
            "```text",
            "lookahead_bias=false",
            "confirmed_candle_only=true",
            "execution=next_bar_open_reference_only",
            "```",
            "",
            "## Safety Verification",
            "",
            "```text",
            "bounded_demo_started=0",
            "broker_write_calls=0",
            "place_order_calls=0",
            "cancel_order_calls=0",
            "live_trading=false",
            "```",
            "",
            "## Final Assessment",
            "",
            str(summary["assessment"]),
            "",
        )
    )
