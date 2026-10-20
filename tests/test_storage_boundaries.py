from __future__ import annotations

import ast
from pathlib import Path


def test_storage_implementations_do_not_import_facade_or_each_other() -> None:
    modules = {"orders", "accounts", "proposals", "private_state", "audit"}
    for name in modules:
        path = Path("app/storage") / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {
                    "app.storage.repositories",
                    *(f"app.storage.{module}" for module in modules),
                }, f"{path}:{node.lineno}: storage owners must not nest committing stores"


def test_facade_does_not_open_connections_or_execute_sql() -> None:
    tree = ast.parse(Path("app/storage/repositories.py").read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert not {"connect", "execute", "executemany", "commit", "rollback"}.intersection(calls)


def test_private_state_store_is_composed_only_by_compatibility_facade() -> None:
    # Extraction must not become a new way for business services to bypass the
    # existing coordinator-only writer rules (checked in the single-writer suite).
    callers: set[Path] = set()
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom) and node.module == "app.storage.private_state"
            for node in ast.walk(tree)
        ):
            callers.add(path)
    assert callers == {Path("app/storage/repositories.py")}


def test_proposal_cli_commands_only_dispatch_and_present() -> None:
    tree = ast.parse(Path("app/cli.py").read_text(encoding="utf-8"))
    commands = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"submit_demo_order", "revalidate_demo_order_proposal"}
    ]
    assert len(commands) == 2
    forbidden = {"place_order", "transition", "reconcile_result", "close", "execute"}
    for command in commands:
        for node in ast.walk(command):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden
