"""Portable research archive and stdlib-only launcher (also copied as snapshot.py)."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT_VERSION = 1
CODE_DIRECTORIES = ("app", "backtest", "scripts")
RESOURCE_DIRECTORIES = ("configs/research", "configs/snapshots")
ROOT_RESOURCES = ("pyproject.toml", "uv.lock", "README.md", "docs/research-snapshots.md")
DATA_FILES = ("data_manifest.json", "normalized/candles.csv")
PURPOSES = ("historical_oos", "prospective")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def publish(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def inside(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or relative.is_absolute()
        or any(part in {".", ".."} or ":" in part for part in relative.parts)
    ):
        raise ValueError(f"unsafe archive path: {name}")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"archive path escapes root: {name}")
    if any(_linked(part) for part in (target, *target.parents) if part != root.parent):
        raise ValueError(f"archive links are forbidden: {name}")
    return target


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def source_files(root: Path) -> list[str]:
    """Directory rules, never a hand-maintained transitive Python file list."""
    names = list(ROOT_RESOURCES)
    for directory, suffix in (
        *((name, ".py") for name in CODE_DIRECTORIES),
        *((name, ".json") for name in RESOURCE_DIRECTORIES),
    ):
        base = inside(root, directory)
        if not base.is_dir():
            raise ValueError(f"required source directory missing: {directory}")
        for path in base.rglob(f"*{suffix}"):
            name = path.relative_to(root).as_posix()
            if any(
                part.startswith(".") or part == "__pycache__"
                for part in path.relative_to(base).parts
            ):
                continue
            inside(root, name)
            names.append(name)
    for name in names:
        if not inside(root, name).is_file():
            raise ValueError(f"required source file missing: {name}")
    return sorted(set(names))


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "byteorder": sys.byteorder,
        "dependency_restore": "uv sync --locked --no-dev --no-install-project; isolated .runtime",
    }


def _copy(source: Path, target: Path) -> None:
    if _linked(source):
        raise ValueError("refusing linked research input")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _copy_report(source: Path, target: Path, expected: str) -> None:
    from backtest.research_governance import verify_report

    verify_report(source, expected_hash=expected)
    for name in (*read(source / "report_manifest.json")["files"], "report_manifest.json"):
        # Reports contain only published research JSON/CSV/Markdown, no arbitrary files.
        if PurePosixPath(name).suffix not in {".json", ".csv", ".md"}:
            raise ValueError(f"unsupported published research resource: {name}")
        _copy(inside(source, name), inside(target, name))


def create_snapshot(
    output: Path,
    freeze_path: Path,
    *,
    source_root: Path,
    historical_cache: Path | None = None,
    training_report: Path | None = None,
    purpose: str = "historical_oos",
    validation_cache: Path | None = None,
    validation_report: Path | None = None,
    external_data: bool = False,
) -> dict[str, Any]:
    from backtest.research_governance import (
        EXECUTION_POLICY,
        SOURCE_FILES,
        check_freeze_hash,
        validate_oos_data,
        validated_dataset,
        verify_freeze_artifacts,
    )

    if purpose not in PURPOSES:
        raise ValueError("invalid research purpose")
    freeze = read(freeze_path)
    check_freeze_hash(freeze)
    if freeze.get("schema_version") not in {1, 2}:
        raise ValueError("unsupported research freeze version")
    if freeze["execution_policy"] != EXECUTION_POLICY:
        raise ValueError("archive execution policy differs from frozen policy")
    history = historical_cache or source_root / freeze["historical_cache"]
    training = training_report or (source_root / freeze["training_summary"]).parent
    verify_freeze_artifacts(freeze, history, training / "summary.json")
    _, history_manifest = validated_dataset(history)
    names = source_files(source_root)
    old_hashes = freeze["source_hashes"]
    expected_names = names if freeze["schema_version"] == 2 else SOURCE_FILES
    if not isinstance(old_hashes, dict) or set(old_hashes) != set(expected_names):
        raise ValueError("frozen source allowlist mismatch")
    comparison = {
        name: "MATCHES_FROZEN_HASH"
        if name in old_hashes and digest(source_root / name) == old_hashes[name]
        else "CHANGED_SINCE_FREEZE"
        if name in old_hashes
        else "NOT_COVERED_BY_ORIGINAL_FREEZE"
        for name in sorted(set(names) | set(old_hashes))
    }
    # Parameter and instrument evidence must remain exact even for post-hoc archives.
    for name in (
        "configs/research/strategy_v3_candidate_specs.json",
        "configs/snapshots/BTC-USDT.json",
    ):
        if comparison[name] != "MATCHES_FROZEN_HASH":
            raise ValueError(f"frozen research configuration changed: {name}")
    if freeze.get("schema_version") == 2 and any(
        value != "MATCHES_FROZEN_HASH" for value in comparison.values()
    ):
        raise ValueError("version 2 frozen source changed; restore exact source before archiving")
    receipt_path = freeze_path.parent / f"{purpose}_consumed.json"
    completion_path = freeze_path.parent / f"{purpose}_complete.json"
    receipt = read(receipt_path) if receipt_path.exists() else None
    completion = read(completion_path) if completion_path.exists() else None
    if completion and not receipt:
        raise ValueError("completion without consumption receipt")
    if receipt and (receipt["freeze_hash"] != freeze["freeze_hash"] or not completion):
        raise ValueError("consumed research is incomplete; audit interrupted run before archiving")
    if receipt and not validation_cache:
        raise ValueError("consumed OOS requires its exact validation cache and published report")
    data = {"historical": (history, history_manifest)}
    if validation_cache:
        _, manifest = validate_oos_data(freeze, validation_cache, purpose=purpose)
        data[purpose] = (validation_cache, manifest)
        if receipt and completion:
            if any(
                row["freeze_hash"] != freeze["freeze_hash"]
                or row["dataset_hash"] != manifest["dataset_hash"]
                for row in (receipt, completion)
            ):
                raise ValueError("OOS consumption/data binding mismatch")
            validation_report = validation_report or Path(receipt["output"])
    if output.exists():
        raise FileExistsError("snapshot output already exists")
    output.mkdir(parents=True)
    # No recursive copies of data, artifacts or configs: only explicit public research resources.
    for name in names:
        _copy(inside(source_root, name), inside(output / "code", name))
    _copy(inside(source_root, "scripts/research_snapshot.py"), output / "snapshot.py")
    _copy(inside(source_root, "docs/research-snapshots.md"), output / "USAGE.md")
    _copy(freeze_path, output / "evidence/cohort.json")
    _copy_report(training, output / "evidence/training", freeze["training_artifact_set_hash"])
    # Preserve all purposes' fences, even if this bundle only executes one purpose.
    for item in PURPOSES:
        for state in ("consumed", "complete", "pending"):
            name = f"{item}_{state}.json"
            original = freeze_path.parent / name
            if original.exists():
                _copy(original, output / "evidence" / name)
    if completion and validation_report:
        _copy_report(
            validation_report, output / "evidence/original_report", completion["artifact_set_hash"]
        )
    datasets = {}
    for key, (cache, manifest) in data.items():
        files = {name: digest(inside(cache, name)) for name in DATA_FILES}
        datasets[key] = {
            "location": f"datasets/{key}",
            "mode": "external" if external_data else "bundled",
            "files": files,
            "dataset_hash": manifest["dataset_hash"],
            "start": manifest["actual_start"],
            "end": manifest["actual_end"],
        }
        if not external_data:
            for name in DATA_FILES:
                _copy(inside(cache, name), inside(output / f"datasets/{key}", name))
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "capture_classification": "POST_HOC_ARCHIVE"
        if freeze["schema_version"] == 1
        else "VERIFIED_V2_SOURCE_CAPTURE",
        "capture_label_zh": "事后归档"
        if freeze["schema_version"] == 1
        else "经冻结哈希核对的源码归档",
        "environment_evidence": "OBSERVED_AT_ARCHIVE_CREATION_NOT_ORIGINAL_FREEZE"
        if freeze["schema_version"] == 1
        else "EXACT_FREEZE_ENVIRONMENT_REQUIRED",
        "environment": environment(),
        "original_freeze_hash": freeze["freeze_hash"],
        "original_freeze_file_sha256": digest(freeze_path),
        "source_evidence": comparison,
        "original_environment_proven": freeze.get("environment") == environment(),
        "original_execution_equivalence_proven": False,
        "purpose": purpose,
        "datasets": datasets,
        "consumption": "COMPLETE" if receipt else "NOT_CONSUMED_AT_CAPTURE",
        "selected_candidate": freeze["selected_candidate"],
        "training_artifact_set_hash": freeze["training_artifact_set_hash"],
        "original_report_artifact_set_hash": completion["artifact_set_hash"]
        if completion
        else None,
        "files": {
            path.relative_to(output).as_posix(): digest(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
    }
    if freeze.get("schema_version") == 2 and freeze["environment"] != environment():
        raise ValueError(
            "archive environment differs from version 2 freeze; restore exact environment"
        )
    manifest["snapshot_hash"] = identity(manifest)
    publish(output / "snapshot.json", manifest)
    verify_snapshot(output, check_data=not external_data)
    return manifest


def verify_snapshot(
    root: Path, *, data_root: Path | None = None, check_data: bool = True
) -> dict[str, Any]:
    manifest = read(root / "snapshot.json")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported research snapshot format version")
    if identity(
        {key: value for key, value in manifest.items() if key != "snapshot_hash"}
    ) != manifest.get("snapshot_hash"):
        raise ValueError("snapshot manifest hash mismatch")
    files = manifest["files"]
    actual = {
        path.relative_to(root).as_posix()
        for directory in ("code", "evidence", "datasets")
        for path in (root / directory).rglob("*")
        if path.is_file()
    } | {"snapshot.py"}
    if (root / "USAGE.md").exists():
        actual.add("USAGE.md")
    if actual != set(files):
        missing = sorted(set(files) - actual)
        unexpected = sorted(actual - set(files))
        raise ValueError(
            f"snapshot file inventory mismatch: missing={missing}; unexpected={unexpected}"
        )
    for name, expected in files.items():
        path = inside(root, name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"snapshot file changed or missing: {name}")
    if check_data:
        for key, dataset in manifest["datasets"].items():
            cache = dataset_path(root, dataset, data_root)
            for name, expected in dataset["files"].items():
                path = inside(cache, name)
                if not path.is_file() or digest(path) != expected:
                    raise ValueError(
                        f"required dataset missing or changed: {key}/{name}; no download fallback"
                    )
    return manifest


def dataset_path(root: Path, dataset: dict[str, Any], data_root: Path | None) -> Path:
    if dataset["mode"] == "external" and data_root is None:
        raise ValueError("external dataset requires --data-root with datasets/<logical-id> layout")
    return inside(
        data_root if dataset["mode"] == "external" and data_root else root, dataset["location"]
    )


def _uv_command(
    root: Path,
    manifest: dict[str, Any],
    uv: str,
    *,
    check: bool,
    offline: bool,
    python: str | None = None,
) -> list[str]:
    command = [
        uv,
        "sync",
        "--project",
        str(root / "code"),
        "--locked",
        "--no-dev",
        "--no-install-project",
        "--python",
        python or manifest["environment"]["python"],
        "--no-python-downloads",
        "--no-config",
        "--link-mode",
        "copy",
    ]
    if check:
        command.append("--check")
    else:
        # Explicit restoration must not attest already corrupted installed files as a new baseline.
        command.append("--reinstall")
    if offline:
        command.append("--offline")
    return command


def _clean_environment(root: Path) -> dict[str, str]:
    # Only OS/runtime necessities; do not forward credentials, PYTHONPATH, uv/pip overrides or settings.
    keep = (
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
    )
    return {
        **{key: os.environ[key] for key in keep if key in os.environ},
        "UV_PROJECT_ENVIRONMENT": str(root / ".runtime"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run(command: list[str], *, root: Path) -> str:
    result = subprocess.run(
        command,
        cwd=root,
        env=_clean_environment(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise ValueError(
            f"locked environment command failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def runtime_python(root: Path) -> Path:
    return root / ".runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_site(root: Path, manifest: dict[str, Any]) -> Path:
    return (
        root
        / ".runtime"
        / (
            "Lib/site-packages"
            if os.name == "nt"
            else f"lib/python{'.'.join(manifest['environment']['python'].split('.')[:2])}/site-packages"
        )
    )


def runtime_inventory(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Bind installed metadata and bytes, even when a missing module has intact dist-info."""
    locked = tomllib.loads((root / "code/uv.lock").read_text(encoding="utf-8"))
    allowed: dict[str, set[str]] = {}
    for package in locked["package"]:
        allowed.setdefault(package["name"].lower().replace("_", "-"), set()).add(package["version"])
    packages = {}
    files = {}
    for distribution in importlib.metadata.distributions(path=[str(runtime_site(root, manifest))]):
        name = str(distribution.metadata["Name"]).lower().replace("_", "-")
        if distribution.version not in allowed.get(name, set()):
            raise ValueError(
                f"installed dependency version not in frozen lock: {name}=={distribution.version}"
            )
        packages[name] = distribution.version
        if not distribution.files:
            raise ValueError(f"installed dependency file inventory missing: {name}")
        for item in distribution.files:
            path = Path(str(distribution.locate_file(item))).resolve()
            if not path.is_relative_to((root / ".runtime").resolve()) or not path.is_file():
                raise ValueError(
                    f"installed dependency file missing or outside runtime: {name}/{item}"
                )
            files[path.relative_to((root / ".runtime").resolve()).as_posix()] = digest(path)
    if not packages:
        raise ValueError("snapshot dependencies are missing")
    return {"snapshot_hash": manifest["snapshot_hash"], "packages": packages, "files": files}


def prepare_runtime(
    root: Path, *, uv: str, offline: bool = False, python: str | None = None
) -> None:
    manifest = verify_snapshot(root, check_data=False)
    inside(root, ".runtime")
    for key in ("implementation", "system", "machine", "byteorder"):
        if manifest["environment"][key] != environment()[key]:
            raise ValueError(f"unsupported environment: {key}; no version/platform fallback")
    if python:
        _run(
            [
                python,
                "-I",
                "-S",
                "-c",
                _python_environment_check(manifest),
            ],
            root=root,
        )
    _run(_uv_command(root, manifest, uv, check=False, offline=offline, python=python), root=root)
    _check_python(root, manifest)
    receipt = root / ".runtime/environment.json"
    receipt.write_text(
        json.dumps(runtime_inventory(root, manifest), sort_keys=True), encoding="utf-8"
    )


def _check_python(root: Path, manifest: dict[str, Any]) -> None:
    inside(root, ".runtime")
    python = runtime_python(root)
    if not python.is_file():
        raise ValueError("snapshot dependency environment missing; run prepare first")
    _run(
        [
            str(python),
            "-I",
            "-S",
            "-c",
            _python_environment_check(manifest),
        ],
        root=root,
    )


def _python_environment_check(manifest: dict[str, Any]) -> str:
    expected = {
        key: manifest["environment"][key]
        for key in ("python", "implementation", "system", "machine", "byteorder")
    }
    return (
        "import platform,sys; "
        "actual = dict(python=platform.python_version(), implementation=platform.python_implementation(), "
        "system=platform.system(), machine=platform.machine(), byteorder=sys.byteorder); "
        f"assert actual == {expected!r}, 'exact Python version required; interpreter/platform mismatch'"
    )


def execute_snapshot(
    root: Path,
    *,
    action: str,
    data_root: Path | None,
    run_id: str | None,
    reason: str | None,
    uv: str,
    cache: Path | None = None,
    output: Path | None = None,
    external_data: bool = False,
) -> None:
    data_root = data_root.resolve() if data_root else None
    manifest = verify_snapshot(root, data_root=data_root, check_data=action != "read-report")
    if action == "read-report":
        if manifest["consumption"] == "COMPLETE":
            report = root / "evidence/original_report"
            expected_hash = manifest["original_report_artifact_set_hash"]
        else:
            completion = read(root / "state" / f"{manifest['purpose']}_complete.json")
            report = inside(root, completion["output"])
            expected_hash = completion["artifact_set_hash"]
        published = read(report / "report_manifest.json")
        if (
            identity(published["files"]) != expected_hash
            or published["artifact_set_hash"] != expected_hash
        ):
            raise ValueError("original report binding mismatch")
        for name, expected in published["files"].items():
            if digest(inside(report, name)) != expected:
                raise ValueError("original published report changed")
        summary = read(report / "summary.json")
        if identity(summary) != published["summary_hash"]:
            raise ValueError("original published summary changed")
        print(json.dumps(summary, ensure_ascii=False))
        return
    if action == "attach-validation":
        if not cache or not output or not reason:
            raise ValueError("data attachment requires --cache, --output and --reason")
    elif action != "read-report":
        if not run_id or not reason or not run_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("execution requires a safe --run-id and explicit --reason")
        if (
            action == "reproduce"
            and manifest["consumption"] != "COMPLETE"
            and not (root / "state" / f"{manifest['purpose']}_complete.json").exists()
        ):
            raise ValueError("no completed original report to reproduce; use validate-once")
        if action == "validate-once" and manifest["consumption"] != "NOT_CONSUMED_AT_CAPTURE":
            raise ValueError(
                "OOS already consumed; only read-report or registered reproduce is allowed"
            )
    _check_python(root, manifest)
    for key in ("implementation", "system", "machine", "byteorder"):
        if manifest["environment"][key] != environment()[key]:
            raise ValueError(f"unsupported environment: {key}; no platform fallback")
    _run(_uv_command(root, manifest, uv, check=True, offline=True), root=root)
    receipt = root / ".runtime/environment.json"
    if not receipt.is_file():
        raise ValueError("dependency environment receipt missing; run prepare")
    expected_runtime = read(receipt)
    actual_runtime = runtime_inventory(root, manifest)
    if expected_runtime != actual_runtime:
        changed = sorted(
            name
            for name in set(expected_runtime["files"]) | set(actual_runtime["files"])
            if expected_runtime["files"].get(name) != actual_runtime["files"].get(name)
        )
        raise ValueError(
            f"installed dependency files changed or missing: {', '.join(changed[:3])}; restore with prepare"
        )
    # -I ignores PYTHONPATH/cwd, -S avoids sitecustomize and editable .pth files.
    site = runtime_site(root, manifest)
    arguments = {
        "action": action,
        "data_root": str(data_root) if data_root else None,
        "run_id": run_id,
        "reason": reason,
        "cache": str(cache.resolve()) if cache else None,
        "output": str(output.resolve()) if output else None,
        "external_data": external_data,
    }
    bootstrap = (
        "import sys; "
        f"sys.path[:0] = [{str(root / 'code')!r}, {str(site)!r}]; "
        "from backtest.research_snapshot import run_snapshot; "
        f"run_snapshot({str(root)!r}, {arguments!r})"
    )
    stdout = _run([str(runtime_python(root)), "-I", "-S", "-B", "-c", bootstrap], root=root)
    if stdout:
        print(stdout, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    create.add_argument("--freeze", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--historical-cache", type=Path)
    create.add_argument("--training-report", type=Path)
    create.add_argument("--purpose", choices=PURPOSES, default="historical_oos")
    create.add_argument("--validation-cache", type=Path)
    create.add_argument("--validation-report", type=Path)
    create.add_argument("--external-data", action="store_true")
    for action in (
        "verify",
        "prepare",
        "read-report",
        "validate-once",
        "reproduce",
        "attach-validation",
    ):
        command = commands.add_parser(action)
        command.add_argument("--snapshot", type=Path, default=Path(__file__).resolve().parent)
        command.add_argument("--data-root", type=Path)
        command.add_argument("--uv", default="uv")
        if action == "prepare":
            command.add_argument("--offline", action="store_true")
            command.add_argument(
                "--python", help="explicit interpreter; exact archived version required"
            )
        if action in {"validate-once", "reproduce"}:
            command.add_argument("--run-id", required=True)
            command.add_argument("--reason", required=True)
        if action == "attach-validation":
            command.add_argument("--cache", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--external-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            if Path(__file__).name == "snapshot.py":
                raise ValueError(
                    "archive launcher cannot create a new cohort; use project creation entry"
                )
            # Direct script execution needs the explicit project root, never ambient PYTHONPATH.
            sys.path.insert(0, str(args.source_root.resolve()))
            result = create_snapshot(
                args.output.resolve(),
                args.freeze.resolve(),
                source_root=args.source_root.resolve(),
                historical_cache=args.historical_cache,
                training_report=args.training_report,
                purpose=args.purpose,
                validation_cache=args.validation_cache,
                validation_report=args.validation_report,
                external_data=args.external_data,
            )
            print(
                json.dumps(
                    {
                        "snapshot_hash": result["snapshot_hash"],
                        "classification": result["capture_classification"],
                    }
                )
            )
        else:
            root = args.snapshot.resolve()
            if args.command == "verify":
                result = verify_snapshot(root, data_root=args.data_root)
                print(json.dumps({"verified": True, "snapshot_hash": result["snapshot_hash"]}))
            elif args.command == "prepare":
                prepare_runtime(root, uv=args.uv, offline=args.offline, python=args.python)
                print(json.dumps({"prepared": True, "python": str(runtime_python(root))}))
            else:
                execute_snapshot(
                    root,
                    action=args.command,
                    data_root=args.data_root,
                    run_id=getattr(args, "run_id", None),
                    reason=getattr(args, "reason", None),
                    uv=args.uv,
                    cache=getattr(args, "cache", None),
                    output=getattr(args, "output", None),
                    external_data=getattr(args, "external_data", False),
                )
                print(
                    json.dumps(
                        {
                            "completed": True,
                            "action": args.command,
                            "run_id": getattr(args, "run_id", None),
                        }
                    )
                )
    except (ValueError, OSError, KeyError) as exc:
        print(f"research snapshot failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
