from __future__ import annotations

import json
from pathlib import Path

from backtest.prospective_artifacts import write_collector_artifacts


def test_collector_artifact_contains_required_audit_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "fixture.txt").write_text("fixture", encoding="utf-8")
    manifest = {
        "total_rows": 0,
        "latest_confirmed_timestamp": None,
        "sealed_partition_count": 0,
        "open_partition_count": 0,
        "dataset_root_hash": "fixture",
    }
    integrity = {
        "integrity_check": "ok",
        "duplicates": 0,
        "missing": 0,
        "invalid": 0,
        "source_revisions": 0,
        "dataset_root_hash": "fixture",
        "partitions": [],
    }
    telemetry = {
        "runtime_seconds": 0,
        "new_confirmed_candles": 0,
        "network_failures": 0,
        "graceful_shutdown": True,
        "pending_tasks": 0,
    }
    database = {"file_hash": "fixture", "mtime_ns": 1, "orders": 0, "fills": 0, "budget_events": 0}
    output = tmp_path / "artifact"
    write_collector_artifacts(
        output,
        manifest=manifest,
        telemetry=telemetry,
        integrity=integrity,
        data_root=data,
        production_db_before=database,
        protected_hashes={},
    )
    required = {
        "report.md",
        "summary.json",
        "prospective_oos_manifest.json",
        "collector_audit.json",
        "integrity_report.json",
        "file_hashes.json",
        "artifact_manifest.json",
    }
    assert required == {path.name for path in output.iterdir()}
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["research_firewall"]["candidate_evaluation_performed"] is False
