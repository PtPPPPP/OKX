"""Synthetic portfolio accounting and risk gates; never an exchange account."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

import pytest

from app.portfolio.personal_risk import (
    CashSnapshot,
    LinearContractHolding,
    ProductMetadata,
    RiskQuote,
    RiskRequest,
    RiskSettings,
    ShockScenario,
    SpotHolding,
    analyze_risk,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
NOW = START + timedelta(hours=1)


def cash(**overrides: Any) -> CashSnapshot:
    value: dict[str, Any] = {
        "balance": "1000",
        "available": "900",
        "updated_at": START,
        "source": "synthetic verified cash",
        "verified": True,
    }
    value.update(overrides)
    return CashSnapshot.model_validate(value)


def spot(**overrides: Any) -> SpotHolding:
    value: dict[str, Any] = {
        "instrument": "BTC-USDT",
        "quantity": "2",
        "updated_at": START,
        "source": "synthetic verified spot holding",
        "verified": True,
    }
    value.update(overrides)
    return SpotHolding.model_validate(value)


def contract(direction: str = "long", **overrides: Any) -> LinearContractHolding:
    value: dict[str, Any] = {
        "id": f"btc-{direction}",
        "instrument": "BTC-USDT-SWAP",
        "direction": direction,
        "base_quantity": "3" if direction == "long" else "1",
        "entry_price": "80" if direction == "long" else "120",
        "updated_at": START,
        "source": "synthetic verified contract holding",
        "verified": True,
        "opened_at": START,
        "stop_price": "70" if direction == "long" else "130",
        "estimated_trade_cost": "2",
    }
    value.update(overrides)
    return LinearContractHolding.model_validate(value)


def quote(instrument: str = "BTC-USDT", **overrides: Any) -> RiskQuote:
    value: dict[str, Any] = {
        "instrument": instrument,
        "price": "100",
        "at": NOW,
        "source": "synthetic public quote",
    }
    value.update(overrides)
    return RiskQuote.model_validate(value)


def request(**overrides: Any) -> RiskRequest:
    value: dict[str, Any] = {
        "as_of": NOW,
        "source": "synthetic full local portfolio",
        "cash": cash(),
        "cash_scope": "all_settled_excluding_derivative_pnl",
        "positions_complete": True,
        "spot_positions": [spot()],
        "contracts": [contract(), contract("short")],
        "contract_collateral": "100",
        "products": {
            "BTC-USDT": {"underlying": "BTC", "asset_class": "crypto"},
            "BTC-USDT-SWAP": {"underlying": "BTC", "asset_class": "crypto"},
        },
        "quotes": [quote(), quote("BTC-USDT-SWAP")],
    }
    value.update(overrides)
    return RiskRequest.model_validate(value)


def settings(**overrides: Any) -> RiskSettings:
    values: dict[str, Any] = {
        "long_term_budget": "300",
        "contract_budget": "200",
        "reserve_cash_target": "100",
        "contract_trade_loss_budget": "40",
        "max_gross_exposure_ratio": "1",
        "max_abs_net_exposure_ratio": "1",
        "min_available_cash_ratio": "0.5",
        "max_scenario_loss": "100",
        "product_weight_limits": {"BTC-USDT": "0.5", "BTC-USDT-SWAP": "0.5"},
    }
    values.update(overrides)
    return RiskSettings.model_validate(values)


def checks(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in report["checks"]}


def scenario(name: str = "btc_down", shock: str = "-0.1") -> ShockScenario:
    return ShockScenario.model_validate({"name": name, "shocks": {"BTC": shock}})


def test_equity_adds_spot_value_and_signed_contract_pnl_only() -> None:
    report = analyze_risk(request(), now=NOW)
    assert report["valuation_complete"] is True
    assert Decimal(report["net_equity"]) == Decimal("1280")
    assert Decimal(report["cash_balance"]) == Decimal("1000")
    assert Decimal(report["available_cash"]) == Decimal("900")
    assert Decimal(report["cash_ratio"]) == Decimal("0.703125")
    assert Decimal(report["cash_balance_ratio"]) == Decimal("0.78125")
    assert Decimal(report["gross_exposure"]) == Decimal("600")
    assert Decimal(report["net_exposure"]) == Decimal("400")
    assert report["action"] is None


def test_collateral_and_three_budget_labels_do_not_increase_or_split_equity() -> None:
    baseline = analyze_risk(request(), now=NOW)
    configured = analyze_risk(request(settings=settings()), now=NOW)
    assert configured["net_equity"] == baseline["net_equity"]
    assert configured["cash_balance"] == baseline["cash_balance"]
    assert configured["available_cash"] == baseline["available_cash"]
    known = checks(configured)
    assert Decimal(known["contract_budget"]["actual"]) == Decimal("100")
    assert Decimal(known["long_term_budget"]["actual"]) == Decimal("200")
    assert Decimal(known["budget_allocation"]["actual"]) == Decimal("600")


def test_same_contract_long_and_short_keep_separate_ids_and_gross_exposure() -> None:
    report = analyze_risk(request(), now=NOW)
    assert len(report["contracts"]) == 2
    assert {position["id"] for position in report["contracts"]} == {"btc-long", "btc-short"}
    product = next(item for item in report["by_product"] if item["name"] == "BTC-USDT-SWAP")
    assert Decimal(product["gross_exposure"]) == Decimal("400")
    assert Decimal(product["net_exposure"]) == Decimal("200")
    assert Decimal(product["product_weight"]) == Decimal("0.3125")
    underlying = next(item for item in report["by_underlying"] if item["name"] == "BTC")
    assert Decimal(underlying["gross_exposure"]) == Decimal("600")
    assert Decimal(underlying["net_exposure"]) == Decimal("400")


def test_spot_and_equal_short_can_have_zero_net_but_nonzero_gross() -> None:
    data = request(contracts=[contract("short", base_quantity="2", entry_price="100")])
    report = analyze_risk(data, now=NOW)
    assert Decimal(report["net_equity"]) == Decimal("1200")
    assert Decimal(report["gross_exposure"]) == Decimal("400")
    assert Decimal(report["net_exposure"]) == 0
    assert Decimal(report["gross_exposure_ratio"]) > 0


@pytest.mark.parametrize("direction,expected_equity", [("long", "1100"), ("short", "900")])
def test_linear_contract_base_quantity_and_direction_control_unrealized_pnl(
    direction: str, expected_equity: str
) -> None:
    data = request(
        spot_positions=[],
        contracts=[contract(direction, base_quantity="5", entry_price="80")],
    )
    report = analyze_risk(data, now=NOW)
    assert Decimal(report["net_equity"]) == Decimal(expected_equity)


@pytest.mark.parametrize(
    "changes",
    [
        {"positions_complete": False},
        {"cash": None},
        {"cash": cash(balance=None, available=None)},
        {"cash": cash(verified=False)},
        {"spot_positions": [spot(quantity=None)]},
        {"spot_positions": [spot(verified=False)]},
        {"contracts": [contract(base_quantity=None)]},
        {"contracts": [contract(entry_price=None)]},
        {"contracts": [contract(verified=False)]},
        {"cash_scope": "unknown"},
        {"cash_scope": "spot_only"},
    ],
)
def test_incomplete_or_unverified_portfolio_never_claims_precise_equity(
    changes: dict[str, Any],
) -> None:
    report = analyze_risk(request(**changes), now=NOW)
    assert report["valuation_complete"] is False
    assert report["net_equity"] is None
    assert report["overall_status"] != "CHECKED"
    assert report["reasons"]
    assert report["action"] is None


@pytest.mark.parametrize("age", [301, -1])
def test_stale_or_future_quote_blocks_account_value(age: int) -> None:
    report = analyze_risk(
        request(quotes=[quote(at=NOW - timedelta(seconds=age)), quote("BTC-USDT-SWAP")]),
        now=NOW,
    )
    assert report["net_equity"] is None
    assert report["valuation_complete"] is False


def test_quote_missing_or_older_than_position_cannot_be_used_as_current_price() -> None:
    missing = analyze_risk(request(quotes=[quote()]), now=NOW)
    assert missing["net_equity"] is None
    earlier = analyze_risk(
        request(
            spot_positions=[spot(updated_at=NOW)],
            quotes=[quote(at=NOW - timedelta(seconds=1)), quote("BTC-USDT-SWAP")],
        ),
        now=NOW,
    )
    assert earlier["net_equity"] is None


def test_future_portfolio_snapshot_cannot_be_valued_as_present() -> None:
    report = analyze_risk(request(as_of=NOW + timedelta(seconds=1)), now=NOW)
    assert report["net_equity"] is None
    assert report["valuation_complete"] is False


@pytest.mark.parametrize("component", ["cash", "spot", "contract", "opened_at"])
def test_future_component_timestamps_prevent_precise_present_equity(component: str) -> None:
    future = NOW + timedelta(seconds=1)
    changes: dict[str, Any]
    if component == "cash":
        changes = {"cash": cash(updated_at=future)}
    elif component == "spot":
        changes = {"spot_positions": [spot(updated_at=future)]}
    elif component == "contract":
        changes = {"contracts": [contract(updated_at=future)]}
    else:
        changes = {"contracts": [contract(opened_at=future)]}
    report = analyze_risk(request(**changes), now=NOW)
    assert report["net_equity"] is None
    assert report["valuation_complete"] is False
    assert report["overall_status"] != "CHECKED"
    assert report["reasons"]


def test_unavailable_cash_is_not_assumed_to_equal_account_balance() -> None:
    report = analyze_risk(request(cash=cash(available=None), settings=settings()), now=NOW)
    assert report["available_cash"] is None
    assert report["cash_ratio"] is None
    assert report["cash_balance_ratio"] is not None
    assert checks(report)["min_available_cash_ratio"]["status"] == "UNKNOWN"


def test_unknown_old_quantity_is_not_reconstructed_from_quote_or_other_assets() -> None:
    data = request(spot_positions=[spot(quantity=None)])
    report = analyze_risk(data, now=NOW)
    assert report["spot_positions"][0]["quantity"] is None
    assert report["net_equity"] is None
    assert report["valuation_complete"] is False


def test_missing_risk_budgets_are_not_defaulted_to_safe_zeroes() -> None:
    report = analyze_risk(request(), now=NOW)
    known = checks(report)
    for name in (
        "long_term_budget",
        "contract_budget",
        "reserve_cash_target",
        "max_gross_exposure_ratio",
        "max_abs_net_exposure_ratio",
        "min_available_cash_ratio",
    ):
        assert known[name]["status"] == "NOT_SET"
        assert known[name]["limit"] is None
    assert report["overall_status"] == "INCOMPLETE"
    assert report["action"] is None


def test_complete_configured_inputs_can_be_checked_without_trade_recommendation() -> None:
    report = analyze_risk(request(settings=settings(), scenarios=[scenario()]), now=NOW)
    assert report["overall_status"] == "CHECKED"
    assert all(item["status"] == "PASS" for item in report["checks"])
    assert report["action"] is None
    assert all(item["action"] is None for item in report["spot_positions"])
    assert all(item["action"] is None for item in report["contracts"])


def test_explicit_zero_limit_is_enforced_instead_of_treated_as_unset() -> None:
    report = analyze_risk(request(settings=settings(contract_trade_loss_budget="0")), now=NOW)
    item = checks(report)["contract_trade_loss_budget"]
    assert item["status"] == "EXCEEDED"
    assert Decimal(item["limit"]) == 0


def test_product_limit_uses_gross_notional_weight_including_both_directions() -> None:
    report = analyze_risk(
        request(settings=settings(product_weight_limits={"BTC-USDT-SWAP": "0.3"})), now=NOW
    )
    item = checks(report)["product_weight:BTC-USDT-SWAP"]
    assert Decimal(item["actual"]) == Decimal("0.3125")
    assert item["status"] == "EXCEEDED"
    assert item["reasons"]
    assert report["overall_status"] == "EXCEEDED"


def test_gross_net_and_cash_ratio_limits_report_actual_breaches() -> None:
    report = analyze_risk(
        request(
            settings=settings(
                max_gross_exposure_ratio="0.4",
                max_abs_net_exposure_ratio="0.3",
                min_available_cash_ratio="0.8",
            )
        ),
        now=NOW,
    )
    known = checks(report)
    for name, actual in [
        ("max_gross_exposure_ratio", "0.46875"),
        ("max_abs_net_exposure_ratio", "0.3125"),
        ("min_available_cash_ratio", "0.703125"),
    ]:
        assert known[name]["status"] == "EXCEEDED"
        assert Decimal(known[name]["actual"]) == Decimal(actual)
        assert known[name]["reasons"]


def test_budget_sum_above_equity_is_reported_without_changing_cash() -> None:
    report = analyze_risk(
        request(
            settings=settings(
                long_term_budget="1000", contract_budget="500", reserve_cash_target="200"
            )
        ),
        now=NOW,
    )
    assert checks(report)["budget_allocation"]["status"] == "EXCEEDED"
    assert Decimal(checks(report)["budget_allocation"]["actual"]) == Decimal("1700")
    assert Decimal(report["net_equity"]) == Decimal("1280")
    assert Decimal(report["cash_balance"]) == Decimal("1000")


def test_trade_loss_budget_uses_entry_stop_base_quantity_and_estimated_cost() -> None:
    report = analyze_risk(request(settings=settings(contract_trade_loss_budget="20")), now=NOW)
    known = checks(report)
    assert Decimal(known["contract_trade_loss:btc-long"]["actual"]) == Decimal("32")
    assert known["contract_trade_loss:btc-long"]["status"] == "EXCEEDED"
    assert Decimal(known["contract_trade_loss:btc-short"]["actual"]) == Decimal("12")
    assert known["contract_trade_loss:btc-short"]["status"] == "PASS"


@pytest.mark.parametrize("changes", [{"stop_price": None}, {"estimated_trade_cost": None}])
def test_missing_stop_or_cost_keeps_trade_loss_unknown(changes: dict[str, Any]) -> None:
    report = analyze_risk(request(contracts=[contract(**changes)], settings=settings()), now=NOW)
    assert checks(report)["contract_trade_loss:btc-long"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("shock,pnl,equity", [("-0.1", "-40", "1240"), ("0.1", "40", "1320")])
def test_underlying_scenarios_apply_signed_moves_to_spot_long_and_short(
    shock: str, pnl: str, equity: str
) -> None:
    report = analyze_risk(request(scenarios=[scenario(shock=shock)]), now=NOW)
    result = report["scenarios"][0]
    assert result["complete"] is True
    assert Decimal(result["total_pnl"]) == Decimal(pnl)
    assert Decimal(result["equity_after"]) == Decimal(equity)
    assert len(result["legs"]) == 3


def test_scenario_loss_budget_reports_loss_as_positive_amount() -> None:
    report = analyze_risk(
        request(scenarios=[scenario()], settings=settings(max_scenario_loss="30")), now=NOW
    )
    item = checks(report)["scenario_loss:btc_down"]
    assert Decimal(item["actual"]) == Decimal("40")
    assert item["status"] == "EXCEEDED"
    assert item["reasons"]


def test_missing_underlying_shock_is_unknown_instead_of_zero() -> None:
    partial = ShockScenario.model_validate({"name": "missing BTC", "shocks": {"ETH": "-0.1"}})
    report = analyze_risk(request(scenarios=[partial], settings=settings()), now=NOW)
    result = report["scenarios"][0]
    assert result["complete"] is False
    assert result["total_pnl"] is None
    assert result["equity_after"] is None
    assert checks(report)["scenario_loss:missing BTC"]["status"] == "UNKNOWN"


def test_total_price_loss_shock_boundary_retains_short_gains() -> None:
    report = analyze_risk(request(scenarios=[scenario(shock="-1")]), now=NOW)
    result = report["scenarios"][0]
    assert result["complete"] is True
    assert Decimal(result["total_pnl"]) == Decimal("-400")
    assert Decimal(result["equity_after"]) == Decimal("880")
    short_leg = next(leg for leg in result["legs"] if leg["contract_id"] == "btc-short")
    assert Decimal(short_leg["pnl"]) == Decimal("100")


def test_known_zero_holdings_do_not_require_market_quote_for_cash_equity() -> None:
    data = request(
        cash=cash(available="1000"),
        cash_scope="spot_only",
        spot_positions=[spot(quantity="0")],
        contracts=[],
        contract_collateral="0",
        quotes=[],
    )
    report = analyze_risk(data, now=NOW)
    assert report["valuation_complete"] is True
    assert Decimal(report["net_equity"]) == Decimal("1000")
    assert Decimal(report["gross_exposure"]) == 0


def test_quote_at_freshness_boundary_is_valid() -> None:
    report = analyze_risk(
        request(quotes=[quote(at=NOW - timedelta(seconds=300)), quote("BTC-USDT-SWAP")]),
        now=NOW,
    )
    assert report["valuation_complete"] is True
    assert Decimal(report["net_equity"]) == Decimal("1280")


@pytest.mark.parametrize("maximum_age", [0, -1, True])
def test_invalid_quote_freshness_configuration_is_rejected(maximum_age: Any) -> None:
    with pytest.raises(ValueError):
        analyze_risk(request(), now=NOW, max_quote_age_seconds=maximum_age)


def test_unknown_metadata_does_not_guess_underlying_or_asset_class() -> None:
    report = analyze_risk(request(products={}, scenarios=[scenario()]), now=NOW)
    assert report["scenarios"][0]["complete"] is False
    assert report["scenarios"][0]["total_pnl"] is None
    assert all(item["name"] != "BTC" for item in report["by_underlying"])
    assert all(item["name"] != "crypto" for item in report["by_asset_class"])
    assert report["correlation_status"] == "not_calculated"


def test_contract_over_72h_only_prompts_review_and_does_not_set_spot_exit_time() -> None:
    report = analyze_risk(
        request(contracts=[contract(opened_at=NOW - timedelta(hours=73))]), now=NOW
    )
    assert report["action"] is None
    assert report["contracts"][0]["holding_review_required"] is True
    assert report["spot_positions"][0]["max_holding_hours"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"opened_at": NOW + timedelta(seconds=1)},
        {"opened_at": START + timedelta(seconds=1)},
        {"updated_at": NOW + timedelta(seconds=1)},
    ],
)
def test_invalid_contract_timeline_blocks_valuation_and_stop_loss(changes: dict[str, Any]) -> None:
    report = analyze_risk(request(contracts=[contract(**changes)]), now=NOW)
    assert report["net_equity"] is None
    assert report["valuation_complete"] is False
    assert report["contracts"][0]["stop_loss_assumption"] is None
    assert report["contracts"][0]["reasons"]


@pytest.mark.parametrize("kind", ["cash", "spot", "contract"])
def test_records_later_than_snapshot_cannot_produce_complete_equity(kind: str) -> None:
    changes: dict[str, Any] = {"as_of": START}
    if kind == "cash":
        changes["cash"] = cash(updated_at=NOW)
    elif kind == "spot":
        changes["spot_positions"] = [spot(updated_at=NOW)]
    else:
        changes["contracts"] = [contract(updated_at=NOW)]
    report = analyze_risk(request(**changes), now=NOW)
    assert report["net_equity"] is None
    assert report["valuation_complete"] is False


def test_overdue_contract_is_visible_even_with_all_limits_satisfied() -> None:
    report = analyze_risk(
        request(
            contracts=[contract(opened_at=NOW - timedelta(hours=73))],
            settings=settings(),
            scenarios=[scenario()],
        ),
        now=NOW,
    )
    assert report["valuation_complete"] is True
    assert report["overall_status"] == "INCOMPLETE"
    assert any("72小时" in reason for reason in report["reasons"])


@pytest.mark.parametrize("balance,expected", [("0", "-99"), ("99", "0")])
def test_nonpositive_equity_has_no_weights_or_passing_ratio_check(
    balance: str, expected: str
) -> None:
    data = request(
        cash=cash(balance=balance, available=balance),
        contract_collateral="0",
        spot_positions=[],
        contracts=[contract("short", base_quantity="1", entry_price="1")],
        settings=settings(),
    )
    report = analyze_risk(data, now=NOW)
    assert Decimal(report["net_equity"]) == Decimal(expected)
    assert report["gross_exposure_ratio"] is None
    assert report["abs_net_exposure_ratio"] is None
    assert report["cash_ratio"] is None
    assert all(item["product_weight"] is None for item in report["by_product"])
    known = checks(report)
    assert known["equity_positive"]["status"] == "EXCEEDED"
    assert known["max_gross_exposure_ratio"]["status"] != "PASS"
    assert known["max_abs_net_exposure_ratio"]["status"] != "PASS"


def test_decimal_calculations_do_not_depend_on_low_caller_precision() -> None:
    with localcontext() as context:
        context.prec = 4
        report = analyze_risk(request(), now=NOW)
    assert Decimal(report["cash_ratio"]) == Decimal("0.703125")
    assert Decimal(report["gross_exposure_ratio"]) == Decimal("0.46875")


@pytest.mark.parametrize("value", [True, 1.1, "NaN", "Infinity", "-1"])
def test_invalid_financial_amounts_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        cash(balance=value)
    with pytest.raises(ValueError):
        spot(quantity=value)
    with pytest.raises(ValueError):
        contract(base_quantity=value)
    with pytest.raises(ValueError):
        quote(price=value)


@pytest.mark.parametrize("value", [True, -0.1, "-1.01", "NaN", "Infinity"])
def test_scenario_shocks_require_finite_decimal_inputs_not_below_minus_one(value: Any) -> None:
    with pytest.raises(ValueError):
        ShockScenario.model_validate({"name": "bad", "shocks": {"BTC": value}})


def test_duplicate_contract_ids_and_quotes_are_rejected() -> None:
    with pytest.raises(ValueError):
        request(contracts=[contract(), contract(id="btc-long", direction="short")])
    with pytest.raises(ValueError):
        request(quotes=[quote(), quote()])


def test_nonmatching_currencies_or_contract_quantity_units_are_rejected() -> None:
    with pytest.raises(ValueError):
        request(spot_positions=[spot(instrument="BTC-USD")])
    with pytest.raises(ValueError):
        request(contracts=[contract(instrument="BTC-USD-SWAP")])
    with pytest.raises(ValueError):
        contract(instrument="BTC-USDT")
    with pytest.raises(ValueError):
        contract(contracts="10")


def test_conflicting_asset_class_for_same_declared_underlying_is_rejected() -> None:
    with pytest.raises(ValueError):
        request(
            products={
                "BTC-USDT": ProductMetadata(underlying="BTC", asset_class="crypto"),
                "BTC-USDT-SWAP": ProductMetadata(underlying="BTC", asset_class="equity"),
            }
        )


def test_cash_scope_cannot_double_count_collateral_or_available_cash() -> None:
    with pytest.raises(ValueError):
        request(contract_collateral="1100")
    with pytest.raises(ValueError):
        request(contract_collateral="200")


def test_timestamps_and_verified_flags_are_explicit() -> None:
    with pytest.raises(ValueError):
        request(as_of=datetime(2026, 11, 1))
    with pytest.raises(ValueError):
        quote(at=datetime(2026, 11, 1))
    with pytest.raises(ValueError):
        cash(verified="true")
    with pytest.raises(ValueError):
        request(positions_complete="true")
