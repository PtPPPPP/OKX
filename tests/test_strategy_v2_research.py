from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.domain.market import Candle
from backtest.strategy_v2_artifacts import write_strategy_v2_artifacts
from backtest.strategy_v2_candidates import CandidateVariant, EntryEpisode
from backtest.strategy_v2_research import (
    CandidateResult,
    StrategyV2Study,
    _random_analysis,
    _trade_candidates,
    _walk_forward,
)
from backtest.vwap_fixed_exit_research import CostModel, TradeCandidate, simulate_portfolio


def _candles(count: int = 24 * 500) -> list[Candle]:
    start = datetime(2023, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    for index in range(count):
        price = Decimal("100") + Decimal(index) / Decimal("100")
        result.append(
            Candle(
                timestamp=start + timedelta(hours=index),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + Decimal("0.1"),
                volume=Decimal("10"),
                confirmed=True,
            )
        )
    return result


def _episode(candles: list[Candle], index: int) -> EntryEpisode:
    stamp = candles[index].timestamp.isoformat()
    returns: dict[int, float | None] = {h: 0.001 for h in (1, 3, 6, 12, 24, 48, 72)}
    return EntryEpisode(
        f"episode-{index}",
        "price_breakout",
        "breakout_20",
        index,
        index,
        stamp,
        index + 1,
        candles[index + 1].timestamp.isoformat(),
        float(candles[index + 1].open),
        1,
        True,
        "signal_ended",
        "bull",
        "normal",
        False,
        returns,
        returns,
        {h: -0.001 for h in returns},
    )


def test_random_benchmark_is_deterministic_and_candidate_isolated() -> None:
    candles = _candles(2_000)
    episodes = tuple(_episode(candles, index) for index in range(200, 1_500, 50))
    variant = CandidateVariant(
        "price_breakout", "breakout_20", "r", "e", "b", {"lookback": 20}, True
    )
    first = _random_analysis(candles, episodes, variant)
    second = _random_analysis(candles, episodes, variant)
    assert first == second
    assert all(row["candidate_id"] == "price_breakout" for row in first)


def test_fixed_exit_uses_next_open_and_same_cost_candidate_contract() -> None:
    candles = _candles(500)
    episodes = (_episode(candles, 200), _episode(candles, 205))
    selected = _trade_candidates(candles, episodes, 24, 0, len(candles))
    assert selected[0].entry_index == 201
    assert selected[0].exit_index == 225
    assert len(selected) == 1


def test_same_cost_model_penalizes_every_candidate() -> None:
    candles = _candles(500)
    episode = _episode(candles, 200)
    candidate = TradeCandidate(
        episode.episode_id,
        episode.signal_timestamp,
        201,
        225,
        episode.market_regime,
        episode.volatility_regime,
        False,
    )
    zero, _ = simulate_portfolio(candles, (candidate,), 24, CostModel.equal_split(0))
    stressed, _ = simulate_portfolio(candles, (candidate,), 24, CostModel.equal_split(20))
    assert float(stressed[0]["net_return"]) < float(zero[0]["net_return"])


def test_episode_generation_is_reproducible() -> None:
    candles = _candles(500)
    episodes = tuple(_episode(candles, index) for index in (200, 250, 300))
    assert episodes == tuple(_episode(candles, index) for index in (200, 250, 300))


def test_walk_forward_is_chronological_and_non_overlapping() -> None:
    candles = _candles(24 * 365 * 3)
    episodes = tuple(_episode(candles, index) for index in range(200, len(candles) - 100, 100))
    rows = _walk_forward(candles, episodes, 24, int(len(candles) * 0.8))
    assert rows
    for row in rows:
        assert row["train_start"] < row["train_end"] <= row["test_start"] < row["test_end"]


def test_artifacts_are_complete_and_do_not_mutate_production_config(tmp_path: Path) -> None:
    config = Path("configs/btc_vwap_shadow.yaml")
    before = config.read_bytes()
    score = {
        "candidate_id": "price_breakout",
        "episodes": 100,
        "forward_edge": 0.01,
        "random_percentile": 0.9,
        "stage_reached": "forward_edge",
        "final_status": "REJECTED_NO_EDGE",
        "recent_edge": -0.01,
        "best_fixed_exit": None,
        "10bps_return": None,
        "20bps_return": None,
        "max_drawdown": None,
        "Sharpe": None,
        "profit_factor": None,
        "top5_concentration": None,
        "walk_forward_return": None,
        "walk_forward_sharpe": None,
        "positive_oos_windows": None,
        "temporal_fragility": True,
        "regime_fragility": True,
        "cost_fragility": False,
    }
    result = CandidateResult(
        "price_breakout",
        "breakout_20",
        "REJECTED_NO_EDGE",
        "forward_edge",
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        score,
    )
    study = StrategyV2Study(
        ({"candidate_id": "price_breakout"},),
        1,
        (),
        (result,),
        (score,),
        (),
        "NO_STRATEGY_CANDIDATE_FOUND",
        True,
    )
    output = tmp_path / "artifact"
    write_strategy_v2_artifacts(
        output,
        study=study,
        candidate_spec_payload={"candidates": []},
        data_manifest={"dataset_hash": "fixture", "normalized_rows": 10},
        source_artifact_hashes={},
        frozen_file_hashes={},
    )
    assert config.read_bytes() == before
    assert {
        "report.md",
        "summary.json",
        "candidate_specs.json",
        "candidate_scoreboard.csv",
        "candidate_A",
    }.issubset({path.name for path in output.iterdir()})
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["research_integrity"]["production_strategy_changed"] is False
    assert summary["final_holdout_not_pristine"] is True
