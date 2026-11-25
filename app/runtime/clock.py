from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class BacktestClock:
    def __init__(self, current: datetime) -> None:
        self._current = current

    def advance_to(self, current: datetime) -> None:
        if current < self._current:
            raise ValueError("回测时钟不允许倒退")
        self._current = current

    def now(self) -> datetime:
        return self._current
