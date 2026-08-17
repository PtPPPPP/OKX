"""Public-only OKX market-data adapters with no credential or order capability."""

from __future__ import annotations

from datetime import datetime
from decimal import InvalidOperation
from typing import Protocol

import httpx

from app.domain.market import Candle
from app.exchange.exceptions import ExchangeError, NetworkError, RequestTimeout
from app.exchange.okx_models import parse_candle
from app.market.historical_data import MarketDataError, normalize_candles, okx_bar
from app.market.network import NetworkConfiguration
from app.market.providers import MarketDataProvider


class PublicHTTPTransport(Protocol):
    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response: ...


class OKXPublicHistoricalDataProvider(MarketDataProvider):
    """Read-only capability for the OKX public historical-candle endpoint."""

    base_url = "https://www.okx.com"
    endpoint = "/api/v5/market/history-candles"

    def __init__(
        self,
        transport: PublicHTTPTransport | None = None,
        *,
        network: NetworkConfiguration | None = None,
    ) -> None:
        self.network = network or NetworkConfiguration.from_environment()
        self.transport = transport or self.network.create_http_client(
            timeout=httpx.Timeout(10.0, connect=5.0)
        )
        self.public_rest_calls = 0

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        if start is not None or end is not None:
            raise ValueError("public historical adapter supports limit only")
        if instrument_id != "BTC-USDT" or bar.lower() != "1h":
            raise ValueError("public continuous Shadow supports BTC-USDT 1H only")
        requested = limit or 300
        if not 1 <= requested <= 300:
            raise ValueError("public historical candle limit must be in 1..300")
        try:
            response = self.transport.get(
                f"{self.base_url}{self.endpoint}",
                params={"instId": instrument_id, "bar": okx_bar(bar), "limit": str(requested)},
                headers={"Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise RequestTimeout("public historical candle request timed out") from exc
        except httpx.RequestError as exc:
            raise NetworkError("public historical candle request failed") from exc
        self.public_rest_calls += 1
        if response.status_code != 200:
            raise ExchangeError(f"public historical candle HTTP status: {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError("public historical candle response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise MarketDataError("public historical candle response is invalid")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise MarketDataError("public historical candle data is invalid")
        try:
            candles = [parse_candle(row) for row in rows if isinstance(row, list)]
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise MarketDataError("public historical candle values are invalid") from exc
        if len(candles) != len(rows) or len({candle.timestamp for candle in candles}) != len(
            candles
        ):
            raise MarketDataError("public historical candle timestamps are invalid")
        confirmed = sorted(
            (candle for candle in candles if candle.confirmed), key=lambda candle: candle.timestamp
        )
        if not confirmed:
            raise MarketDataError("public historical candle response has no confirmed bars")
        return normalize_candles(confirmed, bar=bar)
