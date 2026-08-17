"""Run the default pytest collection in independently validated sequential shards."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ShardResult:
    name: str
    collected: int
    passed: int
    failed: int
    skipped: int
    xfailed: int
    exit_code: int


def _pytest(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )


def _node_ids(paths: list[str]) -> list[str]:
    result = _pytest(["-o", "addopts=", "--collect-only", "-q", *paths])
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    return [line for line in result.stdout.splitlines() if "::" in line]


def _test_files(node_ids: list[str]) -> list[str]:
    return sorted({node_id.split("::", 1)[0] for node_id in node_ids})


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _plan(path: Path, files_per_shard: int) -> None:
    node_ids = _node_ids([])
    files = _test_files(node_ids)
    shards = [
        files[index : index + files_per_shard] for index in range(0, len(files), files_per_shard)
    ]
    _write_json(
        path,
        {
            "default_node_ids": node_ids,
            "shards": shards,
        },
    )
    print(json.dumps({"collected": len(node_ids), "shards": len(shards)}, sort_keys=True))


def _summary_count(output: str, name: str) -> int:
    match = re.search(rf"(\d+) {name}", output)
    return int(match.group(1)) if match else 0


def _run(path: Path, shard_index: int) -> None:
    plan = json.loads(path.read_text(encoding="utf-8"))
    files = plan["shards"][shard_index]
    expected = sorted(
        node_id for node_id in plan["default_node_ids"] if node_id.split("::", 1)[0] in files
    )
    actual = sorted(_node_ids(files))
    if actual != expected:
        raise RuntimeError("shard collection differs from the default test collection")
    result = _pytest(["-o", "addopts=", "-q", *files])
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    summary = ShardResult(
        name=f"shard-{shard_index + 1}",
        collected=len(actual),
        passed=_summary_count(result.stdout, "passed"),
        failed=_summary_count(result.stdout, "failed"),
        skipped=_summary_count(result.stdout, "skipped"),
        xfailed=_summary_count(result.stdout, "xfailed"),
        exit_code=result.returncode,
    )
    _write_json(path.with_name(f"{path.stem}.shard-{shard_index + 1}.json"), asdict(summary))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _verify(path: Path) -> None:
    plan = json.loads(path.read_text(encoding="utf-8"))
    expected = set(plan["default_node_ids"])
    observed: set[str] = set()
    reports: list[dict[str, int | str]] = []
    for index, files in enumerate(plan["shards"], start=1):
        observed.update(_node_ids(files))
        report = path.with_name(f"{path.stem}.shard-{index}.json")
        if not report.exists():
            raise RuntimeError(f"missing shard result: {report.name}")
        reports.append(json.loads(report.read_text(encoding="utf-8")))
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    failed = sum(int(report["failed"]) for report in reports)
    nonzero = [report["name"] for report in reports if int(report["exit_code"]) != 0]
    incomplete = [
        str(report["name"])
        for report in reports
        if int(report["passed"])
        + int(report["failed"])
        + int(report["skipped"])
        + int(report["xfailed"])
        != int(report["collected"])
    ]
    summary = {
        "default_collected_tests": len(expected),
        "sharded_collected_tests": len(observed),
        "missing_node_ids": len(missing),
        "unexpected_node_ids": len(unexpected),
        "total_passed": sum(int(report["passed"]) for report in reports),
        "total_failed": failed,
        "all_shards_exit_code_zero": not nonzero,
        "incomplete_shard_reports": incomplete,
    }
    _write_json(path.with_name(f"{path.stem}.summary.json"), summary)
    print(json.dumps(summary, sort_keys=True))
    if missing or unexpected or failed or nonzero or incomplete:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--files-per-shard", type=int, default=8)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--create-plan", action="store_true")
    action.add_argument("--run-shard", type=int)
    action.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.files_per_shard <= 0:
        raise SystemExit("files-per-shard must be positive")
    if args.create_plan:
        _plan(args.plan, args.files_per_shard)
    elif args.run_shard is not None:
        _run(args.plan, args.run_shard)
    else:
        _verify(args.plan)


if __name__ == "__main__":
    main()
