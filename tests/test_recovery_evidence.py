from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta

import pytest

from app.exchange.recovery_models import RecoveryEndpointCapability
from app.services.recovery_evidence import official_recovery_capabilities

ARCHIVE_ENDPOINTS = (
    "/api/v5/trade/orders-history-archive",
    "/api/v5/trade/fills-history",
    "/api/v5/account/bills-archive",
)
EXPECTED_ENDPOINTS = (
    "/api/v5/trade/orders-pending",
    "/api/v5/trade/orders-history",
    "/api/v5/trade/orders-history-archive",
    "/api/v5/trade/fills-history",
    "/api/v5/account/bills",
    "/api/v5/account/bills-archive",
    "/api/v5/trade/fills-history-archive",
)


def _capabilities(now: datetime) -> tuple[RecoveryEndpointCapability, ...]:
    return official_recovery_capabilities(now, now - timedelta(days=1))


def _archive_starts(now: datetime) -> dict[str, datetime | None]:
    capabilities = {item.endpoint: item for item in _capabilities(now)}
    return {endpoint: capabilities[endpoint].coverage_start for endpoint in ARCHIVE_ENDPOINTS}


@pytest.mark.parametrize(
    ("now", "expected_start"),
    (
        (datetime(2026, 7, 31, 12, 30, tzinfo=UTC), datetime(2026, 4, 30, 12, 30, tzinfo=UTC)),
        (datetime(2026, 5, 31, 12, 30, tzinfo=UTC), datetime(2026, 2, 28, 12, 30, tzinfo=UTC)),
        (datetime(2026, 3, 31, 12, 30, tzinfo=UTC), datetime(2025, 12, 31, 12, 30, tzinfo=UTC)),
        (datetime(2026, 4, 30, 12, 30, tzinfo=UTC), datetime(2026, 1, 30, 12, 30, tzinfo=UTC)),
        (datetime(2026, 1, 31, 12, 30, tzinfo=UTC), datetime(2025, 10, 31, 12, 30, tzinfo=UTC)),
        (datetime(2024, 5, 31, 12, 30, tzinfo=UTC), datetime(2024, 2, 29, 12, 30, tzinfo=UTC)),
        (datetime(2024, 3, 31, 12, 30, tzinfo=UTC), datetime(2023, 12, 31, 12, 30, tzinfo=UTC)),
        (datetime(2024, 2, 29, 12, 30, tzinfo=UTC), datetime(2023, 11, 29, 12, 30, tzinfo=UTC)),
        (datetime(2026, 8, 4, 12, 30, tzinfo=UTC), datetime(2026, 5, 4, 12, 30, tzinfo=UTC)),
    ),
)
def test_three_month_window_clamps_to_valid_target_day(
    now: datetime, expected_start: datetime
) -> None:
    starts = _archive_starts(now)

    assert starts == {endpoint: expected_start for endpoint in ARCHIVE_ENDPOINTS}
    assert all(start.tzinfo is now.tzinfo for start in starts.values() if start is not None)


def test_capability_schema_and_normal_date_behavior_are_unchanged() -> None:
    now = datetime(2026, 8, 4, 12, 30, tzinfo=UTC)
    capabilities = _capabilities(now)

    assert tuple(item.endpoint for item in capabilities) == EXPECTED_ENDPOINTS
    assert len(capabilities) == len(EXPECTED_ENDPOINTS)
    assert capabilities == _capabilities(now)
    assert capabilities[0].coverage_start is None
    assert capabilities[0].coverage_end == now
    assert capabilities[1].coverage_start == datetime(2026, 8, 1, 12, 30, tzinfo=UTC)


def test_all_days_from_2024_through_2027_are_safe_and_calendar_valid() -> None:
    checked = 0
    for year in range(2024, 2028):
        current = datetime(year, 1, 1, 12, 30, tzinfo=UTC)
        end = datetime(year + 1, 1, 1, 12, 30, tzinfo=UTC)
        while current < end:
            starts = _archive_starts(current)
            for start in starts.values():
                assert start is not None
                assert start <= current
                assert start.day <= calendar.monthrange(start.year, start.month)[1]
            checked += 1
            current += timedelta(days=1)

    assert checked == 1461
