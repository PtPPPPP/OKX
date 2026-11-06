"""Persistence checks use isolated synthetic databases only."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.portfolio.ledger_storage import LedgerStore
from tests.test_personal_ledger import opening, transaction


def test_round_trip_and_duplicate_import_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = LedgerStore(path)
    start = opening()
    entry = transaction("manual-1", "buy")
    store.initialize(start)
    assert store.import_transactions([entry, entry]) == (1, 1)
    restarted = LedgerStore(path)
    assert restarted.load() == (start, [entry])
    assert restarted.import_transactions([entry]) == (0, 1)
    assert restarted.load() == (start, [entry])


def test_existing_ledger_cannot_be_reinitialized(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path / "ledger.db")
    store.initialize(opening())
    store.import_transactions([transaction("buy", "buy")])
    before = store.load()
    with pytest.raises((ValueError, FileExistsError)):
        store.initialize(opening("999"))
    assert store.load() == before


def test_read_and_import_missing_database_do_not_create_it(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "ledger.db"
    store = LedgerStore(path)
    with pytest.raises((ValueError, FileNotFoundError, sqlite3.Error)):
        store.load()
    assert not path.exists()
    with pytest.raises((ValueError, FileNotFoundError, sqlite3.Error)):
        store.import_transactions([transaction("buy", "buy")])
    assert not path.exists()


@pytest.mark.parametrize("failure", ["conflict", "oversell", "cash", "chronology"])
def test_entire_import_rolls_back_when_any_record_is_invalid(tmp_path: Path, failure: str) -> None:
    store = LedgerStore(tmp_path / "ledger.db")
    store.initialize(opening())
    original = transaction("existing", "buy")
    store.import_transactions([original])
    good = transaction("good", "buy")
    bad = {
        "conflict": transaction("existing", "buy", price="101"),
        "oversell": transaction("bad", "sell", quantity="3"),
        "cash": transaction("bad", "transfer_out", amount="1000"),
        "chronology": transaction("bad", "buy", at=opening().as_of),
    }[failure]
    with pytest.raises(ValueError):
        store.import_transactions([good, bad])
    assert LedgerStore(store.path).load()[1] == [original]


def test_other_database_is_not_initialized_or_modified(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    before = path.read_bytes()
    store = LedgerStore(path)
    with pytest.raises((ValueError, FileExistsError)):
        store.initialize(opening())
    with pytest.raises((ValueError, sqlite3.Error)):
        store.load()
    with pytest.raises((ValueError, sqlite3.Error)):
        store.import_transactions([transaction("buy", "buy")])
    assert path.read_bytes() == before


def test_concurrent_duplicate_import_commits_only_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    LedgerStore(path).initialize(opening())
    entry = transaction("shared-id", "buy")
    barrier = Barrier(2)

    def import_once() -> tuple[int, int]:
        barrier.wait(timeout=10)
        return LedgerStore(path).import_transactions([entry])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: import_once(), range(2)))
    assert sorted(results) == [(0, 1), (1, 0)]
    assert LedgerStore(path).load()[1] == [entry]


def test_concurrent_spends_cannot_both_use_same_cash(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    LedgerStore(path).initialize(opening("100"))
    barrier = Barrier(2)

    def spend(identifier: str) -> str:
        barrier.wait(timeout=10)
        try:
            LedgerStore(path).import_transactions(
                [transaction(identifier, "transfer_out", amount="75")]
            )
        except ValueError:
            return "rejected"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(spend, ["spend-a", "spend-b"]))
    assert sorted(results) == ["committed", "rejected"]
    assert len(LedgerStore(path).load()[1]) == 1
