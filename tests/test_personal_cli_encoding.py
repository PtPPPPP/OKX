"""Personal CLI pipes use UTF-8 independently of the Windows locale."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = ("personal_ledger", "spot_thesis", "personal_risk")
ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str, encoding: str) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING=f"{encoding}:strict", PYTHONUTF8="0")
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("encoding", ["cp1252", "ascii", "utf-8"])
def test_help_is_utf8_before_argument_parsing(module: str, encoding: str) -> None:
    result = run_cli("-m", f"scripts.{module}", "--help", encoding=encoding)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert "离线" in result.stdout.decode("utf-8")
    assert result.stderr == b""


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_error_message_is_utf8_and_preserves_exit_code(module: str, encoding: str) -> None:
    result = run_cli("-m", f"scripts.{module}", "不支持的命令", encoding=encoding)
    assert result.returncode == 2
    message = result.stderr.decode("utf-8")
    assert "不支持的命令" in message
    assert "UnicodeEncodeError" not in message
    assert "Traceback" not in message


@pytest.mark.parametrize("module", MODULES)
def test_successful_json_preserves_unicode_under_cp1252(module: str, tmp_path: Path) -> None:
    local_root = tmp_path / "合成数据🧪"
    payload = tmp_path / "input.json"
    data = {"as_of": "2026-01-01T00:00:00Z", "source": "合成来源🧪"}
    if module == "personal_ledger":
        data["cash"] = "0"
    payload.write_text(
        json.dumps(data),
        encoding="utf-8",
    )
    if module == "personal_risk":
        arguments = ["report", "--input", str(payload), "--at", "2026-01-01T00:00:00Z"]
    elif module == "personal_ledger":
        arguments = ["--database", str(local_root / "ledger.db"), "init", "--opening", str(payload)]
    else:
        arguments = ["--database", str(local_root / "thesis.db"), "init"]
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"from scripts import {module} as cli\n"
        "cli.LOCAL_ROOT = Path(sys.argv[1])\n"
        "raise SystemExit(cli.main(sys.argv[2:]))\n"
    )
    result = run_cli("-c", code, str(local_root), *arguments, encoding="cp1252")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    report = json.loads(result.stdout.decode("utf-8"))
    assert "🧪" in json.dumps(report, ensure_ascii=False)
    assert result.stderr == b""


@pytest.mark.parametrize("module", ["personal_risk", "spot_thesis"])
def test_markdown_preserves_chinese_under_cp1252(module: str, tmp_path: Path) -> None:
    local_root = tmp_path / "synthetic"
    payload = tmp_path / "risk.json"
    payload.write_text('{"as_of":"2026-01-01T00:00:00Z","source":"synthetic"}', encoding="utf-8")
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"from scripts import {module} as cli\n"
        "cli.LOCAL_ROOT = Path(sys.argv[1])\n"
    )
    if module == "spot_thesis":
        code += "cli.main(['init'])\n"
        arguments = ["report", "--format", "markdown"]
    else:
        arguments = ["report", "--input", str(payload), "--format", "markdown"]
    code += "raise SystemExit(cli.main(sys.argv[2:]))\n"
    result = run_cli("-c", code, str(local_root), *arguments, encoding="cp1252")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    heading = "# 个人组合风险报告" if module == "personal_risk" else "# 现货投资逻辑复核"
    assert heading in result.stdout.decode("utf-8")


def test_import_does_not_change_caller_stream_encoding() -> None:
    code = (
        "import sys\n"
        "before = (sys.stdout.encoding, sys.stderr.encoding)\n"
        "from scripts import personal_ledger, personal_risk, spot_thesis\n"
        "assert (sys.stdout.encoding, sys.stderr.encoding) == before\n"
    )
    result = run_cli("-c", code, encoding="cp1252")
    assert result.returncode == 0, result.stderr.decode("utf-8")


def test_main_accepts_caller_owned_stringio() -> None:
    code = (
        "import contextlib, io\n"
        "from scripts import personal_risk\n"
        "output, errors = io.StringIO(), io.StringIO()\n"
        "with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):\n"
        "    assert personal_risk.main(['template']) == 0\n"
        "assert not output.closed and not errors.closed\n"
        "assert 'user_to_complete' in output.getvalue()\n"
    )
    result = run_cli("-c", code, encoding="cp1252")
    assert result.returncode == 0, result.stderr.decode("utf-8")
