from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.run_config import RunConfig, load_run_config
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.order import Order, OrderRequest, OrderSide, OrderState, OrderType
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.reproducibility import InstrumentSnapshotStore, build_run_start_manifest, canonical_hash
from app.runtime.clock import BacktestClock
from app.services.strategy_shadow import ConfirmedStrategyShadow
from app.strategies.base import strategy_identity
from app.strategies.registry import STRATEGY_REGISTRY, create_strategy, validate_strategy_parameters
from app.strategies.v3 import V3Strategy
from app.strategies.v3_features import V3FeatureStream, generate_v3_signals_reference
from backtest.research_execution import evaluate
from backtest.strategy_v3_candidates import V3Variant, generate_v3_signals, load_v3_specs
from backtest.strategy_v3_features import aggregate_completed

START = datetime(2024, 1, 1, tzinfo=UTC)
VARIANTS = load_v3_specs(
    Path(__file__).parents[1] / "configs/research/strategy_v3_candidate_specs.json"
)


def candles(count: int = 360, *, offset: int = 0) -> list[Candle]:
    result = []
    for index in range(count):
        base = Decimal("100") + Decimal(index) / 20
        close = base + (Decimal("4") if index % 17 == 0 else Decimal("0.1"))
        result.append(
            Candle(
                START + timedelta(hours=offset + index),
                base,
                close + Decimal("0.5"),
                base - Decimal("0.5"),
                close,
                Decimal("100") if index % 17 == 0 else Decimal("10"),
                True,
            )
        )
    return result


def portfolio(position: str = "0") -> PortfolioSnapshot:
    return PortfolioSnapshot({"USDT": Decimal("10000")}, {"BTC-USDT": Decimal(position)}, {})


def instrument() -> Instrument:
    return InstrumentSnapshotStore.load(
        Path(__file__).parents[1] / "configs/snapshots/BTC-USDT.json"
    ).instrument


def config(variant: V3Variant) -> RunConfig:
    return RunConfig.model_validate(
        {
            "mode": "backtest",
            "market": {"instrument_id": "BTC-USDT", "bar": "1h"},
            "strategy": {
                "name": variant.candidate_id,
                "parameters": variant.parameters,
                "implementation_version": V3Strategy.implementation_version,
            },
            "data": {"source": "csv", "path": "synthetic-fixture.csv"},
        }
    )


def shadow(variant: V3Variant, **kwargs: Any) -> ConfirmedStrategyShadow:
    return ConfirmedStrategyShadow(
        config(variant), instrument(), kwargs.pop("portfolio", portfolio()), **kwargs
    )


def semantic(signal: Signal) -> dict[str, Any]:
    return {**asdict(signal), "timestamp": signal.timestamp.isoformat()}


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda item: item.variant_id)
@pytest.mark.parametrize("offset", [0, 1, 3])
def test_step_features_equal_original_research_formulas(variant: V3Variant, offset: int) -> None:
    bars = candles(offset=offset)
    htf = aggregate_completed(bars, hours=4)
    assert generate_v3_signals(bars, htf, variant) == generate_v3_signals_reference(
        bars, htf, variant
    )


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda item: item.variant_id)
def test_batch_step_and_checkpoint_resume_have_identical_signals(
    variant: V3Variant, tmp_path: Path
) -> None:
    bars = candles()
    batch = shadow(variant)
    assert batch.replay(bars[:100], warmup=True) == []
    expected = batch.replay(bars[100:])
    step = shadow(variant)
    for bar in bars[:100]:
        assert step.on_candle(bar, warmup=True) == []
    before = [signal for bar in bars[100:201] for signal in step.on_candle(bar)]
    checkpoint = tmp_path / "state.json"
    step.save_checkpoint(checkpoint)
    import json

    restored = shadow(variant, resume_state=json.loads(checkpoint.read_text(encoding="utf-8")))
    after = [signal for bar in bars[201:] for signal in restored.on_candle(bar)]
    assert list(map(semantic, [*before, *after])) == list(map(semantic, expected))
    assert restored.checkpoint_state() == batch.checkpoint_state()
    assert all(item.timestamp >= bars[100].timestamp + timedelta(hours=1) for item in expected)


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda item: item.variant_id)
def test_future_mutation_does_not_change_published_signals(variant: V3Variant) -> None:
    bars = candles(220)
    changed = [
        *bars[:151],
        *[
            replace(
                bar,
                open=bar.open * 10,
                high=bar.high * 10,
                low=bar.low * 10,
                close=bar.close * 10,
                volume=bar.volume * 10,
            )
            for bar in bars[151:]
        ],
    ]
    assert shadow(variant).replay(bars)[:151] == shadow(variant).replay(changed)[:151]


def test_htf_boundary_uses_only_fully_closed_four_hour_bars() -> None:
    variant = next(item for item in VARIANTS if item.variant_id == "htf_breakout_20")
    bars = candles(48)
    bars[46] = replace(bars[46], close=Decimal("200"), high=Decimal("201"))
    bars[47] = replace(bars[47], close=Decimal("220"), high=Decimal("221"))
    stream = V3FeatureStream(variant)
    flags = [stream.update(bar)[0] for bar in bars]
    assert flags[46] is False  # Only eleven complete 4H bars at 23:00.
    assert flags[47] is True  # The twelfth becomes usable at 00:00.


def feedback(
    adapter: ConfirmedStrategyShadow,
    identifier: str,
    state: OrderState,
    *,
    created: datetime,
    updated: datetime | None = None,
    filled: str = "0",
    position: str = "0",
    side: OrderSide = OrderSide.BUY,
) -> Order:
    order = Order(
        OrderRequest(
            identifier,
            "BTC-USDT",
            side,
            OrderType.LIMIT,
            Decimal("1"),
            Decimal("100"),
            "fixture",
            created,
            strategy_name=adapter.strategy.name,
            bar="1h",
        ),
        state=state,
        filled_quantity=Decimal(filled),
        updated_at=updated or created,
    )
    adapter.on_order_update(order, portfolio(position), at=updated or created)
    return order


def test_pending_feedback_and_nonflat_restore_preserve_holding_timer() -> None:
    variant = next(item for item in VARIANTS if item.variant_id == "volume_breakout_20")
    bars = candles(72)
    left = shadow(variant)
    left.replay(bars[:34], warmup=True)
    now = left.clock.now()
    feedback(left, "A", OrderState.ACCEPTED, created=now)
    feedback(left, "B", OrderState.ACCEPTED, created=now)
    feedback(
        left,
        "A",
        OrderState.PARTIALLY_FILLED,
        created=now,
        updated=now + timedelta(minutes=5),
        filled="0.2",
        position="0.2",
    )
    saved = left.checkpoint_state()
    right = shadow(
        variant, portfolio=portfolio("0.2"), resume_state=saved, open_order_ids=("A", "B")
    )
    for adapter in (left, right):
        feedback(
            adapter,
            "A",
            OrderState.FILLED,
            created=now,
            updated=now + timedelta(minutes=10),
            filled="1",
            position="1",
        )
    expected = left.replay(bars[34:60])
    actual = right.replay(bars[34:60])
    assert expected == actual
    assert all(
        item.action is SignalAction.HOLD for item in actual
    )  # B still outstanding, even after 24H.
    for adapter in (left, right):
        feedback(
            adapter,
            "B",
            OrderState.CANCELLED,
            created=now,
            updated=adapter.clock.now(),
            position="1",
        )
    assert left.on_candle(bars[60]) == right.on_candle(bars[60])
    assert left.last_signal is not None and left.last_signal.action is SignalAction.CLOSE
    assert left.checkpoint_state() == right.checkpoint_state()


def test_cancelling_one_order_cannot_clear_another_pending_order() -> None:
    variant = next(item for item in VARIANTS if item.variant_id == "volume_breakout_20")
    bars = candles(35)
    adapter = shadow(variant)
    adapter.replay(bars[:34], warmup=True)
    now = adapter.clock.now()
    feedback(adapter, "A", OrderState.ACCEPTED, created=now)
    feedback(adapter, "B", OrderState.ACCEPTED, created=now)
    feedback(adapter, "A", OrderState.CANCELLED, created=now)
    assert isinstance(adapter.strategy, V3Strategy)
    assert adapter.strategy.open_order_ids == ("B",)
    assert adapter.on_candle(bars[34])[0].action is SignalAction.HOLD
    control = shadow(variant)
    control.replay(bars[:34], warmup=True)
    assert control.on_candle(bars[34])[0].action is SignalAction.BUY


@pytest.mark.parametrize("kind", ["duplicate", "out_of_order", "gap", "unconfirmed", "unaligned"])
def test_bad_input_is_rejected_without_state_change(kind: str) -> None:
    variant = VARIANTS[0]
    bars = candles(8)
    adapter = shadow(variant)
    adapter.replay(bars[:4])
    before = adapter.checkpoint_state()
    bad = {
        "duplicate": bars[3],
        "out_of_order": bars[1],
        "gap": bars[5],
        "unconfirmed": replace(bars[4], confirmed=False),
        "unaligned": replace(bars[4], timestamp=bars[4].timestamp + timedelta(minutes=1)),
    }[kind]
    with pytest.raises(ValueError):
        adapter.on_candle(bad)
    assert adapter.checkpoint_state() == before
    assert adapter.on_candle(bars[4]) == shadow(variant).replay(bars[:5])[-1:]


@pytest.mark.parametrize(
    "field,value",
    [
        ("format_version", 2),
        ("implementation_version", "other"),
        ("previous", True),
        ("bar_count", 999),
    ],
)
def test_semantically_incompatible_state_is_rejected(field: str, value: Any) -> None:
    adapter = shadow(VARIANTS[0])
    adapter.replay(candles(8))
    saved = adapter.checkpoint_state()
    changed = copy.deepcopy(saved)
    changed["strategy_state"][field] = value
    changed["strategy_state"]["state_hash"] = canonical_hash(
        {key: item for key, item in changed["strategy_state"].items() if key != "state_hash"}
    )
    changed["state_hash"] = canonical_hash(
        {key: item for key, item in changed.items() if key != "state_hash"}
    )
    with pytest.raises(ValueError):
        shadow(VARIANTS[0], resume_state=changed)
    assert adapter.checkpoint_state() == saved


def test_configuration_registry_runtime_report_and_event_identity_match() -> None:
    variant = next(item for item in VARIANTS if item.variant_id == "volume_breakout_20")
    runtime = create_strategy(variant.candidate_id, variant.parameters, instrument())
    assert isinstance(runtime, V3Strategy)
    assert STRATEGY_REGISTRY[runtime.name].implementation_version == runtime.implementation_version
    trace: list[dict[str, Any]] = []
    result = evaluate(
        candles(100), runtime, csv_path=Path("synthetic-fixture.csv"), signal_trace=trace
    )
    expected = strategy_identity(runtime)
    assert result.strategy_name == config(variant).strategy.name == runtime.name
    assert result.summary["strategy_identity"] == expected
    assert all(
        row["strategy_name"] == runtime.name and row["payload"]["strategy_identity"] == expected
        for row in trace
    )
    assert all(fill.strategy_name == runtime.name for fill in result.fills)
    assert result.fills
    manifest = build_run_start_manifest(
        run_id="fixture",
        config=config(variant),
        instrument_snapshot=InstrumentSnapshotStore.load(
            Path(__file__).parents[1] / "configs/snapshots/BTC-USDT.json"
        ),
        run_started_at=START,
    )
    assert manifest["strategy_identity"] == expected


def test_parameter_validation_and_execution_mode_fail_closed() -> None:
    variant = VARIANTS[0]
    with pytest.raises(ValueError):
        validate_strategy_parameters(variant.candidate_id, {**variant.parameters, "extra": 1})
    with pytest.raises(ValueError):
        validate_strategy_parameters(variant.candidate_id, {**variant.parameters, "htf_fast": True})
    with pytest.raises(ValueError):
        validate_strategy_parameters(
            variant.candidate_id, {**variant.parameters, "htf_fast": 100, "htf_slow": 12}
        )
    with pytest.raises(ValueError, match="research strategy"):
        load_run_config(
            cli_overrides={
                "strategy.name": variant.candidate_id,
                "strategy.parameters": variant.parameters,
                "mode": "demo",
            },
            environ={},
        )


def test_nonflat_or_pending_state_requires_explicit_evidence() -> None:
    variant = VARIANTS[0]
    with pytest.raises(ValueError, match="nonflat"):
        shadow(variant, portfolio=portfolio("1"))
    adapter = shadow(variant)
    adapter.replay(candles(2))
    now = adapter.clock.now()
    feedback(adapter, "pending", OrderState.ACCEPTED, created=now)
    with pytest.raises(ValueError, match="reconciliation"):
        shadow(variant, resume_state=adapter.checkpoint_state())
    with pytest.raises(ValueError, match="position"):
        shadow(
            variant,
            portfolio=portfolio("1"),
            resume_state=adapter.checkpoint_state(),
            open_order_ids=("pending",),
        )


def test_open_time_clock_and_late_warmup_are_not_accepted() -> None:
    variant = VARIANTS[0]
    adapter = shadow(variant)
    bar = candles(1)[0]
    context = StrategyContext(
        "run",
        TradingMode.BACKTEST,
        variant.candidate_id,
        instrument(),
        "1h",
        portfolio(),
        MarketSnapshot(bar, bar.close),
        BacktestClock(bar.timestamp),
    )
    with pytest.raises(ValueError, match="decision time"):
        adapter.strategy.on_bar(context, bar)
    adapter.on_candle(bar)
    before = adapter.checkpoint_state()
    with pytest.raises(ValueError, match="return to warmup"):
        adapter.on_candle(candles(2)[1], warmup=True)
    assert adapter.checkpoint_state() == before


def test_actual_backtest_feedback_replayed_into_readonly_shadow_yields_identical_signals() -> None:
    variant = next(item for item in VARIANTS if item.variant_id == "volume_breakout_20")
    timeline: list[tuple[str, Any, PortfolioSnapshot, datetime, bool]] = []
    emitted: list[Signal] = []

    class RecordedV3(V3Strategy):
        def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
            signals = super().on_bar(context, bar)
            timeline.append(
                ("bar", bar, context.portfolio_snapshot, context.now, context.is_warmup)
            )
            emitted.extend(signals)
            return signals

        def on_order_update(self, context: StrategyContext, order: Order) -> None:
            super().on_order_update(context, order)
            timeline.append(
                ("order", copy.deepcopy(order), context.portfolio_snapshot, context.now, False)
            )

    from app.strategies.v3_parameters import validated_parameters

    strategy = RecordedV3(
        variant.candidate_id, validated_parameters(variant.candidate_id, variant.parameters)
    )
    bars = candles(280)
    result = evaluate(
        bars[100:], strategy, csv_path=Path("synthetic-development.csv"), warmup_candles=bars[:100]
    )
    assert result.fills and result.closed_trades
    observed = shadow(variant)
    actual = []
    for kind, value, snapshot, at, warmup in timeline:
        if kind == "bar":
            actual.extend(observed.on_candle(value, warmup=warmup))
        else:
            observed.on_order_update(value, snapshot, at=at)
    assert actual == emitted
    assert strategy.checkpoint_state() == observed.strategy.checkpoint_state()


def test_explicit_version_cannot_silently_resolve_to_another_implementation() -> None:
    variant = VARIANTS[0]
    cfg = config(variant)
    cfg.strategy.implementation_version = "unavailable-version"
    with pytest.raises(ValueError, match="unavailable"):
        ConfirmedStrategyShadow(cfg, instrument(), portfolio())
    cfg.strategy.implementation_version = None
    with pytest.raises(ValueError, match="explicit implementation_version"):
        ConfirmedStrategyShadow(cfg, instrument(), portfolio())
    with pytest.raises(ValueError, match="unavailable"):
        create_strategy(
            variant.candidate_id, variant.parameters, instrument(), implementation_version="other"
        )
