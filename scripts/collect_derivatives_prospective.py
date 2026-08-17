"""Unified public-only derivatives prospective collector CLI."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.market.network import NetworkConfiguration
from backtest.derivatives_artifacts import write_derivatives_artifacts
from backtest.derivatives_collector import DerivativesCollectorRunner, telemetry_dict
from scripts.collect_prospective_oos import database_snapshot, protected_hashes


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--max-runtime-hours", type=float)
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--sources", default="all")
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--data-root", type=Path, default=Path("data/prospective_oos"))
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/prospective-oos"))
    args = parser.parse_args()
    if args.sources != "all":
        raise ValueError("V1 currently requires all sources to preserve coherent basis alignment")
    if args.status:
        path = args.data_root / "derivatives_prospective_manifest.json"
        print(
            path.read_text(encoding="utf-8")
            if path.exists()
            else json.dumps({"status": "NOT_COLLECTED"})
        )
        return
    runtime = 0.0 if args.once else float(args.max_runtime_hours) * 3600
    before = database_snapshot(Path("data/trading.db"))
    protected = protected_hashes()
    runner = DerivativesCollectorRunner(
        args.data_root, NetworkConfiguration.from_environment(), poll_seconds=args.poll_seconds
    )
    telemetry, manifest, metadata = runner.run(max_runtime_seconds=runtime)
    integrity = {name: store.integrity_report() for name, store in runner.stores.items()}
    output = (
        args.artifact_root
        / f"DERIVATIVES_PROSPECTIVE_COLLECTOR_V1_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    summary = write_derivatives_artifacts(
        output,
        manifest=manifest,
        telemetry=telemetry_dict(telemetry),
        metadata=metadata,
        integrity=integrity,
        database_before=before,
        protected_before=protected,
    )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "final_state": summary["final_state"],
                "rows_by_source": manifest["rows_by_source"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
