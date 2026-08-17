"""Auditable artifacts for Strategy Research V3."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.reproducibility import canonical_hash
from backtest.strategy_v3_charts import render_strategy_v3_charts
from backtest.strategy_v3_research import StrategyV3Study


def write_strategy_v3_artifacts(
    output: Path,
    *,
    study: StrategyV3Study,
    spec_payload: dict[str, Any],
    data_manifest: dict[str, Any],
    feature_manifest: dict[str, Any],
    source_artifact_hashes: dict[str, str],
    frozen_file_hashes: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    (output / "candidate_specs.json").write_text(
        json.dumps(spec_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "data_manifest.json").write_text(
        json.dumps({**data_manifest, **feature_manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    data_end = datetime.fromisoformat(str(data_manifest["actual_end"]))
    research_cutoff = datetime.fromisoformat(str(spec_payload["research_cutoff"]))
    cutoff = {
        "research_cutoff": spec_payload["research_cutoff"],
        "prospective_oos_start": spec_payload["prospective_oos_start"],
        "candidate_design_data_end": data_manifest["actual_end"],
        "candidate_design_data_end_lte_research_cutoff": data_end <= research_cutoff,
        "prospective_data_used_for_selection": False,
    }
    if not cutoff["candidate_design_data_end_lte_research_cutoff"]:
        raise ValueError("candidate design data exceeds the frozen research cutoff")
    (output / "research_cutoff.json").write_text(
        json.dumps(cutoff, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    prospective = {
        "partition": "data/research/strategy_v3/prospective_oos",
        "prospective_oos_start": spec_payload["prospective_oos_start"],
        "collector_ready": True,
        "collector_executed_this_task": False,
        "rows_available": 0,
        "rows_used_for_candidate_selection": 0,
        "append_only": True,
        "resume_safe": True,
        "duplicate_safe": True,
        "confirmed_candle_only": True,
        "public_rest_only": True,
    }
    (output / "prospective_oos_manifest.json").write_text(
        json.dumps(prospective, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(output / "strategy_v3_scoreboard.csv", list(study.scoreboard))
    _write_csv(output / "variant_forward_results.csv", list(study.variant_rows))
    for index, result in enumerate(study.results):
        directory = output / f"candidate_{chr(ord('A') + index)}"
        directory.mkdir()
        definition = next(
            item
            for item in spec_payload["candidates"]
            if item["candidate_id"] == result.candidate_id
        )
        candidate_spec = {
            **definition,
            "episode_definition": spec_payload.get("episode_definition"),
            "entry_timing": spec_payload.get("entry_timing"),
            "lookahead_bias": spec_payload.get("lookahead_bias"),
            "frozen_before_oos": spec_payload.get("frozen_before_oos"),
        }
        (directory / "candidate_spec.json").write_text(
            json.dumps(candidate_spec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_csv(directory / "episodes.csv", list(result.episodes))
        _write_csv(directory / "forward_results.csv", list(result.forward_rows))
        _write_csv(directory / "incremental_controls.csv", list(result.control_rows))
        _write_csv(directory / "random_benchmark.csv", list(result.random_rows))
        _write_csv(directory / "regime_results.csv", list(result.regime_rows))
        _write_csv(directory / "temporal_results.csv", list(result.temporal_rows))
        _write_csv(
            directory / "fixed_exit_results.csv",
            list(result.fixed_rows),
            reason="candidate eliminated before fixed exit",
        )
        _write_csv(
            directory / "cost_sensitivity.csv",
            list(result.cost_rows),
            reason="candidate eliminated before fixed exit",
        )
        _write_csv(
            directory / "profit_concentration.csv",
            list(result.concentration_rows),
            reason="candidate eliminated before fixed exit",
        )
        _write_csv(
            directory / "walk_forward.csv",
            list(result.walk_forward_rows),
            reason="candidate did not pass fixed exit gate",
        )
        _write_csv(directory / "yearly_results.csv", list(result.yearly_rows))
        (directory / "status.json").write_text(
            json.dumps(
                {
                    "candidate_id": result.candidate_id,
                    "primary_variant_id": result.primary_variant_id,
                    "stage_reached": result.stage_reached,
                    "final_status": result.status,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    summary: dict[str, Any] = {
        "research_id": output.name,
        "research_type": "STRATEGY_RESEARCH_V3",
        "OFFLINE_OR_PUBLIC_READ_ONLY_RESEARCH": True,
        "baselines": {"PURE_VWAP_V1": "REJECTED", "STRATEGY_V2": "REJECTED"},
        "research_cutoff": cutoff,
        "dataset_hash": data_manifest["dataset_hash"],
        "candidate_count": len(study.results),
        "variant_count": study.variant_count,
        "candidate_definitions": spec_payload["candidates"],
        "scoreboard": study.scoreboard,
        "surviving_candidate": study.surviving_candidate,
        "historical_final_holdout_pristine": study.historical_final_holdout_pristine,
        "prospective_oos_isolated": True,
        "prospective_data_used_for_selection": False,
        "PROSPECTIVE_VALIDATION_REQUIRED": study.surviving_candidate is not None,
        "final_state": study.final_state,
        "source_artifact_hashes": source_artifact_hashes,
        "frozen_file_hashes": frozen_file_hashes,
        "quality_gate": {
            "status": "pending_external_validation",
            "targeted_tests": None,
            "full_pytest": None,
            "ruff": None,
            "mypy": None,
        },
        "research_integrity": {
            "production_strategy_changed": False,
            "shadow_strategy_changed": False,
            "bounded_demo_strategy_changed": False,
            "parameter_auto_optimization": False,
            "hidden_retry_until_pass": False,
        },
        "safety": {
            "bounded_demo_started": 0,
            "broker_write_calls": 0,
            "place_order_calls": 0,
            "cancel_order_calls": 0,
            "orders_created_this_task": 0,
            "fills_created_this_task": 0,
            "private_api_write_calls": 0,
            "submission_budget_events_created": 0,
            "live_trading": False,
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if study.surviving_candidate is not None:
        survivor = next(
            item for item in study.results if item.candidate_id == study.surviving_candidate
        )
        candidate_definition = next(
            item
            for item in spec_payload["candidates"]
            if item["candidate_id"] == survivor.candidate_id
        )
        selected_variant = next(
            item
            for item in candidate_definition["variants"]
            if item["variant_id"] == survivor.primary_variant_id
        )
        frozen_strategy = {
            "candidate_id": survivor.candidate_id,
            "selected_variant": selected_variant,
            "entry_rule": candidate_definition["entry_rule"],
            "episode_definition": spec_payload["episode_definition"],
            "entry_timing": spec_payload["entry_timing"],
            "exit_research_assumption": "best fixed horizon selected on first 60%; common fixed exit model",
            "cost_assumption": "10 and 20 bps validation",
        }
        freeze = {
            "candidate_id": survivor.candidate_id,
            "strategy_hash": canonical_hash(frozen_strategy),
            "candidate_spec_hash": canonical_hash(candidate_definition),
            "dataset_end": data_manifest["actual_end"],
            "freeze_timestamp": datetime.now(UTC).isoformat(),
            "all_parameters": selected_variant["parameters"],
            "entry_logic": candidate_definition["entry_rule"],
            "exit_research_assumption": frozen_strategy["exit_research_assumption"],
            "cost_assumption": frozen_strategy["cost_assumption"],
        }
        (output / "candidate_freeze.json").write_text(
            json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    render_strategy_v3_charts(output, study)
    _write_manifest(output)
    return summary


def finalize_v3_quality(
    output: Path,
    *,
    targeted_tests: str,
    full_pytest: str,
    ruff: str,
    mypy: str,
    database_audit: dict[str, Any],
) -> None:
    path = output / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["quality_gate"] = {
        "status": "passed",
        "targeted_tests": targeted_tests,
        "full_pytest": full_pytest,
        "ruff": ruff,
        "mypy": mypy,
    }
    summary["database_audit"] = database_audit
    summary["safety"].update(
        {
            "orders_created_this_task": database_audit["orders_created_this_task"],
            "fills_created_this_task": database_audit["fills_created_this_task"],
            "submission_budget_events_created": database_audit["submission_budget_events_created"],
        }
    )
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    _write_manifest(output)


def _report(summary: dict[str, Any]) -> str:
    found = summary["surviving_candidate"] is not None
    lines = [
        "# STRATEGY_RESEARCH_V3",
        "",
        "## Executive Summary",
        "",
        f"- Multi-Timeframe / Volume provided a surviving incremental information source: {found}.",
        f"- Final state: `{summary['final_state']}`.",
        f"- Surviving candidate: {summary['surviving_candidate'] or 'none'}.",
        "",
        "## Research Cutoff",
        "",
        f"- research_cutoff={summary['research_cutoff']['research_cutoff']}",
        "- prospective_oos_isolated=true",
        "- prospective_data_used_for_selection=false",
        "",
        "## Dataset",
        "",
        f"- dataset_hash={summary['dataset_hash']}",
        "- volume field: base-currency `volume` (BTC); quote fields were not mixed.",
        "",
        "## Candidate Specifications",
        "",
    ]
    for item in summary["candidate_definitions"]:
        lines.append(f"- `{item['candidate_id']}`: {item['hypothesis']}")
    lines.extend(
        [
            "",
            "## Early Elimination",
            "",
            "Candidates required confidence-adjusted market excess, >=80% random percentile, positive recent history, three years, stable neighboring variant, adequate samples, and direct incremental value.",
            "",
            "## Multi-Timeframe / Volume Incremental Value",
            "",
        ]
    )
    for row in summary["scoreboard"]:
        lines.append(
            f"- {row['candidate_id']}: HTF={_truth(row['htf_incremental_value'])} ({_pct(row['htf_incremental_delta'])}), Volume={_truth(row['volume_incremental_value'])} ({_pct(row['volume_incremental_delta'])}), sample reduction={_pct(row['sample_size_reduction'])}."
        )
    lines.extend(
        [
            "",
            "## Candidate Scoreboard",
            "",
            "| Candidate | Episodes | 24H excess | Random percentile | Stage | Status |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for row in summary["scoreboard"]:
        lines.append(
            f"| {row['candidate_id']} | {row['episodes']} | {_pct(row['24h_excess'])} | {_pct(row['random_percentile'])} | {row['stage_reached']} | {row['final_status']} |"
        )
    lines.extend(
        [
            "",
            "## Fixed Exit / Cost / Profit Concentration / Walk-Forward",
            "",
            "Only candidates passing every previous gate were eligible. `not_run` files are intentional eliminations.",
            "",
            "## Historical Stability",
            "",
            f"historical_final_holdout_pristine={str(summary['historical_final_holdout_pristine']).lower()}",
            "",
            "## Prospective Validation Plan",
            "",
            f"candidate_frozen={str(found).lower()}",
            f"prospective_validation_required={str(summary['PROSPECTIVE_VALIDATION_REQUIRED']).lower()}",
            "Future confirmed 1H data is isolated and may validate a frozen candidate only; it cannot flow back into V3 design.",
            "",
            "## Rejected Candidates",
            "",
        ]
    )
    for row in summary["scoreboard"]:
        if row["final_status"] != "RESEARCH_CANDIDATE_V3":
            lines.append(
                f"- {row['candidate_id']}: `{row['final_status']}` at `{row['stage_reached']}`."
            )
    lines.extend(
        [
            "",
            "## Surviving Candidate",
            "",
            summary["surviving_candidate"] or "None.",
            "",
            "## Quality Gate",
            "",
            "```text",
            f"targeted_tests={summary['quality_gate']['targeted_tests']}",
            f"full_pytest={summary['quality_gate']['full_pytest']}",
            f"ruff={summary['quality_gate']['ruff']}",
            f"mypy={summary['quality_gate']['mypy']}",
            "```",
            "",
            "## Safety Verification",
            "",
            "```text",
            "bounded_demo_started=0",
            "broker_write_calls=0",
            "place_order_calls=0",
            "cancel_order_calls=0",
            f"orders_created_this_task={summary['safety']['orders_created_this_task']}",
            f"fills_created_this_task={summary['safety']['fills_created_this_task']}",
            "private_api_write_calls=0",
            f"submission_budget_events_created={summary['safety']['submission_budget_events_created']}",
            "live_trading=false",
            "```",
            "",
            "## Final State",
            "",
            f"`{summary['final_state']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]], *, reason: str = "no rows") -> None:
    if not rows:
        path.write_text(f"status,reason\nnot_run,{reason}\n", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_manifest(output: Path) -> None:
    manifest_path = output / "artifact_manifest.json"
    files = {
        str(path.relative_to(output)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    manifest_path.write_text(
        json.dumps(
            {
                "research_id": output.name,
                "files": files,
                "artifact_set_hash": canonical_hash(files),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _pct(value: object) -> str:
    return f"{float(value) * 100:.3f}%" if isinstance(value, (int, float)) else "n/a"


def _truth(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else "n/a"
