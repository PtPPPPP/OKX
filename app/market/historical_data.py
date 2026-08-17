from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path

from app.domain.market import Candle


class MarketDataError(ValueError):
    pass


BAR_INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}


def normalize_candles(candles: Iterable[Candle], *, bar: str) -> list[Candle]:
    interval = BAR_INTERVALS.get(bar.lower())
    if interval is None:
        raise MarketDataError(f"不支持的 K 线周期: {bar}")

    raw = list(candles)
    if any(candle.timestamp.tzinfo is None for candle in raw):
        raise MarketDataError("K 线时间必须包含时区")
    timestamps = [candle.timestamp.astimezone(UTC) for candle in raw]
    if timestamps != sorted(timestamps):
        raise MarketDataError("K 线时间乱序")

    unique: list[Candle] = []
    seen: set[datetime] = set()
    for candle in raw:
        timestamp = candle.timestamp.astimezone(UTC)
        if timestamp in seen:
            continue
        seen.add(timestamp)
        normalized = Candle(
            timestamp=timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            confirmed=candle.confirmed,
        )
        _validate_ohlc(normalized)
        unique.append(normalized)

    for previous, current in pairwise(unique):
        if current.timestamp - previous.timestamp != interval:
            raise MarketDataError(
                f"K 线缺失: {previous.timestamp.isoformat()} 到 {current.timestamp.isoformat()}"
            )
    return unique


def _validate_ohlc(candle: Candle) -> None:
    values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    if any(not value.is_finite() for value in values):
        raise MarketDataError("K 线包含非有限数值")
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        raise MarketDataError("OHLC 必须大于 0")
    if candle.volume < 0:
        raise MarketDataError("成交量不得为负数")
    if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        raise MarketDataError("OHLC 高低价关系非法")
    if candle.high < candle.low:
        raise MarketDataError("最高价不得低于最低价")


def save_candles_csv(candles: Iterable[Candle], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume", "confirmed"],
        )
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "timestamp": candle.timestamp.astimezone(UTC).isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                    "confirmed": "1" if candle.confirmed else "0",
                }
            )


def load_candles_csv(path: Path, *, bar: str) -> list[Candle]:
    if not path.is_file():
        raise MarketDataError(f"K 线文件不存在: {path}")
    candles: list[Candle] = []
    try:
        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            required = {"timestamp", "open", "high", "low", "close", "volume", "confirmed"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise MarketDataError("CSV 缺少必要列")
            for row in reader:
                candles.append(
                    Candle(
                        timestamp=datetime.fromisoformat(row["timestamp"]).astimezone(UTC),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                        confirmed=row["confirmed"].strip().lower() in {"1", "true", "yes"},
                    )
                )
    except (KeyError, InvalidOperation, ValueError) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError(f"CSV K 线格式错误: {exc}") from exc
    return normalize_candles(candles, bar=bar)


def okx_bar(bar: str) -> str:
    normalized = bar.lower()
    if normalized not in BAR_INTERVALS:
        raise MarketDataError(f"不支持的 K 线周期: {bar}")
    if normalized.endswith("h") or normalized.endswith("d"):
        return normalized[:-1] + normalized[-1].upper()
    return normalized
