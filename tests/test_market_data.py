from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.historical_data import (
    MarketDataError,
    load_candles_csv,
    normalize_candles,
    save_candles_csv,
)
from tests.conftest import make_candles


def test_csv_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "candles.csv"
    candles = make_candles(["100", "101", "102"])
    save_candles_csv(candles, path)
    assert load_candles_csv(path, bar="5m") == candles


def test_duplicate_timestamp_is_removed() -> None:
    candles = make_candles(["100", "101"])
    assert len(normalize_candles([candles[0], candles[0], candles[1]], bar="5m")) == 2


def test_out_of_order_is_rejected() -> None:
    candles = make_candles(["100", "101"])
    with pytest.raises(MarketDataError, match="乱序"):
        normalize_candles(list(reversed(candles)), bar="5m")


def test_missing_bar_is_rejected() -> None:
    candles = make_candles(["100", "101"])
    candles[1] = replace(candles[1], timestamp=candles[1].timestamp + timedelta(minutes=5))
    with pytest.raises(MarketDataError, match="缺失"):
        normalize_candles(candles, bar="5m")


def test_invalid_ohlc_is_rejected() -> None:
    candle = make_candles(["100"])[0]
    invalid = replace(candle, high=Decimal("99"))
    with pytest.raises(MarketDataError, match="OHLC"):
        normalize_candles([invalid], bar="5m")
