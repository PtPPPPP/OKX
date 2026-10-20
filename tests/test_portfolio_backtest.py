from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.config.run_config import RunConfig
from app.domain.context import StrategyContext
from app.domain.events import EventBus
from app.domain.market import Candle, Instrument, InstrumentType
from app.domain.order import ApprovedOrder, Order, OrderSide, OrderState, OrderType, ProposedOrder
from app.domain.position import Portfolio
from app.domain.signal import Signal, SignalAction
from app.execution.backtest_broker import BacktestBroker
from app.portfolio.backtest import PortfolioBacktestBroker, StrategyBroker
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.reproducibility import canonical_hash
from app.risk.risk_manager import default_risk_manager
from app.runtime.clock import BacktestClock
from app.strategies.base import Strategy
from app.strategies.registry import STRATEGY_REGISTRY, StrategyRegistration, create_strategy
from backtest.engine import BacktestEngine
from backtest.portfolio import PortfolioBacktestEngine, PortfolioConfig, write_immutable_json
from scripts.demo_portfolio import demonstrate

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)


def candle(index: int, price: str = "100", volume: str = "10") -> Candle:
    return Candle(START + timedelta(minutes=5 * index), *([D(price)] * 4), D(volume), True)


def config(*, fee: str = "0.001", allocations: tuple[str, str] = ("60", "40")) -> PortfolioConfig:
    return PortfolioConfig.model_validate(
        {
            "backtest": {"initial_capital": "100", "fee_rate": fee, "slippage_rate": "0"},
            "risk": {
                "max_order_notional": "100",
                "max_total_exposure": "100",
                "max_daily_loss": "20",
                "max_drawdown_pct": "50",
                "max_orders_per_minute": 10,
            },
            "protective_exits": {"enabled": False},
            "instances": [
                {
                    "instance_id": key,
                    "allocation": allocation,
                    "strategy": {"name": "buy_and_hold", "parameters": {}},
                    "order_notional": "80",
                }
                for key, allocation in zip(("alpha", "beta"), allocations, strict=True)
            ],
        }
    )


def approve(
    owner: str,
    order_id: str,
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: str = "0.5",
    price: str = "100",
    at: datetime = START,
) -> ApprovedOrder:
    return ApprovedOrder(
        ProposedOrder(
            order_id,
            "run",
            "fixture",
            "BTC-USDT",
            side,
            OrderType.LIMIT,
            D(quantity),
            D(price),
            order_id,
            at,
            strategy_instance_id=owner,
        ),
        at,
        "offline",
    )


def broker(instrument: Instrument, *, fee: str = "0.001") -> PortfolioBacktestBroker:
    return PortfolioBacktestBroker(
        instrument, D("100"), {"alpha": D("60"), "beta": D("40")}, D(fee), D("0")
    )


class FixtureParameters(BaseModel):
    actions: dict[int, str]


class FixtureStrategy:
    name = "portfolio_fixture"
    description = "deterministic test fixture only"
    required_history = 1
    supported_market_types = frozenset({InstrumentType.SPOT})

    def __init__(self, parameters: FixtureParameters) -> None:
        self.parameters = parameters
        self.index = 0
        self.feedback: list[tuple[str, str]] = []

    def on_start(self, context: StrategyContext) -> None:
        self.index = 0

    def on_bar(self, context: StrategyContext, bar: Candle) -> list[Signal]:
        action = SignalAction(self.parameters.actions.get(self.index, "hold"))
        self.index += 1
        return [Signal("arbitrary", self.name, "BTC-USDT", action, context.now, "fixture", D("1"))]

    def on_order_update(self, context: StrategyContext, order: Order) -> None:
        self.feedback.append((order.request.strategy_instance_id, order.state.value))

    def on_stop(self, context: StrategyContext) -> None:
        pass


@pytest.fixture
def fixture_config(monkeypatch: pytest.MonkeyPatch) -> PortfolioConfig:
    def factory(parameters: BaseModel) -> Strategy:
        assert isinstance(parameters, FixtureParameters)
        return FixtureStrategy(parameters)

    monkeypatch.setitem(
        STRATEGY_REGISTRY,
        "portfolio_fixture",
        StrategyRegistration(
            FixtureParameters,
            factory,
            "test",
            1,
            frozenset({InstrumentType.SPOT}),
            implementation_version="fixture1",
        ),
    )
    data = config(fee="0").model_dump(mode="json")
    for item in data["instances"]:
        item["strategy"] = {
            "name": "portfolio_fixture",
            "parameters": {
                "actions": {0: "buy", 2: "close"} if item["instance_id"] == "alpha" else {0: "buy"}
            },
        }
        item["order_notional"] = "30"
    return PortfolioConfig.model_validate(data)


def test_competing_entries_are_clipped_for_fees_without_double_spend(
    btc_instrument: Instrument,
) -> None:
    runner = PortfolioBacktestEngine(config(), btc_instrument, run_id="run")
    runner.advance(candle(0))
    runner.advance(candle(1))
    report = runner.report()
    assert len(report["fills"]) == 2
    assert all(
        fill["quantity"] * fill["fill_price"] + fill["fee"] <= allocation
        for fill, allocation in zip(report["fills"], (D("60"), D("40")), strict=True)
    )
    assert report["account"]["cash"] >= 0
    assert report["account"]["cash"] < D("0.01")
    assert all(fill["strategy_instance_id"] for fill in report["fills"])
    assert report["benchmark"]["initial_capital"] == D("100")
    assert sum(s["allocated_capital"] for s in report["strategies"].values()) == D("100")


def test_one_strategy_exit_does_not_consume_other_inventory(
    btc_instrument: Instrument,
    fixture_config: PortfolioConfig,
) -> None:
    runner = PortfolioBacktestEngine(fixture_config, btc_instrument, run_id="run")
    report = runner.run([candle(i) for i in range(5)])
    assert report["strategies"]["alpha"]["quantity"] == 0
    assert report["strategies"]["beta"]["quantity"] == D("0.3")
    assert report["account"]["quantity"] == D("0.3")
    assert len(report["strategies"]["alpha"]["closed_trades"]) == 1
    assert report["strategies"]["beta"]["closed_trades"] == []
    assert all(
        owner == key
        for key, engine in runner.engines.items()
        for owner, _ in engine.strategy.feedback
    )  # type: ignore[attr-defined]


def test_identical_strategy_instances_with_different_parameters_have_distinct_identity(
    btc_instrument: Instrument,
) -> None:
    data = config().model_dump(mode="json")
    for item, period in zip(data["instances"], (2, 3), strict=True):
        item["strategy"] = {
            "name": "moving_average_cross",
            "parameters": {"fast_period": period, "slow_period": 5},
        }
    runner = PortfolioBacktestEngine(
        PortfolioConfig.model_validate(data), btc_instrument, run_id="run"
    )
    runner.advance(candle(0))
    identities = [s["strategy_identity"] for s in runner.report()["strategies"].values()]
    assert identities[0] != identities[1]
    assert runner.engines["alpha"].strategy is not runner.engines["beta"].strategy
    events = [event for event in runner.event_bus.events if event.event_type == "SignalGenerated"]
    assert {event.payload["strategy_instance_id"] for event in events} == {"alpha", "beta"}
    assert len({event.payload["signal_id"] for event in events}) == 2


def test_partial_fill_then_cancel_releases_only_remaining_reservation(
    btc_instrument: Instrument,
) -> None:
    shared = broker(btc_instrument)
    shared.begin_bar(candle(0, volume="0.2"))
    order = shared.submit_order(approve("alpha", "a"))
    assert order.state is OrderState.PARTIALLY_FILLED
    assert shared.reserved_cash("alpha") == D("30.03")
    assert shared.available_cash("alpha") == D("9.95")
    shared.cancel_order("BTC-USDT", "a")
    assert shared.reserved_cash() == 0
    assert shared.available_cash("alpha") == D("39.98")
    assert shared.books["alpha"].portfolio.positions["BTC-USDT"] == D("0.2")


def test_all_instances_share_one_bar_volume_budget(btc_instrument: Instrument) -> None:
    shared = broker(btc_instrument, fee="0")
    shared.begin_bar(candle(0, volume="0.3"))
    shared.submit_order(approve("alpha", "a", quantity="0.5"))
    shared.submit_order(approve("beta", "b", quantity="0.4"))
    shared.begin_intrabar(START + timedelta(minutes=5))
    shared.match_orders()
    assert sum(fill.quantity for fill in shared.fills) == D("0.3")
    assert shared.get_order("BTC-USDT", "b").filled_quantity == 0
    shared.begin_bar(candle(1, volume="0.3"))
    shared.match_orders()
    assert shared.get_order("BTC-USDT", "a").state is OrderState.FILLED
    assert shared.get_order("BTC-USDT", "b").filled_quantity == D("0.1")


def test_duplicate_fill_is_noop_and_conflicting_duplicate_rejected(
    btc_instrument: Instrument,
) -> None:
    shared = broker(btc_instrument)
    shared.begin_bar(candle(0))
    shared.submit_order(approve("alpha", "a"))
    before = asdict(shared.portfolio)
    assert not shared.apply_fill(shared.fills[0])
    assert asdict(shared.portfolio) == before
    with pytest.raises(ValueError, match="conflicting duplicate"):
        shared.apply_fill(replace(shared.fills[0], fee=D("123")))


@pytest.mark.parametrize("quantity,price", [("0.5994", "100"), ("0.59941", "100"), ("0.6", "99.9")])
def test_fee_and_precision_boundary_never_overdraw(
    btc_instrument: Instrument,
    quantity: str,
    price: str,
) -> None:
    shared = broker(btc_instrument)
    shared.begin_bar(candle(0, price))
    order = shared.submit_order(approve("alpha", "a", quantity=quantity, price=price))
    assert order.state is (
        OrderState.FILLED if D(quantity) * D(price) * D("1.001") <= 60 else OrderState.REJECTED
    )
    assert shared.available_cash() >= 0 and shared.available_cash("alpha") >= 0


@pytest.mark.parametrize("action", ["steal_sell", "steal_cancel", "overspend", "unknown_owner"])
def test_ownership_and_fixed_partitions_fail_closed(
    btc_instrument: Instrument, action: str
) -> None:
    shared = broker(btc_instrument)
    shared.begin_bar(candle(0))
    shared.submit_order(approve("alpha", "a"))
    if action == "steal_sell":
        assert (
            shared.submit_order(approve("beta", "sell", side=OrderSide.SELL)).state
            is OrderState.REJECTED
        )
    elif action == "steal_cancel":
        with pytest.raises(ValueError, match="another instance"):
            StrategyBroker(shared, "beta").cancel_order("BTC-USDT", "a")
    elif action == "overspend":
        assert (
            shared.submit_order(approve("beta", "b", quantity="0.5")).state is OrderState.REJECTED
        )
    else:
        with pytest.raises(ValueError, match="unknown strategy instance"):
            shared.submit_order(approve("other", "b"))


def test_owned_average_cost_and_account_conserve_after_partial_exit(
    btc_instrument: Instrument,
) -> None:
    shared = broker(btc_instrument)
    shared.begin_bar(candle(0))
    shared.submit_order(approve("alpha", "a", quantity="0.5"))
    shared.begin_bar(candle(1, "80"))
    shared.submit_order(approve("beta", "b", quantity="0.4", price="80", at=candle(1).timestamp))
    shared.begin_bar(candle(2, "120"))
    shared.submit_order(
        approve(
            "alpha",
            "sell",
            quantity="0.2",
            price="120",
            side=OrderSide.SELL,
            at=candle(2).timestamp,
        )
    )
    assert shared.books["alpha"].portfolio.average_entry_prices["BTC-USDT"] == 100
    assert shared.books["beta"].portfolio.average_entry_prices["BTC-USDT"] == 80
    assert shared.portfolio.realized_pnl == D("3.894")
    equity = shared.portfolio.equity("BTC-USDT", "BTC", "USDT", D("120"))
    assert abs(
        equity - (D("100") + shared.portfolio.realized_pnl + shared.unrealized_pnl(D("120")))
    ) < D("1e-20")


@pytest.mark.parametrize("reverse", [False, True])
def test_deterministic_allocation_and_fills_independent_of_input_order(
    btc_instrument: Instrument,
    reverse: bool,
) -> None:
    first = config()
    second = config()
    if reverse:
        second.instances.reverse()
    a = PortfolioBacktestEngine(first, btc_instrument, run_id="same")
    b = PortfolioBacktestEngine(second, btc_instrument, run_id="same")
    candles = [candle(i, volume="0.3") for i in range(6)]
    assert canonical_hash(a.run(candles)) == canonical_hash(b.run(candles))


def test_priority_then_id_determines_liquidity_competition(btc_instrument: Instrument) -> None:
    cfg = config(fee="0")
    cfg.instances[1].priority = 0
    cfg.instances[0].priority = 1
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run")
    runner.advance(candle(0, volume="0.1"))
    runner.advance(candle(1, volume="0.1"))
    assert runner.broker.fills[0].strategy_instance_id == "beta"


@pytest.mark.parametrize("with_cancel", [False, True])
def test_checkpoint_replays_positions_pending_fills_and_legacy_strategy_state(
    btc_instrument: Instrument,
    tmp_path: Path,
    with_cancel: bool,
) -> None:
    cfg = config()
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run")
    runner.advance(candle(0, volume="0.1"))
    runner.advance(candle(1, volume="0.1"))
    if with_cancel:
        order = runner.views["alpha"].get_open_orders()[0]
        runner.cancel_order("alpha", order.request.client_order_id)
    checkpoint = tmp_path / "checkpoint.json"
    runner.save_checkpoint(checkpoint)
    restored = PortfolioBacktestEngine.restore(checkpoint, cfg, btc_instrument)
    assert canonical_hash(restored.report()) == canonical_hash(runner.report())
    assert not restored.broker.apply_fill(restored.broker.fills[0])
    for index in range(2, 12):
        runner.advance(candle(index, volume="0.1"))
        restored.advance(candle(index, volume="0.1"))
    assert canonical_hash(restored.finish()) == canonical_hash(runner.finish())


@pytest.mark.parametrize(
    "change", ["bytes", "state", "format", "runtime", "budget", "fees", "instrument", "parameters"]
)
def test_inconsistent_checkpoint_refuses_recovery(
    btc_instrument: Instrument,
    tmp_path: Path,
    change: str,
) -> None:
    cfg = config()
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run")
    runner.advance(candle(0))
    path = tmp_path / "checkpoint.json"
    runner.save_checkpoint(path)
    data = json.loads(path.read_text())
    if change == "bytes":
        data["history"][0]["candle"]["close"] = "101"
    elif change in {"state", "format", "runtime"}:
        data.pop("content_hash")
        if change == "state":
            data["state_hash"] = "invalid"
        elif change == "format":
            data["format_version"] = 999
        else:
            data["runtime_identity"]["python"] = "unknown"
        data["content_hash"] = canonical_hash(data)
    elif change == "budget":
        cfg.instances[0].allocation = D("59")
    elif change == "fees":
        cfg.backtest.fee_rate = D("0.002")
    elif change == "parameters":
        cfg.instances[0].strategy.name = "moving_average_cross"
    else:
        btc_instrument = replace(btc_instrument, quantity_step=D("0.01"))
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="mismatch"):
        PortfolioBacktestEngine.restore(path, cfg, btc_instrument)


@pytest.mark.parametrize("problem", ["duplicate", "gap", "reverse", "unconfirmed"])
def test_bad_market_input_does_not_change_state(btc_instrument: Instrument, problem: str) -> None:
    runner = PortfolioBacktestEngine(config(), btc_instrument, run_id="run")
    runner.advance(candle(0))
    before = canonical_hash(runner.report())
    bad = {
        "duplicate": candle(0),
        "gap": candle(2),
        "reverse": candle(-1),
        "unconfirmed": replace(candle(1), confirmed=False),
    }[problem]
    with pytest.raises(ValueError):
        runner.advance(bad)
    assert canonical_hash(runner.report()) == before


def test_account_exposure_includes_other_strategies_and_pending_orders(
    btc_instrument: Instrument,
) -> None:
    cfg = config(fee="0")
    cfg.risk.max_order_notional = cfg.risk.max_total_exposure = D("70")
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run")
    runner.advance(candle(0))
    runner.advance(candle(1, volume="0.1"))
    assert len(runner.broker._orders) == 1
    rejected = [e for e in runner.event_bus.events if e.event_type == "RiskRejected"]
    assert any("maximum_exposure" in e.payload["rejected_by"] for e in rejected)


def test_loss_limit_freezes_entries_but_owned_exit_is_allowed(
    btc_instrument: Instrument,
    fixture_config: PortfolioConfig,
) -> None:
    runner = PortfolioBacktestEngine(fixture_config, btc_instrument, run_id="run")
    runner.advance(candle(0))
    runner.advance(candle(1))
    # Market depreciation exceeds account daily loss and drawdown limits.
    runner.advance(candle(2, "10"))
    runner.advance(candle(3, "10"))
    assert runner.broker.books["alpha"].portfolio.positions["BTC-USDT"] == 0
    assert runner.broker.books["beta"].portfolio.positions["BTC-USDT"] > 0


class History:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def get_historical_bars(
        self,
        instrument_id: str,
        bar: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        return self.candles


@pytest.mark.parametrize("protective", [False, True])
def test_single_instance_matches_existing_backtest_economics(
    btc_instrument: Instrument,
    protective: bool,
) -> None:
    cfg = config()
    cfg.instances = cfg.instances[:1]
    cfg.instances[0].allocation = D("100")
    cfg.instances[0].order_notional = D("20")
    cfg.protective_exits.enabled = protective
    candles = [
        candle(0),
        candle(1),
        Candle(candle(2).timestamp, D("100"), D("110"), D("90"), D("105"), D("1"), True),
        candle(3, "105"),
    ]
    multi = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run").run(candles)
    run_cfg = RunConfig(
        strategy=cfg.instances[0].strategy,
        market=cfg.market,
        risk=cfg.risk,
        backtest=cfg.backtest,
        protective_exits=cfg.protective_exits,
    )
    shared = BacktestBroker(
        Portfolio({"USDT": D("100")}),
        btc_instrument,
        cfg.backtest.fee_rate,
        cfg.backtest.slippage_rate,
    )
    risk = cfg.risk
    original = BacktestEngine(
        run_id="run",
        config=run_cfg,
        instrument=btc_instrument,
        provider=History(candles),
        strategy=create_strategy("buy_and_hold", {}, btc_instrument),
        broker=shared,
        position_sizer=FixedNotionalPositionSizer(D("20")),
        risk_manager=default_risk_manager(
            maximum_order_notional=risk.max_order_notional,
            maximum_exposure=risk.max_total_exposure,
            maximum_daily_loss=risk.max_daily_loss,
            maximum_drawdown_pct=risk.max_drawdown_pct,
            maximum_orders_per_minute=10,
            stale_after_seconds=600,
        ),
        clock=BacktestClock(START),
        event_bus=EventBus(),
    ).run()
    assert multi["account"]["equity"] == original.final_equity
    assert multi["account"]["realized_pnl"] == original.summary["realized_pnl"]
    dimensions = ("timestamp", "side", "quantity", "fill_price", "fee", "exit_reason")
    assert [[fill[k] for k in dimensions] for fill in multi["fills"]] == [
        [asdict(fill)[k] for k in dimensions] for fill in original.fills
    ]


def test_interrupted_bar_cannot_publish_success_and_prior_checkpoint_recovers(
    btc_instrument: Instrument,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config()
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run")
    runner.advance(candle(0))
    checkpoint = tmp_path / "valid.json"
    runner.save_checkpoint(checkpoint)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected interruption after accounting")

    monkeypatch.setattr(runner, "assert_conservation", fail)
    with pytest.raises(OSError):
        runner.advance(candle(1))
    for operation in (
        runner.report,
        runner.finish,
        lambda: runner.save_checkpoint(tmp_path / "bad.json"),
    ):
        with pytest.raises(ValueError):
            operation()
    assert not (tmp_path / "bad.json").exists()
    restored = PortfolioBacktestEngine.restore(checkpoint, cfg, btc_instrument)
    restored.advance(candle(1))
    restored.assert_conservation(D("100"))


def test_immutable_checkpoint_does_not_overwrite_and_no_temp_leak(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    write_immutable_json(path, {"first": True})
    with pytest.raises(FileExistsError):
        write_immutable_json(path, {"second": True})
    assert json.loads(path.read_text()) == {"first": True}
    assert list(tmp_path.iterdir()) == [path]


def test_offline_demonstration_matches_resumed_run(tmp_path: Path) -> None:
    result = demonstrate(tmp_path / "demo")
    assert result["exact_resume_match"] is True
    assert result["account_conservation"] is True


def test_v3_instances_share_contract_and_restore_with_positions_and_orders(
    btc_instrument: Instrument,
    tmp_path: Path,
) -> None:
    from app.strategies.v3 import V3Strategy
    from backtest.strategy_v3_candidates import load_v3_specs

    variant = next(
        item
        for item in load_v3_specs(
            Path(__file__).parents[1] / "configs/research/strategy_v3_candidate_specs.json"
        )
        if item.variant_id == "volume_breakout_20"
    )
    data = config().model_dump(mode="json")
    data["market"] = {"instrument_id": "BTC-USDT", "bar": "1h"}
    for item in data["instances"]:
        item["strategy"] = {
            "name": variant.candidate_id,
            "parameters": variant.parameters,
            "implementation_version": V3Strategy.implementation_version,
        }
        item["order_notional"] = "20"
    cfg = PortfolioConfig.model_validate(data)
    bars = []
    for index in range(180):
        base = D("100") + D(index) / 20
        close = base + (D("4") if index % 17 == 0 else D("0.1"))
        bars.append(
            Candle(
                START + timedelta(hours=index),
                base,
                close + D("0.5"),
                base - D("0.5"),
                close,
                D("100") if index % 17 == 0 else D("10"),
                True,
            )
        )
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="v3", warmup_candles=bars[:100])
    checkpoint_index = None
    for index in range(100, 179):
        runner.advance(bars[index])
        if runner.broker.fills and runner.broker.portfolio.positions["BTC-USDT"] > 0:
            checkpoint_index = index
            break
    assert checkpoint_index is not None, "synthetic fixture must exercise a V3 position"
    checkpoint = tmp_path / "v3.json"
    runner.save_checkpoint(checkpoint)
    restored = PortfolioBacktestEngine.restore(checkpoint, cfg, btc_instrument)
    for bar in bars[checkpoint_index + 1 :]:
        runner.advance(bar)
        restored.advance(bar)
    assert canonical_hash(runner.finish()) == canonical_hash(restored.finish())
    assert all(fill.strategy_instance_id in {"alpha", "beta"} for fill in runner.broker.fills)


def test_future_mutation_cannot_change_past_portfolio_results(btc_instrument: Instrument) -> None:
    first = PortfolioBacktestEngine(config(), btc_instrument, run_id="run")
    second = PortfolioBacktestEngine(config(), btc_instrument, run_id="run")
    for index in range(3):
        first.advance(candle(index))
        second.advance(candle(index))
    prefix = canonical_hash(first.report())
    assert prefix == canonical_hash(second.report())
    first.advance(candle(3, "50"))
    second.advance(candle(3, "150"))
    assert first.curve[:3] == second.curve[:3]
    assert first.broker.fills[:2] == second.broker.fills[:2]


@pytest.mark.parametrize(
    "problem", ["duplicate_id", "overallocated", "unknown_strategy", "wrong_market"]
)
def test_configuration_rejects_ambiguous_or_unsupported_portfolios(problem: str) -> None:
    data = config().model_dump(mode="json")
    if problem == "duplicate_id":
        data["instances"][1]["instance_id"] = "alpha"
    elif problem == "overallocated":
        data["instances"][0]["allocation"] = "100"
    elif problem == "unknown_strategy":
        data["instances"][0]["strategy"]["name"] = "missing"
    else:
        data["market"]["instrument_id"] = "ETH-USDT"
    with pytest.raises(ValueError):
        PortfolioConfig.model_validate(data)


def test_unallocated_capital_is_explicit_and_not_lent(btc_instrument: Instrument) -> None:
    cfg = config(allocations=("20", "30"))
    report = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run").run([candle(0), candle(1)])
    assert report["account"]["unallocated_cash"] == D("50")
    assert report["account"]["cash"] >= D("50")
    assert (
        sum(s["equity"] for s in report["strategies"].values()) + D("50")
        == report["account"]["equity"]
    )


def test_sell_reservation_blocks_second_sell_until_cancelled(btc_instrument: Instrument) -> None:
    shared = broker(btc_instrument, fee="0")
    shared.begin_bar(candle(0))
    shared.submit_order(approve("alpha", "buy"))
    first = shared.submit_order(approve("alpha", "sell1", side=OrderSide.SELL, price="110"))
    assert first.state is OrderState.ACCEPTED
    assert (
        shared.submit_order(approve("alpha", "sell2", side=OrderSide.SELL)).state
        is OrderState.REJECTED
    )
    shared.cancel_order("BTC-USDT", "sell1")
    assert (
        shared.submit_order(approve("alpha", "sell3", side=OrderSide.SELL)).state
        is OrderState.FILLED
    )
    assert shared.available_quantity("alpha") == 0


def test_entry_fee_dust_kept_in_owned_book(btc_instrument: Instrument) -> None:
    tiny = replace(btc_instrument, minimum_notional=D("60"))
    report = PortfolioBacktestEngine(config(), tiny, run_id="run").run([candle(0), candle(1)])
    assert not report["fills"]
    assert report["account"]["cash"] == 100
    rejected = report["decisions"]
    assert any("minimum_order" in item["payload"].get("rejected_by", []) for item in rejected)


def test_checkpoint_write_failure_never_publishes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    def fail(source: object, target: object) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError):
        write_immutable_json(tmp_path / "checkpoint.json", {"value": "complete"})
    assert list(tmp_path.iterdir()) == []


def test_cli_uses_only_local_inputs_and_preserves_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import socket
    import sys

    from app.market.historical_data import save_candles_csv
    from scripts.run_portfolio_backtest import main

    def network_forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("offline portfolio must not access network")

    monkeypatch.setattr(socket.socket, "connect", network_forbidden)
    cfg = tmp_path / "config.json"
    cfg.write_text(config().model_dump_json(), encoding="utf-8")
    csv = tmp_path / "bars.csv"
    save_candles_csv([candle(i) for i in range(3)], csv)
    output = tmp_path / "output"
    argv = [
        "run_portfolio_backtest",
        "--config",
        str(cfg),
        "--candles",
        str(csv),
        "--instrument",
        str(Path(__file__).parents[1] / "configs/snapshots/BTC-USDT.json"),
        "--output",
        str(output),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()
    assert json.loads(capsys.readouterr().out)["account"]["cash"]
    before = (output / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        main()
    assert (output / "report.json").read_bytes() == before
    resumed_output = tmp_path / "resumed"
    monkeypatch.setattr(
        sys, "argv", [*argv[:-1], str(resumed_output), "--resume", str(output / "checkpoint.json")]
    )
    main()
    assert (resumed_output / "report.json").read_bytes() == before


@pytest.mark.parametrize("problem", ["duplicate", "gap", "unconfirmed", "naive_time"])
def test_portfolio_csv_reader_does_not_silently_repair_input(tmp_path: Path, problem: str) -> None:
    from app.market.historical_data import load_candles_csv, save_candles_csv

    path = tmp_path / "bars.csv"
    bars = [candle(0), candle(1)]
    if problem == "duplicate":
        bars.insert(1, candle(0))
    elif problem == "gap":
        bars[1] = candle(2)
    elif problem == "unconfirmed":
        bars[1] = replace(bars[1], confirmed=False)
    save_candles_csv(bars, path)
    if problem == "naive_time":
        path.write_text(path.read_text().replace("+00:00", ""))
    with pytest.raises(ValueError):
        load_candles_csv(path, bar="5m", strict=True)


def test_known_submission_failure_releases_new_reservation(
    btc_instrument: Instrument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = broker(btc_instrument)
    shared.begin_bar(candle(0))

    def fail(order: Order) -> bool:
        raise OSError("injected pre-fill failure")

    monkeypatch.setattr(shared, "_match", fail)
    with pytest.raises(OSError):
        shared.submit_order(approve("alpha", "failed"))
    assert shared.reserved_cash() == 0
    assert shared.portfolio.balances["USDT"] == 100
    assert shared.get_order("BTC-USDT", "failed").state is OrderState.CANCELLED


def test_mark_exposure_breach_is_visible_and_pending_entries_cancelled(
    btc_instrument: Instrument,
) -> None:
    cfg = config(fee="0")
    runner = PortfolioBacktestEngine(cfg, btc_instrument, run_id="run")
    runner.advance(candle(0))
    runner.advance(candle(1, volume="0.5"))
    assert runner.broker.get_open_orders()
    runner.advance(candle(2, "300"))
    assert not runner.broker.get_open_orders()
    account = runner.report()["account"]
    assert account["exposure_breached_by_mark"] is True
    assert account["quantity"] == D("0.5")


def test_partial_exit_dust_stays_owned_after_cancel(
    btc_instrument: Instrument,
    fixture_config: PortfolioConfig,
) -> None:
    runner = PortfolioBacktestEngine(fixture_config, btc_instrument, run_id="run")
    for index in range(3):
        runner.advance(candle(index))
    runner.advance(candle(3, volume="0.299"))
    order = runner.views["alpha"].get_open_orders()[0]
    runner.cancel_order("alpha", order.request.client_order_id)
    report = runner.finish()
    assert report["strategies"]["alpha"]["dust_quantity"] == D("0.001")
    assert report["account"]["quantity"] == D("0.301")


def test_checkpoint_cannot_overwrite_other_strategy_feedback(
    btc_instrument: Instrument,
    fixture_config: PortfolioConfig,
    tmp_path: Path,
) -> None:
    runner = PortfolioBacktestEngine(fixture_config, btc_instrument, run_id="run")
    for index in range(2):
        runner.advance(candle(index))
    path = tmp_path / "checkpoint.json"
    runner.save_checkpoint(path)
    restored = PortfolioBacktestEngine.restore(path, fixture_config, btc_instrument)
    for key in restored.engines:
        before = runner.engines[key].strategy
        after = restored.engines[key].strategy
        assert isinstance(before, FixtureStrategy) and isinstance(after, FixtureStrategy)
        assert before.feedback == after.feedback


def test_strategy_failure_stops_all_started_instances(
    btc_instrument: Instrument,
    fixture_config: PortfolioConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = PortfolioBacktestEngine(fixture_config, btc_instrument, run_id="run")
    stopped: list[str] = []
    for key, engine in runner.engines.items():
        monkeypatch.setattr(
            engine.strategy, "on_stop", lambda context, key=key: stopped.append(key)
        )

    def fail(context: StrategyContext, bar: Candle) -> list[Signal]:
        raise RuntimeError("strategy failure")

    monkeypatch.setattr(runner.engines["alpha"].strategy, "on_bar", fail)
    with pytest.raises(RuntimeError):
        runner.advance(candle(0))
    assert sorted(stopped) == ["alpha", "beta"]
    assert runner.failed
