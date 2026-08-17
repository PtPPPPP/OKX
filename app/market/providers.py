from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.domain.market import Candle
from app.exchange.okx_client import OkxClient
from app.market.historical_data import load_candles_csv


class MarketDataProvider(Protocol):
    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]: ...


class CSVMarketDataProvider:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        candles = load_candles_csv(self.path, bar=bar)
        filtered = [
            candle
            for candle in candles
            if (start is None or candle.timestamp >= start)
            and (end is None or candle.timestamp <= end)
        ]
        return filtered[-limit:] if limit is not None else filtered


class OKXHistoricalDataProvider:
    def __init__(self, client: OkxClient) -> None:
        self.client = client

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        if start is not None or end is not None:
            raise ValueError("当前 OKX 历史 Provider 只支持 limit 分页")
        candles = self.client.get_history_candles(instrument_id, bar, limit or 300)
        return [candle for candle in candles if candle.confirmed]
