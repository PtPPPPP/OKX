from app.services.order_attribution import (
    AttributionLevel,
    LocalOrderLink,
    RunRiskClass,
    attribute_fill,
    attribute_order,
    classify_run,
    unique_fills,
    unique_orders,
)


def _link() -> LocalOrderLink:
    return LocalOrderLink("run-a", "client-a", "order-a", frozenset({"trade-a"}), "demo")


def test_same_order_from_multiple_endpoints_is_counted_once() -> None:
    assert len(unique_orders(({"ordId": "order-a"}, {"ordId": "order-a"}))) == 1


def test_same_fill_from_multiple_windows_is_counted_once() -> None:
    assert len(unique_fills(({"tradeId": "trade-a"}, {"tradeId": "trade-a"}))) == 1


def test_order_id_and_client_order_id_are_deterministic() -> None:
    assert attribute_order({"ordId": "order-a"}, [_link()]).level is AttributionLevel.DETERMINISTIC
    assert attribute_order({"clOrdId": "client-a"}, [_link()]).run_id == "run-a"


def test_trade_id_is_deterministic() -> None:
    assert attribute_fill({"tradeId": "trade-a"}, [_link()]).run_id == "run-a"


def test_time_or_instrument_alone_is_not_attribution() -> None:
    result = attribute_order({"ordId": "other", "instId": "BTC-USDT"}, [_link()])
    assert result.level is AttributionLevel.EXCLUDED


def test_multiple_exact_candidates_are_ambiguous() -> None:
    duplicate = LocalOrderLink("run-b", "client-a", None, frozenset(), "demo")
    assert (
        attribute_order({"clOrdId": "client-a"}, [_link(), duplicate]).level
        is AttributionLevel.AMBIGUOUS
    )


def test_incomplete_coverage_is_r6() -> None:
    assert (
        classify_run(
            coverage_complete=False,
            has_current_exposure=False,
            attributed_order_run_ids=set(),
            account_activity_present=False,
        )
        is RunRiskClass.INSUFFICIENT_EXCHANGE_COVERAGE
    )


def test_complete_window_without_activity_is_r1() -> None:
    assert (
        classify_run(
            coverage_complete=True,
            has_current_exposure=False,
            attributed_order_run_ids=set(),
            account_activity_present=False,
        )
        is RunRiskClass.NO_EXCHANGE_ACTIVITY_IN_COVERED_WINDOW
    )


def test_complete_window_with_excluded_activity_is_r2() -> None:
    assert (
        classify_run(
            coverage_complete=True,
            has_current_exposure=False,
            attributed_order_run_ids=set(),
            account_activity_present=True,
        )
        is RunRiskClass.ACCOUNT_ACTIVITY_FULLY_EXCLUDED
    )


def test_known_order_is_r3_and_exposure_is_r7() -> None:
    assert (
        classify_run(
            coverage_complete=True,
            has_current_exposure=False,
            attributed_order_run_ids={"run-a"},
            account_activity_present=False,
        )
        is RunRiskClass.KNOWN_TERMINAL_PROJECT_ORDER
    )
    assert (
        classify_run(
            coverage_complete=True,
            has_current_exposure=True,
            attributed_order_run_ids=set(),
            account_activity_present=False,
        )
        is RunRiskClass.OPEN_OR_UNKNOWN_EXPOSURE
    )


def test_known_non_created_submission_is_r4() -> None:
    assert (
        classify_run(
            coverage_complete=True,
            has_current_exposure=False,
            attributed_order_run_ids=set(),
            account_activity_present=False,
            known_non_created_submission=True,
        )
        is RunRiskClass.KNOWN_NON_CREATED_SUBMISSION
    )
