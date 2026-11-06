"""Offline risk CLI tests use synthetic records and temporary output directories."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import scripts.personal_risk as cli
from app.portfolio.ledger_storage import LedgerStore
from tests.test_risk_ledger_adapter import NOW, opening, request


@pytest.fixture
def risk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "personal-risk"
    monkeypatch.setattr(cli, "LOCAL_ROOT", root)
    return root


def invoke(capsys: pytest.CaptureFixture[str], *arguments: str) -> Any:
    assert cli.main(list(arguments)) == 0
    captured = capsys.readouterr()
    assert not captured.err
    return json.loads(captured.out)


def input_file(tmp_path: Path) -> Path:
    path = tmp_path / "risk-input.json"
    path.write_text(request().model_dump_json(), encoding="utf-8")
    return path


def test_template_leaves_data_and_all_risk_budgets_unknown(
    risk_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    template = invoke(capsys, "template", "--at", NOW.isoformat())
    assert template["cash"] is None
    assert template["positions_complete"] is False
    assert template["cash_scope"] == "unknown"
    assert (
        template["spot_positions"]
        == template["contracts"]
        == template["quotes"]
        == template["scenarios"]
        == []
    )
    assert all(value is None or value == {} for value in template["settings"].values())
    assert not risk_root.exists()


def test_ledger_report_uses_actual_quantity_and_requires_user_confirmation(
    risk_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = input_file(tmp_path)
    ledger = tmp_path / "ledger.db"
    LedgerStore(ledger).initialize(opening())
    before = ledger.read_bytes()
    args = ("report", "--input", str(path), "--ledger", str(ledger), "--at", NOW.isoformat())
    unconfirmed = invoke(capsys, *args)
    assert unconfirmed["net_equity"] is None
    assert unconfirmed["ledger_confirmation"] == "not_confirmed"
    output = risk_root / "report.json"
    report = invoke(
        capsys, *args, "--confirm-ledger", "--available-cash", "800", "--output", str(output)
    )
    assert Decimal(report["net_equity"]) == Decimal("1200")
    assert report["ledger_confirmation"] == "user_confirmed_local_data"
    assert report["valuation_time_mode"] == "explicit"
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert ledger.read_bytes() == before


def test_report_does_not_search_for_any_ledger_by_default(
    risk_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not read any unrequested personal ledger")

    monkeypatch.setattr(cli.LedgerStore, "load", forbidden)
    report = invoke(capsys, "report", "--input", str(input_file(tmp_path)), "--at", NOW.isoformat())
    assert report["net_equity"] is None
    assert report["ledger_confirmation"] == "not_imported"


def test_missing_ledger_is_not_created_and_ledger_options_require_explicit_path(
    risk_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = input_file(tmp_path)
    missing = tmp_path / "missing.db"
    for extra in (["--ledger", str(missing)], ["--confirm-ledger"], ["--available-cash", "1"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["report", "--input", str(path), *extra])
        assert exc.value.code == 2
        capsys.readouterr()
    assert not missing.exists()


def test_output_rejects_overwrite_and_unignored_paths(
    risk_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = risk_root / "template.json"
    invoke(capsys, "template", "--at", NOW.isoformat(), "--output", str(output))
    before = output.read_bytes()
    for destination in (
        output,
        tmp_path / "outside.json",
        risk_root / ".." / "escape.json",
        risk_root,
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(["template", "--output", str(destination)])
        assert exc.value.code == 2
        capsys.readouterr()
    assert output.read_bytes() == before
    assert not (tmp_path / "outside.json").exists()
    assert not (tmp_path / "escape.json").exists()


def test_numeric_json_is_parsed_without_float_precision_loss(
    risk_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "precise.json"
    amount = "1000.12345678901234567890123456789"
    path.write_text(
        '{"as_of":"2026-01-01T00:00:00Z","source":"synthetic","cash_scope":"spot_only",'
        '"positions_complete":true,"cash":{"balance":' + amount + ',"verified":true,'
        '"updated_at":"2026-01-01T00:00:00Z","source":"synthetic"}}',
        encoding="utf-8",
    )
    report = invoke(capsys, "report", "--input", str(path), "--at", NOW.isoformat())
    assert report["cash_balance"] == amount


@pytest.mark.parametrize(
    "content",
    [
        '{"source":"one","source":"two"}',
        '{"as_of":"2026-01-01T00:00:00Z","source":"synthetic","unknown":1}',
        '{"as_of":"2026-01-01","source":"synthetic"}',
    ],
)
def test_invalid_json_or_schema_fails_without_output(
    risk_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    output = risk_root / "bad.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(["report", "--input", str(path), "--output", str(output)])
    assert exc.value.code == 2
    capsys.readouterr()
    assert not output.exists()


def test_markdown_report_shows_unknown_budget_and_safe_metadata(
    risk_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = input_file(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source"] = "synthetic\n# injected\n```html\n<script>bad</script>"
    path.write_text(json.dumps(payload), encoding="utf-8")
    output = risk_root / "report.md"
    assert (
        cli.main(
            [
                "report",
                "--input",
                str(path),
                "--at",
                NOW.isoformat(),
                "--format",
                "markdown",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert output.read_text(encoding="utf-8") == text
    assert text.startswith("# 个人组合风险报告")
    assert "组合净权益：未知/未配置" in text
    assert "风险设置与检查" in text
    assert "不生成交易指令" in text
    assert "整体状态：待补充或设置" in text
    assert "不是预测概率或损失上限" in text
    assert "长期现货用途预算" in text
    assert "\n# injected" not in text
    assert "\n```html" not in text


def test_cli_does_not_import_settings_credentials_or_network(tmp_path: Path) -> None:
    code = """
import builtins
import sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(('dotenv', 'httpx', 'app.config', 'app.exchange', 'app.market')):
        raise AssertionError('forbidden import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from scripts import personal_risk as cli
cli.LOCAL_ROOT = Path(sys.argv[1])
path = cli.LOCAL_ROOT / 'template.json'
assert cli.main(['template', '--at', '2026-01-01T00:00:00Z', '--output', str(path)]) == 0
assert cli.main(['report', '--input', str(path), '--at', '2026-01-01T00:00:00Z']) == 0
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "local-risk")],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"net_equity": null' in completed.stdout
