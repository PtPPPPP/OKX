from __future__ import annotations

import json
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.historical_data import load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_signal_edge import parameter_sensitivity, run_signal_edge_study
from backtest.vwap_signal_edge_artifacts import classify, write_signal_edge_artifacts


def test_artifact_set_is_complete_and_reproducible_except_timestamps(tmp_path: Path) -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_live.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    study = run_signal_edge_study(candles, instrument, parameters)
    sensitivity = parameter_sensitivity(candles, instrument)
    manifest = {
        "status": "complete",
        "dataset_hash": "fixture",
        "duplicate_count": 0,
        "missing_count": 0,
        "invalid_ohlc_count": 0,
        "out_of_order_count": 0,
        "confirmed_candle_only": True,
    }
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_signal_edge_artifacts(
        first,
        study=study,
        candles=candles,
        config=config,
        parameters=parameters,
        data_manifest=manifest,
        parameter_rows=sensitivity,
        strategy_file_hashes={},
    )
    write_signal_edge_artifacts(
        second,
        study=study,
        candles=candles,
        config=config,
        parameters=parameters,
        data_manifest=manifest,
        parameter_rows=sensitivity,
        strategy_file_hashes={},
    )

    required = {
        "report.md",
        "summary.json",
        "data_manifest.json",
        "signals_extended.csv",
        "forward_returns.csv",
        "mfe_mae.csv",
        "regime_analysis.csv",
        "monthly_analysis.csv",
        "temporal_stability.csv",
        "cost_thresholds.csv",
        "parameter_sensitivity.csv",
        "artifact_manifest.json",
    }
    assert required <= {path.name for path in first.iterdir()}
    first_charts = sorted(first.glob("*.png"))
    second_charts = sorted(second.glob("*.png"))
    assert len(first_charts) == 11
    assert [path.name for path in first_charts] == [path.name for path in second_charts]
    assert all(
        left.read_bytes() == right.read_bytes()
        for left, right in zip(first_charts, second_charts, strict=True)
    )
    assert (first / "signals_extended.csv").read_bytes() == (
        second / "signals_extended.csv"
    ).read_bytes()
    assert (first / "forward_returns.csv").read_bytes() == (
        second / "forward_returns.csv"
    ).read_bytes()
    assert (first / "cost_thresholds.csv").read_bytes() == (
        second / "cost_thresholds.csv"
    ).read_bytes()
    summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    assert summary["funding_context"]["status"] == "not_applicable_spot"


def test_classification_uses_only_allowed_vocabulary() -> None:
    allowed_exit = {
        "EXIT_RESEARCH_INSUFFICIENT_DATA",
        "BUY_SIGNAL_NO_CLEAR_EDGE",
        "BUY_SIGNAL_EDGE_COST_FRAGILE",
        "BUY_SIGNAL_EDGE_REGIME_DEPENDENT",
        "BUY_SIGNAL_EDGE_TEMPORALLY_UNSTABLE",
        "BUY_SIGNAL_EDGE_PROMISING",
        "READY_FOR_EXIT_RULE_RESEARCH",
    }
    allowed_assessment = {
        "BACKTEST_INSUFFICIENT_DATA",
        "BACKTEST_BASELINE_UNPROFITABLE",
        "BACKTEST_COST_FRAGILE",
        "BACKTEST_PARAMETER_FRAGILE",
        "BACKTEST_OOS_WEAK",
        "BACKTEST_PROMISING_NEEDS_MORE_VALIDATION",
        "BACKTEST_ROBUST_CANDIDATE",
    }
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_live.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    study = run_signal_edge_study(
        candles, instrument, VWAPShadowParameters.model_validate(config.strategy.parameters)
    )
    result = classify(study, parameter_sensitivity(candles, instrument))

    assert result["exit_readiness"] in allowed_exit
    assert result["strategy_assessment"] in allowed_assessment
