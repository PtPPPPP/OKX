"""Compatibility entry point for the governed prospective OOS collector."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backtest.prospective_oos import PROSPECTIVE_START
from scripts.collect_prospective_oos import main as governed_main


@dataclass(frozen=True, slots=True)
class DailyPartition:
    partition_id: str
    start: datetime
    end: datetime


def completed_daily_partitions(end: datetime) -> tuple[DailyPartition, ...]:
    """Retained for callers that inspect complete UTC days; storage also supports OPEN days."""
    end = end.astimezone(UTC)
    cursor = PROSPECTIVE_START
    result: list[DailyPartition] = []
    while cursor + timedelta(hours=23) <= end:
        result.append(
            DailyPartition(
                partition_id=cursor.date().isoformat(),
                start=cursor,
                end=cursor + timedelta(hours=23),
            )
        )
        cursor += timedelta(days=1)
    return tuple(result)


def main() -> None:
    if not any(value in {"--once", "--max-runtime-hours"} for value in sys.argv[1:]):
        sys.argv.insert(1, "--once")
    governed_main()


if __name__ == "__main__":
    main()
