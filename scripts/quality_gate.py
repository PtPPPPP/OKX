"""Single authoritative quality gate for the OKX demo/backtest framework.

Runs, in order: FORMAT, LINT, TYPE, TEST, SECURITY_SMOKE — each via the
official tool, never reimplemented here. Verification only: this script never
formats, migrates, deletes, installs, or touches the network.

Usage:
    uv run python scripts/quality_gate.py [--skip-slow]
Exit code: 0 iff every gate passes (fail-fast per gate, summary at the end).
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

SECURITY_SMOKE_TESTS = (
    "tests/test_cli_security.py",
    "tests/test_demo_write_boundary.py",
    "tests/test_private_state_submission_fence.py",
    "tests/test_database_migration_workflow.py",
    "tests/test_replay_durability_boundary.py",
    "tests/test_shadow_replay_convergence.py",
    "tests/test_shadow_replay_connection_lifecycle.py",
    "tests/test_settings.py",
    "tests/test_fresh_clone_self_containment.py",
)


def _run(name: str, command: list[str]) -> bool:
    print(f"\n=== {name}: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=_REPO_ROOT)
    status = "PASS" if completed.returncode == 0 else "FAIL"
    print(f"=== {name}: {status}", flush=True)
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full quality gate")
    parser.add_argument(
        "--skip-slow", action="store_true", help="skip the default-test gate (smoke only)"
    )
    args = parser.parse_args()

    results: dict[str, bool] = {
        "FORMAT": _run("FORMAT", ["uv", "run", "ruff", "format", "--check", "."]),
        "LINT": _run("LINT", ["uv", "run", "ruff", "check", "."]),
        "TYPE": _run("TYPE", ["uv", "run", "mypy"]),
    }
    if not args.skip_slow:
        results["TEST"] = _run("TEST", ["uv", "run", "pytest", "-q"])
    results["SECURITY_SMOKE"] = _run(
        "SECURITY_SMOKE", ["uv", "run", "pytest", "-q", *SECURITY_SMOKE_TESTS]
    )

    print("\n=== QUALITY GATE SUMMARY", flush=True)
    for gate, passed in results.items():
        print(f"  {gate}: {'PASS' if passed else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
