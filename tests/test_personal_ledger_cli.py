from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import scripts.personal_ledger as cli


@pytest.fixture
def ledger_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "private-local"
    monkeypatch.setattr(cli, "LOCAL_ROOT", root)
    opening = tmp_path / "opening.json"
    opening.write_text(
        json.dumps(
            {
                "as_of": "2026-01-01T00:00:00Z",
                "source": "synthetic_cli_test",
                "cash": "1000",
                "positions": [],
            }
        ),
        encoding="utf-8",
    )
    return root / "ledger.db", opening


def invoke(capsys: pytest.CaptureFixture[str], database: Path, *args: str) -> Any:
    assert cli.main(["--database", str(database), *args]) == 0
    output = capsys.readouterr()
    assert not output.err
    return json.loads(output.out)


def test_cli_record_import_show_history_and_duplicate_reimport(
    ledger_paths: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, opening = ledger_paths
    assert "created" in invoke(capsys, database, "init", "--opening", str(opening))
    buy = (
        "record",
        "--id",
        "buy-1",
        "--kind",
        "buy",
        "--at",
        "2026-01-01T00:01:00Z",
        "--source",
        "synthetic_manual",
        "--instrument",
        "BTC-USDT",
        "--quantity",
        "2",
        "--price",
        "100",
        "--fee",
        "1",
    )
    assert invoke(capsys, database, *buy) == {"added": 1, "duplicates": 0}
    entries: list[dict[str, Any]] = [
        {
            "id": "sell-1",
            "kind": "sell",
            "at": "2026-01-01T00:02:00Z",
            "source": "synthetic_batch",
            "instrument": "BTC-USDT",
            "quantity": "0.5",
            "price": "120",
            "fee": "0.5",
        },
        {
            "id": "fee-1",
            "kind": "fee",
            "at": "2026-01-01T00:03:00Z",
            "source": "synthetic_batch",
            "amount": "1",
        },
        {
            "id": "in-1",
            "kind": "transfer_in",
            "at": "2026-01-01T00:04:00Z",
            "source": "synthetic_batch",
            "amount": "50",
        },
        {
            "id": "out-1",
            "kind": "transfer_out",
            "at": "2026-01-01T00:05:00Z",
            "source": "synthetic_batch",
            "amount": "7.5",
        },
    ]
    transactions = tmp_path / "transactions.json"
    transactions.write_text(json.dumps(entries), encoding="utf-8")
    assert invoke(capsys, database, "import", "--transactions", str(transactions)) == {
        "added": 4,
        "duplicates": 0,
    }
    assert invoke(capsys, database, "import", "--transactions", str(transactions)) == {
        "added": 0,
        "duplicates": 4,
    }
    # A previously imported entry remains a no-op after newer entries were appended.
    assert invoke(capsys, database, *buy) == {"added": 0, "duplicates": 1}
    quotes = tmp_path / "quotes.json"
    quotes.write_text(
        '[{"instrument":"BTC-USDT","price":"110",'
        '"at":"2026-01-01T00:10:00Z","source":"synthetic_quote"}]',
        encoding="utf-8",
    )
    output = database.parent / "report.json"
    report = invoke(
        capsys,
        database,
        "show",
        "--quotes",
        str(quotes),
        "--at",
        "2026-01-01T00:10:00Z",
        "--output",
        str(output),
    )
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert Decimal(report["cash"]) == Decimal("900")
    assert Decimal(report["account_value"]) == Decimal("1065")
    assert Decimal(report["realized_pnl"]) == Decimal("8.25")
    assert Decimal(report["unrealized_pnl"]) == Decimal("14.25")
    assert Decimal(report["positions"][0]["quantity"]) == Decimal("1.5")
    assert report["valuation_time_mode"] == "explicit"
    history = invoke(capsys, database, "history")
    assert history["opening"]["source"] == "synthetic_cli_test"
    assert len(history["transactions"]) == 5
    assert history["transactions"][0]["id"] == "buy-1"


@pytest.mark.parametrize("target", ["outside.db", "private-local/../escaped.db", "private-local"])
def test_cli_refuses_outputs_outside_local_root(
    ledger_paths: tuple[Path, Path],
    tmp_path: Path,
    target: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, opening = ledger_paths
    database = tmp_path / target
    with pytest.raises(SystemExit) as exc:
        cli.main(["--database", str(database), "init", "--opening", str(opening)])
    assert exc.value.code == 2
    assert "ledger outputs must be inside" in capsys.readouterr().err
    assert not database.exists()


def test_cli_init_and_report_refuse_overwrite(
    ledger_paths: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, opening = ledger_paths
    invoke(capsys, database, "init", "--opening", str(opening))
    before = database.read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--database", str(database), "init", "--opening", str(opening)])
    assert exc.value.code == 2
    capsys.readouterr()
    assert database.read_bytes() == before
    output = database.parent / "existing.json"
    output.write_text("existing content", encoding="utf-8")
    for target in (output, tmp_path / "unignored-report.json"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--database", str(database), "show", "--output", str(target)])
        assert exc.value.code == 2
        capsys.readouterr()
    assert output.read_text(encoding="utf-8") == "existing content"
    assert not (tmp_path / "unignored-report.json").exists()


def test_cli_preserves_json_decimal_and_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "numbers.json"
    path.write_text('\ufeff{"amount": 0.12345678901234567890123456789}', encoding="utf-8")
    assert cli.read_json(path) == {"amount": Decimal("0.12345678901234567890123456789")}
    path.write_text('{"cash":"1","cash":"2"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        cli.read_json(path)


def test_cli_numeric_json_amount_survives_storage_round_trip(
    ledger_paths: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, opening = ledger_paths
    invoke(capsys, database, "init", "--opening", str(opening))
    entries = tmp_path / "precise.json"
    amount = "0.12345678901234567890123456789"
    entries.write_text(
        '[{"id":"exact-1","kind":"transfer_in","at":"2026-01-01T00:01:00Z",'
        f'"source":"synthetic","amount":{amount}}}]',
        encoding="utf-8",
    )
    invoke(capsys, database, "import", "--transactions", str(entries))
    history = invoke(capsys, database, "history")
    assert history["transactions"][0]["amount"] == amount


def test_cli_legacy_import_is_unknown_and_retains_source_time(
    ledger_paths: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, _ = ledger_paths
    positions = tmp_path / "legacy.json"
    positions.write_text(
        '[{"instrument":"BTC-USDT","cost":"100","market_value_usdt":"900",'
        '"updated_at":"2025-12-01T00:00:00Z","source":"synthetic_legacy"}]',
        encoding="utf-8",
    )
    invoke(
        capsys,
        database,
        "import-legacy",
        "--positions",
        str(positions),
        "--as-of",
        "2026-01-01T00:00:00Z",
        "--source",
        "synthetic_import",
    )
    result = invoke(capsys, database, "show", "--at", "2026-01-01T00:00:00Z")
    assert result["complete"] is False
    assert result["account_value"] is None
    assert result["cash"] is None
    position = result["positions"][0]
    assert position["quantity"] is None
    assert position["market_value"] is None
    assert position["historical_market_value"] == "900"
    assert position["historical_source"] == "synthetic_legacy"
    assert position["historical_updated_at"] == "2025-12-01T00:00:00+00:00"


def test_cli_runs_with_settings_dotenv_and_network_imports_forbidden(
    ledger_paths: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    _, opening = ledger_paths
    root = tmp_path / "subprocess-local"
    code = """
import builtins
import sys
from pathlib import Path
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith(('dotenv', 'httpx', 'app.config', 'app.exchange', 'app.market')):
        raise AssertionError('ledger imported credentials or network: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from scripts import personal_ledger as cli
cli.LOCAL_ROOT = Path(sys.argv[1])
assert cli.main(['init', '--opening', sys.argv[2]]) == 0
assert cli.main(['show', '--at', '2026-01-01T00:00:00Z']) == 0
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root), str(opening)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"complete": true' in completed.stdout
    assert '"account_value": "1000"' in completed.stdout
