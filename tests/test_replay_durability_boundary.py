from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.storage.database import Database
from app.storage.migrations import MigrationManager


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'durability.db'}")
    database.initialize()
    return database


def _effective(connection: sqlite3.Connection) -> tuple[str, int, int]:
    return (
        str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        int(connection.execute("PRAGMA synchronous").fetchone()[0]),
        int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
    )


class _Config:
    strategy_name = "vwap_shadow"
    instrument_id = "BTC-USDT"
    timeframe = "1h"


class _Candle:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    close = Decimal("1")
    confirmed = True


def test_default_and_replay_connections_have_distinct_durability(tmp_path: Path) -> None:
    database = _database(tmp_path)

    with database.connect() as default_connection:
        assert _effective(default_connection) == ("wal", 2, 1)

    repository = ContinuousShadowRepository(database)
    with repository.replay_session() as session:
        assert _effective(session._require_open()) == ("wal", 1, 1)


def test_continuous_shadow_public_commit_keeps_full_durability(tmp_path: Path) -> None:
    database = _database(tmp_path)
    observed: list[tuple[str, int, int]] = []

    class _InspectingRepository(ContinuousShadowRepository):
        def _commit_vwap_shadow_candle_tx(
            self, connection: sqlite3.Connection, **kwargs: Any
        ) -> bool:
            observed.append(_effective(connection))
            return True

    committed = _InspectingRepository(database).commit_vwap_shadow_candle(
        run_id="scope-test",
        config=_Config(),
        candle=_Candle(),
        strategy_version="test",
        signal_id="signal",
        signal_type="hold",
        signal_value="{}",
        runtime_state="{}",
        warmup_count=0,
        warmup_completed=False,
        proposal_price=None,
        processed_count=1,
        signal_count=0,
        proposal_count=0,
    )

    assert committed is True
    assert observed == [("wal", 2, 1)]


def test_migration_connection_keeps_full_durability(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "migration.db")
    with manager._connect() as connection:
        assert int(connection.execute("PRAGMA synchronous").fetchone()[0]) == 2


def test_replay_connection_factory_has_no_other_production_callers() -> None:
    callers: set[Path] = set()
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "open_reconstructible_replay_connection"
            for node in ast.walk(tree)
        ):
            callers.add(path)

    assert callers == {Path("app/services/continuous_shadow_repository.py")}


def test_replay_session_has_only_offline_production_caller() -> None:
    callers: set[Path] = set()
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replay_session"
            for node in ast.walk(tree)
        ):
            callers.add(path)

    assert callers == {Path("app/services/shadow_replay.py")}


def test_synchronous_pragma_is_confined_to_named_connection_owners() -> None:
    files = {
        path
        for path in Path("app").rglob("*.py")
        if "PRAGMA synchronous" in path.read_text(encoding="utf-8")
    }
    assert files == {
        Path("app/storage/database.py"),
        Path("app/services/vwap_shadow_soak.py"),
    }
