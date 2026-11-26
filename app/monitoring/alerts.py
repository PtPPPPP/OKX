"""Fail-safe WeCom (Enterprise WeChat) / Webhook real-time alert notifier hooked to EventBus."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any, Literal

import httpx

from app.domain.events import DomainEvent

logger = logging.getLogger("trading.alerts")


class AlertLevel(IntEnum):
    INFO = 10
    WARNING = 20
    CRITICAL = 30

    @classmethod
    def from_str(cls, value: str) -> AlertLevel:
        mapping = {
            "info": cls.INFO,
            "warning": cls.WARNING,
            "critical": cls.CRITICAL,
        }
        level = mapping.get(value.strip().lower())
        if level is None:
            raise ValueError(f"不支持的告警级别: {value}")
        return level


def classify_domain_event(event: DomainEvent) -> tuple[AlertLevel, str, list[str]] | None:
    """Filter and format high-value trading events; return None for noisy heartbeat/HOLD events."""
    etype = event.event_type
    p = event.payload

    if etype == "SignalGenerated":
        action = str(p.get("action", "")).lower()
        if action in {"", "hold"}:
            return None
        raw_meta = p.get("metadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        lines = [
            f"**动作**：`{action.upper()}`",
            f"**原因**：{p.get('reason', '—')}",
        ]
        if meta.get("trend_ma"):
            lines.append(f"**趋势均线**：`{meta['trend_ma']}`")
        if meta.get("bb_lower") and meta.get("bb_middle") and meta.get("bb_upper"):
            lines.append(
                f"**布林带 (下/中/上)**：`{meta['bb_lower']}` / `{meta['bb_middle']}` / `{meta['bb_upper']}`"
            )
        if meta.get("stop_distance_pct"):
            lines.append(f"**止损距离比例**：`{meta['stop_distance_pct']}`")
        return AlertLevel.INFO, f"策略交易信号触发 ({action.upper()})", lines

    if etype == "RiskRejected":
        rejected_by = ", ".join(str(x) for x in p.get("rejected_by", [])) or "unknown_rule"
        reasons = "; ".join(str(x) for x in p.get("reasons", [])) or "未提供原因"
        return (
            AlertLevel.WARNING,
            "风控拦截提醒 (RiskRejected)",
            [
                f"**拦截规则**：`{rejected_by}`",
                f"**拒绝原因**：{reasons}",
            ],
        )

    if etype == "OrderEvent":
        state = str(p.get("state", "")).lower()
        cl_id = str(p.get("client_order_id", "—"))
        filled = str(p.get("filled_quantity", "0"))
        avg_px = str(p.get("average_price", "—"))
        if state in {"unknown", "rejected"}:
            return (
                AlertLevel.CRITICAL,
                f"订单异常告警 ({state.upper()})",
                [
                    f"**客户端单号**：`{cl_id}`",
                    f"**当前状态**：`{state.upper()}`（请立即核查交易所挂单与持仓对账）",
                    f"**已成交数量**：`{filled}`",
                ],
            )
        return (
            AlertLevel.INFO,
            f"订单状态更新 ({state.upper()})",
            [
                f"**客户端单号**：`{cl_id}`",
                f"**订单状态**：`{state.upper()}`",
                f"**成交数量 / 均价**：`{filled}` @ `{avg_px}`",
            ],
        )

    return None


class WeComAlertNotifier:
    """Non-blocking Enterprise WeChat (WeCom) / Webhook alert publisher with credential redaction."""

    def __init__(
        self,
        webhook_url: str,
        *,
        provider: Literal["wecom", "feishu", "generic"] = "wecom",
        min_level: str = "info",
        timeout_seconds: float = 3.0,
        redact_values: Sequence[str] = (),
        http_client: httpx.Client | None = None,
    ) -> None:
        self.webhook_url = webhook_url.strip()
        self.provider = provider
        self.min_level = AlertLevel.from_str(min_level)
        self.timeout_seconds = timeout_seconds
        self._secrets = tuple(s for s in redact_values if s and len(s.strip()) >= 4)
        self._client = http_client

    def handle_event(self, event: DomainEvent) -> bool:
        if not self.webhook_url:
            return False
        classified = classify_domain_event(event)
        if classified is None:
            return False
        level, title, detail_lines = classified
        if level < self.min_level:
            return False
        header_lines = [
            f"**品种 / 策略**：`{event.instrument_id}` / `{event.strategy_name}`",
            f"**运行 ID**：`{event.run_id[:12]}`",
            *detail_lines,
            f"**时间 (UTC)**：`{event.timestamp.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')}`",
        ]
        return self.send_markdown(level=level, title=title, lines=header_lines)

    def send_test_alert(
        self,
        *,
        instrument_id: str = "BTC-USDT",
        strategy_name: str = "ma_bollinger_pullback",
        mode: str = "demo",
    ) -> bool:
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        return self.send_markdown(
            level=AlertLevel.INFO,
            title="企业微信量化告警连通性测试",
            lines=[
                f"**模式 / 品种**：`{mode}` / `{instrument_id}`",
                f"**当前策略**：`{strategy_name}`",
                "**状态**：Webhook 连通正常，实盘/模拟盘信号、订单与风控异常将实时推送至此群。",
                f"**测试时间 (UTC)**：`{now_str}`",
            ],
        )

    def send_markdown(
        self,
        *,
        level: AlertLevel,
        title: str,
        lines: Sequence[str],
    ) -> bool:
        if not self.webhook_url:
            return False
        payload = self.build_payload(level=level, title=title, lines=lines)
        try:
            if self._client is not None:
                response = self._client.post(
                    self.webhook_url,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(self.webhook_url, json=payload)
            if response.is_error:
                logger.warning(
                    "企业微信/Webhook 告警推送返回非 2xx 状态",
                    extra={"status_code": response.status_code, "title": title},
                )
                return False
            return True
        except Exception as exc:
            logger.warning(
                "企业微信/Webhook 告警推送失败（已隔离，不影响交易主流程）",
                extra={"error": self._redact(str(exc)), "title": title},
            )
            return False

    def build_payload(
        self,
        *,
        level: AlertLevel,
        title: str,
        lines: Sequence[str],
    ) -> dict[str, Any]:
        safe_title = self._redact(title)
        safe_lines = [self._redact(line) for line in lines]
        level_tag = level.name
        if self.provider == "wecom":
            color = "info" if level is AlertLevel.INFO else "warning"
            body = "\n".join(f"> {line}" for line in safe_lines)
            content = f'### <font color="{color}">[{level_tag}] {safe_title}</font>\n{body}'
            return {
                "msgtype": "markdown",
                "markdown": {"content": content},
            }
        if self.provider == "feishu":
            text_body = "\n".join([f"[{level_tag}] {safe_title}", *safe_lines])
            return {
                "msg_type": "text",
                "content": {"text": text_body},
            }
        return {
            "level": level_tag,
            "title": safe_title,
            "lines": safe_lines,
        }

    def _redact(self, text: str) -> str:
        cleaned = text
        for secret in self._secrets:
            if secret in cleaned:
                cleaned = cleaned.replace(secret, "***REDACTED***")
        if self.webhook_url and self.webhook_url in cleaned:
            cleaned = cleaned.replace(self.webhook_url, "***WEBHOOK_REDACTED***")
        return cleaned
