"""Append-only local thesis journal, isolated from trading/account storage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.portfolio.spot_thesis import (
    PRODUCTS,
    Evidence,
    Review,
    ThesisRevision,
    evaluate_thesis,
    validate_history,
)

_APP_ID = 0x54485353


class ThesisStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        try:
            with self._connect(write=True, verify=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={_APP_ID}")
                connection.execute("PRAGMA user_version=1")
                connection.execute(
                    "CREATE TABLE thesis_entries (sequence INTEGER PRIMARY KEY, "
                    "kind TEXT NOT NULL CHECK(kind IN ('revision','evidence','review')), "
                    "id TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL, "
                    "result TEXT, UNIQUE(kind,id))"
                )
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER thesis_no_{action.lower()} BEFORE {action} "
                        "ON thesis_entries BEGIN SELECT RAISE(ABORT, 'append-only thesis history'); END"
                    )
        except Exception:
            self.path.unlink()
            raise

    @contextmanager
    def _connect(self, *, write: bool = False, verify: bool = True) -> Iterator[sqlite3.Connection]:
        mode = "rw" if write else "ro"
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=10)
        try:
            if verify and (
                connection.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise ValueError("not a supported thesis database")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise sqlite3.OperationalError("thesis journal requires FULL durability")
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
    def _load(
        connection: sqlite3.Connection,
    ) -> tuple[list[ThesisRevision], list[Evidence], list[Review]]:
        revisions: list[ThesisRevision] = []
        evidence: list[Evidence] = []
        reviews: list[Review] = []
        for kind, identifier, payload in connection.execute(
            "SELECT kind,id,payload FROM thesis_entries ORDER BY sequence"
        ):
            item: ThesisRevision | Evidence | Review
            if kind == "revision":
                item = ThesisRevision.model_validate_json(payload)
                revisions.append(item)
            elif kind == "evidence":
                item = Evidence.model_validate_json(payload)
                evidence.append(item)
            elif kind == "review":
                item = Review.model_validate_json(payload)
                reviews.append(item)
            else:
                raise ValueError("unknown journal entry type")
            if item.id != identifier:
                raise ValueError("journal identifier does not match payload")
        validate_history(revisions, evidence, reviews)
        return revisions, evidence, reviews

    def load(self) -> tuple[list[ThesisRevision], list[Evidence], list[Review]]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._load(connection)

    def append_revision(self, item: ThesisRevision) -> bool:
        return self._append("revision", item)

    def append_evidence(self, item: Evidence) -> bool:
        return self._append("evidence", item)

    def append_review(self, item: Review) -> bool:
        return self._append("review", item)

    def _append(self, kind: str, item: ThesisRevision | Evidence | Review) -> bool:
        with self._connect(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            revisions, evidence, reviews = self._load(connection)
            previous = connection.execute(
                "SELECT payload FROM thesis_entries WHERE kind=? AND id=?", (kind, item.id)
            ).fetchone()
            if previous:
                if type(item).model_validate_json(previous[0]) != item:
                    raise ValueError(f"conflicting {kind} id: {item.id}")
                return False
            result = None
            if isinstance(item, ThesisRevision):
                earlier = [r for r in revisions if r.instrument == item.instrument]
                if earlier and item.recorded_at < earlier[-1].recorded_at:
                    raise ValueError("revisions must be appended in chronological order")
                revisions.append(item)
            elif isinstance(item, Evidence):
                evidence.append(item)
            else:
                current = [r for r in revisions if r.instrument == item.instrument]
                if not current or item.revision_id != current[-1].id:
                    raise ValueError("new review must reference the current revision")
                earlier_reviews = [r for r in reviews if r.instrument == item.instrument]
                if earlier_reviews and item.reviewed_at < earlier_reviews[-1].reviewed_at:
                    raise ValueError("reviews must be appended in chronological order")
                reviews.append(item)
                result = json.dumps(
                    evaluate_thesis(
                        item.instrument, revisions, evidence, reviews, now=item.reviewed_at
                    ),
                    ensure_ascii=False,
                )
            validate_history(revisions, evidence, reviews)
            connection.execute(
                "INSERT INTO thesis_entries(kind,id,payload,recorded_at,result) VALUES (?,?,?,?,?)",
                (kind, item.id, item.model_dump_json(), datetime.now(UTC).isoformat(), result),
            )
            return True

    def report(self, *, now: datetime) -> dict[str, Any]:
        revisions, evidence, reviews = self.load()
        products = [
            evaluate_thesis(product, revisions, evidence, reviews, now=now) for product in PRODUCTS
        ]
        return {"schema_version": 1, "generated_at": now.isoformat(), "products": products}

    def history(self) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            revisions, evidence, reviews = self._load(connection)
            return {
                "schema_version": 1,
                "revisions": [r.model_dump(mode="json") for r in revisions],
                "evidence": [e.model_dump(mode="json") for e in evidence],
                "reviews": [r.model_dump(mode="json") for r in reviews],
                "journal": [
                    {
                        "sequence": row[0],
                        "kind": row[1],
                        "id": row[2],
                        "recorded_at": row[3],
                        "review_result": json.loads(row[4]) if row[4] else None,
                    }
                    for row in connection.execute(
                        "SELECT sequence,kind,id,recorded_at,result FROM thesis_entries ORDER BY sequence"
                    )
                ],
            }
