from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.market.historical_data import MarketDataError
from app.market.okx_public import OKXPublicHistoricalDataProvider


class _Transport:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, params, headers))
        return self.response


def _row(timestamp: datetime, *, confirmed: bool = True) -> list[str]:
    return [
        str(int(timestamp.timestamp() * 1000)),
        "100",
        "101",
        "99",
        "100.5",
        "12",
        "0",
        "0",
        "1" if confirmed else "0",
    ]


def test_public_history_uses_only_market_endpoint_and_returns_confirmed_candles() -> None:
    later = datetime(2026, 1, 1, 1, tzinfo=UTC)
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    transport = _Transport(
        httpx.Response(200, json={"code": "0", "data": [_row(later), _row(earlier)]})
    )
    provider = OKXPublicHistoricalDataProvider(transport)

    candles = provider.get_historical_bars("BTC-USDT", "1h", limit=2)

    assert [candle.timestamp for candle in candles] == [earlier, later]
    assert all(candle.confirmed for candle in candles)
    assert transport.calls == [
        (
            "https://www.okx.com/api/v5/market/history-candles",
            {"instId": "BTC-USDT", "bar": "1H", "limit": "2"},
            {"Accept": "application/json"},
        )
    ]
    assert provider.public_rest_calls == 1


@pytest.mark.parametrize(
    "payload",
    (
        {"code": "0", "data": [["bad"]]},
        {"code": "0", "data": "bad"},
        {"code": "0", "data": [_row(datetime(2026, 1, 1, tzinfo=UTC))] * 2},
        {"code": "0", "data": [_row(datetime(2026, 1, 1, tzinfo=UTC), confirmed=False)]},
    ),
)
def test_public_history_rejects_invalid_or_nonconfirmed_response(
    payload: dict[str, object],
) -> None:
    provider = OKXPublicHistoricalDataProvider(_Transport(httpx.Response(200, json=payload)))
    with pytest.raises(MarketDataError):
        provider.get_historical_bars("BTC-USDT", "1h")


def test_public_history_rejects_credentials_and_nonfixed_capability() -> None:
    provider = OKXPublicHistoricalDataProvider(
        _Transport(httpx.Response(200, json={"code": "0", "data": []}))
    )
    with pytest.raises(ValueError):
        provider.get_historical_bars("ETH-USDT", "1h")
    with pytest.raises(ValueError):
        provider.get_historical_bars("BTC-USDT", "5m")
