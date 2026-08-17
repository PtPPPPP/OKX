"""Pure, fail-closed helpers for read-only exchange order attribution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AttributionLevel(StrEnum):
    DETERMINISTIC = "L1_DETERMINISTIC"
    HIGH_CONFIDENCE = "L2_HIGH_CONFIDENCE"
    AMBIGUOUS = "L3_AMBIGUOUS"
    EXCLUDED = "L4_EXCLUDED"
    UNRESOLVED = "L5_UNRESOLVED"


class RunRiskClass(StrEnum):
    NO_EXCHANGE_ACTIVITY_IN_COVERED_WINDOW = "R1"
    ACCOUNT_ACTIVITY_FULLY_EXCLUDED = "R2"
    KNOWN_TERMINAL_PROJECT_ORDER = "R3"
    KNOWN_NON_CREATED_SUBMISSION = "R4"
    AMBIGUOUS_ACCOUNT_ACTIVITY = "R5"
    INSUFFICIENT_EXCHANGE_COVERAGE = "R6"
    OPEN_OR_UNKNOWN_EXPOSURE = "R7"


@dataclass(frozen=True, slots=True)
class LocalOrderLink:
    run_id: str
    client_order_id: str
    exchange_order_id: str | None
    exchange_fill_ids: frozenset[str]
    source: str


@dataclass(frozen=True, slots=True)
class Attribution:
    evidence_id: str
    run_id: str | None
    level: AttributionLevel
    reason: str


def unique_orders(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Deduplicate the same exchange order returned by multiple official endpoints."""
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        order_id = str(row.get("ordId") or "")
        if not order_id:
            raise ValueError("exchange order is missing ordId")
        result.setdefault(order_id, dict(row))
    return result


def unique_fills(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Deduplicate fills by tradeId, with a strict composite-key fallback."""
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_id = str(row.get("tradeId") or "")
        if not trade_id:
            fields = ("ordId", "fillTime", "fillPx", "fillSz", "fee", "feeCcy", "side", "instId")
            if any(row.get(field) in (None, "") for field in fields):
                raise ValueError("exchange fill lacks tradeId and complete fallback key")
            trade_id = "composite:" + "|".join(str(row[field]) for field in fields)
        result.setdefault(trade_id, dict(row))
    return result


def attribute_order(order: Mapping[str, Any], links: Iterable[LocalOrderLink]) -> Attribution:
    """Only immutable exchange IDs can produce deterministic ownership."""
    candidates = [
        link
        for link in links
        if str(order.get("ordId") or "") == (link.exchange_order_id or "")
        or str(order.get("clOrdId") or "") == link.client_order_id
    ]
    order_id = str(order.get("ordId") or "")
    if len(candidates) == 1:
        return Attribution(
            order_id,
            candidates[0].run_id,
            AttributionLevel.DETERMINISTIC,
            "exact ordId or clOrdId match",
        )
    if len(candidates) > 1:
        return Attribution(
            order_id, None, AttributionLevel.AMBIGUOUS, "multiple local exact-ID candidates"
        )
    return Attribution(order_id, None, AttributionLevel.EXCLUDED, "no local immutable ID match")


def attribute_fill(fill: Mapping[str, Any], links: Iterable[LocalOrderLink]) -> Attribution:
    fill_id = str(fill.get("tradeId") or "")
    candidates = [
        link
        for link in links
        if fill_id in link.exchange_fill_ids
        or str(fill.get("ordId") or "") == (link.exchange_order_id or "")
        or str(fill.get("clOrdId") or "") == link.client_order_id
    ]
    if len(candidates) == 1:
        return Attribution(
            fill_id,
            candidates[0].run_id,
            AttributionLevel.DETERMINISTIC,
            "exact tradeId, ordId, or clOrdId match",
        )
    if len(candidates) > 1:
        return Attribution(
            fill_id, None, AttributionLevel.AMBIGUOUS, "multiple local exact-ID candidates"
        )
    return Attribution(fill_id, None, AttributionLevel.EXCLUDED, "no local immutable ID match")


def classify_run(
    *,
    coverage_complete: bool,
    has_current_exposure: bool,
    attributed_order_run_ids: set[str],
    account_activity_present: bool,
    known_non_created_submission: bool = False,
) -> RunRiskClass:
    """Classify a legacy run without treating a broad time hit as ownership."""
    if has_current_exposure:
        return RunRiskClass.OPEN_OR_UNKNOWN_EXPOSURE
    if not coverage_complete:
        return RunRiskClass.INSUFFICIENT_EXCHANGE_COVERAGE
    if known_non_created_submission:
        return RunRiskClass.KNOWN_NON_CREATED_SUBMISSION
    if attributed_order_run_ids:
        return RunRiskClass.KNOWN_TERMINAL_PROJECT_ORDER
    if account_activity_present:
        return RunRiskClass.ACCOUNT_ACTIVITY_FULLY_EXCLUDED
    return RunRiskClass.NO_EXCHANGE_ACTIVITY_IN_COVERED_WINDOW
