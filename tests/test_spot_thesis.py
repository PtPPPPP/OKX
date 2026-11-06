"""Long-term spot thesis evidence gates, using explicit synthetic statements."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.portfolio.spot_thesis import (
    PRODUCTS,
    Condition,
    Evidence,
    Review,
    ThesisRevision,
    evaluate_thesis,
    template_revision,
    validate_history,
)

START = datetime(2026, 10, 1, tzinfo=UTC)
NOW = START + timedelta(hours=4)
INSTRUMENT = "BTC-USDT"


def revision(**overrides: Any) -> ThesisRevision:
    data: dict[str, Any] = {
        "id": "thesis-1",
        "instrument": INSTRUMENT,
        "recorded_at": START,
        "source": "synthetic user entry",
        "buy_reason": "Synthetic user rationale: test long-term adoption evidence.",
        "check_conditions": [
            {"id": "growth", "text": "Synthetic adoption metric is growing", "kind": "fundamental"}
        ],
        "invalidation_conditions": [
            {
                "id": "decline",
                "text": "Synthetic adoption metric has fallen materially",
                "kind": "fundamental",
            }
        ],
        "review_interval_days": 7,
    }
    data.update(overrides)
    return ThesisRevision.model_validate(data)


def evidence(**overrides: Any) -> Evidence:
    data: dict[str, Any] = {
        "id": "fact-1",
        "instruments": [INSTRUMENT],
        "statement": "Synthetic observed adoption metric increased over the comparison period.",
        "classification": "fact",
        "direction": "support",
        "kind": "fundamental",
        "source": "synthetic source record",
        "published_at": START + timedelta(hours=1),
        "collected_at": START + timedelta(hours=2),
        "expires_at": START + timedelta(days=10),
    }
    data.update(overrides)
    return Evidence.model_validate(data)


def review(**overrides: Any) -> Review:
    data: dict[str, Any] = {
        "id": "review-1",
        "instrument": INSTRUMENT,
        "revision_id": "thesis-1",
        "reviewed_at": START + timedelta(hours=3),
        "reviewer": "synthetic reviewer",
        "notes": "Synthetic human assessment for deterministic testing.",
        "checks": [
            {"condition_id": "growth", "outcome": "met", "evidence_ids": ["fact-1"]},
            {"condition_id": "decline", "outcome": "not_met", "evidence_ids": ["fact-1"]},
        ],
    }
    data.update(overrides)
    return Review.model_validate(data)


def evaluated(
    revisions: list[ThesisRevision] | None = None,
    facts: list[Evidence] | None = None,
    reviews: list[Review] | None = None,
    *,
    now: datetime = NOW,
    instrument: str = INSTRUMENT,
) -> dict[str, Any]:
    return evaluate_thesis(
        instrument,
        [revision()] if revisions is None else revisions,
        [evidence()] if facts is None else facts,
        [review()] if reviews is None else reviews,
        now=now,
    )


def test_empty_five_product_templates_do_not_invent_rationale() -> None:
    assert set(PRODUCTS) == {"BTC-USDT", "ARB-USDT", "XINTC-USDT", "XQQQ-USDT", "XAUT-USDT"}
    for instrument in PRODUCTS:
        template = template_revision(instrument, at=START, source="synthetic", id=instrument)
        report = evaluated([template], [], [], instrument=instrument)
        assert template.buy_reason is None
        assert report["buy_reason"] is None
        assert report["status"] == "MISSING_INPUT"
        assert report["holding_policy"] == "thesis_hold"
        assert report["max_holding_hours"] is None
        assert report["action"] is None


def test_all_five_states_require_their_actual_evidence() -> None:
    assert evaluated([], [], [])["status"] == "MISSING_INPUT"
    assert evaluated(facts=[], reviews=[])["status"] == "PENDING_REVIEW"
    assert evaluated()["status"] == "SUPPORTED"
    opposing = evidence(id="against-1", direction="against")
    assert evaluated(facts=[evidence(), opposing])["status"] == "CONFLICT"
    invalidation_review = review(
        checks=[
            {"condition_id": "growth", "outcome": "not_met", "evidence_ids": ["against-1"]},
            {"condition_id": "decline", "outcome": "met", "evidence_ids": ["against-1"]},
        ]
    )
    invalid = evaluated(facts=[opposing], reviews=[invalidation_review])
    assert invalid["status"] == "INVALIDATED"
    assert invalid["action"] is None


@pytest.mark.parametrize("backfilled", [False, True])
def test_new_evidence_requires_reassessment_of_prior_invalidation(backfilled: bool) -> None:
    opposing = evidence(direction="against")
    invalidation = review(
        checks=[
            {"condition_id": "growth", "outcome": "not_met", "evidence_ids": ["fact-1"]},
            {"condition_id": "decline", "outcome": "met", "evidence_ids": ["fact-1"]},
        ]
    )
    assert evaluated(facts=[opposing], reviews=[invalidation])["status"] == "INVALIDATED"
    new_fact = evidence(
        id="new-support",
        collected_at=START + timedelta(hours=2, minutes=30) if backfilled else NOW,
    )
    pending = evaluated(facts=[opposing, new_fact], reviews=[invalidation])
    assert pending["status"] == "CONFLICT"
    assert pending["unreviewed_evidence_ids"] == [new_fact.id]
    assert "registered_evidence_not_reviewed" in pending["reasons"]
    assert pending["review_id"] == invalidation.id
    assert pending["invalidation_conditions"][0]["outcome"] == "met"
    reassessed = review(
        id="review-2",
        reviewed_at=NOW,
        evidence_ids=[opposing.id, new_fact.id],
        checks=invalidation.checks,
    )
    report = evaluated(facts=[opposing, new_fact], reviews=[invalidation, reassessed])
    assert report["status"] == "INVALIDATED"
    assert report["status_label"] == "触发失效条件"
    assert report["action"] is None


def test_supported_label_matches_registered_support_without_investment_recommendation() -> None:
    report = evaluated()
    assert report["status_label"] == "有支持证据"
    assert report["action"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"buy_reason": None},
        {"check_conditions": []},
        {"invalidation_conditions": []},
        {"review_interval_days": None},
        {
            "check_conditions": [
                {"id": "technical", "text": "Hourly support level holds", "kind": "technical"}
            ]
        },
        {
            "invalidation_conditions": [
                {"id": "time", "text": "72 hours elapsed", "kind": "holding_time"}
            ]
        },
    ],
)
def test_incomplete_user_rationale_stays_missing_input(changes: dict[str, Any]) -> None:
    report = evaluated([revision(**changes)], [], [])
    assert report["status"] == "MISSING_INPUT"
    assert report["reasons"]
    assert report["action"] is None


@pytest.mark.parametrize("classification", ["user_judgment", "model_inference"])
def test_opinion_or_model_inference_cannot_prove_support_or_failure(classification: str) -> None:
    supportive = evidence(classification=classification)
    assert evaluated(facts=[supportive])["status"] == "PENDING_REVIEW"
    opposing = evidence(classification=classification, direction="against")
    invalidation = review(
        checks=[
            {"condition_id": "growth", "outcome": "not_met", "evidence_ids": ["fact-1"]},
            {"condition_id": "decline", "outcome": "met", "evidence_ids": ["fact-1"]},
        ]
    )
    assert evaluated(facts=[opposing], reviews=[invalidation])["status"] == "PENDING_REVIEW"


@pytest.mark.parametrize("kind", ["technical", "holding_time"])
def test_hourly_break_or_elapsed_72h_is_never_fundamental_invalidation(kind: str) -> None:
    observation = evidence(kind=kind, direction="against", statement="Synthetic short-term event")
    invalidation = review(
        checks=[
            {"condition_id": "growth", "outcome": "not_met", "evidence_ids": ["fact-1"]},
            {"condition_id": "decline", "outcome": "met", "evidence_ids": ["fact-1"]},
        ]
    )
    report = evaluated(facts=[observation], reviews=[invalidation])
    assert report["status"] == "PENDING_REVIEW"
    assert report["max_holding_hours"] is None
    assert report["action"] is None


def test_elapsed_72h_alone_does_not_expire_valid_thesis_review() -> None:
    report = evaluated(now=START + timedelta(hours=80))
    assert report["status"] == "SUPPORTED"
    assert report["max_holding_hours"] is None


@pytest.mark.parametrize(
    "changes,freshness",
    [
        ({"published_at": None}, "unknown_publication_time"),
        ({"expires_at": None}, "unknown_expiry"),
        ({"expires_at": START + timedelta(hours=3, minutes=30)}, "expired"),
    ],
)
def test_unknown_or_expired_evidence_does_not_validate_a_condition(
    changes: dict[str, Any], freshness: str
) -> None:
    report = evaluated(facts=[evidence(**changes)])
    assert report["status"] == "PENDING_REVIEW"
    assert report["evidence"][0]["freshness"] == freshness
    assert report["evidence"][0]["active_for_evaluation"] is False


def test_future_collection_is_not_available_as_of_query_time() -> None:
    future = evidence(collected_at=NOW + timedelta(hours=1))
    report = evaluated(facts=[future], reviews=[])
    assert report["status"] == "PENDING_REVIEW"
    assert report["evidence"][0]["active_for_evaluation"] is False


def test_future_publication_cannot_create_a_current_conflict() -> None:
    future = evidence(
        published_at=NOW + timedelta(hours=1),
        collected_at=NOW + timedelta(hours=2),
        direction="against",
    )
    report = evaluated(facts=[future], reviews=[])
    assert report["status"] == "PENDING_REVIEW"
    assert report["evidence"][0]["active_for_evaluation"] is False


def test_review_due_date_requires_reassessment_even_with_unexpired_facts() -> None:
    report = evaluated(now=START + timedelta(days=8))
    assert report["status"] == "PENDING_REVIEW"
    assert report["reasons"]


def test_new_revision_requires_new_review_and_preserves_historical_result() -> None:
    updated = revision(id="thesis-2", recorded_at=NOW, buy_reason="New synthetic user rationale")
    revisions = [revision(), updated]
    assert evaluated(revisions, now=NOW)["status"] == "PENDING_REVIEW"
    assert evaluated(revisions, now=NOW - timedelta(minutes=1))["status"] == "SUPPORTED"
    reassessed = review(id="review-2", revision_id="thesis-2", reviewed_at=NOW)
    report = evaluated(revisions, reviews=[review(), reassessed])
    assert report["status"] == "SUPPORTED"
    assert report["revision_id"] == "thesis-2"


def test_new_related_evidence_requires_current_review() -> None:
    new_fact = evidence(id="fact-2", published_at=START + timedelta(hours=3), collected_at=NOW)
    assert evaluated(facts=[evidence(), new_fact])["status"] == "PENDING_REVIEW"
    reviewed_again = review(id="review-2", reviewed_at=NOW, evidence_ids=["fact-1", "fact-2"])
    assert (
        evaluated(facts=[evidence(), new_fact], reviews=[review(), reviewed_again])["status"]
        == "SUPPORTED"
    )


def test_backfilled_new_evidence_is_not_implicitly_covered_by_older_review() -> None:
    backfilled = evidence(id="backfilled", collected_at=START + timedelta(hours=2, minutes=30))
    report = evaluated(facts=[evidence(), backfilled])
    assert report["status"] == "PENDING_REVIEW"
    assert report["unreviewed_evidence_ids"] == ["backfilled"]
    reviewed_again = review(id="review-2", reviewed_at=NOW, evidence_ids=["fact-1", "backfilled"])
    assert (
        evaluated(facts=[evidence(), backfilled], reviews=[review(), reviewed_again])["status"]
        == "SUPPORTED"
    )


def test_unrelated_product_evidence_does_not_invalidate_btc_review() -> None:
    unrelated = evidence(
        id="arb-fact", instruments=["ARB-USDT"], direction="against", collected_at=NOW
    )
    assert evaluated(facts=[evidence(), unrelated])["status"] == "SUPPORTED"


def test_opposing_fact_conflicts_even_when_a_supporting_review_exists() -> None:
    opposing = evidence(id="opposition", direction="against", collected_at=NOW)
    report = evaluated(facts=[evidence(), opposing])
    assert report["status"] == "CONFLICT"
    assert report["action"] is None


@pytest.mark.parametrize(
    "checks", [[], [{"condition_id": "growth", "outcome": "met", "evidence_ids": ["fact-1"]}]]
)
def test_missing_condition_checks_require_review(checks: list[dict[str, Any]]) -> None:
    assert evaluated(reviews=[review(checks=checks)])["status"] == "PENDING_REVIEW"


@pytest.mark.parametrize("outcome", ["unknown", "not_met"])
def test_unmet_or_unknown_support_condition_is_not_supported(outcome: str) -> None:
    assessed = review(
        checks=[
            {"condition_id": "growth", "outcome": outcome, "evidence_ids": ["fact-1"]},
            {"condition_id": "decline", "outcome": "not_met", "evidence_ids": ["fact-1"]},
        ]
    )
    assert evaluated(reviews=[assessed])["status"] != "SUPPORTED"


def test_missing_evidence_links_cannot_validate_human_outcomes() -> None:
    assessed = review(
        checks=[
            {"condition_id": "growth", "outcome": "met", "evidence_ids": []},
            {"condition_id": "decline", "outcome": "not_met", "evidence_ids": []},
        ]
    )
    assert evaluated(reviews=[assessed])["status"] == "PENDING_REVIEW"


def test_replaced_evidence_retains_history_without_retaining_stale_conflict() -> None:
    old = evidence(direction="against")
    replacement = evidence(
        id="fact-2",
        supersedes_id=old.id,
        published_at=START + timedelta(hours=3),
        collected_at=NOW,
    )
    reassessed = review(
        id="review-2",
        reviewed_at=NOW,
        checks=[
            {"condition_id": "growth", "outcome": "met", "evidence_ids": ["fact-2"]},
            {"condition_id": "decline", "outcome": "not_met", "evidence_ids": ["fact-2"]},
        ],
    )
    report = evaluated(facts=[old, replacement], reviews=[reassessed])
    assert report["status"] == "SUPPORTED"
    assert {item["id"] for item in report["evidence"]} == {"fact-1", "fact-2"}
    retired = next(item for item in report["evidence"] if item["id"] == old.id)
    assert retired["freshness"] == "superseded"
    assert retired["active_for_evaluation"] is False
    assert (
        evaluated(facts=[old, replacement], reviews=[], now=NOW - timedelta(minutes=1))["status"]
        == "CONFLICT"
    )


@pytest.mark.parametrize("missing", ["revision", "condition", "evidence", "cross_product"])
def test_invalid_review_references_are_rejected(missing: str) -> None:
    current = revision()
    fact = evidence()
    assessed = review()
    if missing == "revision":
        assessed = review(revision_id="absent")
    elif missing == "condition":
        assessed = review(
            checks=[{"condition_id": "absent", "outcome": "met", "evidence_ids": [fact.id]}]
        )
    elif missing == "evidence":
        assessed = review(
            checks=[{"condition_id": "growth", "outcome": "met", "evidence_ids": ["absent"]}]
        )
    else:
        fact = evidence(instruments=["ARB-USDT"])
    with pytest.raises(ValueError):
        validate_history([current], [fact], [assessed])


def test_review_cannot_reference_evidence_not_yet_collected() -> None:
    with pytest.raises(ValueError):
        validate_history([revision()], [evidence(collected_at=NOW)], [review()])


def test_evidence_cannot_replace_an_absent_record() -> None:
    with pytest.raises(ValueError):
        validate_history([revision()], [evidence(supersedes_id="missing")], [])


@pytest.mark.parametrize("interval", [0, -1, True, 1.5])
def test_invalid_review_interval_rejected(interval: Any) -> None:
    with pytest.raises(ValueError):
        revision(review_interval_days=interval)


def test_unknown_products_and_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError):
        revision(instrument="BTC-USDT-SWAP")
    with pytest.raises(ValueError):
        revision(recorded_at=datetime(2026, 10, 1))
    with pytest.raises(ValueError):
        evidence(published_at=datetime(2026, 10, 1))
    with pytest.raises(ValueError):
        review(reviewed_at=datetime(2026, 10, 1))


def test_condition_text_cannot_be_empty() -> None:
    with pytest.raises(ValueError):
        Condition(id="empty", text=" ", kind="fundamental")


def test_condition_and_evidence_kind_must_be_explicit() -> None:
    with pytest.raises(ValueError):
        Condition.model_validate({"id": "unclassified", "text": "Hourly price support broke"})
    unclassified = evidence().model_dump()
    unclassified.pop("kind")
    with pytest.raises(ValueError):
        Evidence.model_validate(unclassified)
