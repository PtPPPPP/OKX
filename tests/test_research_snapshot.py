from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from backtest.research_execution import independent_validation
from backtest.research_governance import PROJECT_ROOT, SOURCE_FILES, file_hash, read_json
from backtest.vwap_signal_edge_data import HistoricalCandleCache, RawCandle, _manifest
from scripts.research_snapshot import (
    DATA_FILES,
    create_snapshot,
    digest,
    execute_snapshot,
    identity,
    prepare_runtime,
    source_files,
    verify_snapshot,
)
from tests.test_research_cycle import OBSERVED, OOS_START, freeze, make_cache
from tests.test_research_cycle import research_tree as research_tree


def event_cache(directory: Path) -> Path:
    """Synthetic candles exercising volume breakouts and nonzero fills, no market download."""
    make_cache(directory, OOS_START, 36)
    rows = []
    for index, line in enumerate((directory / "raw/candles.jsonl").read_text().splitlines()):
        row = RawCandle(**json.loads(line))
        price = 100 + index % 3 if index < 10 else 110 + (index - 10) // 3
        rows.append(
            replace(
                row,
                open=str(price - 1),
                high=str(price + 1),
                low=str(price - 2),
                close=str(price),
                volume="100" if index in {10, 20, 30} else "10",
            )
        )
    manifest = _manifest(
        rows,
        instrument="BTC-USDT",
        bar="1h",
        start=OOS_START,
        end=OOS_START + timedelta(hours=35),
        interval=timedelta(hours=1),
        duplicate_count=0,
        batches=1,
        failures=0,
        retries=0,
        generated_at=OBSERVED,
        resume=False,
    )
    HistoricalCandleCache(directory).export(rows, manifest)
    return directory


def build_bundle(tree: dict[str, Path], *, consumed: bool = True, external: bool = False) -> Path:
    freeze(tree)
    cache = event_cache(tree["root"] / "data/research/oos")
    if consumed:
        independent_validation(
            tree["freeze"],
            cache,
            tree["root"] / "artifacts/oos",
            purpose="historical_oos",
            source_root=tree["root"],
        )
    output = tree["root"].parent / "bundle"
    create_snapshot(
        output,
        tree["freeze"],
        source_root=tree["root"],
        validation_cache=cache,
        external_data=external,
    )
    return output


@pytest.fixture
def bundle(research_tree: dict[str, Path]) -> Path:
    return build_bundle(research_tree)


@pytest.fixture
def uv() -> str:
    executable = os.environ.get("RESEARCH_TEST_UV") or shutil.which("uv")
    if not executable:
        pytest.skip("isolated runtime integration requires uv or RESEARCH_TEST_UV")
    return executable


def prepare(bundle: Path, uv: str) -> None:
    prepare_runtime(
        bundle, uv=uv, offline=True, python=str(getattr(sys, "_base_executable", sys.executable))
    )


def launch(
    bundle: Path,
    action: str,
    uv: str,
    *,
    cwd: Path,
    run_id: str = "replay",
    data_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-I", str(bundle / "snapshot.py"), action, "--uv", uv]
    if action in {"reproduce", "validate-once"}:
        command += ["--run-id", run_id, "--reason", "explicit offline regression"]
    if data_root:
        command += ["--data-root", str(data_root)]
    return subprocess.run(
        command, cwd=cwd, env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8"
    )


def test_directory_inventory_covers_dependencies_and_excludes_sensitive_resources(
    research_tree: dict[str, Path],
) -> None:
    root = research_tree["root"]
    for name in (
        ".env",
        "app/.env",
        "configs/research/.env",
        "logs/private.log",
        ".venv/credentials.json",
        "data/account.db",
        "configs/demo-secrets.yaml",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO_NOT_ARCHIVE_SENTINEL", encoding="utf-8")
    bundle = build_bundle(research_tree)
    manifest = verify_snapshot(bundle)
    assert "app/risk/position_sizing.py" in source_files(root)
    assert "code/app/risk/position_sizing.py" in manifest["files"]
    assert "code/pyproject.toml" in manifest["files"]
    assert "USAGE.md" in manifest["files"]
    assert all(
        "DO_NOT_ARCHIVE_SENTINEL" not in (bundle / name).read_text(encoding="utf-8")
        for name in manifest["files"]
    )
    assert not any(name.endswith((".sqlite3", ".db", ".log", ".pyc")) for name in manifest["files"])
    assert (
        read_json(bundle / "evidence/cohort.json")["historical_cache"] == "data/research/historical"
    )


@pytest.mark.parametrize(
    "target",
    [
        "code/app/risk/position_sizing.py",
        "code/configs/research/strategy_v3_candidate_specs.json",
        "code/uv.lock",
        "code/pyproject.toml",
        "datasets/historical_oos/normalized/candles.csv",
        "evidence/historical_oos_consumed.json",
        "evidence/training/summary.json",
        "snapshot.py",
    ],
)
def test_tampering_rejected_before_execution(bundle: Path, target: str) -> None:
    path = bundle / target
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        execute_snapshot(
            bundle,
            action="reproduce",
            data_root=None,
            run_id="bad",
            reason="tamper check",
            uv="nonexistent-uv",
        )
    assert not (bundle / "runs").exists()
    assert not (bundle / "registrations").exists()


def test_missing_and_unexpected_code_and_manifest_are_rejected(bundle: Path) -> None:
    path = bundle / "code/app/risk/position_sizing.py"
    content = path.read_bytes()
    path.unlink()
    with pytest.raises(ValueError, match="inventory"):
        verify_snapshot(bundle)
    path.write_bytes(content)
    injected = bundle / "code/sitecustomize.py"
    injected.write_text("raise RuntimeError('unexpected code')", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        verify_snapshot(bundle)
    injected.unlink()
    manifest = read_json(bundle / "snapshot.json")
    manifest["selected_candidate"] = "tampered"
    (bundle / "snapshot.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest hash"):
        verify_snapshot(bundle)


def test_original_report_read_does_not_require_environment_or_rerun(
    bundle: Path, capsys: Any
) -> None:
    execute_snapshot(
        bundle, action="read-report", data_root=None, run_id=None, reason=None, uv="nonexistent-uv"
    )
    assert json.loads(capsys.readouterr().out)["selected_candidate"] is None
    assert not (bundle / "registrations").exists()
    assert not (bundle / "runs").exists()


def test_missing_environment_cannot_produce_success_report(bundle: Path) -> None:
    with pytest.raises(ValueError, match="dependency environment missing"):
        execute_snapshot(
            bundle,
            action="reproduce",
            data_root=None,
            run_id="bad",
            reason="missing dependencies",
            uv="nonexistent-uv",
        )
    assert not (bundle / "runs").exists()


def test_external_data_is_bound_and_never_downloaded(
    research_tree: dict[str, Path], tmp_path: Path
) -> None:
    bundle = build_bundle(research_tree, external=True)
    with pytest.raises(ValueError, match="--data-root"):
        verify_snapshot(bundle)
    external = tmp_path / "external"
    for key, cache in (
        ("historical", research_tree["cache"]),
        ("historical_oos", research_tree["root"] / "data/research/oos"),
    ):
        for name in DATA_FILES:
            target = external / "datasets" / key / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache / name, target)
    verify_snapshot(bundle, data_root=external)
    path = external / "datasets/historical_oos/normalized/candles.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="dataset missing or changed"):
        verify_snapshot(bundle, data_root=external)
    path.unlink()
    with pytest.raises(ValueError, match="dataset missing or changed"):
        verify_snapshot(bundle, data_root=external)


def test_legacy_archive_records_limits_without_rewriting_evidence(
    research_tree: dict[str, Path],
) -> None:
    tree = research_tree
    payload = freeze(tree)
    payload["schema_version"] = 1
    payload.pop("environment")
    payload["source_hashes"] = {name: payload["source_hashes"][name] for name in SOURCE_FILES}
    payload["freeze_hash"] = identity(
        {key: value for key, value in payload.items() if key != "freeze_hash"}
    )
    tree["freeze"].write_text(json.dumps(payload), encoding="utf-8")
    changed = tree["root"] / "app/risk/risk_manager.py"
    changed.write_bytes(changed.read_bytes() + b"\n# later change\n")
    before = digest(tree["freeze"])
    bundle = tree["root"].parent / "legacy"
    result = create_snapshot(bundle, tree["freeze"], source_root=tree["root"])
    assert result["capture_classification"] == "POST_HOC_ARCHIVE"
    assert result["original_environment_proven"] is False
    assert (
        result["source_evidence"]["app/risk/position_sizing.py"] == "NOT_COVERED_BY_ORIGINAL_FREEZE"
    )
    assert result["source_evidence"]["app/risk/risk_manager.py"] == "CHANGED_SINCE_FREEZE"
    assert digest(tree["freeze"]) == before == digest(bundle / "evidence/cohort.json")


def test_v2_rejects_changed_source_at_capture(research_tree: dict[str, Path]) -> None:
    freeze(research_tree)
    path = research_tree["root"] / "app/risk/position_sizing.py"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen source changed"):
        create_snapshot(
            research_tree["root"].parent / "bad",
            research_tree["freeze"],
            source_root=research_tree["root"],
        )


def test_interrupted_consumption_is_not_reset_by_migration(research_tree: dict[str, Path]) -> None:
    payload = freeze(research_tree)
    receipt = research_tree["freeze"].parent / "historical_oos_consumed.json"
    receipt.write_text(json.dumps({"freeze_hash": payload["freeze_hash"]}), encoding="utf-8")
    before = receipt.read_bytes()
    with pytest.raises(ValueError, match="incomplete"):
        create_snapshot(
            research_tree["root"].parent / "bad",
            research_tree["freeze"],
            source_root=research_tree["root"],
        )
    assert receipt.read_bytes() == before


def test_portable_isolated_reproduction_preserves_originals(
    research_tree: dict[str, Path],
    tmp_path: Path,
    uv: str,
    monkeypatch: Any,
) -> None:
    bundle = build_bundle(research_tree)
    original_files = {
        path: file_hash(path)
        for directory in ("artifacts", "data")
        for path in (research_tree["root"] / directory).rglob("*")
        if path.is_file()
    }
    prepare(bundle, uv)
    first = launch(bundle, "reproduce", uv, cwd=tmp_path, run_id="first")
    assert first.returncode == 0, first.stderr
    # The development copy becomes unusable. Snapshot execution must never import it.
    for name in ("backtest/research_execution.py", "app/risk/position_sizing.py"):
        (research_tree["root"] / name).write_text(
            "raise RuntimeError('DEVELOPMENT_IMPORTED')\n", encoding="utf-8"
        )
    monkeypatch.setenv("PYTHONPATH", str(research_tree["root"]) + os.pathsep + str(PROJECT_ROOT))
    monkeypatch.setenv("OKX_API_KEY", "MUST_NOT_REACH_WORKER")
    moved = tmp_path / "independent folder" / "moved"
    shutil.copytree(bundle, moved, ignore=shutil.ignore_patterns(".runtime"))
    prepare(moved, uv)
    verify_snapshot(moved)
    second = launch(moved, "reproduce", uv, cwd=tmp_path, run_id="second")
    assert second.returncode == 0, second.stderr
    a = read_json(bundle / "registrations/first_complete.json")
    b = read_json(moved / "registrations/second_complete.json")
    assert a["economic_fingerprint"] == b["economic_fingerprint"]
    assert a["signal_fingerprint"] == b["signal_fingerprint"]
    assert a["matches_original_economics"] is True and b["matches_original_economics"] is True
    summary = read_json(moved / "runs/second/summary.json")
    assert summary["selected_candidate"] is None
    assert any(
        row["base"]["actionable_signal_count"] > 0 and row["base"]["fill_count"] > 0
        for row in summary["candidate_results"]
    )
    assert all(
        "training_rejection_is_locked" in row["rejection_reasons"]
        for row in summary["candidate_results"]
    )
    refused = launch(moved, "validate-once", uv, cwd=tmp_path, run_id="reselect")
    assert refused.returncode != 0 and "already consumed" in refused.stderr
    assert not (moved / "runs/reselect").exists()
    assert {path: file_hash(path) for path in original_files} == original_files


def test_first_validation_fence_survives_copy_and_requires_explicit_reproduction(
    research_tree: dict[str, Path],
    tmp_path: Path,
    uv: str,
) -> None:
    bundle = build_bundle(research_tree, consumed=False)
    prepare(bundle, uv)
    first = launch(bundle, "validate-once", uv, cwd=tmp_path, run_id="original")
    assert first.returncode == 0, first.stderr
    moved = tmp_path / "moved"
    shutil.copytree(bundle, moved, ignore=shutil.ignore_patterns(".runtime"))
    prepare(moved, uv)
    refused = launch(moved, "validate-once", uv, cwd=tmp_path, run_id="new-validation")
    assert refused.returncode != 0 and "already consumed" in refused.stderr
    repeated = launch(moved, "reproduce", uv, cwd=tmp_path, run_id="registered-replay")
    assert repeated.returncode == 0, repeated.stderr
    assert (moved / "runs/original").is_dir()
    assert (moved / "runs/registered-replay").is_dir()


def test_missing_or_wrong_dependency_fails_before_registration(
    bundle: Path, tmp_path: Path, uv: str
) -> None:
    prepare(bundle, uv)
    site = (
        bundle
        / ".runtime"
        / (
            "Lib/site-packages"
            if os.name == "nt"
            else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        )
    )
    metadata = next(site.glob("pydantic-*.dist-info/METADATA"))
    saved = metadata.read_bytes()
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace("\nVersion: 2.", "\nVersion: 0.", 1),
        encoding="utf-8",
    )
    try:
        result = launch(bundle, "reproduce", uv, cwd=tmp_path)
        assert result.returncode != 0 and "dependency version not in frozen lock" in result.stderr
        assert not (bundle / "runs").exists()
    finally:
        metadata.write_bytes(saved)
    package = site / "pydantic"
    missing = site / "missing-pydantic"
    package.rename(missing)
    try:
        result = launch(bundle, "reproduce", uv, cwd=tmp_path)
        assert result.returncode != 0 and "pydantic" in result.stderr
        assert not (bundle / "runs").exists()
        assert not (bundle / "registrations").exists()
    finally:
        missing.rename(package)


def test_attach_future_data_uses_frozen_code_and_cannot_replace_it(
    research_tree: dict[str, Path],
    tmp_path: Path,
    uv: str,
    monkeypatch: Any,
) -> None:
    freeze(research_tree)
    parent = tmp_path / "before-data"
    create_snapshot(parent, research_tree["freeze"], source_root=research_tree["root"])
    original_hash = digest(parent / "snapshot.json")
    cache = event_cache(research_tree["root"] / "data/research/later-oos")
    prepare(parent, uv)
    monkeypatch.setenv("PYTHONPATH", str(research_tree["root"]))
    (research_tree["root"] / "backtest/research_execution.py").write_text(
        "raise RuntimeError('DEVELOPMENT_IMPORTED')", encoding="utf-8"
    )
    derived = tmp_path / "with-data"
    command = [
        sys.executable,
        "-I",
        str(parent / "snapshot.py"),
        "attach-validation",
        "--cache",
        str(cache),
        "--output",
        str(derived),
        "--reason",
        "scheduled data arrival",
        "--uv",
        uv,
    ]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert digest(parent / "snapshot.json") == original_hash
    verify_snapshot(derived)
    assert digest(derived / "code/backtest/research_execution.py") == digest(
        parent / "code/backtest/research_execution.py"
    )
    prepare(derived, uv)
    result = launch(derived, "validate-once", uv, cwd=tmp_path, run_id="first")
    assert result.returncode == 0, result.stderr
    command[2] = str(derived / "snapshot.py")
    command[command.index("--output") + 1] = str(tmp_path / "replacement")
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0 and "replacement forbidden" in result.stderr


def test_external_data_can_reproduce_from_another_directory(
    research_tree: dict[str, Path],
    tmp_path: Path,
    uv: str,
) -> None:
    bundle = build_bundle(research_tree, external=True)
    external = tmp_path / "external-data"
    for key, cache in (
        ("historical", research_tree["cache"]),
        ("historical_oos", research_tree["root"] / "data/research/oos"),
    ):
        for name in DATA_FILES:
            target = external / "datasets" / key / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache / name, target)
    prepare(bundle, uv)
    result = launch(bundle, "reproduce", uv, cwd=tmp_path, data_root=external)
    assert result.returncode == 0, result.stderr
    assert (
        read_json(bundle / "registrations/replay_complete.json")["matches_original_economics"]
        is True
    )


def test_wrong_python_version_fails_without_creating_environment(bundle: Path, uv: str) -> None:
    manifest = read_json(bundle / "snapshot.json")
    manifest["environment"]["python"] = "0.0.0"
    manifest["snapshot_hash"] = identity(
        {key: value for key, value in manifest.items() if key != "snapshot_hash"}
    )
    (bundle / "snapshot.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="exact Python version required"):
        prepare_runtime(bundle, uv=uv, offline=True, python=sys.executable)
    assert not (bundle / ".runtime").exists()
