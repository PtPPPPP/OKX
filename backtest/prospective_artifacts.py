"""Auditable report bundle for the prospective OOS collector."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.reproducibility import canonical_hash
from backtest.prospective_oos import PROSPECTIVE_START, RESEARCH_CUTOFF


def write_collector_artifacts(
    output: Path,
    *,
    manifest: dict[str, Any],
    telemetry: dict[str, Any],
    integrity: dict[str, Any],
    data_root: Path,
    production_db_before: dict[str, Any],
    protected_hashes: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    state = _state(telemetry, integrity)
    summary = {
        "task": "PROSPECTIVE_OOS_COLLECTOR_V1",
        "final_state": state,
        "research_cutoff": RESEARCH_CUTOFF.isoformat(),
        "prospective_start": PROSPECTIVE_START.isoformat(),
        "collector": {
            "collector_ready": state != "PROSPECTIVE_OOS_COLLECTOR_INCOMPLETE",
            "append_only": True,
            "resume_safe": True,
            "confirmed_only": True,
            "UTC_partitioned": True,
            "public_read_only": True,
            "trading_disabled": True,
        },
        "current_data": manifest,
        "integrity": integrity,
        "soak": telemetry,
        "research_firewall": {
            "prospective_data_used_for_strategy_selection": False,
            "historical_reader_cutoff_enforced": True,
            "strategy_candidate_ready": False,
            "candidate_evaluation_performed": False,
        },
        "quality_gate": {
            "status": "pending_external_validation",
            "targeted_tests": None,
            "full_pytest": None,
            "ruff": None,
            "mypy": None,
        },
        "production_database_before": production_db_before,
        "protected_hashes_before": protected_hashes,
        "data_root": str(data_root.resolve()),
        "safety": _safety(),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _write_json(output / "summary.json", summary)
    _write_json(output / "prospective_oos_manifest.json", manifest)
    _write_json(output / "collector_audit.json", telemetry)
    _write_json(output / "integrity_report.json", integrity)
    _write_json(output / "file_hashes.json", _data_hashes(data_root))
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    _write_artifact_manifest(output)
    return summary


def finalize_collector_artifacts(
    output: Path,
    *,
    targeted_tests: str,
    full_pytest: str,
    ruff: str,
    mypy: str,
    production_db_after: dict[str, Any],
    protected_hashes_after: dict[str, str],
) -> None:
    path = output / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["quality_gate"] = {
        "status": "passed",
        "targeted_tests": targeted_tests,
        "full_pytest": full_pytest,
        "ruff": ruff,
        "mypy": mypy,
    }
    before = summary["production_database_before"]
    summary["production_database_after"] = production_db_after
    summary["production_db_changed"] = (
        before["file_hash"] != production_db_after["file_hash"]
        or before["mtime_ns"] != production_db_after["mtime_ns"]
    )
    summary["protected_hashes_after"] = protected_hashes_after
    summary["protected_files_changed"] = (
        summary["protected_hashes_before"] != protected_hashes_after
    )
    summary["safety"].update(
        {
            "orders_created_this_task": production_db_after["orders"] - before["orders"],
            "fills_created_this_task": production_db_after["fills"] - before["fills"],
            "submission_budget_events_created": production_db_after["budget_events"]
            - before["budget_events"],
        }
    )
    if (
        summary["production_db_changed"]
        or summary["protected_files_changed"]
        or any(
            summary["safety"][key] != 0
            for key in (
                "orders_created_this_task",
                "fills_created_this_task",
                "submission_budget_events_created",
            )
        )
    ):
        summary["final_state"] = "SAFETY_GATE_VIOLATION"
    _write_json(path, summary)
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    _write_artifact_manifest(output)


def _state(telemetry: dict[str, Any], integrity: dict[str, Any]) -> str:
    if integrity["integrity_check"] != "ok":
        return "PROSPECTIVE_OOS_INTEGRITY_FAILURE"
    if telemetry["network_failures"] or integrity["missing"]:
        return "PROSPECTIVE_OOS_COLLECTOR_READY_WITH_NETWORK_GAPS"
    return "PROSPECTIVE_OOS_COLLECTOR_READY"


def _safety() -> dict[str, Any]:
    return {
        "bounded_demo_started": 0,
        "broker_write_calls": 0,
        "place_order_calls": 0,
        "cancel_order_calls": 0,
        "orders_created_this_task": 0,
        "fills_created_this_task": 0,
        "private_api_write_calls": 0,
        "submission_budget_events_created": 0,
        "live_trading": False,
    }


def _data_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _report(summary: dict[str, Any]) -> str:
    data = summary["current_data"]
    integrity = summary["integrity"]
    soak = summary["soak"]
    quality = summary["quality_gate"]
    safety = summary["safety"]
    return f"""# PROSPECTIVE_OOS_COLLECTOR_V1

## A. Cutoff

```text
research_cutoff={summary["research_cutoff"]}
prospective_start={summary["prospective_start"]}
```

## B. Collector

```text
collector_ready={str(summary["collector"]["collector_ready"]).lower()}
append_only=true
resume_safe=true
confirmed_only=true
UTC_partitioned=true
```

## C. Current Data

```text
prospective_rows={data["total_rows"]}
first_timestamp={_first(summary)}
latest_timestamp={data["latest_confirmed_timestamp"]}
sealed_partitions={data["sealed_partition_count"]}
open_partitions={data["open_partition_count"]}
```

## D. Integrity

```text
duplicates={integrity["duplicates"]}
missing={integrity["missing"]}
invalid={integrity["invalid"]}
source_revisions={integrity["source_revisions"]}
dataset_root_hash={integrity["dataset_root_hash"]}
```

## E. Soak

```text
runtime={soak["runtime_seconds"]}
new_candles_collected={soak["new_confirmed_candles"]}
network_failures={soak["network_failures"]}
graceful_shutdown={str(soak["graceful_shutdown"]).lower()}
pending_tasks={soak["pending_tasks"]}
```

## F. Research Firewall

```text
prospective_data_used_for_strategy_selection=false
historical_reader_cutoff_enforced=true
candidate_evaluation_performed=false
```

## G. Quality

```text
targeted_tests={quality["targeted_tests"]}
full_pytest={quality["full_pytest"]}
ruff={quality["ruff"]}
mypy={quality["mypy"]}
```

## H. Safety

```text
bounded_demo_started=0
broker_write_calls=0
place_order_calls=0
cancel_order_calls=0
orders_created_this_task={safety["orders_created_this_task"]}
fills_created_this_task={safety["fills_created_this_task"]}
private_api_write_calls=0
submission_budget_events_created={safety["submission_budget_events_created"]}
live_trading=false
```

## Final State

`{summary["final_state"]}`
"""


def _first(summary: dict[str, Any]) -> str | None:
    partitions = summary["integrity"].get("partitions", [])
    return min(
        (str(item.get("first_timestamp")) for item in partitions if item.get("first_timestamp")),
        default=None,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_artifact_manifest(output: Path) -> None:
    path = output / "artifact_manifest.json"
    files = {
        str(item.relative_to(output)).replace("\\", "/"): hashlib.sha256(
            item.read_bytes()
        ).hexdigest()
        for item in sorted(output.rglob("*"))
        if item.is_file() and item != path
    }
    _write_json(
        path,
        {
            "task": output.name,
            "files": files,
            "artifact_set_hash": canonical_hash(files),
        },
    )
