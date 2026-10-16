"""Content-bound research datasets and immutable, honestly dated candidate freezes."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.market import Candle
from app.market.historical_data import normalize_candles
from app.reproducibility import canonical_hash
from backtest.strategy_v3_candidates import load_v3_specs
from backtest.vwap_signal_edge_data import load_normalized_candles
from scripts.research_snapshot import environment, source_files

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Read compatibility only. New freezes use directory-based source_files().
SOURCE_FILES = (
    "app/config/run_config.py",
    "app/domain/market.py",
    "app/domain/order.py",
    "app/domain/position.py",
    "app/domain/context.py",
    "app/domain/signal.py",
    "app/domain/events.py",
    "app/domain/risk.py",
    "app/config/settings.py",
    "app/reproducibility.py",
    "app/strategies/base.py",
    "app/strategies/buy_and_hold.py",
    "app/strategies/registry.py",
    "app/execution/base.py",
    "app/execution/order_manager.py",
    "app/position_sizing/base.py",
    "app/execution/backtest_broker.py",
    "app/trading_engine.py",
    "app/risk/risk_manager.py",
    "app/position_sizing/fixed_notional.py",
    "app/runtime/clock.py",
    "app/market/historical_data.py",
    "app/market/network.py",
    "backtest/engine.py",
    "backtest/metrics.py",
    "backtest/report.py",
    "backtest/strategy_v3_candidates.py",
    "backtest/strategy_v3_features.py",
    "backtest/research_governance.py",
    "backtest/research_execution.py",
    "backtest/vwap_signal_edge_data.py",
    "scripts/run_research_cycle.py",
    "configs/research/strategy_v3_candidate_specs.json",
    "configs/snapshots/BTC-USDT.json",
    "uv.lock",
)
EXECUTION_POLICY: dict[str, Any] = {
    "model": "ohlc_limit_v2",
    "instrument": "BTC-USDT",
    "bar": "1h",
    "initial_capital": "10000",
    "order_notional": "20",
    "maximum_exposure": "100",
    "fee_rate": "0.001",
    "slippage_rate": "0.0005",
    "volume_participation": "1",
    "stop_loss_pct": "1",
    "take_profit_pct": "2",
    "maximum_hold_hours": 24,
    "cost_multipliers": [1, 2],
    "minimum_closed_trades": 100,
    "minimum_positive_year_fraction": "0.6",
    "prospective_validation_days": 30,
    "entry_semantics": "false-to-true episode; next-bar limit at signal close",
}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON research artifact must be an object")
    return payload


def _bound_path(value: str | Path, source_root: Path, directory: str) -> Path:
    path = Path(value)
    path = (source_root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to((source_root / directory).resolve()):
        raise ValueError("frozen data/report path escapes the research workspace")
    return path


def write_json_once(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create is the publication fence: a frozen artifact is never replaced.
    with path.open("x", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)


def validated_dataset(cache: Path) -> tuple[list[Candle], dict[str, Any]]:
    manifest = read_json(cache / "data_manifest.json")
    if (
        manifest.get("status") != "complete"
        or manifest.get("source") != "OKX_PUBLIC_REST_HISTORY_CANDLES"
    ):
        raise ValueError("research dataset is incomplete or not public OKX history")
    if manifest.get("instrument") != "BTC-USDT" or manifest.get("timeframe") != "1h":
        raise ValueError("research cycle supports BTC-USDT 1H only")
    for field in (
        "duplicate_count",
        "missing_count",
        "invalid_ohlc_count",
        "out_of_order_count",
        "unconfirmed_count",
    ):
        if manifest.get(field) != 0:
            raise ValueError(f"data quality gate failed: {field}")
    if manifest.get("confirmed_candle_only") is not True:
        raise ValueError("research requires confirmed candles")
    path = cache / "normalized" / "candles.csv"
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    canonical = []
    for row in rows:
        stamp = datetime.fromisoformat(row["timestamp"])
        fetched = datetime.fromisoformat(row["downloaded_at"])
        if (
            stamp.tzinfo is None
            or fetched.tzinfo is None
            or stamp.microsecond
            or int(stamp.timestamp()) % 3600
        ):
            raise ValueError("research timestamps must be UTC-aware, hourly aligned")
        if fetched < stamp + timedelta(hours=1):
            raise ValueError("confirmed candle was purportedly fetched before its close")
        if (
            row["instrument"] != "BTC-USDT"
            or row["bar"] != "1h"
            or row["source"] != manifest["source"]
        ):
            raise ValueError("candle source identity mismatch")
        canonical.append(
            {
                "timestamp_ms": int(stamp.timestamp() * 1000),
                **{
                    name: row[name]
                    for name in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "volume_ccy",
                        "volume_quote",
                        "confirm",
                    )
                },
            }
        )
    if canonical_hash(canonical) != manifest.get("dataset_hash"):
        raise ValueError("normalized dataset hash mismatch")
    candles = normalize_candles(load_normalized_candles(path, bar="1h"), bar="1h")
    if not candles or len(candles) != manifest.get("normalized_rows"):
        raise ValueError("dataset row count mismatch")
    start, end = (
        datetime.fromisoformat(manifest["requested_start"]),
        datetime.fromisoformat(manifest["requested_end"]),
    )
    if candles[0].timestamp != start or candles[-1].timestamp != end:
        raise ValueError("dataset does not cover its complete requested range")
    if (
        manifest.get("actual_start") != start.isoformat()
        or manifest.get("actual_end") != end.isoformat()
    ):
        raise ValueError("dataset actual range mismatch")
    if any(row["confirm"] != "1" for row in rows):
        raise ValueError("unconfirmed candle")
    generated = datetime.fromisoformat(str(manifest["generated_at"]))
    if generated.tzinfo is None or generated > datetime.now(UTC) + timedelta(seconds=60):
        raise ValueError("dataset generation time is missing a timezone or is in the future")
    if any(datetime.fromisoformat(row["downloaded_at"]) > generated for row in rows):
        raise ValueError("candle receipt is newer than its dataset manifest")
    if generated.tzinfo is None or end + timedelta(hours=1) > generated:
        raise ValueError("dataset includes future/unclosed data")
    return candles, manifest


def freeze_cohort(
    path: Path,
    *,
    historical_cache: Path,
    training_summary: Path,
    now: datetime | None = None,
    source_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    historical_cache = _bound_path(historical_cache, source_root, "data/research")
    training_summary = _bound_path(training_summary, source_root, "artifacts")
    _, manifest = validated_dataset(historical_cache)
    training = read_json(training_summary)
    verify_report(training_summary.parent)
    if (
        training.get("phase") != "historical_development"
        or training.get("dataset_hash") != manifest["dataset_hash"]
    ):
        raise ValueError("training report/data binding mismatch")
    specs_path = source_root / "configs/research/strategy_v3_candidate_specs.json"
    specs = read_json(specs_path)
    last = datetime.fromisoformat(str(manifest["actual_end"]))
    boundary = datetime.fromisoformat(str(specs["prospective_oos_start"]))
    if last + timedelta(hours=1) != boundary or last > datetime.fromisoformat(
        str(specs["research_cutoff"])
    ):
        raise ValueError("historical cutoff mismatch")
    if now < last + timedelta(hours=1):
        raise ValueError("freeze cannot precede the training data")
    primary = [variant for variant in load_v3_specs(specs_path) if variant.primary]
    if len(primary) != len(specs["candidates"]) or len(
        {variant.candidate_id for variant in primary}
    ) != len(primary):
        raise ValueError("each frozen candidate must have exactly one primary variant")
    locked = {str(row["candidate_id"]): row for row in training["candidate_results"]}
    if set(locked) != {variant.candidate_id for variant in primary}:
        raise ValueError("training report must cover the predeclared cohort")
    selected = training.get("selected_candidate")
    if selected is not None and selected not in {
        identifier for identifier, row in locked.items() if row["qualified"] is True
    }:
        raise ValueError("selected candidate did not qualify on training data")
    candidates = []
    for variant in primary:
        if not isinstance(locked[variant.candidate_id]["qualified"], bool):
            raise ValueError("training qualification must be a boolean")
        candidates.append(
            {
                "candidate_id": variant.candidate_id,
                "variant_id": variant.variant_id,
                "parameters": variant.parameters,
                "hypothesis": variant.hypothesis,
                "training_qualified": bool(locked[variant.candidate_id]["qualified"]),
                "training_rejection_reasons": locked[variant.candidate_id]["rejection_reasons"],
            }
        )
    payload = {
        "schema_version": 2,
        "environment": environment(),
        "frozen_at": now.isoformat(),
        "historical_oos_start": boundary.isoformat(),
        "historical_oos_end": (
            now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        ).isoformat(),
        "prospective_start": (
            now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        ).isoformat(),
        "prospective_end": (
            now.replace(minute=0, second=0, microsecond=0)
            + timedelta(days=int(EXECUTION_POLICY["prospective_validation_days"]))
        ).isoformat(),
        "historical_oos_classification": "RETROSPECTIVE_AFTER_FREEZE_ACCESS",
        "legacy_freeze_timestamp_proven": False,
        "selected_candidate": training.get("selected_candidate"),
        "candidates": candidates,
        "execution_policy": EXECUTION_POLICY,
        "historical_cache": historical_cache.relative_to(source_root.resolve()).as_posix(),
        "historical_csv_sha256": file_hash(historical_cache / "normalized/candles.csv"),
        "historical_manifest_sha256": file_hash(historical_cache / "data_manifest.json"),
        "training_summary": training_summary.relative_to(source_root.resolve()).as_posix(),
        "training_summary_sha256": file_hash(training_summary),
        "training_artifact_set_hash": read_json(training_summary.parent / "report_manifest.json")[
            "artifact_set_hash"
        ],
        "dataset_hash": manifest["dataset_hash"],
        "source_hashes": {
            name: file_hash(source_root / name) for name in source_files(source_root)
        },
        "oos_cannot_promote_training_rejects": True,
        "trading_permission": False,
    }
    payload["freeze_hash"] = canonical_hash(payload)
    write_json_once(path, payload)
    return payload


def verify_report(directory: Path, *, expected_hash: str | None = None) -> dict[str, Any]:
    manifest = read_json(directory / "report_manifest.json")
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or not files
        or canonical_hash(files) != manifest.get("artifact_set_hash")
    ):
        raise ValueError("research report manifest mismatch")
    if expected_hash is not None and manifest["artifact_set_hash"] != expected_hash:
        raise ValueError("published research report changed")
    for name, digest in files.items():
        target = (directory / name).resolve()
        if not target.is_relative_to(directory.resolve()) or file_hash(target) != digest:
            raise ValueError("research report artifact changed")
    summary = read_json(directory / "summary.json")
    if canonical_hash(summary) != manifest.get("summary_hash"):
        raise ValueError("research report summary changed")
    return summary


def check_freeze_hash(payload: dict[str, Any]) -> None:
    content = {key: value for key, value in payload.items() if key != "freeze_hash"}
    if canonical_hash(content) != payload.get("freeze_hash"):
        raise ValueError("candidate freeze hash mismatch")


def verify_freeze(path: Path, *, source_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    payload = read_json(path)
    check_freeze_hash(payload)
    if payload.get("schema_version") not in {1, 2}:
        raise ValueError("unsupported research freeze version")
    if payload["schema_version"] == 2 and payload.get("environment") != environment():
        raise ValueError("frozen Python/platform environment mismatch")
    if payload.get("execution_policy") != EXECUTION_POLICY:
        raise ValueError("frozen execution policy mismatch")
    hashes = payload.get("source_hashes")
    expected = source_files(source_root) if payload.get("schema_version") == 2 else SOURCE_FILES
    if not isinstance(hashes, dict) or set(hashes) != set(expected):
        raise ValueError("frozen source allowlist mismatch")
    if any(file_hash(source_root / name) != hashes[name] for name in expected):
        raise ValueError("research source changed after freeze")
    cache = _bound_path(payload["historical_cache"], source_root, "data/research")
    summary_path = _bound_path(payload["training_summary"], source_root, "artifacts")
    verify_freeze_artifacts(payload, cache, summary_path)
    return payload


def verify_freeze_artifacts(payload: dict[str, Any], cache: Path, summary_path: Path) -> None:
    """Check immutable evidence using explicit relocated paths, without rewriting the freeze."""
    if (
        file_hash(cache / "normalized/candles.csv") != payload["historical_csv_sha256"]
        or file_hash(cache / "data_manifest.json") != payload["historical_manifest_sha256"]
    ):
        raise ValueError("historical dataset changed after freeze")
    if file_hash(summary_path) != payload["training_summary_sha256"]:
        raise ValueError("training decision changed after freeze")
    verify_report(summary_path.parent, expected_hash=payload["training_artifact_set_hash"])


def validation_dataset(
    freeze_path: Path,
    cache: Path,
    *,
    purpose: str,
    source_root: Path = PROJECT_ROOT,
) -> tuple[list[Candle], dict[str, Any], dict[str, Any]]:
    freeze = verify_freeze(freeze_path, source_root=source_root)
    candles, manifest = validate_oos_data(freeze, cache, purpose=purpose)
    return candles, manifest, freeze


def validate_oos_data(
    freeze: dict[str, Any], cache: Path, *, purpose: str
) -> tuple[list[Candle], dict[str, Any]]:
    if purpose not in {"historical_oos", "prospective"}:
        raise ValueError("validation purpose must be explicit")
    candles, manifest = validated_dataset(cache)
    first = candles[0].timestamp
    if purpose == "historical_oos" and (
        first != datetime.fromisoformat(freeze["historical_oos_start"])
        or candles[-1].timestamp != datetime.fromisoformat(freeze["historical_oos_end"])
    ):
        raise ValueError("historical OOS must cover the complete frozen boundary interval")
    if purpose == "prospective" and (
        first != datetime.fromisoformat(freeze["prospective_start"])
        or candles[-1].timestamp != datetime.fromisoformat(freeze["prospective_end"])
    ):
        raise ValueError("prospective validation must cover its complete predeclared window")
    with (cache / "normalized/candles.csv").open(encoding="utf-8", newline="") as file:
        if any(
            datetime.fromisoformat(row["downloaded_at"])
            < datetime.fromisoformat(freeze["frozen_at"])
            for row in csv.DictReader(file)
        ):
            raise ValueError("OOS was accessed before candidate freeze")
    return candles, manifest
