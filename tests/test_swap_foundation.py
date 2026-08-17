from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.swap.config import load_swap_backtest_config
from app.swap.data import build_bundle
from app.swap.domain import ContractSpecification, DataStatus, OpenInterestPoint, SwapCandle
from app.swap.indicators import anchored_vwap, macd
from app.swap.strategy import AnchoredVWAPMultifactorSwapStrategy


def _spec() -> ContractSpecification:
    return ContractSpecification(
        "BTC-USDT-SWAP",
        Decimal("0.01"),
        Decimal("1"),
        Decimal("0.1"),
        Decimal("1"),
        Decimal("1"),
        Decimal("100000"),
        Decimal("1"),
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _candle(index: int, timeframe: str = "5m", *, quote: bool = True) -> SwapCandle:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index)
    close = Decimal("100") + Decimal(index)
    return SwapCandle(
        "BTC-USDT-SWAP",
        timeframe,
        start,
        start + timedelta(minutes=5),
        close,
        close + 1,
        close - 1,
        close,
        Decimal("10"),
        Decimal("1000") if quote else None,
        True,
        completeness_status=DataStatus.COMPLETE,
    )


def test_contract_conversions_and_rejects_unsupported_contract() -> None:
    spec = _spec()
    assert spec.contracts_to_base_quantity(Decimal("2")) == Decimal("0.02")
    assert spec.notional_to_contracts(Decimal("2.99"), Decimal("100")) == Decimal("2")
    with pytest.raises(ValueError, match="USDT"):
        ContractSpecification(
            "BTC-USD-SWAP",
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
        )


def test_vwap_resets_at_utc_day_boundary() -> None:
    first = _candle(0)
    second = _candle(288)
    assert anchored_vwap([first, second]) == Decimal("388")


def test_bundle_rejects_missing_quote_volume_and_stale_oi() -> None:
    candles = [_candle(index, quote=False) for index in range(40)]
    bundle = build_bundle(
        candles[-1],
        candles,
        candles,
        candles,
        [OpenInterestPoint("BTC-USDT-SWAP", candles[0].close_time, Decimal("100"), None, None)],
        _spec(),
    )
    assert set(bundle.rejection_reasons) == {"quote_volume_missing", "oi_missing_or_stale"}


def test_macd_requires_confirmed_history_and_is_deterministic() -> None:
    candles = [_candle(index) for index in range(40)]
    value = macd(candles)
    assert value is not None and value.ready and value.above_zero


def test_strategy_returns_data_incomplete_without_future_oi() -> None:
    candles = [_candle(index) for index in range(40)]
    oi = [
        OpenInterestPoint(
            "BTC-USDT-SWAP", item.close_time, Decimal("100") + Decimal(index), None, None
        )
        for index, item in enumerate(candles)
    ]
    bundle = build_bundle(candles[-1], candles, candles, candles, oi, _spec())
    signal = AnchoredVWAPMultifactorSwapStrategy().evaluate(bundle)
    assert signal.action.value in {"data_incomplete", "no_trade", "open_long", "open_short"}


def test_swap_config_is_backtest_only() -> None:
    config = load_swap_backtest_config(
        __import__("pathlib").Path("configs/btc_swap_multifactor_backtest.yaml")
    )
    assert config.market.instrument_id == "BTC-USDT-SWAP"
