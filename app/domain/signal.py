from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class SignalAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    strategy_name: str
    instrument_id: str
    action: SignalAction
    timestamp: datetime
    reason: str
    confidence: Decimal
    suggested_quantity: Decimal | None = None
    suggested_notional: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
