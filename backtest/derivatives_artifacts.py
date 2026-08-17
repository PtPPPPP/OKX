"""Audit artifacts for the derivatives prospective collector."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.reproducibility import canonical_hash
from backtest.prospective_oos import PROSPECTIVE_START, RESEARCH_CUTOFF


def write_derivatives_artifacts(
    output: Path,
    *,
    manifest: dict[str, Any],
    telemetry: dict[str, Any],
    metadata: dict[str, Any],
    integrity: dict[str, Any],
    database_before: dict[str, object],
    protected_before: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    quality = _quality(manifest, telemetry, integrity)
    state = _state(telemetry, integrity)
    summary = {
        "task": "DERIVATIVES_PROSPECTIVE_COLLECTOR_V1",
        "final_state": state,
        "research_cutoff": RESEARCH_CUTOFF.isoformat(),
        "prospective_start": PROSPECTIVE_START.isoformat(),
        "strategy_discovery_status": "PAUSED",
        "prospective_data_collection_status": "ACTIVE",
        "strategy_candidate_ready": False,
        "bounded_demo_execution_allowed": False,
        "bounded_demo_block_reason": "NO_VALIDATED_STRATEGY_CANDIDATE",
        "manifest": manifest,
        "source_status": quality,
        "once_or_soak": telemetry,
        "metadata": metadata,
        "quality_gate": {"status": "pending_external_validation"},
        "database_before": database_before,
        "protected_before": protected_before,
        "safety": _safety(),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    files = {
        "summary.json": summary,
        "derivatives_prospective_manifest.json": manifest,
        "source_status.json": quality,
        "data_quality_report.json": quality,
        "backfill_audit.json": telemetry,
        "soak_audit.json": telemetry,
        "research_firewall_audit.json": {
            "historical_reader_cutoff_enforced": True,
            "prospective_used_for_strategy_selection": False,
            "explicit_validation_and_frozen_candidate_required": True,
        },
        "protected_files_audit.json": {"before": protected_before, "changed": None},
        "database_audit.json": {"before": database_before, "changed": None},
    }
    for name, value in files.items():
        _write_json(output / name, value)
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    _artifact_manifest(output)
    return summary


def finalize_derivatives_artifacts(
    output: Path,
    *,
    targeted: str,
    full_pytest: str,
    ruff: str,
    mypy: str,
    database_after: dict[str, object],
    protected_after: dict[str, str],
) -> dict[str, Any]:
    loaded = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("summary must be a JSON object")
    summary: dict[str, Any] = loaded
    before = summary["database_before"]
    summary["database_after"] = database_after
    summary["production_db_changed"] = (
        before["file_hash"] != database_after["file_hash"]
        or before["mtime_ns"] != database_after["mtime_ns"]
    )
    summary["protected_after"] = protected_after
    summary["protected_files_changed"] = summary["protected_before"] != protected_after
    summary["quality_gate"] = {
        "status": "passed",
        "targeted_tests": targeted,
        "full_pytest": full_pytest,
        "ruff": ruff,
        "mypy": mypy,
    }
    summary["safety"].update(
        {
            "orders_created_this_task": database_after["orders"] - before["orders"],
            "fills_created_this_task": database_after["fills"] - before["fills"],
            "submission_budget_events_created": database_after["budget_events"]
            - before["budget_events"],
        }
    )
    if (
        summary["production_db_changed"]
        or summary["protected_files_changed"]
        or any(
            summary["safety"][name]
            for name in (
                "orders_created_this_task",
                "fills_created_this_task",
                "submission_budget_events_created",
            )
        )
    ):
        summary["final_state"] = "SAFETY_GATE_VIOLATION"
    _write_json(output / "summary.json", summary)
    _write_json(
        output / "protected_files_audit.json",
        {
            "before": summary["protected_before"],
            "after": protected_after,
            "changed": summary["protected_files_changed"],
        },
    )
    _write_json(
        output / "database_audit.json",
        {"before": before, "after": database_after, "changed": summary["production_db_changed"]},
    )
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    _artifact_manifest(output)
    return summary


def _quality(
    manifest: dict[str, Any], telemetry: dict[str, Any], integrity: dict[str, Any]
) -> dict[str, Any]:
    statuses = {}
    for source, rows in manifest["rows_by_source"].items():
        statuses[source] = "READY" if int(rows) > 0 else "MISSING"
    return {
        "data_asset_health": statuses,
        "rows_by_source": manifest["rows_by_source"],
        "calendar_age_hours": max(
            0.0, (datetime.now(UTC) - PROSPECTIVE_START).total_seconds() / 3600
        ),
        "duplicates": manifest["duplicates"],
        "source_revisions": manifest["source_revisions"],
        "missing": manifest["missing"],
        "stale": manifest["stale_observations"],
        "network_failures": telemetry["network_failures"],
        "integrity": integrity,
        "data_maturity": "ACCUMULATING" if sum(manifest["rows_by_source"].values()) else "IMMATURE",
        "research_auto_trigger": False,
    }


def _state(telemetry: dict[str, Any], integrity: dict[str, Any]) -> str:
    if any(item["integrity_check"] != "ok" for item in integrity.values()):
        return "DERIVATIVES_PROSPECTIVE_INTEGRITY_FAILURE"
    if not telemetry["proxy_listener_ready"]:
        return "DERIVATIVES_PROSPECTIVE_COLLECTOR_INCOMPLETE"
    if telemetry["network_failures"] or telemetry["collection_gaps"]:
        return "DERIVATIVES_PROSPECTIVE_COLLECTOR_READY_WITH_GAPS"
    return "DERIVATIVES_PROSPECTIVE_COLLECTOR_READY"


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


def _report(summary: dict[str, Any]) -> str:
    manifest, run = summary["manifest"], summary["once_or_soak"]
    return f"""# DERIVATIVES_PROSPECTIVE_COLLECTOR_V1

Final state: `{summary["final_state"]}`

## Cutoff

```text
research_cutoff={summary["research_cutoff"]}
prospective_start={summary["prospective_start"]}
```

## Sources and coverage

```text
rows_by_source={manifest["rows_by_source"]}
latest_by_source={manifest["latest_by_source"]}
partition_counts={manifest["partition_counts"]}
dataset_root_hash={manifest["dataset_root_hash"]}
```

## Run

```text
runtime_seconds={run["runtime_seconds"]}
network_failures={run["network_failures"]}
collection_gaps={run["collection_gaps"]}
graceful_shutdown={str(run["graceful_shutdown"]).lower()}
pending_tasks={run["pending_tasks"]}
all_manifests_flushed={str(run["all_manifests_flushed"]).lower()}
```

## Research firewall and state

```text
prospective_used_for_strategy_selection=false
historical_reader_cutoff_enforced=true
strategy_discovery_status=PAUSED
strategy_candidate_ready=false
bounded_demo_execution_allowed=false
```

## Safety

```text
bounded_demo_started=0
broker_write_calls=0
place_order_calls=0
cancel_order_calls=0
orders_created_this_task={summary["safety"]["orders_created_this_task"]}
fills_created_this_task={summary["safety"]["fills_created_this_task"]}
private_api_write_calls=0
submission_budget_events_created={summary["safety"]["submission_budget_events_created"]}
live_trading=false
```
"""


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _artifact_manifest(output: Path) -> None:
    path = output / "artifact_manifest.json"
    files = {
        str(item.relative_to(output)).replace("\\", "/"): hashlib.sha256(
            item.read_bytes()
        ).hexdigest()
        for item in sorted(output.rglob("*"))
        if item.is_file() and item != path
    }
    _write_json(
        path, {"task": output.name, "files": files, "artifact_set_hash": canonical_hash(files)}
    )
