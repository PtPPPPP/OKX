"""Auditable, manual spot-thesis records; no trading decisions or network access.

The statuses describe the registered materials and human review, not investment
truth. Technical observations and elapsed holding time are auxiliary evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

PRODUCTS = ("BTC-USDT", "ARB-USDT", "XINTC-USDT", "XQQQ-USDT", "XAUT-USDT")
Kind = Literal["fundamental", "technical", "holding_time"]
Status = Literal["MISSING_INPUT", "PENDING_REVIEW", "SUPPORTED", "CONFLICT", "INVALIDATED"]
STATUS_LABELS: dict[Status, str] = {
    "MISSING_INPUT": "待补充",
    "PENDING_REVIEW": "待复核",
    "SUPPORTED": "有支持证据",
    "CONFLICT": "存在冲突",
    "INVALIDATED": "触发失效条件",
}
REASON_DETAILS = {
    "no_thesis_revision": "尚未登记该产品的投资逻辑版本。",
    "buy_reason_missing": "尚未填写买入核心理由，不能默认认定投资逻辑成立。",
    "fundamental_check_conditions_missing": "尚未填写明确分类为基本面的可检查条件。",
    "fundamental_invalidation_conditions_missing": "尚未填写明确分类为基本面的逻辑失效条件。",
    "review_interval_missing": "尚未填写定期复核间隔。",
    "reviewed_fundamental_invalidation_met": "当前人工复核记录至少一项基本面失效条件满足，并引用了有效事实材料。",
    "registered_fundamental_facts_against_thesis": "登记材料中存在有效的基本面反面事实，需结合来源与条件进行复核。",
    "supporting_and_opposing_materials_coexist": "登记的支持与反面事实材料同时存在；此状态描述材料冲突，不代表已验证的市场结论。",
    "current_revision_not_reviewed": "当前投资逻辑版本尚未经过人工复核。",
    "review_overdue": "人工复核已超过约定间隔，需要重新复核。",
    "evidence_added_since_review": "上次复核之后新增了相关材料，原复核不能直接作为当前结论。",
    "registered_evidence_not_reviewed": "存在尚未在当前复核中明确登记为已阅的材料，包括可能回填较早采集时间的材料。",
    "fresh_fundamental_support_missing": "缺少发布时间、有效期明确且仍有效的基本面支持事实。",
    "check_conditions_not_verified_met": "尚未逐项以有效基本面事实复核检查条件为满足。",
    "invalidation_conditions_not_verified_clear": "尚未逐项以有效基本面事实复核失效条件为未满足。",
    "current_review_and_registered_facts_support_conditions": "当前人工复核与有效登记事实支持检查条件，且已逐项复核失效条件未满足；这不是买卖建议。",
}


def _nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("identifier, text, or source must not be empty")
    return value


def _product(value: str) -> str:
    if value not in PRODUCTS:
        raise ValueError("unsupported spot-thesis product")
    return value


def _timestamp_input(value: Any) -> Any:
    if not isinstance(value, (str, datetime)):
        raise ValueError("timestamp must be an ISO string or timezone-aware datetime")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC)


def _version(value: Any) -> Any:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be integer 1")
    return value


def _interval(value: Any) -> Any:
    if value is not None and (type(value) is not int or not 1 <= value <= 36500):
        raise ValueError("review interval must be an integer from 1 to 36500 days")
    return value


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


Identifier = Annotated[str, AfterValidator(_nonempty)]
Product = Annotated[str, AfterValidator(_product)]
Timestamp = Annotated[datetime, BeforeValidator(_timestamp_input), AfterValidator(_utc)]


class ThesisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Condition(ThesisModel):
    id: Identifier
    text: Identifier
    kind: Kind


class ThesisRevision(ThesisModel):
    id: Identifier
    instrument: Product
    recorded_at: Timestamp
    source: Identifier
    buy_reason: Annotated[str | None, AfterValidator(_reason)] = None
    check_conditions: list[Condition] = Field(default_factory=list)
    invalidation_conditions: list[Condition] = Field(default_factory=list)
    review_interval_days: Annotated[int | None, BeforeValidator(_interval)] = None
    schema_version: Annotated[Literal[1], BeforeValidator(_version)] = 1

    @model_validator(mode="after")
    def unique_conditions(self) -> Self:
        _unique_ids([*self.check_conditions, *self.invalidation_conditions], "condition")
        return self


class Evidence(ThesisModel):
    id: Identifier
    instruments: list[Product]
    statement: Identifier
    classification: Literal["fact", "user_judgment", "model_inference"]
    direction: Literal["support", "against", "neutral"]
    kind: Kind
    source: Identifier
    published_at: Timestamp | None
    collected_at: Timestamp
    expires_at: Timestamp | None = None
    supersedes_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        if not self.instruments or len(self.instruments) != len(set(self.instruments)):
            raise ValueError("evidence needs distinct applicable products")
        if self.published_at is not None:
            if self.published_at > self.collected_at:
                raise ValueError("evidence cannot be collected before publication")
            if self.expires_at is not None and self.expires_at < self.published_at:
                raise ValueError("evidence cannot expire before publication")
        if self.supersedes_id == self.id:
            raise ValueError("evidence cannot supersede itself")
        return self


class ConditionCheck(ThesisModel):
    condition_id: Identifier
    outcome: Literal["met", "not_met", "unknown"]
    evidence_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("duplicate evidence reference in condition check")
        return self


class Review(ThesisModel):
    id: Identifier
    instrument: Product
    revision_id: Identifier
    reviewed_at: Timestamp
    reviewer: Identifier
    notes: str = ""
    checks: list[ConditionCheck] = Field(default_factory=list)
    evidence_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_checks(self) -> Self:
        ids = [item.condition_id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate condition check")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("duplicate evidence reference in review")
        return self


T = TypeVar("T", Condition, ThesisRevision, Evidence, Review)


def _unique_ids(items: list[T], label: str) -> dict[str, T]:
    result = {}
    for item in items:
        if item.id in result:
            raise ValueError(f"duplicate {label} ID: {item.id}")
        result[item.id] = item
    return result


def _latest_revision(
    instrument: str, revisions: list[ThesisRevision], at: datetime
) -> ThesisRevision | None:
    eligible = [
        (index, revision)
        for index, revision in enumerate(revisions)
        if revision.instrument == instrument and revision.recorded_at <= at
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda entry: (entry[1].recorded_at, entry[0]))[1]


def validate_history(
    revisions: list[ThesisRevision], evidence: list[Evidence], reviews: list[Review]
) -> None:
    """Validate references without discarding historical revisions or old facts.

    IDs are unique within each record type. Old reviews retain their original
    revision; the storage append boundary enforces the current revision for new
    reviews. Backfilled revisions must not invalidate retained review history.
    """
    revision_by_id = _unique_ids(revisions, "revision")
    evidence_by_id = _unique_ids(evidence, "evidence")
    _unique_ids(reviews, "review")
    evidence_order = {item.id: index for index, item in enumerate(evidence)}
    for item in evidence:
        if item.supersedes_id is None:
            continue
        old = evidence_by_id.get(item.supersedes_id)
        if old is None:
            raise ValueError("superseded evidence ID does not exist")
        if not set(old.instruments).intersection(item.instruments):
            raise ValueError("supersession must share an applicable product")
        if (old.collected_at, evidence_order[old.id]) >= (
            item.collected_at,
            evidence_order[item.id],
        ):
            raise ValueError("superseded evidence must precede the replacing evidence")
    for review in reviews:
        revision = revision_by_id.get(review.revision_id)
        if revision is None or revision.instrument != review.instrument:
            raise ValueError("review references missing or cross-product thesis revision")
        if revision.recorded_at > review.reviewed_at:
            raise ValueError("review cannot precede its thesis revision")
        conditions = {
            condition.id
            for condition in [*revision.check_conditions, *revision.invalidation_conditions]
        }
        referenced_ids = set(review.evidence_ids)
        for check in review.checks:
            if check.condition_id not in conditions:
                raise ValueError("review references a condition absent from its revision")
            referenced_ids.update(check.evidence_ids)
        for evidence_id in referenced_ids:
            cited = evidence_by_id.get(evidence_id)
            if cited is None or review.instrument not in cited.instruments:
                raise ValueError("review references missing or cross-product evidence")
            if cited.collected_at > review.reviewed_at:
                raise ValueError("review cannot use evidence collected after review time")


def template_revision(instrument: str, *, at: datetime, source: str, id: str) -> ThesisRevision:
    """Return an explicitly incomplete template, without inventing a buy rationale."""
    return ThesisRevision(id=id, instrument=instrument, recorded_at=at, source=source)


def _freshness(item: Evidence, now: datetime) -> str:
    if item.collected_at > now:
        return "not_collected"
    if item.published_at is None:
        return "unknown_publication_time"
    if item.published_at > now:
        return "not_published"
    if item.expires_at is None:
        return "unknown_expiry"
    if item.expires_at <= now:
        return "expired"
    return "fresh"


def _evidence_report(
    instrument: str, evidence: list[Evidence], now: datetime
) -> list[dict[str, Any]]:
    relevant = [item for item in evidence if instrument in item.instruments]
    visible = [item for item in relevant if item.collected_at <= now]
    superseded: dict[str, list[str]] = {}
    for item in visible:
        if item.supersedes_id is not None:
            superseded.setdefault(item.supersedes_id, []).append(item.id)
    result = []
    for item in relevant:
        entry = item.model_dump(mode="json")
        freshness = _freshness(item, now)
        entry["freshness"] = "superseded" if item.id in superseded else freshness
        entry["time_freshness"] = freshness
        entry["superseded_by"] = superseded.get(item.id, [])
        entry["available_at_evaluation"] = item.collected_at <= now
        entry["active_for_evaluation"] = freshness == "fresh" and item.id not in superseded
        entry["eligible_fundamental_fact"] = (
            entry["active_for_evaluation"]
            and item.classification == "fact"
            and item.kind == "fundamental"
        )
        result.append(entry)
    return result


def _conditions_report(
    conditions: list[Condition], review: Review | None, eligible_ids: set[str]
) -> list[dict[str, Any]]:
    checks = {check.condition_id: check for check in review.checks} if review else {}
    result = []
    for condition in conditions:
        check = checks.get(condition.id)
        ids = check.evidence_ids if check else []
        effective_ids = [identifier for identifier in ids if identifier in eligible_ids]
        result.append(
            {
                **condition.model_dump(mode="json"),
                "outcome": check.outcome if check else "unknown",
                "evidence_ids": ids,
                "effective_evidence_ids": effective_ids,
                "effective": bool(
                    condition.kind == "fundamental"
                    and check is not None
                    and check.outcome != "unknown"
                    and effective_ids
                ),
                "review_id": review.id if review else None,
            }
        )
    return result


def evaluate_thesis(
    instrument: str,
    revisions: list[ThesisRevision],
    evidence: list[Evidence],
    reviews: list[Review],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate registered evidence and review completeness at a specified UTC time."""
    _product(instrument)
    now = _utc(now)
    validate_history(revisions, evidence, reviews)
    revision = _latest_revision(instrument, revisions, now)
    current_reviews = [
        (index, review)
        for index, review in enumerate(reviews)
        if review.instrument == instrument
        and review.reviewed_at <= now
        and revision is not None
        and review.revision_id == revision.id
    ]
    review = (
        max(current_reviews, key=lambda item: (item[1].reviewed_at, item[0]))[1]
        if current_reviews
        else None
    )
    evidence_report = _evidence_report(instrument, evidence, now)
    eligible = [item for item in evidence_report if item["eligible_fundamental_fact"]]
    eligible_ids = {str(item["id"]) for item in eligible}
    check_report = _conditions_report(
        revision.check_conditions if revision else [], review, eligible_ids
    )
    invalidation_report = _conditions_report(
        revision.invalidation_conditions if revision else [], review, eligible_ids
    )
    fundamental_checks = [item for item in check_report if item["kind"] == "fundamental"]
    fundamental_invalidations = [
        item for item in invalidation_report if item["kind"] == "fundamental"
    ]
    missing = []
    if revision is None:
        missing.append("no_thesis_revision")
    if revision is None or revision.buy_reason is None:
        missing.append("buy_reason_missing")
    if not fundamental_checks:
        missing.append("fundamental_check_conditions_missing")
    if not fundamental_invalidations:
        missing.append("fundamental_invalidation_conditions_missing")
    if revision is None or revision.review_interval_days is None:
        missing.append("review_interval_missing")
    due_at = (
        review.reviewed_at + timedelta(days=revision.review_interval_days)
        if review is not None and revision is not None and revision.review_interval_days is not None
        else None
    )
    review_fresh = due_at is not None and now <= due_at
    evidence_since_review = [
        str(item["id"])
        for item in evidence_report
        if review is not None
        and item["available_at_evaluation"]
        and datetime.fromisoformat(item["collected_at"]) > review.reviewed_at
    ]
    reviewed_ids = (
        set(review.evidence_ids).union(*(check.evidence_ids for check in review.checks))
        if review is not None
        else set()
    )
    unreviewed_evidence = [
        str(item["id"])
        for item in evidence_report
        if item["available_at_evaluation"]
        and not item["superseded_by"]
        and item["id"] not in reviewed_ids
    ]
    support = [str(item["id"]) for item in eligible if item["direction"] == "support"]
    against = [str(item["id"]) for item in eligible if item["direction"] == "against"]
    invalidated = [
        str(item["id"])
        for item in fundamental_invalidations
        if item["effective"] and item["outcome"] == "met"
    ]
    all_checks_met = all(
        item["effective"] and item["outcome"] == "met" for item in fundamental_checks
    )
    invalidations_not_met = all(
        item["effective"] and item["outcome"] == "not_met" for item in fundamental_invalidations
    )
    reasons = list(missing)
    status: Status
    if missing:
        status = "MISSING_INPUT"
    elif review_fresh and invalidated and not evidence_since_review and not unreviewed_evidence:
        status = "INVALIDATED"
        reasons.append("reviewed_fundamental_invalidation_met")
    elif against:
        status = "CONFLICT"
        reasons.append("registered_fundamental_facts_against_thesis")
        if support:
            reasons.append("supporting_and_opposing_materials_coexist")
        if review is None:
            reasons.append("current_revision_not_reviewed")
        elif not review_fresh:
            reasons.append("review_overdue")
        if evidence_since_review:
            reasons.append("evidence_added_since_review")
        if unreviewed_evidence:
            reasons.append("registered_evidence_not_reviewed")
    else:
        if review is None:
            reasons.append("current_revision_not_reviewed")
        elif not review_fresh:
            reasons.append("review_overdue")
        if evidence_since_review:
            reasons.append("evidence_added_since_review")
        if unreviewed_evidence:
            reasons.append("registered_evidence_not_reviewed")
        if not support:
            reasons.append("fresh_fundamental_support_missing")
        if not all_checks_met:
            reasons.append("check_conditions_not_verified_met")
        if not invalidations_not_met:
            reasons.append("invalidation_conditions_not_verified_clear")
        status = "PENDING_REVIEW" if reasons else "SUPPORTED"
        if status == "SUPPORTED":
            reasons.append("current_review_and_registered_facts_support_conditions")
    return {
        "schema_version": 1,
        "instrument": instrument,
        "evaluated_at": now.isoformat(),
        "holding_policy": "thesis_hold",
        "max_holding_hours": None,
        "action": None,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reasons": reasons,
        "reason_details": [REASON_DETAILS[reason] for reason in reasons],
        "scope": "registered materials and manual review only; no trading recommendation",
        "conflict_scope": "conflict among registered materials, not a verified market conclusion",
        "revision_id": revision.id if revision else None,
        "review_id": review.id if review else None,
        "buy_reason": revision.buy_reason if revision else None,
        "review_interval_days": revision.review_interval_days if revision else None,
        "next_review_at": due_at.isoformat() if due_at else None,
        "review_is_fresh": review_fresh,
        "evidence_added_since_review": evidence_since_review,
        "unreviewed_evidence_ids": unreviewed_evidence,
        "supporting_evidence_ids": support,
        "opposing_evidence_ids": against,
        "invalidated_condition_ids": invalidated,
        "check_conditions": check_report,
        "invalidation_conditions": invalidation_report,
        "evidence": evidence_report,
        "revision": revision.model_dump(mode="json") if revision else None,
        "review": review.model_dump(mode="json") if review else None,
        "revision_history": [
            item.model_dump(mode="json")
            for item in revisions
            if item.instrument == instrument and item.recorded_at <= now
        ],
        "review_history": [
            item.model_dump(mode="json")
            for item in reviews
            if item.instrument == instrument and item.reviewed_at <= now
        ],
    }
