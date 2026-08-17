from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config.settings import Settings
from app.domain.capability import MaxAvailableSize
from app.domain.environment import TradingEnvironment
from app.domain.market import Candle, Instrument, TradeMode
from app.domain.order import Order, OrderRequest, OrderState, OrderType
from app.domain.position import AccountConfiguration, PortfolioSnapshot
from app.exchange.auth_diagnostics import (
    AuthenticationCategory,
    AuthenticationDiagnostic,
    classify_authentication_failure,
)
from app.exchange.exceptions import (
    AuthenticationError,
    ExchangeError,
    NetworkError,
    OrderNotFound,
    OrderRejected,
    RateLimitError,
    RequestTimeout,
)
from app.exchange.okx_models import (
    parse_account_configuration,
    parse_candle,
    parse_cost_fill,
    parse_derivative_positions,
    parse_instrument,
    parse_order,
    parse_portfolio,
)
from app.exchange.recovery_models import (
    AccountBill,
    ExchangeFill,
    ExchangeOrder,
    RecoveryQueryEvidence,
)
from app.execution.demo_write_authorization import (
    DemoWriteAuthorization,
    DemoWriteOperation,
    require_cancel_authorization,
    require_place_authorization,
)
from app.market.historical_data import normalize_candles, okx_bar
from app.market.network import NetworkConfiguration
from app.portfolio.cost_basis import CostFill
from app.runtime.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class OkxEnvironment:
    mode: TradingEnvironment
    rest_base_url: str
    public_ws_url: str
    private_ws_url: str
    simulated_trading: bool


DEMO_ENVIRONMENT = OkxEnvironment(
    TradingEnvironment.DEMO,
    "https://www.okx.com",
    "wss://wspap.okx.com:8443/ws/v5/public",
    "wss://wspap.okx.com:8443/ws/v5/private",
    True,
)


class OkxClient:
    """Minimal OKX V5 REST client. Private calls are permanently demo-only."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        clock: Clock | None = None,
        network: NetworkConfiguration | None = None,
        environment: OkxEnvironment = DEMO_ENVIRONMENT,
    ) -> None:
        self.settings = settings
        self.network = network or NetworkConfiguration.from_environment()
        self.client = client or self.network.create_http_client(
            timeout=httpx.Timeout(10.0, connect=5.0)
        )
        self.clock = clock or SystemClock()
        if not isinstance(environment, OkxEnvironment):
            raise PermissionError("OKX client requires an explicit known environment")
        if environment.mode is not TradingEnvironment.DEMO or not environment.simulated_trading:
            raise PermissionError("live or non-simulated OKX environment is not implemented")
        self.environment = environment
        self._demo_environment_verified = False
        self.server_time_ms: int | None = None
        self.server_time_synced_at: datetime | None = None
        self.server_offset_ms = 0
        self.last_authentication_diagnostic: AuthenticationDiagnostic | None = None
        self.public_rest_calls = 0
        self.private_rest_calls = 0
        self.private_api_write_calls = 0
        self.place_order_calls = 0
        self.cancel_order_calls = 0

    def close(self) -> None:
        self.client.close()

    def get_server_time(self) -> int:
        data = self._request("GET", "/api/v5/public/time")
        server_time = int(self._first(data)["ts"])
        local_time = self.clock.now()
        self.server_time_ms = server_time
        self.server_time_synced_at = local_time
        self.server_offset_ms = server_time - int(local_time.timestamp() * 1000)
        return server_time

    def get_instrument(self, instrument_id: str) -> Instrument:
        data = self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": instrument_id},
        )
        instrument = parse_instrument(self._first(data))
        if (
            instrument.instrument_id != instrument_id
            or not instrument.tradable
            or not instrument.base_currency
            or not instrument.quote_currency
            or instrument.price_tick <= 0
            or instrument.quantity_step <= 0
            or instrument.minimum_quantity <= 0
        ):
            raise ExchangeError(f"交易品种不存在或当前不可交易: {instrument_id}")
        return instrument

    def list_instruments(self) -> list[Instrument]:
        payload = self._request("GET", "/api/v5/public/instruments", params={"instType": "SPOT"})
        rows = payload.get("data", [])
        return [parse_instrument(row) for row in rows if isinstance(row, dict)]

    def get_instrument_rules(self, instrument_id: str) -> Instrument:
        return self.get_instrument(instrument_id)

    def get_history_candles(
        self,
        instrument_id: str,
        bar: str = "5m",
        limit: int = 300,
    ) -> list[Candle]:
        if not 1 <= limit <= 1000:
            raise ValueError("历史 K 线数量必须在 1 到 1000 之间")
        collected: dict[int, Candle] = {}
        after: str | None = None
        while len(collected) < limit:
            batch_limit = min(300, limit - len(collected))
            params = {
                "instId": instrument_id,
                "bar": okx_bar(bar),
                "limit": str(batch_limit),
            }
            if after:
                params["after"] = after
            payload = self._request("GET", "/api/v5/market/history-candles", params=params)
            rows = payload.get("data", [])
            if not rows:
                break
            parsed = [parse_candle(row) for row in rows]
            for candle in parsed:
                collected[int(candle.timestamp.timestamp() * 1000)] = candle
            oldest = min(int(row[0]) for row in rows)
            next_after = str(oldest)
            if next_after == after or len(rows) < batch_limit:
                break
            after = next_after
        ordered = [collected[key] for key in sorted(collected)][-limit:]
        return normalize_candles(ordered, bar=bar)

    def get_portfolio(
        self, instrument: Instrument, *, configuration: AccountConfiguration | None = None
    ) -> PortfolioSnapshot:
        account_configuration = configuration or self.get_account_configuration()
        currencies = f"{instrument.base_currency},{instrument.quote_currency}"
        payload = self._request(
            "GET", "/api/v5/account/balance", params={"ccy": currencies}, private=True
        )
        return parse_portfolio(
            self._first(payload), instrument, configuration=account_configuration
        )

    def authentication_diagnostic(
        self, error: AuthenticationError, *, endpoint: str | None = None
    ) -> AuthenticationDiagnostic:
        if isinstance(error.diagnostic, AuthenticationDiagnostic):
            return error.diagnostic
        return self._authentication_diagnostic(
            AuthenticationCategory.UNKNOWN_AUTHENTICATION_ERROR,
            endpoint=endpoint,
            request_path=endpoint,
            message=str(error),
        )

    def get_account_configuration(self) -> AccountConfiguration:
        payload = self._request("GET", "/api/v5/account/config", private=True)
        return parse_account_configuration(self._first(payload))

    def probe_demo_environment(self) -> AccountConfiguration:
        """Prove that credentials are accepted with the Demo-only request binding."""
        self._require_demo_environment()
        configuration = self.get_account_configuration()
        self._demo_environment_verified = True
        return configuration

    def get_pending_orders(self, instrument_id: str) -> list[Order]:
        payload = self._request(
            "GET",
            "/api/v5/trade/orders-pending",
            params={"instType": "SPOT", "instId": instrument_id},
            private=True,
        )
        return [parse_order(item) for item in payload.get("data", [])]

    def get_trade_fills(self, instrument_id: str) -> list[CostFill]:
        payload = self._request(
            "GET",
            "/api/v5/trade/fills-history",
            params={"instType": "SPOT", "instId": instrument_id},
            private=True,
        )
        rows = payload.get("data", [])
        return [parse_cost_fill(item) for item in rows if isinstance(item, dict)]

    def get_order_history(
        self, instrument_id: str, begin: datetime, end: datetime
    ) -> tuple[list[Order], RecoveryQueryEvidence]:
        params = {
            "instType": "SPOT",
            "instId": instrument_id,
            "begin": str(int(begin.timestamp() * 1000)),
            "end": str(int(end.timestamp() * 1000)),
            "limit": "100",
        }
        payload = self._request("GET", "/api/v5/trade/orders-history", params=params, private=True)
        rows = payload.get("data", [])
        orders = [parse_order(item) for item in rows if isinstance(item, dict)]
        return orders, RecoveryQueryEvidence(
            "/api/v5/trade/orders-history", begin, end, 1, len(orders), True
        )

    def get_order_history_archive(
        self, instrument_id: str, begin: datetime, end: datetime
    ) -> tuple[list[Order], RecoveryQueryEvidence]:
        params = {
            "instType": "SPOT",
            "instId": instrument_id,
            "begin": str(int(begin.timestamp() * 1000)),
            "end": str(int(end.timestamp() * 1000)),
            "limit": "100",
        }
        payload = self._request(
            "GET", "/api/v5/trade/orders-history-archive", params=params, private=True
        )
        rows = payload.get("data", [])
        orders = [parse_order(item) for item in rows if isinstance(item, dict)]
        return orders, RecoveryQueryEvidence(
            "/api/v5/trade/orders-history-archive", begin, end, 1, len(orders), True
        )

    def get_recovery_orders(
        self, instrument_id: str, begin: datetime, end: datetime, *, archive: bool = False
    ) -> tuple[list[ExchangeOrder], RecoveryQueryEvidence]:
        path = "/api/v5/trade/orders-history-archive" if archive else "/api/v5/trade/orders-history"
        rows, evidence = self._paged_recovery_rows(path, instrument_id, begin, end)
        return [_exchange_order(row) for row in rows], evidence

    def get_recovery_orders_pending(
        self, instrument_id: str, begin: datetime, end: datetime
    ) -> tuple[list[ExchangeOrder], RecoveryQueryEvidence]:
        payload = self._request(
            "GET",
            "/api/v5/trade/orders-pending",
            params={"instType": "SPOT", "instId": instrument_id},
            private=True,
        )
        rows = [row for row in payload.get("data", []) if isinstance(row, dict)]
        evidence = RecoveryQueryEvidence(
            "/api/v5/trade/orders-pending", begin, end, 1, len(rows), True
        )
        return [_exchange_order(row) for row in rows], evidence

    def get_recovery_fills(
        self, instrument_id: str, begin: datetime, end: datetime
    ) -> tuple[list[ExchangeFill], RecoveryQueryEvidence]:
        """Retrieve the only documented private fill history endpoint (last three months)."""
        rows, evidence = self._paged_recovery_rows(
            "/api/v5/trade/fills-history", instrument_id, begin, end, cursor_key="tradeId"
        )
        return [_exchange_fill(row) for row in rows], evidence

    def get_recovery_bills(
        self, instrument_id: str, begin: datetime, end: datetime
    ) -> tuple[list[AccountBill], RecoveryQueryEvidence]:
        rows, evidence = self._paged_recovery_rows(
            "/api/v5/account/bills", instrument_id, begin, end
        )
        return [_account_bill(row) for row in rows], evidence

    def _paged_recovery_rows(
        self,
        path: str,
        instrument_id: str,
        begin: datetime,
        end: datetime,
        *,
        cursor_key: str = "ordId",
    ) -> tuple[list[dict[str, Any]], RecoveryQueryEvidence]:
        params = {
            "instType": "SPOT",
            "instId": instrument_id,
            "begin": str(int(begin.timestamp() * 1000)),
            "end": str(int(end.timestamp() * 1000)),
            "limit": "100",
        }
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        for _ in range(100):
            payload = self._request("GET", path, params=params, private=True)
            batch = [row for row in payload.get("data", []) if isinstance(row, dict)]
            pages += 1
            for row in batch:
                identity = str(row.get(cursor_key) or row.get("billId") or row)
                if identity not in seen:
                    seen.add(identity)
                    rows.append(row)
            if len(batch) < 100:
                break
            cursor = str(batch[-1].get(cursor_key) or "")
            if not cursor or cursor == params.get("after"):
                raise ExchangeError("OKX recovery pagination cursor did not advance")
            params["after"] = cursor
        else:
            raise ExchangeError("OKX recovery pagination exceeded safety limit")
        times = [_recovery_timestamp(row) for row in rows]
        valid_times = [item for item in times if item is not None]
        return rows, RecoveryQueryEvidence(
            path,
            begin,
            end,
            pages,
            len(rows),
            True,
            first_record_time=min(valid_times) if valid_times else None,
            last_record_time=max(valid_times) if valid_times else None,
        )

    def get_derivative_positions(self) -> dict[str, Decimal]:
        payload = self._request(
            "GET",
            "/api/v5/account/positions",
            private=True,
        )
        return parse_derivative_positions(payload.get("data", []))

    def get_max_available_size(
        self, instrument_id: str, trade_mode: TradeMode = TradeMode.CASH
    ) -> MaxAvailableSize:
        if trade_mode is not TradeMode.CASH:
            raise ValueError("本阶段只允许查询 SPOT cash 可用交易额度")
        payload = self._request(
            "GET",
            "/api/v5/account/max-avail-size",
            params={"instId": instrument_id, "tdMode": trade_mode.value},
            private=True,
        )
        row = self._first(payload)
        return MaxAvailableSize(
            str(row["instId"]),
            trade_mode,
            _optional_decimal(row.get("availBuy")),
            _optional_decimal(row.get("availSell")),
            self.clock.now(),
        )

    def get_last_price(self, instrument_id: str) -> Decimal:
        payload = self._request("GET", "/api/v5/market/ticker", params={"instId": instrument_id})
        price = _optional_decimal(self._first(payload).get("last"))
        if price is None or price <= 0:
            raise ExchangeError("OKX 未返回有效现货价格")
        return price

    def get_ticker(self, instrument_id: str) -> tuple[Decimal, Decimal, Decimal, datetime]:
        payload = self._request("GET", "/api/v5/market/ticker", params={"instId": instrument_id})
        row = self._first(payload)
        bid = _optional_decimal(row.get("bidPx"))
        ask = _optional_decimal(row.get("askPx"))
        last = _optional_decimal(row.get("last"))
        if bid is None or ask is None or last is None or min(bid, ask, last) <= 0 or bid > ask:
            raise ExchangeError("OKX ticker bid/ask/last is invalid")
        return bid, ask, last, self.clock.now()

    def place_order(
        self,
        request: OrderRequest,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        if authorization is None:
            raise PermissionError("Demo order submission requires explicit one-use authorization")
        authorization.assert_place_matches(request)
        self.settings.require_demo_credentials()
        self.probe_demo_environment()
        require_place_authorization(authorization, request)
        if request.order_type is not OrderType.LIMIT:
            raise ValueError("当前 MVP 只允许模拟盘限价单")
        order = Order(request=request)
        order.transition(OrderState.SUBMITTED, at=self.clock.now())
        body = {
            "instId": request.instrument_id,
            "tdMode": "cash",
            "clOrdId": request.client_order_id,
            "side": request.side.value,
            "ordType": "limit",
            "px": str(request.price),
            "sz": str(request.quantity),
        }
        try:
            self.place_order_calls += 1
            payload = self._request(
                "POST",
                "/api/v5/trade/order",
                body=body,
                private=True,
                retry=False,
                write_authorization=authorization,
            )
        except NetworkError:
            try:
                return self.query_order(request.instrument_id, request.client_order_id)
            except (NetworkError, OrderNotFound):
                order.transition(OrderState.UNKNOWN, at=self.clock.now())
                return order
        item = self._first(payload)
        self._check_item(item)
        order.exchange_order_id = str(item.get("ordId") or "") or None
        order.transition(OrderState.ACCEPTED, at=self.clock.now())
        return order

    def query_order(self, instrument_id: str, client_order_id: str) -> Order:
        payload = self._request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": instrument_id, "clOrdId": client_order_id},
            private=True,
        )
        rows = payload.get("data", [])
        if not rows:
            raise OrderNotFound("OKX 未找到该订单")
        return parse_order(rows[0])

    def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        *,
        authorization: DemoWriteAuthorization | None = None,
    ) -> Order:
        if authorization is None:
            raise PermissionError("Demo order cancellation requires explicit one-use authorization")
        authorization.assert_cancel_matches(instrument_id, client_order_id)
        self.probe_demo_environment()
        require_cancel_authorization(authorization, instrument_id, client_order_id)
        current = self.query_order(instrument_id, client_order_id)
        if not current.is_open:
            return current
        current.transition(OrderState.CANCEL_PENDING, at=self.clock.now())
        self.cancel_order_calls += 1
        payload = self._request(
            "POST",
            "/api/v5/trade/cancel-order",
            body={"instId": instrument_id, "clOrdId": client_order_id},
            private=True,
            retry=False,
            write_authorization=authorization,
        )
        self._check_item(self._first(payload))
        confirmed = self.query_order(instrument_id, client_order_id)
        if confirmed.state is OrderState.CANCELLED:
            return confirmed
        return current

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
        retry: bool = True,
        write_authorization: DemoWriteAuthorization | None = None,
    ) -> dict[str, Any]:
        if method.upper() != "GET":
            expected_operation = {
                "/api/v5/trade/order": DemoWriteOperation.PLACE,
                "/api/v5/trade/cancel-order": DemoWriteOperation.CANCEL,
            }.get(path)
            if expected_operation is None or write_authorization is None:
                raise PermissionError("raw OKX write request is not authorized")
            self._require_demo_environment()
            if not self._demo_environment_verified:
                raise PermissionError("Demo credentials must be verified before an OKX write")
            write_authorization.assert_environment_bound(self.environment.mode)
            write_authorization.assert_consumed_for(expected_operation)
        if private:
            self.settings.require_demo_credentials()
        query = urlencode(params or {})
        request_path = f"{path}?{query}" if query else path
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if private:
                headers.update(self._auth_headers(method, request_path, body_text))
                headers["x-simulated-trading"] = "1"
            try:
                if private:
                    self.private_rest_calls += 1
                    if method.upper() != "GET":
                        self.private_api_write_calls += 1
                else:
                    self.public_rest_calls += 1
                response = self.client.request(
                    method,
                    f"{self.environment.rest_base_url}{request_path}",
                    content=body_text.encode("utf-8") if body_text else None,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                if attempt + 1 == attempts:
                    raise RequestTimeout("OKX 请求超时") from exc
                self._backoff(attempt)
                continue
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    raise NetworkError("OKX 网络请求失败") from exc
                self._backoff(attempt)
                continue

            if response.status_code == 429:
                if attempt + 1 == attempts:
                    raise RateLimitError("OKX API 触发限频")
                self._backoff(attempt)
                continue
            if response.status_code in {401, 403}:
                error_payload = _error_payload(response)
                code = (
                    str(error_payload.get("code"))
                    if error_payload.get("code") is not None
                    else None
                )
                message = str(error_payload.get("msg") or "HTTP authentication rejected")
                diagnostic = self._authentication_diagnostic(
                    classify_authentication_failure(
                        okx_code=code, http_status=response.status_code, message=message
                    ),
                    okx_code=code,
                    http_status=response.status_code,
                    endpoint=path,
                    request_path=request_path,
                    message=message,
                )
                raise AuthenticationError("OKX API authentication failed", diagnostic=diagnostic)
            if response.is_error:
                error_payload = _error_payload(response)
                code = str(error_payload.get("code") or "http_error")
                message = str(error_payload.get("msg") or "HTTP error")
                raise ExchangeError(f"OKX HTTP {response.status_code} {code}: {message}")
            if response.is_error:
                raise ExchangeError(f"OKX HTTP 错误: {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ExchangeError("OKX 返回了无效 JSON") from exc
            if not isinstance(payload, dict):
                raise ExchangeError("OKX 返回结构无效")
            code = str(payload.get("code", ""))
            if code != "0":
                message = str(payload.get("msg") or "未提供错误信息")
                if code in {
                    "50101",
                    "50102",
                    "50103",
                    "50104",
                    "50105",
                    "50106",
                    "50107",
                    "50108",
                    "50109",
                    "50110",
                    "50111",
                    "50112",
                    "50113",
                    "50114",
                }:
                    diagnostic = self._authentication_diagnostic(
                        classify_authentication_failure(
                            okx_code=code, http_status=response.status_code, message=message
                        ),
                        okx_code=code,
                        http_status=response.status_code,
                        endpoint=path,
                        request_path=request_path,
                        message=message,
                    )
                    raise AuthenticationError(
                        "OKX API authentication failed", diagnostic=diagnostic
                    )
                raise ExchangeError(f"OKX 业务错误 {code}: {message}")
            return payload
        raise NetworkError("OKX 请求失败")

    def _require_demo_environment(self) -> None:
        if (
            self.environment.mode is not TradingEnvironment.DEMO
            or not self.environment.simulated_trading
        ):
            raise PermissionError("OKX Demo request environment binding is invalid")

    def _authentication_diagnostic(
        self,
        category: AuthenticationCategory,
        *,
        endpoint: str | None,
        request_path: str | None,
        message: str,
        okx_code: str | None = None,
        http_status: int | None = None,
    ) -> AuthenticationDiagnostic:
        local_time = self.clock.now()
        server_time = (
            datetime.fromtimestamp(self.server_time_ms / 1000, tz=UTC)
            if self.server_time_ms is not None
            else None
        )
        skew = (
            int((local_time - server_time).total_seconds() * 1000)
            if server_time is not None
            else None
        )
        likely_causes = {
            AuthenticationCategory.INVALID_API_KEY: (
                "API key is invalid, disabled, or for another environment",
            ),
            AuthenticationCategory.INVALID_SIGNATURE: (
                "secret, signature input, or request path does not match",
            ),
            AuthenticationCategory.INVALID_PASSPHRASE: ("passphrase does not match the API key",),
            AuthenticationCategory.EXPIRED_TIMESTAMP: (
                "request timestamp differs from OKX server time",
            ),
            AuthenticationCategory.IP_WHITELIST_REJECTED: (
                "current outbound IP is not on the API-key allowlist",
            ),
            AuthenticationCategory.PERMISSION_DENIED: ("the API key lacks read permission",),
        }.get(
            category,
            ("OKX rejected private authentication; inspect the error code and API settings",),
        )
        diagnostic = AuthenticationDiagnostic(
            category,
            okx_code,
            http_status,
            endpoint,
            self.environment.simulated_trading,
            self.settings.credential_diagnostics(),
            local_time,
            server_time,
            skew,
            request_path,
            likely_causes,
            (
                "Confirm this is a Demo Trading API key with Read permission.",
                "Confirm key, secret, and passphrase are from the same API key.",
                "Confirm the current outbound IP satisfies any API-key allowlist.",
            ),
        )
        self.last_authentication_diagnostic = diagnostic
        return diagnostic

    def _auth_headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        timestamp = (
            (self.clock.now() + timedelta(milliseconds=self.server_offset_ms))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        secret = self.settings.okx_secret_key.get_secret_value().encode()
        signature = base64.b64encode(
            hmac.new(secret, message.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "OK-ACCESS-KEY": self.settings.okx_api_key.get_secret_value(),
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.settings.okx_passphrase.get_secret_value(),
        }

    @staticmethod
    def _first(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ExchangeError("OKX 返回空数据")
        return data[0]

    @staticmethod
    def _check_item(item: dict[str, Any]) -> None:
        code = str(item.get("sCode", "0"))
        if code != "0":
            raise OrderRejected(f"OKX 拒绝订单 {code}: {item.get('sMsg') or '未提供原因'}")

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep((2**attempt) * 0.1 + random.uniform(0, 0.05))


def _error_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _exchange_order(row: dict[str, Any]) -> ExchangeOrder:
    ts = row.get("cTime") or row.get("uTime")
    return ExchangeOrder(
        str(row["ordId"]) if row.get("ordId") else None,
        str(row["clOrdId"]) if row.get("clOrdId") else None,
        str(row.get("instId") or ""),
        str(row.get("side") or ""),
        str(row.get("ordType") or ""),
        str(row["px"]) if row.get("px") else None,
        str(row["sz"]) if row.get("sz") else None,
        str(row.get("state")) if row.get("state") else None,
        datetime.fromtimestamp(int(ts) / 1000, UTC) if ts else None,
        row,
    )


def _exchange_fill(row: dict[str, Any]) -> ExchangeFill:
    ts = row.get("ts") or row.get("fillTime")
    return ExchangeFill(
        str(row["tradeId"]) if row.get("tradeId") else None,
        str(row["ordId"]) if row.get("ordId") else None,
        str(row["clOrdId"]) if row.get("clOrdId") else None,
        str(row.get("instId") or ""),
        str(row.get("side") or ""),
        str(row["fillPx"]) if row.get("fillPx") else None,
        str(row["fillSz"]) if row.get("fillSz") else None,
        datetime.fromtimestamp(int(ts) / 1000, UTC) if ts else None,
        row,
    )


def _account_bill(row: dict[str, Any]) -> AccountBill:
    ts = row.get("ts")
    return AccountBill(
        str(row["billId"]) if row.get("billId") else None,
        str(row["instId"]) if row.get("instId") else None,
        str(row["type"]) if row.get("type") else None,
        str(row["balChg"]) if row.get("balChg") else None,
        datetime.fromtimestamp(int(ts) / 1000, UTC) if ts else None,
        row,
    )


def _recovery_timestamp(row: dict[str, Any]) -> datetime | None:
    value = row.get("fillTime") or row.get("cTime") or row.get("ts") or row.get("uTime")
    return datetime.fromtimestamp(int(value) / 1000, UTC) if value else None
