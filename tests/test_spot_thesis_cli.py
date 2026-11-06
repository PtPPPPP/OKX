"""Offline CLI workflow and local-only artifact boundaries for spot theses."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import scripts.spot_thesis as cli
from tests.test_spot_thesis import NOW, START, evidence, review, revision


@pytest.fixture
def local_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "personal-theses"
    monkeypatch.setattr(cli, "LOCAL_ROOT", root)
    return root / "theses.db"


def invoke(capsys: pytest.CaptureFixture[str], database: Path, *arguments: str) -> Any:
    assert cli.main(["--database", str(database), *arguments]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    return json.loads(captured.out)


def test_template_and_init_do_not_populate_reasons_or_save_unrequested_templates(
    local_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    templates = invoke(capsys, local_database, "template", "--at", START.isoformat())
    assert len(templates) == 5
    assert all(item["buy_reason"] is None for item in templates)
    assert all(not item["check_conditions"] for item in templates)
    assert all(not item["invalidation_conditions"] for item in templates)
    assert not local_database.exists()
    invoke(capsys, local_database, "init")
    report = invoke(capsys, local_database, "report", "--at", NOW.isoformat())
    assert len(report["products"]) == 5
    assert all(item["status"] == "MISSING_INPUT" for item in report["products"])
    history = invoke(capsys, local_database, "history")
    assert history["revisions"] == []


def test_register_evidence_review_report_and_history_keep_original_result(
    local_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, local_database, "init")
    for command, item in [
        ("register", revision()),
        ("evidence", evidence()),
        ("review", review()),
    ]:
        path = tmp_path / f"{command}.json"
        path.write_text(item.model_dump_json(), encoding="utf-8")
        assert invoke(capsys, local_database, command, "--file", str(path)) == {"added": True}
        assert invoke(capsys, local_database, command, "--file", str(path)) == {"added": False}
    report_file = local_database.parent / "review.json"
    report = invoke(
        capsys,
        local_database,
        "report",
        "--at",
        NOW.isoformat(),
        "--output",
        str(report_file),
    )
    assert report == json.loads(report_file.read_text(encoding="utf-8"))
    btc = next(item for item in report["products"] if item["instrument"] == "BTC-USDT")
    assert btc["status"] == "SUPPORTED"
    assert btc["action"] is None
    history_before = invoke(capsys, local_database, "history")
    original = next(row for row in history_before["journal"] if row["kind"] == "review")
    assert original["review_result"]["status"] == "SUPPORTED"

    opposing = evidence(
        id="new-opposing-fact", direction="against", collected_at=NOW + timedelta(hours=1)
    )
    path = tmp_path / "opposing.json"
    path.write_text(opposing.model_dump_json(), encoding="utf-8")
    invoke(capsys, local_database, "evidence", "--file", str(path))
    later = invoke(capsys, local_database, "report", "--at", (NOW + timedelta(hours=1)).isoformat())
    assert (
        next(item for item in later["products"] if item["instrument"] == "BTC-USDT")["status"]
        == "CONFLICT"
    )
    history_after = invoke(capsys, local_database, "history")
    assert len(history_after["evidence"]) == 2
    assert next(row for row in history_after["journal"] if row["kind"] == "review") == original


def test_markdown_report_preserves_classification_and_does_not_generate_actions(
    local_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, local_database, "init")
    reason = revision(buy_reason="Synthetic [reference](https://example.invalid) <tag>")
    path = tmp_path / "rationale.json"
    path.write_text(reason.model_dump_json(), encoding="utf-8")
    invoke(capsys, local_database, "register", "--file", str(path))
    path = tmp_path / "inference.json"
    path.write_text(evidence(classification="model_inference").model_dump_json(), encoding="utf-8")
    invoke(capsys, local_database, "evidence", "--file", str(path))
    output = local_database.parent / "review.md"
    assert (
        cli.main(
            [
                "--database",
                str(local_database),
                "report",
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
    assert text.startswith("# 现货投资逻辑复核")
    assert "PENDING_REVIEW" in text
    assert '"classification": "model_inference"' in text
    assert '"action": null' in text
    assert '"max_holding_hours": null' in text
    assert "买入理由：Synthetic \\[reference\\]" in text


@pytest.mark.parametrize(
    "target", ["outside.db", "personal-theses/../escaped.db", "personal-theses"]
)
def test_cli_refuses_database_outside_local_root(
    local_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    path = tmp_path / target
    with pytest.raises(SystemExit) as exc:
        cli.main(["--database", str(path), "init"])
    assert exc.value.code == 2
    assert "outputs must be inside" in capsys.readouterr().err
    assert not path.exists()


def test_cli_artifacts_and_init_refuse_overwrite(
    local_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, local_database, "init")
    before = local_database.read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--database", str(local_database), "init"])
    assert exc.value.code == 2
    capsys.readouterr()
    assert local_database.read_bytes() == before
    output = local_database.parent / "existing.json"
    output.write_text("keep existing content", encoding="utf-8")
    for target in (output, tmp_path / "unignored.json"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--database", str(local_database), "history", "--output", str(target)])
        assert exc.value.code == 2
        capsys.readouterr()
    assert output.read_text(encoding="utf-8") == "keep existing content"
    assert not (tmp_path / "unignored.json").exists()


def test_cli_rejects_duplicate_json_keys_before_any_append(
    local_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(capsys, local_database, "init")
    path = tmp_path / "duplicate.json"
    path.write_text('{"buy_reason":"first","buy_reason":"second"}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--database", str(local_database), "register", "--file", str(path)])
    assert exc.value.code == 2
    assert "duplicate JSON key" in capsys.readouterr().err
    assert invoke(capsys, local_database, "history")["revisions"] == []


def test_cli_runs_with_settings_credentials_and_network_imports_forbidden(tmp_path: Path) -> None:
    root = tmp_path / "isolated-local"
    code = """
import builtins
import sys
from pathlib import Path
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith(('dotenv', 'httpx', 'app.config', 'app.exchange', 'app.market')):
        raise AssertionError('thesis CLI imported credentials or network: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from scripts import spot_thesis as cli
cli.LOCAL_ROOT = Path(sys.argv[1])
assert cli.main(['init']) == 0
assert cli.main(['template', '--at', '2026-10-01T00:00:00Z']) == 0
assert cli.main(['report', '--at', '2026-10-01T00:00:00Z']) == 0
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"buy_reason": null' in completed.stdout
    assert '"status": "MISSING_INPUT"' in completed.stdout
