from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.run_config import load_run_config
from app.domain.capability import MaxAvailableSize
from app.domain.market import InstrumentType, TradeMode
from app.domain.order import OrderSide, OrderType
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.services.bounded_demo import CrossSignalDetector
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    ProposalStatus,
)
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from tests.conftest import make_instrument


def test_acceptance_config_is_demo_only() -> None:
    config = load_run_config(Path("configs/btc_ma_demo_acceptance.yaml"), environ={})
    assert config.strategy.acceptance_only
    assert config.market.bar == "1m"
    assert config.strategy.parameters["fast_period"] == 3
    assert config.strategy.parameters["slow_period"] == 8


def test_acceptance_config_rejects_non_demo() -> None:
    with pytest.raises(ValueError, match="acceptance_only"):
        load_run_config(
            Path("configs/btc_ma_demo_acceptance.yaml"), environ={"TRADING_MODE": "backtest"}
        )


def test_buy_cross_is_single_transition() -> None:
    detector = CrossSignalDetector(3, 8)
    below = detector.relation([Decimal("10")] * 8)
    above = detector.relation([Decimal("10")] * 5 + [Decimal("20")] * 3)
    assert below == "fast_equal_slow"
    assert above == "fast_above_slow"
    assert detector.signal(below, above) == "buy_cross"
    assert detector.signal(above, above) is None


def test_sell_cross_is_single_transition() -> None:
    detector = CrossSignalDetector(3, 8)
    above = detector.relation([Decimal("10")] * 5 + [Decimal("20")] * 3)
    below = detector.relation([Decimal("20")] * 2 + [Decimal("10")] * 6)
    assert detector.signal(above, below) == "sell_cross"
    assert detector.signal(below, below) is None


def test_detector_rejects_insufficient_warmup() -> None:
    with pytest.raises(ValueError, match="insufficient"):
        CrossSignalDetector(3, 8).relation([Decimal("1")] * 7)


def test_sell_proposal_blocks_without_managed_inventory() -> None:
    now = datetime.now(UTC)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    proposal = DemoOrderPreflightService().prepare_order(
        intent=DemoOrderIntent(
            "run",
            "moving_average_cross",
            "BTC-USDT",
            InstrumentType.SPOT,
            TradeMode.CASH,
            OrderSide.SELL,
            OrderType.LIMIT,
            Decimal("5"),
            "continuous_demo",
            now,
        ),
        config=load_run_config(Path("configs/btc_ma_demo.yaml"), environ={}),
        instrument=instrument,
        portfolio=PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, now),
        ),
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("5"), Decimal("5"), now),
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("50000"),
        now=now,
        managed_quantity=Decimal("0"),
    )
    assert proposal.status is ProposalStatus.BLOCKED
    assert "no_strategy_managed_inventory" in proposal.blockers


def test_managed_fill_is_idempotent_and_sellable(tmp_path: Path) -> None:
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'inventory.db'}"))
    repository.database.initialize()
    repository.apply_managed_fill(
        strategy_name="moving_average_cross",
        run_id="r",
        instrument_id="BTC-USDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.0001"),
        price=Decimal("50000"),
    )
    repository.apply_managed_fill(
        strategy_name="moving_average_cross",
        run_id="r",
        instrument_id="BTC-USDT",
        side=OrderSide.SELL,
        quantity=Decimal("0.00004"),
        price=Decimal("50100"),
    )
    assert repository.managed_strategy_quantity("moving_average_cross", "r", "BTC-USDT") == Decimal(
        "0.00006"
    )


def test_proposal_linkage_is_persisted(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    instrument = make_instrument("BTC-USDT", "BTC", "USDT", "0.00001", "0.1")
    proposal = DemoOrderPreflightService().prepare_order(
        intent=DemoOrderIntent(
            "r",
            "moving_average_cross",
            "BTC-USDT",
            InstrumentType.SPOT,
            TradeMode.CASH,
            OrderSide.BUY,
            OrderType.LIMIT,
            Decimal("5"),
            "continuous_demo",
            now,
        ),
        config=load_run_config(Path("configs/btc_ma_demo_acceptance.yaml"), environ={}),
        instrument=instrument,
        portfolio=PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, now),
        ),
        max_size=MaxAvailableSize("BTC-USDT", TradeMode.CASH, Decimal("5"), Decimal("5"), now),
        derivative_positions={},
        open_order_count=0,
        reference_price=Decimal("50000"),
        now=now,
        signal_id="signal-1",
        candle_id="candle-1",
        acceptance_only=True,
    )
    repository = TradingRepository(Database(f"sqlite:///{tmp_path / 'linkage.db'}"))
    repository.database.initialize()
    repository.save_demo_order_proposal(proposal)
    loaded = repository.load_demo_order_proposal(proposal.proposal_id)
    assert loaded is not None
    assert (loaded.signal_id, loaded.candle_id, loaded.acceptance_only) == (
        "signal-1",
        "candle-1",
        True,
    )
