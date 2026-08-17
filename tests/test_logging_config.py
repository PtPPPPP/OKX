from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest

import app.monitoring.logging_config as logging_config
from app.monitoring.logging_config import JsonFormatter, _redact, configure_logging

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]


def _format_record(
    formatter: JsonFormatter,
    message: str,
    *,
    exc_info: _ExcInfo | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, Any]:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    if exc_info is not None:
        record.exc_info = exc_info
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    return cast(dict[str, Any], json.loads(formatter.format(record)))


def test_free_text_secret_values_are_redacted() -> None:
    formatter = JsonFormatter(redact_values=("super-secret-key", "super-secret-pass"))
    payload = _format_record(formatter, "auth failed super-secret-key with super-secret-pass")
    assert "super-secret-key" not in payload["message"]
    assert "super-secret-pass" not in payload["message"]
    assert isinstance(payload["message"], str)
    assert payload["message"].count("***") == 2


def test_sensitive_dict_keys_are_redacted() -> None:
    redacted = _redact({"okx_api_key": "raw-key", "signature": "sig", "client_order_id": "c1"})
    assert redacted["okx_api_key"] == "***"
    assert redacted["signature"] == "***"
    assert redacted["client_order_id"] == "c1"


def test_sensitive_extra_fields_are_never_emitted() -> None:
    """Extras outside the JSON whitelist must never reach the log payload."""
    formatter = JsonFormatter()
    payload = _format_record(
        formatter,
        "state saved",
        extra={"okx_api_key": "raw-key", "client_order_id": "c1"},
    )
    assert "okx_api_key" not in payload
    assert "raw-key" not in json.dumps(payload)


def test_exception_traceback_is_preserved() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom-message")
    except ValueError:
        payload = _format_record(formatter, "failure", exc_info=sys.exc_info())
    assert payload["exception_type"] == "ValueError"
    assert payload["exception_message"] == "boom-message"
    assert "ValueError" in payload["traceback"]
    assert "boom-message" in payload["traceback"]


def test_traceback_secrets_are_redacted() -> None:
    formatter = JsonFormatter(redact_values=("boom-message",))
    try:
        raise ValueError("boom-message")
    except ValueError:
        payload = _format_record(formatter, "failure", exc_info=sys.exc_info())
    assert "boom-message" not in payload["traceback"]
    assert "boom-message" not in payload["exception_message"]


def test_rotating_handlers_are_configured(tmp_path: Path) -> None:
    configure_logging("INFO", tmp_path)
    handlers = list(logging.getLogger().handlers)
    try:
        assert handlers
        for handler in handlers:
            assert isinstance(handler, RotatingFileHandler)
            assert handler.maxBytes > 0
            assert handler.backupCount >= 1
        for name in ("app.log", "trading.log", "error.log"):
            assert (tmp_path / name).is_file()
    finally:
        logging.getLogger().handlers.clear()


def test_rotation_rolls_over_large_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotation must actually trigger, stay bounded, and keep logging safe.

    The production threshold (5 MB) is injected down to 2 KB for the test via
    the module constant read at configure time; production values stay intact.
    """
    monkeypatch.setattr(logging_config, "_LOG_BYTES_PER_FILE", 2 * 1024)
    configure_logging("INFO", tmp_path, redact_values=("secret-value",))
    logger = logging.getLogger("trading.rotation")
    try:
        for index in range(120):
            logger.info("filler line %04d %s", index, "x" * 40)

        assert (tmp_path / "app.log").is_file()
        rotated = [path for path in tmp_path.glob("app.log.*") if path.suffix in {".1", ".2", ".3"}]
        assert rotated, "expected at least one rotated app.log backup"
        assert len(rotated) <= logging_config._LOG_BACKUP_COUNT, "retention must stay bounded"

        logger.info("after-rollover line secret-value")
        current = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert "secret-value" not in current
        payload = json.loads(current.splitlines()[-1])
        assert payload["logger"] == "trading.rotation"
        assert payload["message"] == "after-rollover line ***"
    finally:
        logging.getLogger().handlers.clear()
