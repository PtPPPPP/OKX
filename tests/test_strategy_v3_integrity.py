from __future__ import annotations

import json
from pathlib import Path

from backtest.strategy_v3_artifacts import write_strategy_v3_artifacts
from backtest.strategy_v3_research import StrategyV3Study, V3CandidateResult


def test_research_cutoff_and_prospective_partition_are_audited(tmp_path: Path) -> None:
    score = {
        "candidate_id": "fixture",
        "hypothesis": "fixture",
        "episodes": 0,
        "control_episodes": 0,
        "sample_size_reduction": 0,
        "htf_incremental_value": None,
        "htf_incremental_delta": None,
        "volume_incremental_value": None,
        "volume_incremental_delta": None,
        "6h_excess": 0,
        "12h_excess": 0,
        "24h_excess": 0,
        "48h_excess": 0,
        "24h_excess_vs_vwap_v1": 0,
        "24h_excess_vs_relevant_v2": 0,
        "random_percentile": 0,
        "fixed_exit_best_horizon": None,
        "return_10bps": None,
        "return_20bps": None,
        "max_drawdown": None,
        "Sharpe": None,
        "profit_factor": None,
        "remove_top5_result": None,
        "remove_top10_result": None,
        "walk_forward_return": None,
        "positive_oos_windows": None,
        "cost_fragility": False,
        "temporal_fragility": True,
        "profit_concentration": False,
        "stage_reached": "forward_edge",
        "final_status": "REJECTED_NO_EDGE",
    }
    result = V3CandidateResult(
        "fixture",
        "fixture_v1",
        "REJECTED_NO_EDGE",
        "forward_edge",
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        score,
    )
    study = StrategyV3Study(
        ({"candidate_id": "fixture"},),
        1,
        (),
        (result,),
        (score,),
        None,
        "NO_STRATEGY_CANDIDATE_FOUND_V3",
        False,
    )
    spec = {
        "research_cutoff": "2026-08-12T23:59:59.999999+00:00",
        "prospective_oos_start": "2026-08-13T00:00:00+00:00",
        "episode_definition": "false-to-true",
        "entry_timing": "next open",
        "lookahead_bias": False,
        "frozen_before_oos": True,
        "candidates": [
            {
                "candidate_id": "fixture",
                "hypothesis": "fixture",
                "entry_rule": "fixture",
                "variants": [],
            }
        ],
    }
    output = tmp_path / "v3"
    write_strategy_v3_artifacts(
        output,
        study=study,
        spec_payload=spec,
        data_manifest={
            "dataset_hash": "fixture",
            "normalized_rows": 1,
            "actual_end": "2026-08-12T01:00:00+00:00",
        },
        feature_manifest={},
        source_artifact_hashes={},
        frozen_file_hashes={},
    )
    cutoff = json.loads((output / "research_cutoff.json").read_text(encoding="utf-8"))
    prospective = json.loads((output / "prospective_oos_manifest.json").read_text(encoding="utf-8"))
    assert cutoff["candidate_design_data_end_lte_research_cutoff"] is True
    assert cutoff["prospective_data_used_for_selection"] is False
    assert prospective["rows_used_for_candidate_selection"] == 0
    assert not (output / "candidate_freeze.json").exists()
    assert (output / "candidate_A" / "candidate_spec.json").exists()


def test_v3_artifact_does_not_mutate_production_config(tmp_path: Path) -> None:
    config = Path("configs/btc_vwap_shadow.yaml")
    before = config.read_bytes()
    test_research_cutoff_and_prospective_partition_are_audited(tmp_path)
    assert config.read_bytes() == before
