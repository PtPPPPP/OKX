"""Bounded, public-only data collection for daily reference reports."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import httpx

from app.domain.market import Candle
from app.market.historical_data import MarketDataError, normalize_candles
from app.market.network import NetworkConfiguration

HOUR = timedelta(hours=1)
PUBLIC_ENDPOINTS = frozenset(
    {
        "/api/v5/public/time",
        "/api/v5/public/instruments",
        "/api/v5/market/history-candles",
        "/api/v5/market/ticker",
        "/api/v5/public/funding-rate",
        "/api/v5/public/open-interest",
    }
)


class DailyPublicClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or NetworkConfiguration.from_environment().create_http_client(
            timeout=httpx.Timeout(15, connect=5)
        )
        self.sleep = sleep

    def close(self) -> None:
        self.client.close()

    def get(self, endpoint: str, params: dict[str, str]) -> list[Any]:
        if endpoint not in PUBLIC_ENDPOINTS:
            raise ValueError("daily report accepts only allowlisted public endpoints")
        for attempt in range(3):
            self.sleep(0.15)
            try:
                response = self.client.get("https://www.okx.com" + endpoint, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise MarketDataError("public API temporarily unavailable")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("code") != "0":
                    raise MarketDataError("public API returned a non-success code")
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise MarketDataError("public API data must be a list")
                return rows
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise MarketDataError(f"public request failed: {endpoint}") from None
                self.sleep(0.5 * 2**attempt + random.uniform(0, 0.2))
        raise AssertionError("unreachable")

    def server_time(self) -> datetime:
        rows = self.get("/api/v5/public/time", {})
        if not rows or not isinstance(rows[0], dict) or not rows[0].get("ts"):
            raise MarketDataError("server time is missing")
        return datetime.fromtimestamp(int(rows[0]["ts"]) / 1000, UTC)

    def instruments(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for kind in ("SPOT", "SWAP"):
            for row in self.get("/api/v5/public/instruments", {"instType": kind}):
                if isinstance(row, dict) and row.get("instId"):
                    result[str(row["instId"])] = row
        return result

    def candles(
        self, instrument: str, *, cutoff: datetime, days: int, bar: Literal["1h", "1d"] = "1h"
    ) -> list[Candle]:
        if bar not in {"1h", "1d"} or not 1 <= days <= 3650 or cutoff.tzinfo is None:
            raise ValueError("invalid public history request")
        interval = HOUR if bar == "1h" else timedelta(days=1)
        api_bar = "1H" if bar == "1h" else "1Dutc"
        bars_per_day = 24 if bar == "1h" else 1
        start = cutoff - timedelta(days=days)
        cursor = str(int(cutoff.timestamp() * 1000))
        gathered: dict[datetime, Candle] = {}
        # 100 rows per page; one spare page covers an incomplete current candle.
        for _ in range((days * bars_per_day + 99) // 100 + 2):
            rows = self.get(
                "/api/v5/market/history-candles",
                {
                    "instId": instrument,
                    "bar": api_bar,
                    "limit": "100",
                    "after": cursor,
                },
            )
            if not rows:
                break
            if any(not isinstance(row, list) or len(row) != 9 for row in rows):
                raise MarketDataError("invalid candle schema")
            oldest = min(int(row[0]) for row in rows)
            if oldest >= int(cursor):
                raise MarketDataError("historical pagination made no progress")
            for row in rows:
                if len(row) != 9 or str(row[8]) not in {"0", "1"}:
                    raise MarketDataError("invalid candle schema")
                stamp = datetime.fromtimestamp(int(row[0]) / 1000, UTC)
                if str(row[8]) != "1" or stamp < start or stamp + interval > cutoff:
                    continue
                # volCcy is base units for both SPOT and SWAP, unlike contract volume.
                candle = Candle(
                    timestamp=stamp,
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[6])),
                    confirmed=True,
                )
                if stamp in gathered and gathered[stamp] != candle:
                    raise MarketDataError("conflicting duplicate candle")
                gathered[stamp] = candle
            if oldest <= int(start.timestamp() * 1000):
                break
            cursor = str(oldest)
        candles = normalize_candles(sorted(gathered.values(), key=lambda x: x.timestamp), bar=bar)
        if any(
            c.timestamp.minute or c.timestamp.second or c.timestamp.microsecond for c in candles
        ):
            raise MarketDataError("candles must align to UTC hours")
        if bar == "1d" and any(c.timestamp.hour for c in candles):
            raise MarketDataError("daily candles must align to UTC midnight")
        if not candles or cutoff - (candles[-1].timestamp + interval) > interval:
            raise MarketDataError("latest confirmed candle is missing or stale")
        return candles

    def context(self, instrument: str, *, cutoff: datetime) -> dict[str, Any]:
        result: dict[str, Any] = {"quality": "READY", "warnings": []}
        endpoints = [("ticker", "/api/v5/market/ticker")]
        if instrument.endswith("-SWAP"):
            endpoints.extend(
                [
                    ("funding", "/api/v5/public/funding-rate"),
                    ("open_interest", "/api/v5/public/open-interest"),
                ]
            )
        for name, endpoint in endpoints:
            try:
                rows = self.get(endpoint, {"instId": instrument})
                if not rows or not isinstance(rows[0], dict) or rows[0].get("instId") != instrument:
                    raise MarketDataError("missing or mismatched snapshot")
                row = rows[0]
                stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, UTC)
                # Snapshots are observed after the candle cutoff; never feed them into history.
                if not -60 <= (datetime.now(UTC) - stamp).total_seconds() <= 600:
                    raise MarketDataError("snapshot is stale or future-dated")
                fields = {
                    "ticker": ("bidPx", "askPx", "last"),
                    "funding": ("fundingRate",),
                    "open_interest": ("oi", "oiUsd"),
                }[name]
                for field in fields:
                    number = Decimal(str(row[field]))
                    if not number.is_finite() or (name != "funding" and number < 0):
                        raise MarketDataError("invalid snapshot number")
                if name == "ticker" and not (0 < Decimal(row["bidPx"]) <= Decimal(row["askPx"])):
                    raise MarketDataError("missing or crossed quote")
                if name == "funding":
                    datetime.fromtimestamp(int(row["fundingTime"]) / 1000, UTC)
                result[name] = row
            except (MarketDataError, KeyError, TypeError, ValueError, ArithmeticError):
                result["quality"] = "PARTIAL"
                result["warnings"].append(f"{name}: unavailable or stale")
        result["candle_cutoff"] = cutoff.isoformat()
        result["usage"] = "current context only; excluded from historical rules and replay"
        return result
