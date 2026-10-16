"""Worker loaded only from a verified snapshot in its isolated locked environment."""

from __future__ import annotations

import csv
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backtest.research_execution import execute_frozen_validation
from backtest.research_governance import (
    EXECUTION_POLICY,
    check_freeze_hash,
    read_json,
    validate_oos_data,
    validated_dataset,
    verify_freeze_artifacts,
    verify_report,
)
from scripts.research_snapshot import (
    DATA_FILES,
    dataset_path,
    digest,
    identity,
    inside,
    publish,
    verify_snapshot,
)


def economic_fingerprint(report: Path) -> str:
    """Compare economics and signal counts; random broker order IDs are not economic evidence."""
    content: dict[str, Any] = {}
    for path in sorted(report.rglob("*")):
        if path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as handle:
                content[path.relative_to(report).as_posix()] = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"run_id", "client_order_id"}
                        and not (key == "signal_id" and row.get("strategy_name") == "buy_and_hold")
                    }
                    for row in csv.DictReader(handle)
                ]
        elif path.name in {"summary.json", "backtest_summary.json"}:
            content[path.relative_to(report).as_posix()] = read_json(path)
    return identity(content)


def run_snapshot(root_name: str, arguments: dict[str, Any]) -> None:
    root = Path(root_name)
    external = Path(arguments["data_root"]) if arguments["data_root"] else None
    snapshot = verify_snapshot(root, data_root=external)
    code = root / "code"
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] in {"app", "backtest", "scripts"}:
            path = getattr(module, "__file__", None)
            if path and not Path(path).resolve().is_relative_to(code.resolve()):
                raise ValueError(f"research module imported outside snapshot: {name}")
    freeze = read_json(root / "evidence/cohort.json")
    check_freeze_hash(freeze)
    if (
        freeze["execution_policy"] != EXECUTION_POLICY
        or freeze["freeze_hash"] != snapshot["original_freeze_hash"]
    ):
        raise ValueError("frozen policy/cohort binding mismatch")
    history = dataset_path(root, snapshot["datasets"]["historical"], external)
    verify_freeze_artifacts(freeze, history, root / "evidence/training/summary.json")
    action = arguments["action"]
    if action == "attach-validation":
        attach_validation(root, snapshot, freeze, arguments)
        return
    original = root / "evidence/original_report"
    if action == "read-report":
        if snapshot["consumption"] != "COMPLETE":
            raise ValueError("no original published report in this snapshot")
        summary = verify_report(
            original, expected_hash=snapshot["original_report_artifact_set_hash"]
        )
        print(json.dumps(summary))
        return
    purpose = snapshot["purpose"]
    if purpose not in snapshot["datasets"]:
        raise ValueError("validation data not captured; no downloading or replacement allowed")
    cache = dataset_path(root, snapshot["datasets"][purpose], external)
    candles, manifest = validate_oos_data(freeze, cache, purpose=purpose)
    history_bars, _ = validated_dataset(history)
    output = root / "runs" / arguments["run_id"]
    if output.exists() or (root / "registrations" / f"{arguments['run_id']}.json").exists():
        raise ValueError("run ID already registered; use a new ID for an explicit reproduction")
    state = root / "state" / f"{purpose}_consumed.json"
    completion = root / "state" / f"{purpose}_complete.json"
    original_hash = snapshot["original_report_artifact_set_hash"]
    reference = original
    if action == "validate-once":
        if snapshot["consumption"] != "NOT_CONSUMED_AT_CAPTURE" or state.exists():
            raise ValueError("OOS already consumed; cannot validate again or replace output/data")
        publish(
            state,
            {
                "freeze_hash": freeze["freeze_hash"],
                "dataset_hash": manifest["dataset_hash"],
                "output": output.relative_to(root).as_posix(),
                "status": "CONSUMED_BEFORE_EVALUATION",
            },
        )
    elif action == "reproduce":
        if snapshot["consumption"] != "COMPLETE":
            if not completion.exists():
                raise ValueError("no completed validation available for reproduction")
            completed = read_json(completion)
            reference = root / completed["output"]
            original_hash = completed["artifact_set_hash"]
        verify_report(reference, expected_hash=original_hash)
    else:
        raise ValueError("unsupported snapshot execution action")
    registration = {
        "snapshot_hash": snapshot["snapshot_hash"],
        "freeze_hash": freeze["freeze_hash"],
        "dataset_hash": manifest["dataset_hash"],
        "purpose": purpose,
        "action": action,
        "run_id": arguments["run_id"],
        "reason": arguments["reason"],
        "registered_at": datetime.now(UTC).isoformat(),
        "output": output.relative_to(root).as_posix(),
        "capture_classification": snapshot["capture_classification"],
        "original_report_artifact_set_hash": original_hash,
        "trading_permission": False,
    }
    publish(root / "registrations" / f"{arguments['run_id']}.json", registration)
    signal_traces: dict[str, Any] = {}
    execute_frozen_validation(
        freeze,
        candles,
        manifest,
        history_bars,
        cache,
        output,
        purpose=purpose,
        source_root=code,
        signal_traces=signal_traces,
    )
    publish(root / "registrations" / f"{arguments['run_id']}_signals.json", signal_traces)
    actual_hash = read_json(output / "report_manifest.json")["artifact_set_hash"]
    result = {
        **registration,
        "artifact_set_hash": actual_hash,
        "economic_fingerprint": economic_fingerprint(output),
        "signal_fingerprint": identity(signal_traces),
        "completed_at": datetime.now(UTC).isoformat(),
        "source_root": "code",
        "imported_execution_module": "code/backtest/research_execution.py",
    }
    if action == "reproduce":
        result["matches_original_economics"] = result[
            "economic_fingerprint"
        ] == economic_fingerprint(reference)
        if not result["matches_original_economics"]:
            publish(root / "registrations" / f"{arguments['run_id']}_failed.json", result)
            raise ValueError(
                "reproduction economics differ; result retained for audit, no success receipt"
            )
    publish(root / "registrations" / f"{arguments['run_id']}_complete.json", result)
    if action == "validate-once":
        publish(completion, result)


def attach_validation(
    root: Path, snapshot: dict[str, Any], freeze: dict[str, Any], arguments: dict[str, Any]
) -> None:
    """Extend a pre-data freeze without changing code, candidates, windows or existing evidence."""
    purpose = snapshot["purpose"]
    if (
        purpose in snapshot["datasets"]
        or snapshot["consumption"] != "NOT_CONSUMED_AT_CAPTURE"
        or (root / "state" / f"{purpose}_consumed.json").exists()
    ):
        raise ValueError("validation data already bound or OOS consumed; replacement forbidden")
    cache = Path(arguments["cache"])
    _, manifest = validate_oos_data(freeze, cache, purpose=purpose)
    output = Path(arguments["output"])
    if output.exists() or output.resolve().is_relative_to(root.resolve()):
        raise ValueError("attachment requires a new directory outside the parent snapshot")
    files = {name: digest(inside(cache, name)) for name in DATA_FILES}
    output.mkdir(parents=True)
    for name in snapshot["files"]:
        target = inside(output, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inside(root, name), target)
    shutil.copyfile(root / "snapshot.json", output / "evidence/parent_snapshot.json")
    updated = {**snapshot, "datasets": dict(snapshot["datasets"])}
    updated["datasets"][purpose] = {
        "location": f"datasets/{purpose}",
        "mode": "external" if arguments["external_data"] else "bundled",
        "files": files,
        "dataset_hash": manifest["dataset_hash"],
        "start": manifest["actual_start"],
        "end": manifest["actual_end"],
    }
    if not arguments["external_data"]:
        for name in DATA_FILES:
            target = inside(output / f"datasets/{purpose}", name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inside(cache, name), target)
    updated["parent_snapshot_hash"] = snapshot["snapshot_hash"]
    updated["created_at"] = datetime.now(UTC).isoformat()
    publish(
        output / "evidence/data_attachment.json",
        {
            "parent_snapshot_hash": snapshot["snapshot_hash"],
            "purpose": purpose,
            "dataset_hash": manifest["dataset_hash"],
            "reason": arguments["reason"],
            "attached_at": updated["created_at"],
            "freeze_hash": freeze["freeze_hash"],
        },
    )
    updated["files"] = {
        path.relative_to(output).as_posix(): digest(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    updated.pop("snapshot_hash")
    updated["snapshot_hash"] = identity(updated)
    publish(output / "snapshot.json", updated)
    verify_snapshot(output, check_data=False)
    print(json.dumps({"attached": True, "snapshot_hash": updated["snapshot_hash"]}))
