from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings, TradingMode
from app.exchange.auth_diagnostics import AuthenticationCategory
from app.exchange.exceptions import AuthenticationError
from app.exchange.okx_client import OkxClient
from app.runtime.clock import BacktestClock


def settings() -> Settings:
    return Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("api-key"),
        okx_secret_key=SecretStr("never-emit-this-secret"),
        okx_passphrase=SecretStr("never-emit-this-passphrase"),
    )


def test_rest_signature_matches_fixed_okx_prehash_vector() -> None:
    vector_settings = Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr("api-key"),
        okx_secret_key=SecretStr("secret"),
        okx_passphrase=SecretStr("passphrase"),
    )
    client = OkxClient(
        vector_settings,
        clock=BacktestClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    headers = client._auth_headers("get", "/api/v5/account/config", "")
    assert headers["OK-ACCESS-TIMESTAMP"] == "2026-01-01T00:00:00.000Z"
    assert headers["OK-ACCESS-SIGN"] == "d10BcO+AkQ6DXMpiXA7gWN34xoj0RtyiZNij+pNgfcE="


def test_private_request_uses_demo_header_and_full_query_path() -> None:
    received: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.raw_path.decode()
        received["simulated"] = request.headers["x-simulated-trading"]
        received["signature"] = request.headers["OK-ACCESS-SIGN"]
        return httpx.Response(200, json={"code": "0", "data": [{}]})

    client = OkxClient(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=BacktestClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    client.server_time_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    client._request("GET", "/api/v5/account/balance", params={"ccy": "BTC,USDT"}, private=True)
    assert received["path"] == "/api/v5/account/balance?ccy=BTC%2CUSDT"
    assert received["simulated"] == "1"
    assert received["signature"]


def test_authentication_error_preserves_safe_okx_details_only() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "50113", "msg": "Invalid sign"})

    client = OkxClient(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=BacktestClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    client.server_time_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    with pytest.raises(AuthenticationError) as caught:
        client._request("GET", "/api/v5/account/config", private=True)
    diagnostic = client.authentication_diagnostic(caught.value).safe_dict()
    assert diagnostic["category"] == AuthenticationCategory.INVALID_SIGNATURE.value
    assert diagnostic["okx_code"] == "50113"
    assert diagnostic["request_path"] == "/api/v5/account/config"
    assert "api-key" not in str(diagnostic)
    assert "never-emit-this-secret" not in str(diagnostic)
    assert "never-emit-this-passphrase" not in str(diagnostic)


def test_credential_diagnostic_detects_whitespace_without_exposing_value() -> None:
    configured = Settings(
        trading_mode=TradingMode.DEMO,
        okx_api_key=SecretStr(" key "),
        okx_secret_key=SecretStr("secret"),
        okx_passphrase=SecretStr("passphrase"),
    )
    status = configured.credential_diagnostics()["OKX_API_KEY"]
    assert status.has_leading_or_trailing_whitespace
    assert not status.contains_linebreak
