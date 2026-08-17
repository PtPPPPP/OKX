from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.market import Candle, Instrument
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_episode_research import (
    _episode_summary,
    _overlap_statistics,
    build_episodes,
    run_episode_study,
)
from backtest.vwap_shadow_research import ShadowSignalRecord, replay_shadow


def _candles(count: int, *, gap_after: int | None = None) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    for index in range(count):
        extra = timedelta(hours=1) if gap_after is not None and index > gap_after else timedelta()
        timestamp = start + timedelta(hours=index) + extra
        result.append(
            Candle(
                timestamp=timestamp,
                open=Decimal("101") + index,
                high=Decimal("103") + index,
                low=Decimal("99") + index,
                close=Decimal("100") + index,
                volume=Decimal("10"),
                confirmed=True,
            )
        )
    return result


def _records(candles: list[Candle], actions: list[str]) -> list[ShadowSignalRecord]:
    result: list[ShadowSignalRecord] = []
    for index, (candle, action) in enumerate(zip(candles, actions, strict=True)):
        buy = action == "buy"
        next_candle = candles[index + 1] if index + 1 < len(candles) else None
        result.append(
            ShadowSignalRecord(
                timestamp=candle.timestamp.isoformat(),
                action=action,
                reason="fixture",
                close=str(candle.close),
                vwap="100" if buy else None,
                deviation_bps=str(100 + index) if buy else None,
                proposal_eligible=buy,
                execution_timestamp=(
                    next_candle.timestamp.isoformat() if buy and next_candle else None
                ),
                execution_reference_price=(str(next_candle.open) if buy and next_candle else None),
            )
        )
    return result


def test_single_buy_creates_one_closed_episode() -> None:
    candles = _candles(3)
    episodes = build_episodes(candles, _records(candles, ["hold", "buy", "hold"]))

    assert len(episodes) == 1
    assert episodes[0].closed is True
    assert episodes[0].buy_signal_count == 1
    assert episodes[0].duration_bars == 1


def test_consecutive_buys_collapse_into_one_episode() -> None:
    candles = _candles(5)
    episodes = build_episodes(candles, _records(candles, ["hold", "buy", "buy", "buy", "hold"]))

    assert len(episodes) == 1
    assert episodes[0].buy_signal_count == 3
    assert episodes[0].duration_bars == 3
    assert episodes[0].duration_hours == 3


def test_separated_buys_create_separate_episodes() -> None:
    candles = _candles(5)
    episodes = build_episodes(candles, _records(candles, ["hold", "buy", "hold", "buy", "hold"]))

    assert len(episodes) == 2
    assert episodes[0].episode_id != episodes[1].episode_id


def test_final_buy_creates_open_episode() -> None:
    candles = _candles(3)
    episodes = build_episodes(candles, _records(candles, ["hold", "buy", "buy"]))

    assert len(episodes) == 1
    assert episodes[0].closed is False
    assert episodes[0].closure_reason == "dataset_end"
    assert episodes[0].first_entry_reference_price == float(candles[2].open)


def test_missing_bar_forces_episode_boundary_and_blocks_cross_gap_entry() -> None:
    candles = _candles(5, gap_after=1)
    episodes = build_episodes(candles, _records(candles, ["hold", "buy", "buy", "buy", "hold"]))

    assert len(episodes) == 2
    assert episodes[0].closed is False
    assert episodes[0].closure_reason == "data_gap"
    assert episodes[0].first_entry_reference_price is None


def test_episode_entry_uses_first_buy_next_bar_open_without_lookahead() -> None:
    candles = _candles(8)
    records = _records(candles, ["hold", "buy", "buy", "hold", "hold", "hold", "hold", "hold"])
    original = build_episodes(candles, records)[0]
    altered = list(candles)
    altered[4:] = [
        Candle(
            timestamp=candle.timestamp,
            open=Decimal("999"),
            high=Decimal("1000"),
            low=Decimal("998"),
            close=Decimal("999"),
            volume=candle.volume,
            confirmed=True,
        )
        for candle in altered[4:]
    ]
    replayed = build_episodes(
        altered, _records(altered, ["hold", "buy", "buy", "hold", "hold", "hold", "hold", "hold"])
    )[0]

    assert original.first_entry_reference_timestamp == candles[2].timestamp.isoformat()
    assert original.first_entry_reference_price == float(candles[2].open)
    assert original.start_vwap == replayed.start_vwap
    assert original.start_deviation == replayed.start_deviation
    assert original.market_regime == replayed.market_regime


def test_overlap_and_one_position_conflict_are_distinct() -> None:
    candles = _candles(12)
    actions = [
        "buy",
        "hold",
        "buy",
        "hold",
        "buy",
        "hold",
        "hold",
        "hold",
        "hold",
        "hold",
        "hold",
        "hold",
    ]
    episodes = build_episodes(candles, _records(candles, actions))
    row = next(item for item in _overlap_statistics(episodes) if item["horizon_hours"] == 6)

    assert row["total_episodes"] == 3
    assert row["episode_overlap_count"] == 2
    assert row["tradable_if_one_position_only"] == 1
    assert row["blocked_by_existing_position"] == 2


def test_episode_forward_return_uses_first_entry_only() -> None:
    candles = _candles(8)
    episode = build_episodes(
        candles,
        _records(candles, ["hold", "buy", "buy", "hold", "hold", "hold", "hold", "hold"]),
    )[0]

    expected = float(candles[4].close) / float(candles[2].open) - 1
    assert episode.returns[3] == expected


def test_episode_mfe_mae_start_at_next_bar_open() -> None:
    candles = _candles(10)
    episode = build_episodes(
        candles,
        _records(
            candles,
            ["hold", "buy", "buy", "hold", "hold", "hold", "hold", "hold", "hold", "hold"],
        ),
    )[0]
    entry = float(candles[2].open)
    window = candles[2:8]

    assert episode.mfe[6] == max(float(candle.high) for candle in window) / entry - 1
    assert episode.mae[6] == min(float(candle.low) for candle in window) / entry - 1


def test_formal_strategy_parity_and_episode_replay_are_deterministic(
    btc_instrument: Instrument,
) -> None:
    candles = _candles(220)
    parameters = VWAPShadowParameters(vwap_window=24, buy_deviation_bps=Decimal("100"))
    expected = replay_shadow(candles, btc_instrument, parameters)

    first = run_episode_study(candles, btc_instrument, parameters)
    assert list(first.records) == expected


def test_episode_replay_is_deterministic(btc_instrument: Instrument) -> None:
    candles = _candles(220)
    parameters = VWAPShadowParameters(vwap_window=24, buy_deviation_bps=Decimal("100"))
    first = run_episode_study(candles, btc_instrument, parameters)
    second = run_episode_study(candles, btc_instrument, parameters)

    assert first == second


def test_episode_gap_is_measured_from_previous_episode_end() -> None:
    candles = _candles(7)
    episodes = build_episodes(
        candles,
        _records(candles, ["hold", "buy", "buy", "hold", "hold", "buy", "hold"]),
    )

    summary = _episode_summary(
        _records(candles, ["hold", "buy", "buy", "hold", "hold", "buy", "hold"]),
        episodes,
    )

    assert summary["median_gap_between_episodes"] == 3
