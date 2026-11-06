"""Local ledger storage, isolated from the trading storage package's imports."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.portfolio.personal_ledger import Opening, Transaction, replay

_APPLICATION_ID = 0x504C4447
_VERSION = 1


class LedgerStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self, opening: Opening) -> None:
        """Create once, refusing even an existing empty file; no migrations."""
        replay(opening, [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        try:
            with self._connect(write=True, verify=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_VERSION}")
                connection.execute(
                    "CREATE TABLE ledger_opening (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "payload TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE ledger_transactions (sequence INTEGER PRIMARY KEY, "
                    "id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO ledger_opening VALUES (1, ?)", (opening.model_dump_json(),)
                )
        except Exception:
            # This call alone created the file via exclusive open.
            self.path.unlink()
            raise

    @contextmanager
    def _connect(self, *, write: bool = False, verify: bool = True) -> Iterator[sqlite3.Connection]:
        mode = "rw" if write else "ro"
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=10)
        try:
            if verify:
                app_id = connection.execute("PRAGMA application_id").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if app_id != _APPLICATION_ID or version != _VERSION:
                    raise ValueError("not a supported personal ledger database")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise sqlite3.OperationalError("personal ledger requires FULL durability")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _load(connection: sqlite3.Connection) -> tuple[Opening, list[Transaction]]:
        row = connection.execute("SELECT payload FROM ledger_opening WHERE id=1").fetchone()
        if row is None:
            raise ValueError("personal ledger opening is missing")
        opening = Opening.model_validate_json(row[0])
        entries = [
            Transaction.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT payload FROM ledger_transactions ORDER BY sequence"
            )
        ]
        return opening, entries

    def load(self) -> tuple[Opening, list[Transaction]]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            result = self._load(connection)
            replay(*result)
            return result

    def import_transactions(self, entries: list[Transaction]) -> tuple[int, int]:
        """Deduplicate, validate and commit a complete batch under one writer lock."""
        with self._connect(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            opening, existing = self._load(connection)
            known = {item.id: item for item in existing}
            pending: list[Transaction] = []
            duplicates = 0
            for item in entries:
                if item.id in known:
                    if known[item.id] != item:
                        raise ValueError(f"conflicting transaction id: {item.id}")
                    duplicates += 1
                else:
                    known[item.id] = item
                    pending.append(item)
            replay(opening, existing + pending)
            connection.executemany(
                "INSERT INTO ledger_transactions (id, payload) VALUES (?, ?)",
                [(item.id, item.model_dump_json()) for item in pending],
            )
            return len(pending), duplicates
