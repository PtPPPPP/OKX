"""Public-data collection, historical report rebuild, immutable freeze and OOS validation.

No account, private API, production database, or exchange write command is used.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.market.network import NetworkConfiguration, NetworkMode
from backtest.research_execution import historical_execution, independent_validation
from backtest.research_governance import (
    PROJECT_ROOT,
    freeze_cohort,
    verify_freeze,
    write_json_once,
)
from backtest.vwap_signal_edge_data import HistoricalCandleCache, OKXHistoricalCandleDownloader


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("research timestamps must include UTC offset")
    stamp = stamp.astimezone(UTC)
    if stamp.minute or stamp.second or stamp.microsecond:
        raise ValueError("1H data boundaries must be aligned to whole hours")
    return stamp


def _network(mode: str) -> NetworkConfiguration:
    if mode == "direct":
        return NetworkConfiguration(mode=NetworkMode.DIRECT)
    configuration = NetworkConfiguration.from_environment()
    if configuration.mode is not NetworkMode.PROXY:
        raise ValueError("proxy mode requires explicit OKX_NETWORK_MODE/OKX_PROXY_URL")
    return configuration


def _download(
    cache: Path, start: datetime, end: datetime, mode: str, maximum_batches: int
) -> dict[str, Any]:
    downloader = OKXHistoricalCandleDownloader(
        HistoricalCandleCache(cache),
        network=_network(mode),
        maximum_batches=maximum_batches,
        request_interval_seconds=0.3,
    )
    try:
        result = downloader.download(instrument="BTC-USDT", bar="1h", start=start, end=end)
    finally:
        downloader.close()
    return asdict(result)


def _public_now(mode: str) -> datetime:
    with _network(mode).create_http_client(timeout=httpx.Timeout(15, connect=10)) as client:
        response = client.get(
            "https://www.okx.com/api/v5/public/time", headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise ValueError("public exchange time unavailable")
    return datetime.fromtimestamp(int(payload["data"][0]["ts"]) / 1000, tz=UTC)


def _rebuild(cache: Path, output_root: Path) -> dict[str, Any]:
    if (output_root / "rebuild_index.json").exists():
        raise ValueError("rebuild index already exists; choose a new output root")
    stages: dict[str, Any] = {}
    common = ["--cache", str(cache), "--output-root", str(output_root)]

    def execute(module: str, extra: list[str]) -> Path:
        environment = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        executable = (
            str(getattr(sys, "_base_executable", sys.executable))
            if os.name == "nt"
            else sys.executable
        )
        result = subprocess.run(
            [executable, "-m", f"scripts.{module}", *common, *extra],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode:
            raise RuntimeError(f"{module} failed: {result.stderr[-2000:]}")
        payload = json.loads(result.stdout)
        stages[module] = payload
        print(json.dumps({"stage_completed": module, **payload}), flush=True)
        return Path(payload["output"])

    episode = execute("run_vwap_episode_research", [])
    fixed = execute("run_vwap_fixed_exit_research", ["--episode-artifact", str(episode)])
    walk = execute(
        "run_vwap_walk_forward_research",
        [
            "--episode-artifact",
            str(episode),
            "--fixed-exit-artifact",
            str(fixed),
        ],
    )
    v2 = execute(
        "run_strategy_v2_research",
        [
            "--vwap-episode-artifact",
            str(episode),
            "--vwap-fixed-artifact",
            str(fixed),
            "--vwap-walk-forward-artifact",
            str(walk),
        ],
    )
    v3 = execute(
        "run_strategy_v3_research",
        [
            "--vwap-episode-artifact",
            str(episode),
            "--vwap-walk-forward-artifact",
            str(walk),
            "--strategy-v2-artifact",
            str(v2),
        ],
    )
    payload = {
        "rebuild_type": "REGENERATED_NOT_RECOVERED_ORIGINALS",
        "stages": stages,
        "v3_artifact": str(v3),
        "public_or_offline_only": True,
    }
    write_json_once(output_root / "rebuild_index.json", payload)
    return payload


def main() -> int:
    snapshot_actions = {
        "create",
        "verify",
        "prepare",
        "read-report",
        "validate-once",
        "reproduce",
        "attach-validation",
    }
    if len(sys.argv) > 1 and sys.argv[1].removeprefix("snapshot-") in snapshot_actions:
        from scripts.research_snapshot import main as snapshot_main

        return snapshot_main([sys.argv[1].removeprefix("snapshot-"), *sys.argv[2:]])
    parser = argparse.ArgumentParser(description="Governed BTC-USDT 1H research cycle")
    commands = parser.add_subparsers(dest="command", required=True)
    for action in sorted(snapshot_actions):
        commands.add_parser(f"snapshot-{action}", help=f"research snapshot: {action}")
    collect = commands.add_parser("download-history")
    collect.add_argument("--cache", type=Path, required=True)
    collect.add_argument(
        "--start", type=_timestamp, default=_timestamp("2023-08-13T00:00:00+00:00")
    )
    collect.add_argument("--end", type=_timestamp, default=_timestamp("2026-08-12T23:00:00+00:00"))
    rebuild = commands.add_parser("rebuild-reports")
    rebuild.add_argument("--cache", type=Path, required=True)
    rebuild.add_argument("--output-root", type=Path, required=True)
    historical = commands.add_parser("historical-execution")
    historical.add_argument("--cache", type=Path, required=True)
    historical.add_argument("--legacy-v3-artifact", type=Path, required=True)
    historical.add_argument("--output", type=Path, required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--historical-cache", type=Path, required=True)
    freeze.add_argument("--training-summary", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    oos = commands.add_parser("download-oos")
    oos.add_argument("--freeze", type=Path, required=True)
    oos.add_argument("--cache", type=Path, required=True)
    oos.add_argument("--purpose", choices=["historical_oos", "prospective"], required=True)
    oos.add_argument("--end", type=_timestamp)
    validate = commands.add_parser("validate")
    validate.add_argument("--freeze", type=Path, required=True)
    validate.add_argument("--cache", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument("--purpose", choices=["historical_oos", "prospective"], required=True)
    for command in (collect, oos):
        command.add_argument("--network-mode", choices=["direct", "proxy"], default="direct")
        command.add_argument("--maximum-batches", type=int, default=128)
    args = parser.parse_args()
    payload: dict[str, Any]
    if args.command == "download-history":
        payload = _download(
            args.cache, args.start, args.end, args.network_mode, args.maximum_batches
        )
    elif args.command == "rebuild-reports":
        payload = _rebuild(args.cache, args.output_root)
    elif args.command == "historical-execution":
        payload = historical_execution(
            args.cache, args.output, legacy_v3_artifact=args.legacy_v3_artifact
        )
    elif args.command == "freeze":
        payload = freeze_cohort(
            args.output,
            historical_cache=args.historical_cache,
            training_summary=args.training_summary,
        )
    elif args.command == "validate":
        payload = independent_validation(args.freeze, args.cache, args.output, purpose=args.purpose)
    else:
        frozen = verify_freeze(args.freeze)
        latest = _public_now(args.network_mode).replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=1)
        start = _timestamp(
            frozen["historical_oos_start"]
            if args.purpose == "historical_oos"
            else frozen["prospective_start"]
        )
        end = args.end or (
            _timestamp(frozen["historical_oos_end"])
            if args.purpose == "historical_oos"
            else _timestamp(frozen["prospective_end"])
        )
        if args.purpose == "historical_oos" and end != _timestamp(frozen["historical_oos_end"]):
            raise ValueError("cannot change the historical OOS window after freezing")
        if args.purpose == "prospective" and end != _timestamp(frozen["prospective_end"]):
            raise ValueError("cannot change the prospective window after freezing")
        if end > latest:
            payload = {
                "status": "PENDING_FUTURE_WINDOW",
                "freeze_hash": frozen["freeze_hash"],
                "purpose": args.purpose,
                "planned_start": start.isoformat(),
                "planned_end": end.isoformat(),
                "latest_closed": latest.isoformat(),
                "ready_after_utc": (end + timedelta(hours=1)).isoformat(),
                "validation_executed": False,
            }
            pending = args.freeze.parent / "prospective_pending.json"
            if not pending.exists():
                write_json_once(pending, payload)
        else:
            if start >= end:
                raise ValueError("no complete OOS interval available")
            payload = _download(args.cache, start, end, args.network_mode, args.maximum_batches)
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 2 if payload.get("status") == "quality_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
