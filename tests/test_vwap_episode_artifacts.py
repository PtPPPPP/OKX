from __future__ import annotations

import json
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.historical_data import load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_episode_artifacts import assess_episode_study, write_episode_artifacts
from backtest.vwap_episode_research import run_episode_study


def test_episode_artifact_is_complete_and_reproducible(tmp_path: Path) -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_600.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    study = run_episode_study(candles, instrument, parameters)
    manifest = {
        "status": "complete",
        "dataset_hash": "fixture",
        "normalized_rows": len(candles),
        "duplicate_count": 0,
        "missing_count": 0,
        "invalid_ohlc_count": 0,
        "out_of_order_count": 0,
        "confirmed_candle_only": True,
    }
    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        write_episode_artifacts(
            output,
            study=study,
            config=config,
            parameters=parameters,
            data_manifest=manifest,
            strategy_file_hashes={},
        )

    required = {
        "report.md",
        "summary.json",
        "episodes.csv",
        "episode_forward_returns.csv",
        "episode_mfe_mae.csv",
        "episode_overlap.csv",
        "episode_regime_analysis.csv",
        "episode_temporal_analysis.csv",
        "data_manifest.json",
        "artifact_manifest.json",
    }
    assert required == {path.name for path in first.iterdir()}
    for name in required - {"summary.json", "report.md", "artifact_manifest.json"}:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    assert summary["research_only"] is True
    assert summary["strategy_parameters_changed"] is False
    assert summary["strategy_parity_pass"] is True
    assert summary["lookahead_bias"] is False
    assert summary["final_state"] == "EPISODE_RESEARCH_INCOMPLETE"
    assert isinstance(summary["raw_positive_mean_ci_horizons"], int)
    assert isinstance(summary["episode_positive_mean_ci_horizons"], int)
    report = (first / "report.md").read_text(encoding="utf-8")
    assert "Dedup reduces statistical edge evidence" in report
    assert "Dedup reduces 24H mean edge" not in report


def test_episode_assessment_uses_only_allowed_final_states() -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_600.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    study = run_episode_study(
        candles,
        instrument,
        VWAPShadowParameters.model_validate(config.strategy.parameters),
    )

    assert assess_episode_study(study)["final_state"] in {
        "EPISODE_RESEARCH_INCOMPLETE",
        "EPISODE_DEDUP_REDUCES_EDGE",
        "EPISODE_EDGE_STILL_WEAK",
        "EPISODE_EDGE_PROMISING",
        "READY_FOR_FIXED_EXIT_RESEARCH",
    }
