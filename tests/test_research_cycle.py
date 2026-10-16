from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.config.run_config import RunConfig
from app.reproducibility import canonical_hash
from backtest.research_execution import V3ExecutionStrategy, independent_validation
from backtest.research_governance import (
    PROJECT_ROOT,
    file_hash,
    freeze_cohort,
    read_json,
    validated_dataset,
    validation_dataset,
    verify_freeze,
)
from backtest.strategy_v3_candidates import load_v3_specs
from backtest.vwap_signal_edge_data import HistoricalCandleCache, _manifest, _parse_raw
from scripts.research_snapshot import source_files

OBSERVED = datetime(2026, 10, 14, 15, tzinfo=UTC)
FROZEN = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
OOS_START = datetime(2026, 8, 13, tzinfo=UTC)


def make_cache(
    directory: Path,
    start: datetime,
    count: int,
    *,
    fetched: datetime = OBSERVED,
    price_offset: int = 0,
) -> Path:
    rows = []
    for index in range(count):
        timestamp = start + timedelta(hours=index)
        price = str(100 + index % 3 + price_offset)
        rows.append(
            _parse_raw(
                [
                    str(int(timestamp.timestamp() * 1000)),
                    price,
                    str(int(price) + 1),
                    str(int(price) - 1),
                    price,
                    "10",
                    "1000",
                    "1000",
                    "1",
                ],
                instrument="BTC-USDT",
                bar="1h",
                downloaded_at=fetched.isoformat(),
            )
        )
    manifest = _manifest(
        rows,
        instrument="BTC-USDT",
        bar="1h",
        start=start,
        end=start + timedelta(hours=count - 1),
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


@pytest.fixture
def research_tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "workspace"
    for name in source_files(PROJECT_ROOT):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / name, target)
    cache = make_cache(root / "data/research/historical", OOS_START - timedelta(hours=120), 120)
    manifest = read_json(cache / "data_manifest.json")
    specs = read_json(root / "configs/research/strategy_v3_candidate_specs.json")
    summary = root / "artifacts/training/summary.json"
    summary.parent.mkdir(parents=True)
    payload = {
        "phase": "historical_development",
        "dataset_hash": manifest["dataset_hash"],
        "selected_candidate": None,
        "candidate_results": [
            {
                "candidate_id": item["candidate_id"],
                "qualified": False,
                "rejection_reasons": ["fixture_rejection"],
            }
            for item in specs["candidates"]
        ],
    }
    summary.write_text(json.dumps(payload), encoding="utf-8")
    files = {"summary.json": file_hash(summary)}
    (summary.parent / "report_manifest.json").write_text(
        json.dumps(
            {
                "summary_hash": canonical_hash(payload),
                "files": files,
                "artifact_set_hash": canonical_hash(files),
            }
        ),
        encoding="utf-8",
    )
    return {
        "root": root,
        "cache": cache,
        "summary": summary,
        "freeze": root / "artifacts/freeze/cohort.json",
    }


def freeze(tree: dict[str, Path]) -> dict[str, Any]:
    return freeze_cohort(
        tree["freeze"],
        historical_cache=tree["cache"],
        training_summary=tree["summary"],
        now=FROZEN,
        source_root=tree["root"],
    )


def test_large_csv_is_allowed_but_standard_okx_limit_is_preserved(tmp_path: Path) -> None:
    config = RunConfig.model_validate(
        {"data": {"source": "csv", "path": tmp_path / "local.csv", "limit": 26304}}
    )
    assert config.data.limit == 26304
    with pytest.raises(ValueError, match="1000"):
        RunConfig.model_validate({"data": {"source": "okx", "limit": 26304}})


def test_manifest_cannot_hide_missing_request_boundaries(research_tree: dict[str, Path]) -> None:
    tree = research_tree
    path = tree["cache"] / "data_manifest.json"
    manifest = read_json(path)
    manifest["requested_start"] = (
        datetime.fromisoformat(manifest["requested_start"]) - timedelta(hours=1)
    ).isoformat()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="complete requested range"):
        validated_dataset(tree["cache"])


def test_dataset_payload_tampering_is_detected(research_tree: dict[str, Path]) -> None:
    path = research_tree["cache"] / "normalized/candles.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(",100,101,99,100,", ",100,101,99,99.5,", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="dataset hash"):
        validated_dataset(research_tree["cache"])


def test_freeze_is_exclusive_and_historical_oos_is_honestly_classified(
    research_tree: dict[str, Path],
) -> None:
    tree = research_tree
    payload = freeze(tree)
    assert len(payload["candidates"]) == 5
    assert payload["selected_candidate"] is None
    assert payload["legacy_freeze_timestamp_proven"] is False
    assert payload["historical_oos_classification"] == "RETROSPECTIVE_AFTER_FREEZE_ACCESS"
    assert datetime.fromisoformat(payload["prospective_start"]) == FROZEN.replace(hour=13, minute=0)
    assert datetime.fromisoformat(payload["prospective_end"]) == FROZEN.replace(
        minute=0
    ) + timedelta(days=30)
    assert (
        verify_freeze(tree["freeze"], source_root=tree["root"])["freeze_hash"]
        == payload["freeze_hash"]
    )
    with pytest.raises(FileExistsError):
        freeze(tree)


@pytest.mark.parametrize("target", ["freeze", "code", "data", "training"])
def test_freeze_detects_tampering(research_tree: dict[str, Path], target: str) -> None:
    tree = research_tree
    freeze(tree)
    if target == "freeze":
        payload = read_json(tree["freeze"])
        payload["candidates"][0]["parameters"]["extra"] = 123
        tree["freeze"].write_text(json.dumps(payload), encoding="utf-8")
    else:
        path = (
            tree["root"] / "app/risk/risk_manager.py"
            if target == "code"
            else tree["cache"] / "normalized/candles.csv"
            if target == "data"
            else tree["summary"]
        )
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"changed|mismatch"):
        verify_freeze(tree["freeze"], source_root=tree["root"])


def test_historical_oos_rejects_overlap_and_access_before_freeze(
    research_tree: dict[str, Path],
) -> None:
    tree = research_tree
    freeze(tree)
    cache = make_cache(tree["root"] / "data/research/overlap", OOS_START - timedelta(hours=1), 24)
    with pytest.raises(ValueError, match="frozen boundary"):
        validation_dataset(
            tree["freeze"], cache, purpose="historical_oos", source_root=tree["root"]
        )
    old = make_cache(
        tree["root"] / "data/research/old_access",
        OOS_START,
        36,
        fetched=FROZEN - timedelta(minutes=10),
    )
    with pytest.raises(ValueError, match="accessed before"):
        validation_dataset(tree["freeze"], old, purpose="historical_oos", source_root=tree["root"])


def test_past_holdout_cannot_be_called_prospective(research_tree: dict[str, Path]) -> None:
    tree = research_tree
    freeze(tree)
    cache = make_cache(tree["root"] / "data/research/oos", OOS_START, 36)
    with pytest.raises(ValueError, match="complete predeclared window"):
        validation_dataset(tree["freeze"], cache, purpose="prospective", source_root=tree["root"])


def test_complete_post_freeze_window_is_required_and_accepted(
    research_tree: dict[str, Path],
) -> None:
    tree = research_tree
    payload = freeze(tree)
    start = datetime.fromisoformat(payload["prospective_start"])
    short = make_cache(tree["root"] / "data/research/short", start, 24)
    with pytest.raises(ValueError, match="complete predeclared window"):
        validation_dataset(tree["freeze"], short, purpose="prospective", source_root=tree["root"])
    full = make_cache(tree["root"] / "data/research/full", start, 720)
    bars, _, _ = validation_dataset(
        tree["freeze"], full, purpose="prospective", source_root=tree["root"]
    )
    assert len(bars) == 720


def test_normalized_candle_cannot_predate_its_confirmation(tmp_path: Path) -> None:
    cache = make_cache(tmp_path / "cache", OOS_START, 3, fetched=OOS_START)
    with pytest.raises(ValueError, match="before its close"):
        validated_dataset(cache)


def test_oos_is_one_shot_idempotent_and_cannot_promote_rejects(
    research_tree: dict[str, Path],
) -> None:
    tree = research_tree
    freeze(tree)
    cache = make_cache(tree["root"] / "data/research/oos", OOS_START, 36)
    output = tree["root"] / "artifacts/validation"
    result = independent_validation(
        tree["freeze"], cache, output, purpose="historical_oos", source_root=tree["root"]
    )
    assert result["candidate_selection_uses_oos"] is False
    assert result["selected_candidate"] is None
    assert all(not row["oos_passed"] for row in result["candidate_results"])
    assert all(
        "training_rejection_is_locked" in row["rejection_reasons"]
        for row in result["candidate_results"]
    )
    repeated = independent_validation(
        tree["freeze"], cache, output, purpose="historical_oos", source_root=tree["root"]
    )
    assert repeated["freeze_hash"] == result["freeze_hash"]
    different = make_cache(tree["root"] / "data/research/different", OOS_START, 36, price_offset=1)
    with pytest.raises(ValueError, match="already consumed"):
        independent_validation(
            tree["freeze"], different, output, purpose="historical_oos", source_root=tree["root"]
        )
    published = output / "report.md"
    published.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        independent_validation(
            tree["freeze"], cache, output, purpose="historical_oos", source_root=tree["root"]
        )


def test_frozen_source_allowlist_prevents_path_escape(research_tree: dict[str, Path]) -> None:
    tree = research_tree
    payload = freeze(tree)
    payload["source_hashes"]["../../outside"] = "fake"
    payload["freeze_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "freeze_hash"}
    )
    tree["freeze"].write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="allowlist"):
        verify_freeze(tree["freeze"], source_root=tree["root"])


def test_training_reject_cannot_be_selected_even_with_a_valid_report_manifest(
    research_tree: dict[str, Path],
) -> None:
    tree = research_tree
    payload = read_json(tree["summary"])
    payload["selected_candidate"] = payload["candidate_results"][0]["candidate_id"]
    tree["summary"].write_text(json.dumps(payload), encoding="utf-8")
    files = {"summary.json": file_hash(tree["summary"])}
    (tree["summary"].parent / "report_manifest.json").write_text(
        json.dumps(
            {
                "summary_hash": canonical_hash(payload),
                "files": files,
                "artifact_set_hash": canonical_hash(files),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="did not qualify"):
        freeze(tree)


def test_execution_entry_flags_cannot_change_when_future_bars_change(
    research_tree: dict[str, Path],
) -> None:
    from dataclasses import replace
    from decimal import Decimal

    tree = research_tree
    bars, _ = validated_dataset(tree["cache"])
    altered = [
        *bars[:80],
        *[
            replace(
                bar,
                open=Decimal("10000"),
                high=Decimal("10001"),
                low=Decimal("9999"),
                close=Decimal("10000"),
                volume=Decimal("1000"),
            )
            for bar in bars[80:]
        ],
    ]
    for variant in load_v3_specs(
        tree["root"] / "configs/research/strategy_v3_candidate_specs.json"
    ):
        original = V3ExecutionStrategy(bars, variant)
        changed = V3ExecutionStrategy(altered, variant)
        assert all(
            original._flags[bar.timestamp] == changed._flags[bar.timestamp] for bar in bars[:80]
        )
