from __future__ import annotations

import csv
import json
from pathlib import Path

from app.config.run_config import load_run_config
from app.market.historical_data import load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_episode_research import run_episode_study
from backtest.vwap_fixed_exit_artifacts import (
    REQUIRED_FILES,
    assess_fixed_exit_study,
    write_fixed_exit_artifacts,
)
from backtest.vwap_fixed_exit_research import run_fixed_exit_study


def test_fixed_exit_artifact_contract_and_reproducibility(tmp_path: Path) -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_600.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    parameters = VWAPShadowParameters.model_validate(config.strategy.parameters)
    episodes = run_episode_study(candles, instrument, parameters).episodes
    study = run_fixed_exit_study(candles, episodes)
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
    first, second = tmp_path / "first", tmp_path / "second"
    for output in (first, second):
        write_fixed_exit_artifacts(
            output,
            study=study,
            config=config,
            parameters=parameters,
            data_manifest=manifest,
            strategy_file_hashes={},
            episode_artifact_hash="episode-fixture",
        )

    assert REQUIRED_FILES.issubset({path.name for path in first.iterdir()})
    deterministic = REQUIRED_FILES - {"summary.json", "report.md", "artifact_manifest.json"}
    for name in deterministic:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    assert summary["research_only"] is True
    assert summary["offline_research_only"] is True
    assert summary["production_strategy_changed"] is False
    assert summary["entry_contract"] == "episode_first_buy_next_bar_open"
    with (first / "trades_24h.csv").open(encoding="utf-8", newline="") as file:
        assert {int(row["round_trip_cost_bps"]) for row in csv.DictReader(file)} == {
            0,
            5,
            10,
            15,
            20,
        }
    assert summary["final_state"] in {
        "FIXED_EXIT_UNPROFITABLE",
        "FIXED_EXIT_COST_FRAGILE",
        "FIXED_EXIT_OOS_WEAK",
        "FIXED_EXIT_REGIME_DEPENDENT",
        "FIXED_EXIT_PROMISING",
        "READY_FOR_WALK_FORWARD_RESEARCH",
    }


def test_assessment_uses_only_allowed_state(tmp_path: Path) -> None:
    del tmp_path
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_600.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    params = VWAPShadowParameters.model_validate(config.strategy.parameters)
    study = run_fixed_exit_study(candles, run_episode_study(candles, instrument, params).episodes)
    final_state = assess_fixed_exit_study(study)["final_state"]
    assert isinstance(final_state, str)
    assert final_state.startswith(("FIXED_EXIT_", "READY_FOR_"))
