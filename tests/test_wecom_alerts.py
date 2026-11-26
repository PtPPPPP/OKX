from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from app.cli import app
from app.domain.events import DomainEvent, EventBus
from app.monitoring.alerts import AlertLevel, WeComAlertNotifier, classify_domain_event


def _make_event(event_type: str, payload: dict[str, Any]) -> DomainEvent:
    return DomainEvent(
        run_id="run1234567890abcdef",
        timestamp=datetime(2026, 11, 27, 1, 0, tzinfo=UTC),
        event_type=event_type,
        instrument_id="BTC-USDT",
        strategy_name="ma_bollinger_pullback",
        payload=payload,
        idempotency_key=f"key-{event_type}-{payload}",
    )


def test_classify_domain_event_filters_hold_and_grades_critical() -> None:
    hold_ev = _make_event("SignalGenerated", {"action": "hold", "reason": "未满足入场条件"})
    assert classify_domain_event(hold_ev) is None

    buy_ev = _make_event(
        "SignalGenerated",
        {
            "action": "buy",
            "reason": "上升趋势中价格回调至布林带下轨",
            "metadata": {
                "trend_ma": "60000",
                "bb_lower": "58000",
                "bb_middle": "59500",
                "bb_upper": "61000",
                "stop_distance_pct": "0.029",
            },
        },
    )
    classified_buy = classify_domain_event(buy_ev)
    assert classified_buy is not None
    assert classified_buy[0] is AlertLevel.INFO
    assert "BUY" in classified_buy[1]

    reject_ev = _make_event(
        "RiskRejected",
        {"rejected_by": ["max_total_exposure"], "reasons": ["超过总风险敞口"]},
    )
    classified_reject = classify_domain_event(reject_ev)
    assert classified_reject is not None
    assert classified_reject[0] is AlertLevel.WARNING

    unknown_order_ev = _make_event(
        "OrderEvent",
        {"client_order_id": "cl-99", "state": "unknown", "filled_quantity": "0"},
    )
    classified_unknown = classify_domain_event(unknown_order_ev)
    assert classified_unknown is not None
    assert classified_unknown[0] is AlertLevel.CRITICAL


def test_wecom_notifier_formats_markdown_redacts_secrets_and_isolates_errors() -> None:
    sent_payloads: list[dict[str, Any]] = []
    should_fail = False

    def handler(request: httpx.Request) -> httpx.Response:
        if should_fail:
            return httpx.Response(500, text="server error")
        body = json.loads(request.content.decode("utf-8"))
        sent_payloads.append(body)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-webhook-key"
    notifier = WeComAlertNotifier(
        webhook_url,
        provider="wecom",
        min_level="info",
        redact_values=("my-super-secret-api-key",),
        http_client=client,
    )

    bus = EventBus()
    bus.subscribe(notifier.handle_event)

    # 1. HOLD signal must not trigger any webhook request
    bus.publish(_make_event("SignalGenerated", {"action": "hold", "reason": "hold"}))
    assert len(sent_payloads) == 0

    # 2. BUY signal containing a secret string must be sent as WeCom markdown with secret redacted
    bus.publish(
        _make_event(
            "SignalGenerated",
            {
                "action": "buy",
                "reason": "回调买入 (key=my-super-secret-api-key)",
                "metadata": {"stop_distance_pct": "0.025"},
            },
        )
    )
    assert len(sent_payloads) == 1
    msg = sent_payloads[0]
    assert msg["msgtype"] == "markdown"
    content = msg["markdown"]["content"]
    assert '<font color="info">[INFO]' in content
    assert "BTC-USDT" in content
    assert "my-super-secret-api-key" not in content
    assert "***REDACTED***" in content

    # 3. Webhook HTTP 500 error must be swallowed safely without raising into EventBus.publish
    should_fail = True
    published_ok = bus.publish(
        _make_event(
            "OrderEvent",
            {"client_order_id": "cl-err", "state": "unknown", "filled_quantity": "0"},
        )
    )
    assert published_ok is True


def test_cli_test_alert_fails_clearly_when_webhook_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    result = CliRunner().invoke(app, ["test-alert"])
    assert result.exit_code == 1
    assert "ALERT_WEBHOOK_URL" in result.output
