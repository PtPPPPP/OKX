from __future__ import annotations

import json
import logging
import traceback as _traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_SENSITIVE_KEYS = {
    "authorization",
    "ok-access-key",
    "ok-access-passphrase",
    "ok-access-sign",
    "okx_api_key",
    "okx_passphrase",
    "okx_secret_key",
    "passphrase",
    "proxy_url",
    "secret",
    "signature",
}

_LOG_BYTES_PER_FILE = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


class JsonFormatter(logging.Formatter):
    """Structured JSON logs with credential redaction and preserved tracebacks."""

    def __init__(self, *, redact_values: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._redact_values = tuple(value for value in redact_values if value)

    def format(self, record: logging.LogRecord) -> str:
        message = self._redact_free_text(record.getMessage())
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, _exc_tb = record.exc_info
            payload["exception_type"] = exc_type.__name__
            payload["exception_message"] = self._redact_free_text(str(exc_value))
            payload["traceback"] = self._redact_free_text(
                "".join(_traceback.format_exception(*record.exc_info))
            )
        for key in (
            "trading_mode",
            "command",
            "instrument_id",
            "strategy",
            "run_id",
            "signal_id",
            "client_order_id",
            "risk_result",
            "order_state",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(_redact(payload), ensure_ascii=False, default=str)

    def _redact_free_text(self, text: str | None) -> str:
        if not text:
            return text or ""
        redacted = text
        for value in self._redact_values:
            redacted = redacted.replace(value, "***")
        return redacted


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if key.lower() in _SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class TradingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("trading")


class ErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


def configure_logging(
    level: str = "INFO",
    directory: Path = Path("logs"),
    *,
    redact_values: tuple[str, ...] = (),
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    formatter = JsonFormatter(redact_values=redact_values)

    app_handler = RotatingFileHandler(
        directory / "app.log",
        maxBytes=_LOG_BYTES_PER_FILE,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    trading_handler = RotatingFileHandler(
        directory / "trading.log",
        maxBytes=_LOG_BYTES_PER_FILE,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    trading_handler.addFilter(TradingFilter())
    trading_handler.setFormatter(formatter)
    root.addHandler(trading_handler)

    error_handler = RotatingFileHandler(
        directory / "error.log",
        maxBytes=_LOG_BYTES_PER_FILE,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.addFilter(ErrorFilter())
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)
