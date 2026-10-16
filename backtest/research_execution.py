"""Execute a predeclared V3 research cohort with the corrected spot backtest model."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config.run_config import RunConfig
from app.domain.events import EventBus
from app.domain.market import Candle
from app.domain.position import Portfolio
from app.execution.backtest_broker import BacktestBroker
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.reproducibility import InstrumentSnapshotStore, canonical_hash
from app.risk.risk_manager import default_risk_manager
from app.runtime.clock import BacktestClock
from app.strategies.base import StatefulStrategy, Strategy
from app.strategies.buy_and_hold import BuyAndHoldParameters, BuyAndHoldStrategy
from app.strategies.registry import create_strategy
from backtest.engine import BacktestEngine, BacktestResult
from backtest.report import write_backtest_report
from backtest.research_governance import (
    EXECUTION_POLICY,
    PROJECT_ROOT,
    file_hash,
    read_json,
    validated_dataset,
    validation_dataset,
    verify_report,
    write_json_once,
)
from backtest.strategy_v3_candidates import V3Variant, load_v3_specs
from scripts.run_strategy_v2_research import _require_artifact


def create_v3_strategy(variant: V3Variant, source_root: Path = PROJECT_ROOT) -> Strategy:
    instrument = InstrumentSnapshotStore.load(
        source_root / "configs/snapshots/BTC-USDT.json"
    ).instrument
    return create_strategy(variant.candidate_id, variant.parameters, instrument)


class _History:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        return self.candles


def evaluate(
    candles: list[Candle],
    strategy: Strategy,
    *,
    csv_path: Path,
    multiplier: int = 1,
    protective: bool = True,
    source_root: Path = PROJECT_ROOT,
    signal_trace: list[dict[str, Any]] | None = None,
    warmup_candles: list[Candle] | None = None,
) -> BacktestResult:
    policy = EXECUTION_POLICY
    model = getattr(strategy, "parameters", None)
    parameters = (
        strategy.parameter_values
        if isinstance(strategy, StatefulStrategy)
        else model.model_dump(mode="json")
        if isinstance(model, BaseModel)
        else {}
    )
    config = RunConfig.model_validate(
        {
            "mode": "backtest",
            "market": {"instrument_id": "BTC-USDT", "bar": "1h"},
            "strategy": {
                "name": strategy.name,
                "parameters": parameters,
                "implementation_version": getattr(strategy, "implementation_version", None),
            },
            "position_sizing": {"parameters": {"order_notional": policy["order_notional"]}},
            "backtest": {
                "initial_capital": policy["initial_capital"],
                "fee_rate": Decimal(str(policy["fee_rate"])) * multiplier,
                "slippage_rate": Decimal(str(policy["slippage_rate"])) * multiplier,
                "volume_participation": policy["volume_participation"],
            },
            "protective_exits": {
                "enabled": protective,
                "stop_loss_pct": policy["stop_loss_pct"],
                "take_profit_pct": policy["take_profit_pct"],
            },
            "risk": {
                "max_order_notional": policy["order_notional"],
                "max_total_exposure": policy["maximum_exposure"],
                "max_daily_loss": "10",
                "max_drawdown_pct": "5",
            },
            "data": {"source": "csv", "path": csv_path, "limit": len(candles)},
        }
    )
    instrument = InstrumentSnapshotStore.load(
        source_root / "configs/snapshots/BTC-USDT.json"
    ).instrument
    portfolio = Portfolio({"USDT": config.backtest.initial_capital})
    broker = BacktestBroker(
        portfolio,
        instrument,
        config.backtest.fee_rate,
        config.backtest.slippage_rate,
        volume_participation=config.backtest.volume_participation,
    )
    risk = default_risk_manager(
        maximum_order_notional=config.risk.max_order_notional,
        maximum_exposure=config.risk.max_total_exposure,
        maximum_daily_loss=config.risk.max_daily_loss,
        maximum_drawdown_pct=config.risk.max_drawdown_pct,
        maximum_orders_per_minute=config.risk.max_orders_per_minute,
        stale_after_seconds=config.risk.stale_after_seconds,
    )
    bus = EventBus()
    result = BacktestEngine(
        run_id=canonical_hash(
            [
                strategy.name,
                candles[0].timestamp.isoformat(),
                candles[-1].timestamp.isoformat(),
                multiplier,
            ]
        )[:32],
        config=config,
        instrument=instrument,
        provider=_History(candles),
        strategy=strategy,
        position_sizer=FixedNotionalPositionSizer(Decimal(str(policy["order_notional"]))),
        risk_manager=risk,
        broker=broker,
        clock=BacktestClock((warmup_candles or candles)[0].timestamp),
        event_bus=bus,
        warmup_candles=warmup_candles,
    ).run()
    if signal_trace is not None:
        signal_trace.extend(
            {
                "timestamp": event.timestamp.isoformat(),
                "strategy_name": event.strategy_name,
                "instrument_id": event.instrument_id,
                "payload": {
                    key: value for key, value in event.payload.items() if key != "signal_id"
                },
            }
            for event in bus.events
            if event.event_type == "SignalGenerated"
        )
    return result


def _metrics(result: BacktestResult) -> dict[str, Any]:
    pnls = sorted(result.closed_trade_pnls, reverse=True)
    years: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for trade in result.closed_trades:
        years[str(trade.exited_at.year)] += trade.pnl
    return {
        **result.summary,
        "pnl_without_top5": sum(pnls[5:], start=Decimal("0")),
        "pnl_without_top10": sum(pnls[10:], start=Decimal("0")),
        "yearly_closed_trade_pnl": dict(sorted(years.items())),
        "positive_year_fraction": Decimal(sum(value > 0 for value in years.values()))
        / Decimal(len(years))
        if years
        else Decimal("0"),
    }


def _execution_rejections(base: dict[str, Any], stress: dict[str, Any]) -> list[str]:
    reasons = []
    if int(base["closed_trade_count"]) < int(EXECUTION_POLICY["minimum_closed_trades"]):
        reasons.append("insufficient_closed_trades")
    if Decimal(str(base["net_pnl"])) <= 0:
        reasons.append("unprofitable_after_costs")
    if Decimal(str(stress["net_pnl"])) <= 0:
        reasons.append("cost_stress_failed")
    if Decimal(str(base["pnl_without_top5"])) <= 0 or Decimal(str(base["pnl_without_top10"])) <= 0:
        reasons.append("profit_concentration")
    if Decimal(str(base["positive_year_fraction"])) < Decimal(
        str(EXECUTION_POLICY["minimum_positive_year_fraction"])
    ):
        reasons.append("temporal_instability")
    return reasons


def historical_execution(
    cache: Path,
    output: Path,
    *,
    legacy_v3_artifact: Path,
    source_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    candles, manifest = validated_dataset(cache)
    legacy_hash = _require_artifact(
        legacy_v3_artifact, "STRATEGY_RESEARCH_V3_", manifest["dataset_hash"]
    )
    legacy = read_json(legacy_v3_artifact / "summary.json")
    rows = {str(row["candidate_id"]): row for row in legacy["scoreboard"]}
    variants = [
        v
        for v in load_v3_specs(source_root / "configs/research/strategy_v3_candidate_specs.json")
        if v.primary
    ]
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for variant in variants:
        metrics = []
        for multiplier in EXECUTION_POLICY["cost_multipliers"]:
            result = evaluate(
                candles,
                create_v3_strategy(variant, source_root),
                csv_path=cache / "normalized/candles.csv",
                multiplier=multiplier,
                source_root=source_root,
            )
            write_backtest_report(result, output / variant.candidate_id / f"cost_{multiplier}x")
            metrics.append(_metrics(result))
        reasons = _execution_rejections(metrics[0], metrics[1])
        legacy_status = rows[variant.candidate_id]["final_status"]
        if legacy_status != "RESEARCH_CANDIDATE_V3":
            reasons.insert(0, f"legacy_gate:{legacy_status}")
        results.append(
            {
                "candidate_id": variant.candidate_id,
                "variant_id": variant.variant_id,
                "base": metrics[0],
                "stress": metrics[1],
                "legacy_status": legacy_status,
                "qualified": not reasons,
                "rejection_reasons": reasons,
            }
        )
    selected = next((row["candidate_id"] for row in results if row["qualified"]), None)
    baseline = evaluate(
        candles,
        BuyAndHoldStrategy(BuyAndHoldParameters()),
        csv_path=cache / "normalized/candles.csv",
        protective=False,
        source_root=source_root,
    )
    write_backtest_report(baseline, output / "buy_hold_control")
    payload = {
        "phase": "historical_development",
        "dataset_hash": manifest["dataset_hash"],
        "legacy_v3_artifact_hash": legacy_hash,
        "legacy_reports_are_rebuilt_not_restored_originals": True,
        "execution_policy": EXECUTION_POLICY,
        "candidate_results": results,
        "selected_candidate": selected,
        "buy_hold_control": baseline.summary,
        "cash_control_net_pnl": "0",
        "live_trading": False,
        "final_state": "CANDIDATE_FROZEN_FOR_VALIDATION" if selected else "NO_QUALIFIED_CANDIDATE",
    }
    _finish_report(output, payload)
    return payload


def independent_validation(
    freeze_path: Path,
    cache: Path,
    output: Path,
    *,
    purpose: str,
    source_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    candles, manifest, freeze = validation_dataset(
        freeze_path, cache, purpose=purpose, source_root=source_root
    )
    # A single consumption fence applies to historical OOS. A prospective
    # window is also one-shot per freeze, requiring a new declared window/run.
    receipt = freeze_path.parent / f"{purpose}_consumed.json"
    if receipt.exists():
        previous = read_json(receipt)
        if previous.get("freeze_hash") != freeze["freeze_hash"]:
            raise ValueError("OOS consumption belongs to another freeze")
        if previous.get("dataset_hash") != manifest["dataset_hash"]:
            raise ValueError(
                "OOS already consumed: refusing replacement or retest on different data"
            )
        expected_output = Path(previous["output"])
        if expected_output.resolve() != output.resolve():
            raise ValueError("OOS already consumed at another output path")
        completion = read_json(freeze_path.parent / f"{purpose}_complete.json")
        if (
            completion.get("freeze_hash") != freeze["freeze_hash"]
            or completion.get("dataset_hash") != manifest["dataset_hash"]
        ):
            raise ValueError("OOS completion binding mismatch")
        return verify_report(output, expected_hash=completion["artifact_set_hash"])
    write_json_once(
        receipt,
        {
            "freeze_hash": freeze["freeze_hash"],
            "dataset_hash": manifest["dataset_hash"],
            "output": str(output.resolve()),
            "status": "CONSUMED_BEFORE_EVALUATION",
        },
    )
    history_path = Path(freeze["historical_cache"])
    history, _ = validated_dataset(
        history_path if history_path.is_absolute() else source_root / history_path
    )
    payload = execute_frozen_validation(
        freeze, candles, manifest, history, cache, output, purpose=purpose, source_root=source_root
    )
    write_json_once(
        freeze_path.parent / f"{purpose}_complete.json",
        {
            "freeze_hash": freeze["freeze_hash"],
            "dataset_hash": manifest["dataset_hash"],
            "artifact_set_hash": read_json(output / "report_manifest.json")["artifact_set_hash"],
        },
    )
    return payload


def execute_frozen_validation(
    freeze: dict[str, Any],
    candles: list[Candle],
    manifest: dict[str, Any],
    history: list[Candle],
    cache: Path,
    output: Path,
    *,
    purpose: str,
    source_root: Path,
    signal_traces: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Same computation for original validation and explicitly registered reproductions."""
    output.mkdir(parents=True, exist_ok=False)
    warmup = history[-300:]
    if (
        purpose == "prospective"
        and warmup[-1].timestamp + timedelta(hours=1) != candles[0].timestamp
    ):
        # Forward context is known only through this window's own observations.
        # Do not pretend stale August bars are continuous with an October start.
        warmup = []
    variants = {
        v.candidate_id: v
        for v in load_v3_specs(source_root / "configs/research/strategy_v3_candidate_specs.json")
        if v.primary
    }
    results = []
    for frozen in freeze["candidates"]:
        variant = variants[frozen["candidate_id"]]
        if variant.parameters != frozen["parameters"] or variant.variant_id != frozen["variant_id"]:
            raise ValueError("candidate parameters changed after freeze")
        metrics = []
        for multiplier in EXECUTION_POLICY["cost_multipliers"]:
            trace: list[dict[str, Any]] | None = [] if signal_traces is not None else None
            result = evaluate(
                candles,
                create_v3_strategy(variant, source_root),
                csv_path=cache / "normalized/candles.csv",
                multiplier=multiplier,
                source_root=source_root,
                signal_trace=trace,
                warmup_candles=warmup,
            )
            if signal_traces is not None:
                signal_traces[f"{variant.candidate_id}/cost_{multiplier}x"] = trace
            write_backtest_report(result, output / variant.candidate_id / f"cost_{multiplier}x")
            metrics.append(_metrics(result))
        reasons = _execution_rejections(metrics[0], metrics[1])
        training_qualified = frozen["training_qualified"]
        if not training_qualified:
            reasons.insert(0, "training_rejection_is_locked")
        results.append(
            {
                "candidate_id": variant.candidate_id,
                "base": metrics[0],
                "stress": metrics[1],
                "training_qualified": training_qualified,
                "oos_passed": not reasons,
                "rejection_reasons": reasons,
                "trading_permission": False,
            }
        )
    baseline_trace: list[dict[str, Any]] | None = [] if signal_traces is not None else None
    baseline = evaluate(
        candles,
        BuyAndHoldStrategy(BuyAndHoldParameters()),
        csv_path=cache / "normalized/candles.csv",
        protective=False,
        source_root=source_root,
        signal_trace=baseline_trace,
    )
    if signal_traces is not None:
        signal_traces["buy_hold_control"] = baseline_trace
    write_backtest_report(baseline, output / "buy_hold_control")
    payload = {
        "phase": purpose,
        "classification": "RETROSPECTIVE_AFTER_FREEZE_ACCESS"
        if purpose == "historical_oos"
        else "POST_FREEZE_PROSPECTIVE",
        "freeze_hash": freeze["freeze_hash"],
        "dataset_hash": manifest["dataset_hash"],
        "data_start": manifest["actual_start"],
        "data_end": manifest["actual_end"],
        "warmup_bars": len(warmup),
        "warmup_used_for_orders": False,
        "candidate_results": results,
        "selected_candidate": freeze["selected_candidate"],
        "candidate_selection_uses_oos": False,
        "rejected_candidates_cannot_be_promoted": True,
        "buy_hold_control": baseline.summary,
        "cash_control_net_pnl": "0",
        "future_validation_required": purpose == "historical_oos",
        "trading_permission": False,
        "final_state": "NO_QUALIFIED_CANDIDATE"
        if freeze["selected_candidate"] is None
        else "PROSPECTIVE_VALIDATION_REQUIRED"
        if purpose == "historical_oos"
        else "RESEARCH_REVIEW_REQUIRED",
    }
    _finish_report(output, payload)
    return payload


def _finish_report(output: Path, payload: dict[str, Any]) -> None:
    write_json_once(output / "summary.json", payload)
    lines = [
        "# Research cycle report",
        "",
        f"- Phase: {payload['phase']}",
        f"- Dataset hash: {payload['dataset_hash']}",
        f"- State: {payload['final_state']}",
        "- Trading permission: false",
        "",
        "| Candidate | Net PnL | Stress PnL | Closed trades | Rejections |",
        "|---|---:|---:|---:|---|",
    ]
    for row in payload["candidate_results"]:
        lines.append(
            f"| {row['candidate_id']} | {row['base']['net_pnl']} | {row['stress']['net_pnl']} | "
            f"{row['base']['closed_trade_count']} | {', '.join(row['rejection_reasons'])} |"
        )
    lines += [
        "",
        "Historical reports are rebuilt outputs, not recovered originals.",
        "Historical OOS is retrospective; it is not post-freeze forward evidence.",
        "A rejected training hypothesis remains rejected even if its OOS diagnostic is positive.",
        "Legacy signal studies and corrected execution reports use different models; do not mix their PnL.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    files = {
        str(path.relative_to(output)).replace("\\", "/"): file_hash(path)
        for path in output.rglob("*")
        if path.is_file()
    }
    # JSON serialization converts Decimals to strings; hash the published form.
    write_json_once(
        output / "report_manifest.json",
        {
            "summary_hash": canonical_hash(read_json(output / "summary.json")),
            "files": files,
            "artifact_set_hash": canonical_hash(files),
        },
    )
