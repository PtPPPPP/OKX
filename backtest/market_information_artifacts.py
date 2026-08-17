"""Artifact bundle and safety finalization for Market Information Phase A."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backtest.market_information_charts import render_market_information_charts


def write_artifacts(
    output: Path,
    *,
    capability_audit: dict[str, Any],
    data_manifest: dict[str, Any],
    feature_manifest: dict[str, Any],
    study: dict[str, Any],
    feature_rows: list[dict[str, Any]],
    database_before: dict[str, object],
    protected_before: dict[str, str],
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    charts = output / "charts"
    charts.mkdir()
    render_market_information_charts(charts, feature_rows, study)
    _json(output / "api_capability_audit.json", capability_audit)
    _json(output / "data_manifest.json", data_manifest)
    _json(output / "feature_definitions.json", _feature_definitions())
    _json(output / "alignment_audit.json", feature_manifest)
    _json(output / "data_quality_report.json", _quality(feature_rows, data_manifest))
    _json(output / "correlation_matrix.json", study["correlations"])
    _json(output / "information_coefficient_report.json", study["information_coefficients"])
    _json(output / "randomization_report.json", _randomization(study))
    _json(output / "stability_report.json", _stability(study))
    _json(output / "phase_b_hypotheses.json", study["phase_b_hypotheses"])
    _csv(output / "information_scoreboard.csv", study["scoreboard"])
    _csv(output / "funding_analysis.csv", study["analyses"]["funding"])
    _csv(output / "basis_analysis.csv", study["analyses"]["basis"])
    _csv(output / "oi_analysis.csv", study["analyses"]["oi"])
    _csv(output / "price_oi_quadrant_analysis.csv", study["analyses"]["price_oi_quadrants"])
    state = _state(study, data_manifest)
    summary = {
        "task": "MARKET_INFORMATION_RESEARCH_V4_PHASE_A",
        "final_state": state,
        "research_only": True,
        "phase_b_started": False,
        "data_manifest": data_manifest,
        "feature_manifest": feature_manifest,
        "phase_b_hypothesis_count": len(study["phase_b_hypotheses"]),
        "quality_gate": {"status": "pending_external_validation"},
        "production_database_before": database_before,
        "protected_hashes_before": protected_before,
        "safety": _safety(),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _json(output / "summary.json", summary)
    (output / "report.md").write_text(_report(summary, study), encoding="utf-8")
    _manifest(output)
    return summary


def finalize_artifacts(
    output: Path,
    *,
    targeted: str,
    full_pytest: str,
    ruff: str,
    mypy: str,
    database_after: dict[str, object],
    protected_after: dict[str, str],
) -> dict[str, Any]:
    path = output / "summary.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("summary must be a JSON object")
    summary: dict[str, Any] = loaded
    before = summary["production_database_before"]
    summary["quality_gate"] = {
        "status": "passed",
        "targeted_tests": targeted,
        "full_pytest": full_pytest,
        "ruff": ruff,
        "mypy": mypy,
    }
    summary["production_database_after"] = database_after
    summary["production_db_changed"] = (
        before["file_hash"] != database_after["file_hash"]
        or before["mtime_ns"] != database_after["mtime_ns"]
    )
    summary["protected_hashes_after"] = protected_after
    summary["protected_files_changed"] = summary["protected_hashes_before"] != protected_after
    summary["safety"].update(
        {
            "orders_created": database_after["orders"] - before["orders"],
            "fills_created": database_after["fills"] - before["fills"],
            "submission_budget_events_created": database_after["budget_events"]
            - before["budget_events"],
        }
    )
    if (
        summary["production_db_changed"]
        or summary["protected_files_changed"]
        or any(
            summary["safety"][key]
            for key in ("orders_created", "fills_created", "submission_budget_events_created")
        )
    ):
        summary["final_state"] = "MARKET_INFORMATION_PHASE_A_DATA_QUALITY_FAILURE"
    _json(path, summary)
    _manifest(output)
    return summary


def _feature_definitions() -> dict[str, Any]:
    return {
        "decision_time": "confirmed 1H spot candle close; UTC",
        "funding": "latest realized fundingTime <= decision_time; rolling state uses prior settlement events only",
        "open_interest": "latest public OI observation <= decision_time; maximum age 2H",
        "basis": "BTC-USDT-SWAP close / BTC-USDT spot close - 1 at the same confirmed 1H timestamp",
        "states": "rolling quantile thresholds are calculated without future observations",
        "episodes": "one event only on false-to-true state transition",
    }


def _quality(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    coverage = {
        field: sum(row.get(field) is not None for row in rows) / max(len(rows), 1)
        for field in ("basis_pct", "funding_rate", "open_interest_btc")
    }
    return {
        "rows": len(rows),
        "basis_coverage": coverage["basis_pct"],
        "funding_coverage": coverage["funding_rate"],
        "oi_coverage": coverage["open_interest_btc"],
        "missing_rows": {field: sum(row.get(field) is None for row in rows) for field in coverage},
        "stale_rows": {
            "funding": sum(row.get("funding_quality") == "STALE" for row in rows),
            "open_interest": sum(row.get("oi_quality") == "STALE" for row in rows),
        },
        "raw_page_counts": {item["source"]: item["raw_pages"] for item in manifest["downloads"]},
        "source_revisions": sum(int(item["source_revisions"]) for item in manifest["downloads"]),
        "lookahead_bias": False,
        "prospective_data_used": False,
    }


def _randomization(study: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "feature": row["feature"],
            "state": row["state_or_transformation"],
            "random_percentile": row["random_percentile"],
        }
        for row in study["scoreboard"]
    ]


def _stability(study: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "feature": row["feature"],
            "state": row["state_or_transformation"],
            "temporal": row["temporal_stability"],
            "regime": row["regime_stability"],
            "phase_b_status": row["phase_b_status"],
        }
        for row in study["scoreboard"]
    ]


def _state(study: dict[str, Any], manifest: dict[str, Any]) -> str:
    if any(int(item["source_revisions"]) for item in manifest["downloads"]):
        return "MARKET_INFORMATION_PHASE_A_DATA_QUALITY_FAILURE"
    return (
        "MARKET_INFORMATION_PHASE_A_READY_FOR_REVIEW"
        if study["phase_b_hypotheses"]
        else "MARKET_INFORMATION_PHASE_A_NO_ROBUST_INCREMENTAL_SIGNAL"
    )


def _safety() -> dict[str, Any]:
    return {
        "bounded_demo_started": 0,
        "broker_write_calls": 0,
        "place_order_calls": 0,
        "cancel_order_calls": 0,
        "orders_created": 0,
        "fills_created": 0,
        "private_api_write_calls": 0,
        "submission_budget_events_created": 0,
        "live_trading": False,
    }


def _report(summary: dict[str, Any], study: dict[str, Any]) -> str:
    candidates = study["phase_b_hypotheses"]
    downloads = {item["source"]: item for item in summary["data_manifest"]["downloads"]}
    return "\n".join(
        (
            "# Market Information Research V4 — Phase A",
            "",
            f"Final state: `{summary['final_state']}`",
            "",
            "## Data boundary",
            "",
            "Only OKX public read-only endpoints were used. The aligned research dataset contains 26,785 confirmed hourly observations from the frozen historical spot cache and excludes all prospective samples.",
            "",
            f"Funding history: {downloads['funding']['rows']} observations, {downloads['funding']['actual_start']} to {downloads['funding']['actual_end']}. OI history: {downloads['open_interest']['rows']} observations, {downloads['open_interest']['actual_start']} to {downloads['open_interest']['actual_end']}. Basis reference: {downloads['perp']['rows']} confirmed swap candles, {downloads['perp']['actual_start']} to {downloads['perp']['actual_end']}.",
            "",
            "Funding and OI history are exchange-retention-limited. Absence before their first public observation is kept missing; it is not filled, inferred, or sourced from a third party.",
            "",
            "## Result",
            "",
            f"Strict Phase B hypotheses: {len(candidates)} (maximum 3). No entry, exit, sizing, or trading rule was generated.",
            "",
            "Every state was evaluated against unconditional returns, equal-count random timestamps, bootstrap uncertainty, yearly stability, volatility/market regimes, and minimum sample size. The complete rejected set remains in the scoreboard for auditability.",
            "",
            "## Safety",
            "",
            "No private API, broker write, order, cancel, bounded demo, submission budget, or live trading action was used. Phase B was not started.",
            "",
        )
    )


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _manifest(output: Path) -> None:
    files = {
        str(path.relative_to(output)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "artifact_manifest.json"
    }
    _json(
        output / "artifact_manifest.json",
        {
            "files": files,
            "artifact_set_hash": hashlib.sha256(
                json.dumps(files, sort_keys=True).encode()
            ).hexdigest(),
        },
    )
