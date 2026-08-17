from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.config.run_config import RunConfig, load_run_config
from app.config.settings import TradingMode
from app.domain.context import MarketSnapshot, StrategyContext
from app.domain.market import Candle, Instrument
from app.domain.position import PortfolioSnapshot
from app.domain.signal import Signal, SignalAction
from app.reproducibility import InstrumentSnapshotStore
from app.runtime.clock import BacktestClock
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.vwap_continuous_shadow import ContinuousVWAPShadowRunner
from app.storage.database import Database
from app.strategies.vwap_shadow import VWAPShadowParameters, VWAPShadowStrategy


def _candle(timestamp: datetime, price: str, *, confirmed: bool = True) -> Candle:
    value = Decimal(price)
    return Candle(timestamp, value, value + 1, value - 1, value, Decimal("10"), confirmed)


class _PublicHistory:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls = 0
        self.limits: list[int | None] = []

    def get_historical_bars(
        self, *_: object, limit: int | None = None, **__: object
    ) -> list[Candle]:
        self.calls += 1
        self.limits.append(limit)
        return self.candles[-limit:] if limit is not None else self.candles


async def _feed(candles: list[Candle]) -> AsyncIterator[Candle]:
    for candle in candles:
        yield candle


def _shadow_signal_after_bootstrap(
    instrument: Instrument, bootstrap: list[Candle], live: Candle
) -> Signal:
    strategy = VWAPShadowStrategy(
        VWAPShadowParameters(vwap_window=24, buy_deviation_bps=Decimal("100"))
    )
    context = StrategyContext(
        run_id="bootstrap-regression",
        mode=TradingMode.BACKTEST,
        strategy_name="vwap_shadow",
        instrument=instrument,
        bar="1h",
        portfolio_snapshot=PortfolioSnapshot({}, {}, {}, trusted_for_trading=False),
        market_snapshot=MarketSnapshot(live, live.close),
        clock=BacktestClock(live.timestamp + timedelta(hours=1)),
    )
    strategy.on_start(context)
    for candle in bootstrap:
        strategy.on_bar(context, candle)
    return strategy.on_bar(context, live)[0]


def _continuous_config_and_instrument() -> tuple[RunConfig, Instrument]:
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    return config, instrument


def test_bootstrap_requests_one_extra_raw_bar_for_an_unconfirmed_current_candle(
    tmp_path: Path,
) -> None:
    config, instrument = _continuous_config_and_instrument()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    required = 29
    confirmed = [_candle(start + timedelta(hours=index), "100") for index in range(required)]
    current = _candle(start + timedelta(hours=required), "99", confirmed=False)
    history = _PublicHistory([*confirmed, current])
    runner = ContinuousVWAPShadowRunner(
        Database(f"sqlite:///{tmp_path / 'bootstrap-unconfirmed.db'}"), config, instrument, history
    )

    bootstrap = runner._bootstrap(runner._settings())

    assert history.limits == [required + 1]
    assert bootstrap == confirmed
    assert len(bootstrap) == required
    assert all(candle.confirmed for candle in bootstrap)


def test_bootstrap_preserves_latest_required_confirmed_bars_when_every_bar_is_confirmed(
    tmp_path: Path,
) -> None:
    config, instrument = _continuous_config_and_instrument()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    confirmed = [_candle(start + timedelta(hours=index), str(100 + index)) for index in range(30)]
    history = _PublicHistory(confirmed)
    runner = ContinuousVWAPShadowRunner(
        Database(f"sqlite:///{tmp_path / 'bootstrap-confirmed.db'}"), config, instrument, history
    )

    bootstrap = runner._bootstrap(runner._settings())

    assert history.limits == [30]
    assert bootstrap == confirmed[-29:]
    assert bootstrap[-1] == confirmed[-1]


def test_extra_raw_bootstrap_bar_preserves_fixed_fixture_vwap_signal_and_proposal_eligibility(
    tmp_path: Path,
) -> None:
    config, instrument = _continuous_config_and_instrument()
    assert config.strategy.parameters == {"vwap_window": 24, "buy_deviation_bps": 100}
    start = datetime(2026, 1, 1, tzinfo=UTC)
    confirmed = [
        _candle(start + timedelta(hours=index), str(100 + (index % 3))) for index in range(30)
    ]
    required = 29
    old_bootstrap = confirmed[-required:]
    runner = ContinuousVWAPShadowRunner(
        Database(f"sqlite:///{tmp_path / 'bootstrap-regression.db'}"),
        config,
        instrument,
        _PublicHistory(confirmed),
    )
    new_bootstrap = runner._bootstrap(runner._settings())

    for live_price, expected_action in (("97", SignalAction.BUY), ("101", SignalAction.HOLD)):
        live = _candle(start + timedelta(hours=30), live_price)
        old_signal = _shadow_signal_after_bootstrap(instrument, old_bootstrap, live)
        new_signal = _shadow_signal_after_bootstrap(instrument, new_bootstrap, live)

        assert new_signal.metadata["vwap"] == old_signal.metadata["vwap"]
        assert new_signal.metadata["deviation_bps"] == old_signal.metadata["deviation_bps"]
        assert new_signal.action is old_signal.action is expected_action
        assert (new_signal.action is SignalAction.BUY) == (old_signal.action is SignalAction.BUY)


def test_continuous_vwap_shadow_bootstraps_without_historical_events_and_deduplicates(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bootstrap = [_candle(start + timedelta(hours=index), "100") for index in range(29)]
    live = [
        _candle(start + timedelta(hours=29), "98", confirmed=False),
        _candle(start + timedelta(hours=29), "98"),
        _candle(start + timedelta(hours=29), "98"),
    ]
    database = Database(f"sqlite:///{tmp_path / 'continuous.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument
    history = _PublicHistory(bootstrap)

    result = asyncio.run(
        ContinuousVWAPShadowRunner(database, config, instrument, history).run(
            _feed(live), maximum_confirmed_bars=1
        )
    )

    assert result.bootstrap_bars == 29
    assert result.confirmed_bars_processed == 1
    assert result.buy_signals == result.proposals == 1
    assert result.duplicates == 0
    assert history.limits[0] == 30
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM strategy_signal_events").fetchone()[0] == 1
        proposal = connection.execute(
            "SELECT quantity,notional,submission_performed,exchange_order_id,capability_status,risk_status FROM shadow_order_proposals"
        ).fetchone()
        assert tuple(proposal) == ("0", "0", 0, None, "read_only", "blocked")
        assert connection.execute("SELECT COUNT(*) FROM demo_order_proposals").fetchone()[0] == 0


def test_continuous_vwap_shadow_reconciles_a_public_gap_before_processing(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [_candle(start + timedelta(hours=index), "100") for index in range(32)]
    database = Database(f"sqlite:///{tmp_path / 'gap.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing("manifest", "database", {"test": True}, "test")
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})
    assert config.data.instrument_snapshot is not None
    instrument = InstrumentSnapshotStore.load(config.data.instrument_snapshot).instrument

    class GapHistory(_PublicHistory):
        def get_historical_bars(
            self, *_: object, limit: int | None = None, **__: object
        ) -> list[Candle]:
            self.calls += 1
            source = self.candles[:29] if self.calls == 1 else self.candles
            return source[-limit:] if limit is not None else source

    result = asyncio.run(
        ContinuousVWAPShadowRunner(database, config, instrument, GapHistory(candles)).run(
            _feed([candles[31]]), maximum_confirmed_bars=3
        )
    )

    assert result.gaps == 1
    assert result.confirmed_bars_processed == 3
