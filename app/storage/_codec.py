from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None
