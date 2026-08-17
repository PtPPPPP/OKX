from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path("app")
COORDINATOR_INTERNALS = {
    Path("app/services/private_state_coordinator.py"),
    Path("app/services/private_events.py"),
    Path("app/services/reconciliation.py"),
}
PRIVATE_STATE_WRITERS = {
    "apply_private_state_event",
    "begin_private_connection_epoch",
    "begin_private_reconciliation",
    "confirm_private_state_snapshots",
    "freeze_private_state",
    "record_private_ws_watermark",
}


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in APP_ROOT.rglob("*.py")]


def test_only_coordinator_production_code_imports_private_reconciliation_components() -> None:
    forbidden: list[str] = []
    for path, tree in _modules():
        if path in COORDINATOR_INTERNALS:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.services.reconciliation":
                continue
            names = {alias.name for alias in node.names}
            direct = names & {"AccountSync", "ReconciliationService"}
            if direct:
                forbidden.append(f"{path}:{','.join(sorted(direct))}")
            if "PrivateEventProcessor" in names:
                forbidden.append(f"{path}:PrivateEventProcessor")
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "app.services.private_events" and any(
                alias.name == "PrivateEventProcessor" for alias in node.names
            ):
                forbidden.append(f"{path}:PrivateEventProcessor")
    assert not forbidden, "Coordinator 外部禁止导入私有状态归并组件: " + "; ".join(forbidden)


def test_only_coordinator_path_calls_private_state_repository_writers() -> None:
    forbidden: list[str] = []
    for path, tree in _modules():
        if path in COORDINATOR_INTERNALS or path == Path("app/storage/repositories.py"):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in PRIVATE_STATE_WRITERS:
                forbidden.append(f"{path}:{node.func.attr}")
    assert not forbidden, "Coordinator 外部禁止推进私有状态: " + "; ".join(forbidden)
