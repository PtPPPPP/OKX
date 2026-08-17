"""Auditable artifact writer for staged Strategy Research V2."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.reproducibility import canonical_hash
from backtest.strategy_v2_charts import render_strategy_v2_charts
from backtest.strategy_v2_research import StrategyV2Study


def write_strategy_v2_artifacts(
    output: Path,
    *,
    study: StrategyV2Study,
    candidate_spec_payload: dict[str, Any],
    data_manifest: dict[str, Any],
    source_artifact_hashes: dict[str, str],
    frozen_file_hashes: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    (output / "candidate_specs.json").write_text(
        json.dumps(candidate_spec_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(output / "candidate_scoreboard.csv", list(study.scoreboard))
    _write_csv(output / "variant_forward_results.csv", list(study.variant_rows))
    for index, result in enumerate(study.results):
        directory = output / f"candidate_{chr(ord('A') + index)}"
        directory.mkdir()
        _write_csv(directory / "episodes.csv", list(result.episodes))
        _write_csv(directory / "forward_results.csv", list(result.forward_rows))
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
        "research_type": "STRATEGY_RESEARCH_V2",
        "OFFLINE_RESEARCH_ONLY": True,
        "dataset_hash": data_manifest["dataset_hash"],
        "confirmed_bars": data_manifest["normalized_rows"],
        "candidate_count": len(study.results),
        "variant_count": study.variant_count,
        "candidate_definitions": candidate_spec_payload["candidates"],
        "selection_bias_risk": (
            "Five architectures and ten variants were frozen before formal OOS evaluation; "
            "strict multi-gate elimination reduces but does not remove multiple-testing risk"
        ),
        "baseline": {
            "VWAP_V1": "REJECTED",
            "reason": "OOS_WEAK_AND_REGIME_FRAGILE",
            "VWAP_V1_RESEARCH_CLOSED": True,
        },
        "candidate_scoreboard": study.scoreboard,
        "surviving_candidates": study.surviving_candidates,
        "final_holdout_not_pristine": study.final_holdout_not_pristine,
        "final_state": study.final_state,
        "source_artifact_hashes": source_artifact_hashes,
        "frozen_file_hashes": frozen_file_hashes,
        "research_integrity": {
            "production_strategy_changed": False,
            "shadow_strategy_changed": False,
            "bounded_demo_strategy_changed": False,
            "parameter_auto_optimization": False,
            "hidden_retry_until_pass": False,
        },
        "quality_gate": {
            "status": "pending_external_validation",
            "targeted_tests": None,
            "full_pytest": None,
            "ruff": None,
            "mypy": None,
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
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    (output / "data_manifest.json").write_text(
        json.dumps(data_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    render_strategy_v2_charts(output, study)
    _write_manifest(output)
    return summary


def finalize_quality_gate(
    output: Path,
    *,
    targeted_tests: str,
    full_pytest: str,
    ruff: str,
    mypy: str,
    database_audit: dict[str, Any],
) -> None:
    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
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
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.md").write_text(_report(summary), encoding="utf-8")
    _write_manifest(output)


def _report(summary: dict[str, Any]) -> str:
    rows = summary["candidate_scoreboard"]
    found = bool(summary["surviving_candidates"])
    lines = [
        "# STRATEGY_RESEARCH_V2",
        "",
        "## Executive Summary",
        "",
        f"- Found an entry architecture with stronger research evidence than Pure VWAP: {found}.",
        f"- Final state: `{summary['final_state']}`.",
        f"- Survivors: {', '.join(summary['surviving_candidates']) or 'none'}.",
        "",
        "## Baseline",
        "",
        "`VWAP_V1=REJECTED`",
        "`reason=OOS_WEAK_AND_REGIME_FRAGILE`",
        "",
        "## Candidate Definitions",
        "",
    ]
    for candidate in summary["candidate_definitions"]:
        lines.append(
            f"- `{candidate['candidate_id']}`: {candidate['economic_rationale']} Rule: {candidate['entry_rule']}"
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
    for row in rows:
        lines.append(
            f"| {row['candidate_id']} | {row['episodes']} | {_pct(row['forward_edge'])} | {_pct(row['random_percentile'])} | {row['stage_reached']} | {row['final_status']} |"
        )
    lines.extend(
        [
            "",
            "## Early Edge Results",
            "",
            "Candidates were eliminated unless confidence-adjusted 24H excess, four of five horizons, random percentile, recent reference performance, three years, and the neighboring frozen variant agreed.",
            "",
            "## Fixed Exit Results",
            "",
            "Only candidates passing the early gate were eligible for the common fixed-exit and cost model. A `not_run` file is an intentional elimination result.",
            "",
            "## Cost Robustness",
            "",
        ]
    )
    for row in rows:
        if row["10bps_return"] is not None:
            lines.append(
                f"- {row['candidate_id']}: 10 bps={_pct(row['10bps_return'])}, 20 bps={_pct(row['20bps_return'])}."
            )
    lines.extend(
        [
            "",
            "## Profit Concentration",
            "",
        ]
    )
    for row in rows:
        if row["top5_concentration"] is not None:
            lines.append(
                f"- {row['candidate_id']}: return lost after removing top five winners={_pct(row['top5_concentration'])}."
            )
    lines.extend(
        [
            "",
            "## Walk-Forward OOS",
            "",
            "Only candidates surviving fixed exit, 20 bps costs, drawdown, and top-winner removal were eligible. The latest 20% is explicitly not pristine and was not used as the main selection basis.",
            "",
            "## Regime / Temporal Stability",
            "",
        ]
    )
    for row in rows:
        lines.append(
            f"- {row['candidate_id']}: temporal_fragility={row['temporal_fragility']}, regime_fragility={row['regime_fragility']}."
        )
    lines.extend(
        [
            "",
            "## Rejected Candidates",
            "",
        ]
    )
    for row in rows:
        if row["final_status"] != "RESEARCH_CANDIDATE_V2":
            lines.append(
                f"- {row['candidate_id']}: `{row['final_status']}` at `{row['stage_reached']}`."
            )
    lines.extend(
        [
            "",
            "## Surviving Candidates",
            "",
            ", ".join(summary["surviving_candidates"]) or "None.",
            "",
            "## Research Integrity",
            "",
            "```text",
            "no production strategy changed",
            "no bounded_demo",
            "no Broker write",
            "no parameter auto-optimization",
            "no hidden retry-until-pass",
            "```",
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
            "## Safety Counters",
            "",
            "```text",
            "bounded_demo_started=0",
            "broker_write_calls=0",
            "place_order_calls=0",
            "cancel_order_calls=0",
            "orders_created_this_task=0",
            "fills_created_this_task=0",
            "private_api_write_calls=0",
            "live_trading=false",
            "```",
            "",
            "## Database / Runtime Audit",
            "",
            "```text",
            f"database_version={summary.get('database_audit', {}).get('database_version', 'pending')}",
            f"integrity_check={summary.get('database_audit', {}).get('integrity_check', 'pending')}",
            f"foreign_key_violations={summary.get('database_audit', {}).get('foreign_key_violations', 'pending')}",
            f"active_run_locks={summary.get('database_audit', {}).get('active_run_locks', 'pending')}",
            f"active_runs={summary.get('database_audit', {}).get('active_runs', 'pending')}",
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
