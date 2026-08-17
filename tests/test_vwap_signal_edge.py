from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.config.run_config import load_run_config
from app.domain.market import Candle, Instrument
from app.market.historical_data import load_candles_csv
from app.reproducibility import InstrumentSnapshotStore
from app.strategies.vwap_shadow import VWAPShadowParameters
from backtest.vwap_signal_edge import (
    _benchmark,
    _clustering,
    _episode_ids,
    _forward_return,
    _market_regime,
    _random_benchmark,
    _volatility_regime,
    descriptive,
    run_signal_edge_study,
)


def _candles(closes: list[str]) -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal(close) + Decimal("1"),
            high=Decimal(close) + Decimal("2"),
            low=Decimal(close) - Decimal("2"),
            close=Decimal(close),
            volume=Decimal("10"),
            confirmed=True,
        )
        for index, close in enumerate(closes)
    ]


def test_forward_return_uses_next_open_and_requested_horizon() -> None:
    candles = _candles(["100", "90", "99", "108"])
    entry = float(candles[1].open)

    assert _forward_return(candles, 1, entry, 1) == float(candles[1].close) / entry - 1
    assert _forward_return(candles, 1, entry, 3) == float(candles[3].close) / entry - 1
    assert _forward_return(candles, 1, entry, 4) is None


def test_signal_study_has_no_lookahead_and_calculates_mfe_mae(
    btc_instrument: Instrument,
) -> None:
    candles = _candles(["100", "100", "90", "95", "105", "98", "110", "100"])
    study = run_signal_edge_study(
        candles,
        btc_instrument,
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("100")),
    )
    first = study.observations[0]
    signal_index = next(
        index
        for index, candle in enumerate(candles)
        if candle.timestamp.isoformat() == first.signal_timestamp
    )

    assert first.entry_reference_timestamp == candles[signal_index + 1].timestamp.isoformat()
    assert first.entry_reference_price == float(candles[signal_index + 1].open)
    if first.mfe[6] is not None:
        window = candles[signal_index + 1 : signal_index + 7]
        assert (
            first.mfe[6]
            == max(float(candle.high) for candle in window) / first.entry_reference_price - 1
        )
        assert (
            first.mae[6]
            == min(float(candle.low) for candle in window) / first.entry_reference_price - 1
        )


def test_regime_classification_is_causal() -> None:
    history = _candles([str(100 + index / 10) for index in range(200)])
    altered_future = history + _candles(["1000"] * 100)

    assert _market_regime(history, 180) == _market_regime(altered_future, 180)
    assert _volatility_regime(history, 180) == _volatility_regime(altered_future, 180)


def test_consecutive_buy_bars_form_one_episode() -> None:
    candles = _candles(["100"] * 8)

    episodes = _episode_ids([1, 2, 3, 5, 7], candles)

    assert episodes[1] == episodes[2] == episodes[3]
    assert episodes[5] != episodes[3]
    assert episodes[7] != episodes[5]


def test_descriptive_statistics_and_random_benchmark_are_deterministic(
    btc_instrument: Instrument,
) -> None:
    stats = descriptive([-0.1, 0.0, 0.1])
    assert stats["count"] == 3
    assert stats["median"] == 0.0
    assert stats["positive_rate"] == 1 / 3

    candles = _candles([str(100 + index % 10) for index in range(500)])
    study = run_signal_edge_study(
        candles,
        btc_instrument,
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("1")),
    )
    assert _random_benchmark(candles, study.observations) == _random_benchmark(
        candles, study.observations
    )


def test_same_input_produces_same_complete_study(btc_instrument: Instrument) -> None:
    candles = _candles([str(100 + (index % 24) - 12) for index in range(600)])
    parameters = VWAPShadowParameters(vwap_window=24, buy_deviation_bps=Decimal("100"))

    first = run_signal_edge_study(candles, btc_instrument, parameters)
    second = run_signal_edge_study(candles, btc_instrument, parameters)

    assert first == second


def test_frozen_baseline_replay_has_exact_strategy_parity() -> None:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    candles = load_candles_csv(Path("tests/fixtures/vwap/btc_usdt_1h_600.csv"), bar="1h")
    instrument = InstrumentSnapshotStore.load(Path("configs/snapshots/BTC-USDT.json")).instrument
    study = run_signal_edge_study(
        candles,
        instrument,
        VWAPShadowParameters.model_validate(config.strategy.parameters),
    )
    with Path("tests/fixtures/vwap/vwap_baseline_v1_signals.csv").open(
        encoding="utf-8", newline=""
    ) as file:
        frozen = list(csv.DictReader(file))

    assert len(study.signal_records) == len(frozen)
    for actual, expected in zip(study.signal_records, frozen, strict=True):
        assert actual.timestamp == expected["timestamp"]
        assert actual.action == expected["action"]
        assert (actual.vwap or "") == expected["vwap"]
        assert (actual.deviation_bps or "") == expected["deviation_bps"]
        assert actual.proposal_eligible is (expected["proposal_eligible"] == "True")
        assert (actual.execution_timestamp or "") == expected["execution_timestamp"]
        assert (actual.execution_reference_price or "") == expected["execution_reference_price"]


def test_benchmark_and_clustering_report_both_inference_levels(
    btc_instrument: Instrument,
) -> None:
    candles = _candles([str(100 + (index % 8)) for index in range(500)])
    study = run_signal_edge_study(
        candles,
        btc_instrument,
        VWAPShadowParameters(vwap_window=2, buy_deviation_bps=Decimal("1")),
    )

    assert {row["scope"] for row in _benchmark(candles, study.observations)} == {
        "signal",
        "episode",
    }
    clustering = _clustering(study.observations)
    raw_count = clustering["raw_signal_count"]
    episode_count = clustering["signal_episode_count"]
    assert isinstance(raw_count, int)
    assert isinstance(episode_count, int)
    assert raw_count >= episode_count
