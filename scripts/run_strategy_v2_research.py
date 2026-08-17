"""Run frozen Strategy Research V2 using only local public-market history."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.reproducibility import canonical_hash
from backtest.strategy_v2_artifacts import write_strategy_v2_artifacts
from backtest.strategy_v2_candidates import load_candidate_specs
from backtest.strategy_v2_research import run_strategy_v2_study
from backtest.vwap_signal_edge_data import load_normalized_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Strategy Research V2")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--candidate-specs",
        type=Path,
        default=Path("configs/research/strategy_v2_candidate_specs.json"),
    )
    parser.add_argument("--vwap-episode-artifact", type=Path, required=True)
    parser.add_argument("--vwap-fixed-artifact", type=Path, required=True)
    parser.add_argument("--vwap-walk-forward-artifact", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/backtests"))
    args = parser.parse_args()
    data_manifest = json.loads((args.cache / "data_manifest.json").read_text(encoding="utf-8"))
    _require_data_gate(data_manifest)
    source_hashes = {
        "VWAP_EPISODE_V1": _require_artifact(
            args.vwap_episode_artifact, "VWAP_EPISODE_V1_", data_manifest["dataset_hash"]
        ),
        "VWAP_FIXED_EXIT_V1": _require_artifact(
            args.vwap_fixed_artifact, "VWAP_FIXED_EXIT_V1_", data_manifest["dataset_hash"]
        ),
        "VWAP_WALK_FORWARD_V1": _require_artifact(
            args.vwap_walk_forward_artifact, "VWAP_WALK_FORWARD_V1_", data_manifest["dataset_hash"]
        ),
    }
    episode_summary = json.loads(
        (args.vwap_episode_artifact / "summary.json").read_text(encoding="utf-8")
    )
    vwap_forward_means = {
        int(row["horizon_hours"]): float(row["mean"])
        for row in episode_summary["forward_statistics"]
        if row["scope"] == "episode"
    }
    walk_summary = json.loads(
        (args.vwap_walk_forward_artifact / "summary.json").read_text(encoding="utf-8")
    )
    if walk_summary.get("PURE_VWAP_RESEARCH_STOP_RECOMMENDED") is not True:
        raise ValueError("Strategy V2 requires a closed Pure VWAP V1 baseline")
    specs_payload = json.loads(args.candidate_specs.read_text(encoding="utf-8"))
    variants = load_candidate_specs(args.candidate_specs)
    candles = load_normalized_candles(args.cache / "normalized" / "candles.csv", bar="1h")
    study = run_strategy_v2_study(candles, variants, vwap_forward_means)
    output = (
        args.output_root / f"STRATEGY_RESEARCH_V2_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    frozen = (
        args.candidate_specs,
        Path("app/strategies/vwap_shadow.py"),
        Path("configs/btc_vwap_shadow.yaml"),
    )
    summary = write_strategy_v2_artifacts(
        output,
        study=study,
        candidate_spec_payload=specs_payload,
        data_manifest=data_manifest,
        source_artifact_hashes=source_hashes,
        frozen_file_hashes={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen
        },
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "candidate_count": len(study.results),
                "variant_count": study.variant_count,
                "survivors": list(study.surviving_candidates),
                "final_state": summary["final_state"],
            },
            ensure_ascii=False,
        )
    )


def _require_artifact(path: Path, prefix: str, dataset_hash: str) -> str:
    if (path / "INVALIDATED.json").exists():
        raise ValueError(f"invalidated artifact: {path}")
    artifact = json.loads((path / "artifact_manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    if not str(artifact.get("research_id", "")).startswith(prefix):
        raise ValueError(f"artifact type mismatch: {path}")
    if summary.get("dataset_hash") != dataset_hash:
        raise ValueError(f"artifact dataset mismatch: {path}")
    declared = artifact.get("files")
    if not isinstance(declared, dict):
        raise ValueError(f"malformed artifact manifest: {path}")
    actual = {
        str(name): hashlib.sha256((path / str(name)).read_bytes()).hexdigest() for name in declared
    }
    if actual != declared or canonical_hash(actual) != artifact.get("artifact_set_hash"):
        raise ValueError(f"artifact hash verification failed: {path}")
    return str(artifact["artifact_set_hash"])


def _require_data_gate(manifest: dict[str, Any]) -> None:
    if manifest.get("status") != "complete" or manifest.get("confirmed_candle_only") is not True:
        raise ValueError("Strategy V2 data gate failed")
    for field in ("duplicate_count", "missing_count", "invalid_ohlc_count", "out_of_order_count"):
        if int(manifest.get(field, -1)) != 0:
            raise ValueError(f"Strategy V2 data quality failure: {field}")


if __name__ == "__main__":
    main()
