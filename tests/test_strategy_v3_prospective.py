from __future__ import annotations

from datetime import UTC, datetime

from scripts.collect_strategy_v3_prospective import completed_daily_partitions


def test_prospective_collector_uses_immutable_complete_utc_days() -> None:
    partitions = completed_daily_partitions(datetime(2026, 8, 14, 22, tzinfo=UTC))
    assert len(partitions) == 1
    assert partitions[0].partition_id == "2026-08-13"
    assert partitions[0].start == datetime(2026, 8, 13, tzinfo=UTC)
    assert partitions[0].end == datetime(2026, 8, 13, 23, tzinfo=UTC)


def test_prospective_collector_adds_new_partition_without_changing_old_request() -> None:
    first = completed_daily_partitions(datetime(2026, 8, 13, 23, tzinfo=UTC))
    later = completed_daily_partitions(datetime(2026, 8, 14, 23, tzinfo=UTC))
    assert later[:1] == first
    assert [item.partition_id for item in later] == ["2026-08-13", "2026-08-14"]
