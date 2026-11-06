"""Append-only thesis history tests use synthetic records and temporary SQLite."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from app.portfolio.thesis_storage import ThesisStore
from tests.test_spot_thesis import NOW, evidence, review, revision


def test_empty_store_contains_no_invented_reasons_and_reports_five_products(tmp_path: Path) -> None:
    store = ThesisStore(tmp_path / "thesis.db")
    store.initialize()
    assert store.load() == ([], [], [])
    report = store.report(now=NOW)
    assert report["schema_version"] == 1
    assert len(report["products"]) == 5
    assert all(item["status"] == "MISSING_INPUT" for item in report["products"])
    assert all(item["buy_reason"] is None for item in report["products"])


def test_restart_preserves_all_history_and_same_ids_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "thesis.db"
    store = ThesisStore(path)
    store.initialize()
    for method, item in [
        (store.append_revision, revision()),
        (store.append_evidence, evidence()),
        (store.append_review, review()),
    ]:
        assert method(item) is True
        assert method(item) is False
    restarted = ThesisStore(path)
    assert restarted.load() == ([revision()], [evidence()], [review()])
    assert restarted.append_revision(revision()) is False
    assert restarted.append_evidence(evidence()) is False
    assert restarted.append_review(review()) is False
    history = restarted.history()
    assert len(history["revisions"]) == len(history["evidence"]) == len(history["reviews"]) == 1
    btc = next(
        item for item in restarted.report(now=NOW)["products"] if item["instrument"] == "BTC-USDT"
    )
    assert btc["status"] == "SUPPORTED"


@pytest.mark.parametrize("kind", ["revision", "evidence", "review"])
def test_conflicting_id_never_overwrites_existing_history(tmp_path: Path, kind: str) -> None:
    store = ThesisStore(tmp_path / "thesis.db")
    store.initialize()
    store.append_revision(revision())
    store.append_evidence(evidence())
    store.append_review(review())
    before = store.load()
    with pytest.raises(ValueError):
        if kind == "revision":
            store.append_revision(revision(buy_reason="Conflicting synthetic rationale"))
        elif kind == "evidence":
            store.append_evidence(evidence(statement="Conflicting synthetic statement"))
        else:
            store.append_review(review(notes="Conflicting synthetic review"))
    assert store.load() == before


def test_invalid_reference_rolls_back_without_losing_existing_records(tmp_path: Path) -> None:
    store = ThesisStore(tmp_path / "thesis.db")
    store.initialize()
    store.append_revision(revision())
    store.append_evidence(evidence())
    before = store.load()
    with pytest.raises(ValueError):
        store.append_review(review(revision_id="missing"))
    with pytest.raises(ValueError):
        store.append_evidence(evidence(id="next", supersedes_id="missing"))
    assert store.load() == before


def test_other_database_and_existing_ledger_cannot_be_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
    before = path.read_bytes()
    store = ThesisStore(path)
    with pytest.raises((ValueError, FileExistsError)):
        store.initialize()
    with pytest.raises((ValueError, sqlite3.Error)):
        store.load()
    assert path.read_bytes() == before
    own = ThesisStore(tmp_path / "thesis.db")
    own.initialize()
    own.append_revision(revision())
    with pytest.raises((ValueError, FileExistsError)):
        own.initialize()
    assert own.load() == ([revision()], [], [])


def test_missing_store_read_does_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "thesis.db"
    store = ThesisStore(path)
    with pytest.raises((ValueError, FileNotFoundError, sqlite3.Error)):
        store.load()
    with pytest.raises((ValueError, FileNotFoundError, sqlite3.Error)):
        store.append_revision(revision())
    assert not path.exists()


def test_concurrent_reimport_records_only_one_revision(tmp_path: Path) -> None:
    path = tmp_path / "thesis.db"
    ThesisStore(path).initialize()
    barrier = Barrier(2)

    def append_once() -> bool:
        barrier.wait(timeout=10)
        return ThesisStore(path).append_revision(revision())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: append_once(), range(2)))
    assert sorted(results) == [False, True]
    assert ThesisStore(path).load() == ([revision()], [], [])


def test_sqlite_rejects_history_update_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "thesis.db"
    store = ThesisStore(path)
    store.initialize()
    store.append_revision(revision())
    store.append_evidence(evidence())
    before = store.load()
    with closing(sqlite3.connect(path)) as connection:
        for statement in (
            "UPDATE thesis_entries SET payload='{}' WHERE kind='evidence'",
            "DELETE FROM thesis_entries WHERE kind='evidence'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
            connection.rollback()
    assert store.load() == before


def test_new_revision_does_not_allow_new_review_of_outdated_revision(tmp_path: Path) -> None:
    store = ThesisStore(tmp_path / "thesis.db")
    store.initialize()
    store.append_revision(revision())
    store.append_evidence(evidence())
    store.append_review(review())
    store.append_revision(revision(id="thesis-2", recorded_at=NOW))
    before = store.load()
    with pytest.raises(ValueError):
        store.append_review(review(id="review-2", reviewed_at=NOW))
    assert store.load() == before
    assert before[2] == [review()]


def test_new_materials_preserve_the_original_invalidation_review_snapshot(tmp_path: Path) -> None:
    store = ThesisStore(tmp_path / "thesis.db")
    store.initialize()
    store.append_revision(revision())
    store.append_evidence(evidence(direction="against"))
    store.append_review(
        review(
            checks=[
                {"condition_id": "growth", "outcome": "not_met", "evidence_ids": ["fact-1"]},
                {"condition_id": "decline", "outcome": "met", "evidence_ids": ["fact-1"]},
            ]
        )
    )
    before = next(row for row in store.history()["journal"] if row["kind"] == "review")
    assert before["review_result"]["status"] == "INVALIDATED"
    store.append_evidence(evidence(id="new-support", collected_at=NOW))
    current = next(
        item for item in store.report(now=NOW)["products"] if item["instrument"] == "BTC-USDT"
    )
    assert current["status"] == "CONFLICT"
    assert next(row for row in store.history()["journal"] if row["kind"] == "review") == before
