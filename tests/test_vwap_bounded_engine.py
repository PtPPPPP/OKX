from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from app.config.run_config import load_run_config
from app.domain.capability import MaxAvailableSize
from app.domain.market import Candle, Instrument, InstrumentStatus, InstrumentType, TradeMode
from app.domain.position import AccountConfiguration, AccountMode, PortfolioSnapshot
from app.market.websocket import OKXPublicWebSocketProvider
from app.services.bounded_demo import BoundedDemoConfiguration, BoundedDemoEngine
from app.services.demo_session import DemoSessionStart, DemoTradingSession
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.reconciliation import AccountSnapshot, ReconciliationStatus
from app.storage.database import Database
from app.strategies.registry import create_strategy


class FakeBroker:
    def __init__(self, history: list[Candle]) -> None:
        self.history = history
        self.place_calls = 0

    def get_history_candles(self, *_: object) -> list[Candle]:
        return self.history

    def get_max_available_size(self, instrument_id: str) -> MaxAvailableSize:
        return MaxAvailableSize(
            instrument_id, TradeMode.CASH, Decimal("1"), Decimal("1000"), datetime.now(UTC)
        )

    def get_derivative_positions(self) -> dict[str, Decimal]:
        return {}

    def get_ticker(self, _: str) -> tuple[Decimal, Decimal, Decimal, datetime]:
        return Decimal("98"), Decimal("98.1"), Decimal("98"), datetime.now(UTC)

    def place_order(self, _: object) -> object:
        self.place_calls += 1
        raise AssertionError("test proposal must not reach a real broker")


class FakeSession:
    def __init__(
        self, broker: FakeBroker, instrument: Instrument, account: AccountSnapshot
    ) -> None:
        self.client = broker
        self.start_snapshot = account
        self._start = DemoSessionStart(instrument, account, ReconciliationStatus.HEALTHY)

    @property
    def order_submission_ready(self) -> bool:
        return True

    def start(self) -> DemoSessionStart:
        return self._start

    def close(self) -> None:
        return None


class FakeStream:
    def __init__(self, candle: Candle) -> None:
        self.candle = candle

    async def stream_confirmed_candles(self, *_: object) -> AsyncIterator[Candle]:
        yield self.candle

    async def stop(self) -> None:
        return None


class BlockingStream:
    def __init__(self) -> None:
        self.stopped = False

    async def stream_confirmed_candles(self, *_: object) -> AsyncIterator[Candle]:
        await asyncio.Future()
        raise AssertionError("unreachable")
        yield _candle(datetime.now(UTC), "0")  # pragma: no cover

    async def stop(self) -> None:
        self.stopped = True


class FailingStopStream(BlockingStream):
    async def stop(self) -> None:
        raise RuntimeError("public websocket shutdown failed")


async def _no_sleep(_: float) -> None:
    return None


def _candle(timestamp: datetime, price: str) -> Candle:
    value = Decimal(price)
    return Candle(timestamp, value, value + 1, value - 1, value, Decimal("10"), True)


def test_vwap_bounded_engine_uses_registry_and_managed_inventory_only(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = [_candle(start + timedelta(minutes=index), "100") for index in range(24)]
    candidate = _candle(start + timedelta(minutes=24), "98")
    instrument = Instrument(
        "BTC-USDT",
        "BTC",
        "USDT",
        InstrumentType.SPOT,
        Decimal("0.1"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal("1"),
        InstrumentStatus.LIVE,
    )
    portfolio = PortfolioSnapshot(
        {"USDT": Decimal("1000"), "BTC": Decimal("9")},
        {"BTC-USDT": Decimal("9")},
        {"BTC-USDT": Decimal("99")},
        account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, start),
    )
    account = AccountSnapshot(portfolio, Decimal("100"), start, ())
    broker = FakeBroker(history)
    database = Database(f"sqlite:///{tmp_path / 'bounded.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing(
        "manifest", "database", {"test": True}, "test runtime"
    )
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_bounded_acceptance.yaml"), environ={})
    result = asyncio.run(
        BoundedDemoEngine(
            database,
            cast(DemoTradingSession, FakeSession(broker, instrument, account)),
            cast(OKXPublicWebSocketProvider, FakeStream(candidate)),
        ).run(
            BoundedDemoConfiguration(
                "BTC-USDT", "vwap_mean_reversion", "1m", maximum_runtime_minutes=1
            ),
            config,
        )
    )
    assert result.processed_candle_count == 1
    assert result.generated_signal_count == 1
    assert result.proposal_count == 1
    assert broker.place_calls == 0
    with database.connect() as connection:
        runtime = connection.execute(
            "SELECT strategy_name,state_json FROM strategy_runtime_states"
        ).fetchone()
        proposal = connection.execute(
            "SELECT strategy_name,side FROM demo_order_proposals"
        ).fetchone()
    assert runtime[0] == "vwap_mean_reversion" and '"entry_price"' in runtime[1]
    assert tuple(proposal) == ("vwap_mean_reversion", "buy")


def test_moving_average_bounded_engine_uses_registered_strategy(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = [_candle(start + timedelta(minutes=index), "100") for index in range(35)]
    candidate = _candle(start + timedelta(minutes=35), "110")
    instrument = Instrument(
        "BTC-USDT",
        "BTC",
        "USDT",
        InstrumentType.SPOT,
        Decimal("0.1"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal("1"),
        InstrumentStatus.LIVE,
    )
    portfolio = PortfolioSnapshot(
        {"USDT": Decimal("1000"), "BTC": Decimal("0")},
        {"BTC-USDT": Decimal("0")},
        {},
        account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, start),
    )
    account = AccountSnapshot(portfolio, Decimal("100"), start, ())
    broker = FakeBroker(history)
    database = Database(f"sqlite:///{tmp_path / 'bounded-ma.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing(
        "manifest", "database", {"test": True}, "test runtime"
    )
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_ma_demo_acceptance.yaml"), environ={})
    with patch("app.services.bounded_demo.create_strategy", wraps=create_strategy) as factory:
        result = asyncio.run(
            BoundedDemoEngine(
                database,
                cast(DemoTradingSession, FakeSession(broker, instrument, account)),
                cast(OKXPublicWebSocketProvider, FakeStream(candidate)),
            ).run(
                BoundedDemoConfiguration(
                    "BTC-USDT",
                    "moving_average_cross",
                    "1m",
                    maximum_runtime_minutes=1,
                    maximum_confirmed_decision_candles=1,
                ),
                config,
            )
        )
    assert result.processed_candle_count == 1
    assert result.generated_signal_count == 1
    assert result.proposal_count == 1
    assert factory.call_args.args[0] == "moving_average_cross"


def test_bounded_engine_deadline_stops_when_no_confirmed_candle_arrives(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = [_candle(start + timedelta(minutes=index), "100") for index in range(24)]
    instrument = Instrument(
        "BTC-USDT",
        "BTC",
        "USDT",
        InstrumentType.SPOT,
        Decimal("0.1"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal("1"),
        InstrumentStatus.LIVE,
    )
    account = AccountSnapshot(
        PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, start),
        ),
        Decimal("100"),
        start,
        (),
    )
    database = Database(f"sqlite:///{tmp_path / 'bounded-deadline.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing(
        "manifest", "database", {"test": True}, "test runtime"
    )
    generation.activate(generation_id)
    stream = BlockingStream()
    config = load_run_config(Path("configs/btc_vwap_bounded_acceptance.yaml"), environ={})

    with patch("app.services.bounded_demo.asyncio.sleep", new=_no_sleep):
        result = asyncio.run(
            BoundedDemoEngine(
                database,
                cast(DemoTradingSession, FakeSession(FakeBroker(history), instrument, account)),
                cast(OKXPublicWebSocketProvider, stream),
            ).run(
                BoundedDemoConfiguration(
                    "BTC-USDT",
                    "vwap_mean_reversion",
                    "1m",
                    maximum_runtime_seconds=1,
                ),
                config,
            )
        )

    assert result.processed_candle_count == 0
    assert result.submitted_order_count == 0
    assert stream.stopped
    with database.connect() as connection:
        event = connection.execute(
            "SELECT details_json FROM continuous_demo_run_events "
            "WHERE run_id=? AND event_type='bounded_demo_shutdown'",
            (result.run_id,),
        ).fetchone()
    assert event is not None
    details = json.loads(event[0])
    assert details["runtime_deadline_reached"] is True
    assert details["environment"] == "OKX_DEMO"
    assert details["signals_buy"] == 0
    assert details["signals_hold"] == 0
    assert details["risk_reviews_attempted"] == 0
    assert details["budget_reservation_attempts"] == 0
    assert details["orders_created"] == 0
    assert details["fills_created"] == 0
    assert details["bootstrap_confirmed_candle_count"] == 24
    assert details["confirmed_candles_processed"] == 0
    assert details["broker_write_calls"] == 0
    assert details["place_order_calls"] == 0
    assert details["cancel_order_calls"] == 0
    assert details["run_finalized"] is True
    assert details["lock_released"] is True
    assert details["clean_shutdown"] is True
    assert details["external_process_kill"] is False


def test_bounded_engine_releases_lock_when_public_websocket_shutdown_fails(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = [_candle(start + timedelta(minutes=index), "100") for index in range(24)]
    instrument = Instrument(
        "BTC-USDT",
        "BTC",
        "USDT",
        InstrumentType.SPOT,
        Decimal("0.1"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal("1"),
        InstrumentStatus.LIVE,
    )
    account = AccountSnapshot(
        PortfolioSnapshot(
            {},
            {},
            {},
            account_configuration=AccountConfiguration(AccountMode.SPOT, None, False, None, start),
        ),
        Decimal("100"),
        start,
        (),
    )
    database = Database(f"sqlite:///{tmp_path / 'bounded-stop-failure.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing(
        "manifest", "database", {"test": True}, "test runtime"
    )
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_bounded_acceptance.yaml"), environ={})

    with (
        patch("app.services.bounded_demo.asyncio.sleep", new=_no_sleep),
        pytest.raises(RuntimeError, match="public websocket shutdown failed"),
    ):
        asyncio.run(
            BoundedDemoEngine(
                database,
                cast(DemoTradingSession, FakeSession(FakeBroker(history), instrument, account)),
                cast(OKXPublicWebSocketProvider, FailingStopStream()),
            ).run(
                BoundedDemoConfiguration(
                    "BTC-USDT", "vwap_mean_reversion", "1m", maximum_runtime_seconds=1
                ),
                config,
            )
        )

    with database.connect() as connection:
        active_locks = connection.execute(
            "SELECT COUNT(*) FROM continuous_run_locks WHERE released_at IS NULL"
        ).fetchone()[0]
        event = connection.execute(
            "SELECT details_json FROM continuous_demo_run_events "
            "WHERE event_type='bounded_demo_shutdown'"
        ).fetchone()
    assert active_locks == 0
    assert event is not None
    assert json.loads(event[0])["clean_shutdown"] is False


@pytest.mark.parametrize(
    "configuration",
    [
        BoundedDemoConfiguration(
            "BTC-USDT", "moving_average_cross", "1m", maximum_confirmed_decision_candles=7
        ),
        BoundedDemoConfiguration(
            "BTC-USDT", "moving_average_cross", "1m", maximum_managed_exposure=Decimal("6")
        ),
    ],
)
def test_bounded_engine_rejects_limits_above_authorized_bounds(
    tmp_path: Path, configuration: BoundedDemoConfiguration
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'bounded-limits.db'}")
    database.initialize()
    config = load_run_config(Path("configs/btc_ma_demo_acceptance.yaml"), environ={})
    with pytest.raises(ValueError, match="bounded demo limits exceeded"):
        asyncio.run(
            BoundedDemoEngine(
                database,
                cast(DemoTradingSession, object()),
                cast(OKXPublicWebSocketProvider, object()),
            ).run(configuration, config)
        )
