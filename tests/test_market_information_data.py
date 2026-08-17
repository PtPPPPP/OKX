from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.market.network import NetworkConfiguration, NetworkMode
from backtest.market_information_data import (
    ImmutableJsonRecords,
    OKXMarketInformationClient,
    download_funding,
)


class FakeTransport:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, str]] = []
        self.closed = False

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        self.calls.append(params)
        payload = self.payloads.pop(0)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    def close(self) -> None:
        self.closed = True


def _client(transport: FakeTransport) -> OKXMarketInformationClient:
    return OKXMarketInformationClient(
        NetworkConfiguration(NetworkMode.PROXY, "http://127.0.0.1:7890"),
        transport=transport,
        sleep=lambda _: None,
    )


def test_client_rejects_non_proxy_mode() -> None:
    with pytest.raises(ValueError, match="proxy"):
        OKXMarketInformationClient(NetworkConfiguration(NetworkMode.DIRECT))


def test_client_uses_public_endpoint_and_closes() -> None:
    transport = FakeTransport([{"code": "0", "data": []}])
    client = _client(transport)
    assert client.get("/api/v5/public/time", {})["code"] == "0"
    client.close()
    assert transport.closed


def test_client_rejects_nonzero_api_code() -> None:
    client = _client(FakeTransport([{"code": "1", "data": []}] * 3))
    with pytest.raises(Exception, match="code"):
        client.get("/api/v5/public/time", {})


def test_immutable_records_detect_duplicate_and_revision(tmp_path: Path) -> None:
    cache = ImmutableJsonRecords(tmp_path)
    assert cache.commit(1, {"x": 1}) == "new"
    assert cache.commit(1, {"x": 1}) == "duplicate"
    assert cache.commit(1, {"x": 2}) == "revision"
    assert cache.rows() == [{"x": 1}]


def test_page_payload_is_immutable(tmp_path: Path) -> None:
    cache = ImmutableJsonRecords(tmp_path)
    cache.save_page("same", {"version": 1})
    cache.save_page("same", {"version": 2})
    saved = json.loads(next(cache.pages.iterdir()).read_text(encoding="utf-8"))
    assert saved == {"version": 1}


def test_checkpoint_round_trip(tmp_path: Path) -> None:
    cache = ImmutableJsonRecords(tmp_path)
    cache.save_checkpoint(cursor="123", complete=False)
    assert cache.checkpoint() == {"cursor": "123", "complete": False}


def test_completed_funding_download_is_resume_safe(tmp_path: Path) -> None:
    cache = ImmutableJsonRecords(tmp_path / "raw" / "funding")
    cache.save_checkpoint(cursor="123", complete=True)
    transport = FakeTransport([])
    result = download_funding(_client(transport), tmp_path)
    assert result.requests == 0
    assert transport.calls == []


def test_funding_download_filters_after_cutoff(tmp_path: Path) -> None:
    payload = {
        "code": "0",
        "data": [
            {"fundingTime": "1786550399999", "realizedRate": "0.1"},
            {"fundingTime": "1786550400000", "realizedRate": "0.2"},
        ],
    }
    result = download_funding(_client(FakeTransport([payload])), tmp_path)
    assert result.rows == 1
    assert result.retention_limited


def test_dataset_hash_is_deterministic(tmp_path: Path) -> None:
    payload = {"code": "0", "data": [{"fundingTime": "1786550399999", "realizedRate": "0.1"}]}
    first = download_funding(_client(FakeTransport([payload])), tmp_path)
    second = download_funding(_client(FakeTransport([])), tmp_path)
    assert first.dataset_hash == second.dataset_hash
