import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.position import AccountConfiguration, AccountMode, BalanceValidationStatus
from app.exchange.okx_models import (
    parse_account_configuration,
    parse_asset_balance,
    parse_portfolio,
)
from tests.conftest import make_instrument


def row(ccy: str = "USDT", **overrides: str) -> dict[str, str]:
    value = {
        "ccy": ccy,
        "cashBal": "10",
        "availBal": "7",
        "frozenBal": "3",
        "eq": "10",
        "eqUsd": "10",
        "uTime": "1767225600000",
    }
    value.update(overrides)
    return value


def spot() -> AccountConfiguration:
    return AccountConfiguration(AccountMode.SPOT, "net_mode", False, None, datetime.now(UTC))


def test_cash_available_and_frozen_remain_distinct() -> None:
    asset = parse_asset_balance(row(), account_mode=AccountMode.SPOT)
    assert asset.cash_balance == Decimal("10")
    assert asset.available_balance == Decimal("7")
    assert asset.frozen_balance == Decimal("3")
    assert asset.holding_quantity == Decimal("10")
    assert asset.spendable_quantity == Decimal("7")


def test_missing_fields_are_not_invented() -> None:
    payload = row()
    payload.pop("frozenBal")
    payload.pop("eq")
    asset = parse_asset_balance(payload, account_mode=AccountMode.SPOT)
    assert asset.frozen_balance is None
    assert asset.equity is None
    assert asset.validation_status is BalanceValidationStatus.PASSED


def test_unknown_mode_does_not_claim_spot_holdings() -> None:
    asset = parse_asset_balance(row())
    assert asset.holding_quantity is None
    assert asset.spendable_quantity is None
    assert asset.validation_status is BalanceValidationStatus.INSUFFICIENT_DATA


def test_account_mode_contract_is_explicit() -> None:
    assert (
        parse_account_configuration({"acctLv": "1", "posMode": "net_mode"}).account_mode
        is AccountMode.SPOT
    )
    assert (
        parse_account_configuration({"acctLv": "3"}).account_mode
        is AccountMode.MULTI_CURRENCY_MARGIN
    )


def test_spot_portfolio_uses_explicit_holding_authority() -> None:
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.0001", "0.1")
    portfolio = parse_portfolio(
        {
            "details": [
                row("BTC", cashBal="1", availBal="0.4", frozenBal="0.6"),
                row("USDT", cashBal="100", availBal="25", frozenBal="75"),
            ]
        },
        instrument,
        configuration=spot(),
    )
    assert portfolio.position("BTC-USDT") == Decimal("1")
    assert portfolio.available_position("BTC-USDT", "BTC") == Decimal("0.4")
    assert portfolio.trusted_for_trading


def test_sanitized_okx_fixture_preserves_account_and_managed_equity_separately() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "okx"
    configuration = parse_account_configuration(
        json.loads((fixture_dir / "account_config_spot.json").read_text(encoding="utf-8"))
    )
    portfolio = parse_portfolio(
        json.loads((fixture_dir / "account_balance_spot.json").read_text(encoding="utf-8")),
        make_instrument("BTC-USDT", "BTC", "USDT", "0.0001", "0.1"),
        configuration=configuration,
    )
    assert portfolio.account_equity is not None
    assert portfolio.account_equity.okx_total_equity == Decimal("110")
    assert portfolio.position("BTC-USDT") == Decimal("1")
