from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.market.network import NetworkConfiguration
from backtest.vwap_signal_edge_data import HistoricalCandleCache, OKXHistoricalCandleDownloader


def _network() -> NetworkConfiguration:
    return NetworkConfiguration.from_environment(
        {"OKX_NETWORK_MODE": "proxy", "OKX_PROXY_URL": "http://127.0.0.1:7890"}
    )


def _row(timestamp: datetime, *, confirm: str = "1") -> list[str]:
    return [
        str(int(timestamp.timestamp() * 1000)),
        "100",
        "102",
        "99",
        "101",
        "10",
        "1010",
        "1010",
        confirm,
    ]


class _PagedTransport:
    def __init__(self, pages: list[list[list[str]] | Exception]) -> None:
        self.pages = list(pages)
        self.params: list[dict[str, str]] = []

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        assert url.endswith("/api/v5/market/history-candles")
        assert headers == {"Accept": "application/json"}
        self.params.append(params)
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, json={"code": "0", "data": result})


def test_paginated_download_preserves_raw_and_normalized(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [_row(start + timedelta(hours=index)) for index in range(4)]
    transport = _PagedTransport([list(reversed(rows[2:])), list(reversed(rows[:2]))])
    cache = HistoricalCandleCache(tmp_path / "cache")
    downloader = OKXHistoricalCandleDownloader(
        cache, network=_network(), transport=transport, retry_limit=0, page_limit=2
    )
    result = downloader.download(
        instrument="BTC-USDT", bar="1h", start=start, end=start + timedelta(hours=3)
    )

    assert result.normalized_rows == 4
    assert result.status == "complete"
    assert result.missing_count == result.duplicate_count == result.invalid_ohlc_count == 0
    assert cache.raw_path.read_text(encoding="utf-8").count("\n") == 4
    assert len(cache.normalized_path.read_text(encoding="utf-8").splitlines()) == 5
    assert transport.params[1]["after"] == rows[2][0]


def test_resume_starts_from_committed_checkpoint(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [_row(start + timedelta(hours=index)) for index in range(4)]
    cache = HistoricalCandleCache(tmp_path / "cache")
    first = _PagedTransport(
        [
            list(reversed(rows[2:])),
            httpx.ConnectError("offline", request=httpx.Request("GET", "https://www.okx.com")),
        ]
    )
    downloader = OKXHistoricalCandleDownloader(
        cache, network=_network(), transport=first, retry_limit=0, page_limit=2
    )
    with pytest.raises(httpx.ConnectError):
        downloader.download(
            instrument="BTC-USDT", bar="1h", start=start, end=start + timedelta(hours=3)
        )

    second = _PagedTransport([list(reversed(rows[:2]))])
    resumed = OKXHistoricalCandleDownloader(
        cache, network=_network(), transport=second, retry_limit=0, page_limit=2
    ).download(instrument="BTC-USDT", bar="1h", start=start, end=start + timedelta(hours=3))

    assert resumed.normalized_rows == 4
    assert second.params[0]["after"] == rows[2][0]
    assert resumed.download_failures == 1


def test_quality_manifest_detects_missing_unconfirmed_and_duplicates(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    page = [
        _row(start + timedelta(hours=3)),
        _row(start + timedelta(hours=2), confirm="0"),
        _row(start),
        _row(start),
    ]
    transport = _PagedTransport([page])
    result = OKXHistoricalCandleDownloader(
        HistoricalCandleCache(tmp_path / "cache"),
        network=_network(),
        transport=transport,
        retry_limit=0,
        page_limit=4,
    ).download(instrument="BTC-USDT", bar="1h", start=start, end=start + timedelta(hours=3))

    assert result.duplicate_count == 1
    assert result.unconfirmed_count == 1
    assert result.missing_count == 2
    assert result.status == "quality_failed"


def test_downloader_rejects_non_proxy_network(tmp_path: Path) -> None:
    direct = NetworkConfiguration.from_environment({"OKX_NETWORK_MODE": "direct"})
    with pytest.raises(ValueError, match="proxy"):
        OKXHistoricalCandleDownloader(HistoricalCandleCache(tmp_path), network=direct)


def test_resume_rejects_cache_from_different_request(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    cache = HistoricalCandleCache(tmp_path / "cache")
    first = _PagedTransport([[_row(start)]])
    OKXHistoricalCandleDownloader(
        cache, network=_network(), transport=first, retry_limit=0, page_limit=1
    ).download(instrument="BTC-USDT", bar="1h", start=start, end=start + timedelta(hours=1))

    second = _PagedTransport([])
    downloader = OKXHistoricalCandleDownloader(
        cache, network=_network(), transport=second, retry_limit=0, page_limit=1
    )
    with pytest.raises(ValueError, match="different history request"):
        downloader.download(
            instrument="ETH-USDT", bar="1h", start=start, end=start + timedelta(hours=1)
        )
