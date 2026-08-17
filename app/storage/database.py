from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path

from app.storage.migrations import MigrationError, MigrationManager


class StorageError(RuntimeError):
    pass


class _DatabaseDurability(IntEnum):
    """Closed set of owner-selected SQLite durability contracts."""

    FULL = 2
    RECONSTRUCTIBLE_REPLAY = 1


class Database:
    def __init__(self, database_url: str) -> None:
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            raise ValueError("仅支持 sqlite:/// 数据库地址")
        self.path = Path(database_url.removeprefix(prefix)).resolve()
        self.safe_stopped = False

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existed = self.path.exists() and self.path.stat().st_size > 0
            manager = MigrationManager(self.path)
            if not existed:
                manager.migrate(backup=False)
            else:
                manager.require_current()
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
        except (sqlite3.Error, MigrationError) as exc:
            self.safe_stopped = True
            raise StorageError(f"数据库初始化失败: {exc}") from exc

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection(_DatabaseDurability.FULL)
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            self.safe_stopped = True
            raise StorageError(f"数据库写入或读取失败: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def open_reconstructible_replay_connection(self) -> sqlite3.Connection:
        """Open the replay-only relaxed connection owned by ShadowReplaySession.

        Successful commits may need deterministic replay after system-level
        durability loss. This factory must never be used for funds-affecting,
        migration, reconciliation, authorization or order state.
        """
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection(_DatabaseDurability.RECONSTRUCTIBLE_REPLAY)
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if journal_mode != "wal":
                raise sqlite3.OperationalError(
                    f"reconstructible replay requires WAL, effective={journal_mode}"
                )
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            self.safe_stopped = True
            raise StorageError(f"replay 数据库连接初始化失败: {exc}") from exc

    def _open_connection(self, durability: _DatabaseDurability) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA synchronous={int(durability)}")
            effective = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            if effective != int(durability):
                raise sqlite3.OperationalError(
                    f"SQLite synchronous mismatch: expected={int(durability)}, effective={effective}"
                )
            return connection
        except Exception:
            connection.close()
            raise
