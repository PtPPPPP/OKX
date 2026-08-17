from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import Settings, TradingMode
from app.domain.events import EventBus
from app.domain.position import Portfolio, PortfolioSnapshot
from app.exchange.okx_client import OkxClient
from app.execution.backtest_broker import BacktestBroker
from app.execution.read_only_broker import ReadOnlyBroker
from app.market.providers import (
    CSVMarketDataProvider,
    MarketDataProvider,
    OKXHistoricalDataProvider,
)
from app.market.websocket import OKXPublicWebSocketProvider
from app.position_sizing.fixed_notional import FixedNotionalPositionSizer
from app.reproducibility import (
    InstrumentSnapshotStore,
    RecordingMarketDataProvider,
    build_run_manifest,
    build_run_start_manifest,
    write_run_manifest,
)
from app.risk.risk_manager import default_risk_manager
from app.runners.demo import (
    DemoEvaluationRunner,
    PublicObserveRunner,
    StaticPortfolioSource,
)
from app.runtime.clock import BacktestClock, SystemClock
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.reconciliation import ReconciliationStatus
from app.session import SessionDescriptor, TradingSession
from app.storage.database import Database
from app.storage.repositories import TradingRepository
from app.strategies.registry import create_strategy
from app.trading_engine import ProtectiveExitPolicy, TradingEngine
from backtest.engine import BacktestEngine


def build_backtest_session(config: RunConfig, settings: Settings) -> TradingSession:
    if config.mode is not TradingMode.BACKTEST:
        raise ValueError("回测 Session 要求 mode=backtest")
    client = OkxClient(settings)
    try:
        instrument_snapshot = InstrumentSnapshotStore().resolve(
            config.market.instrument_id,
            configured_path=config.data.instrument_snapshot,
            fetch=client.get_instrument,
        )
        instrument = instrument_snapshot.instrument
        if instrument.instrument_type is not config.market.instrument_type:
            raise ValueError("配置市场类型与交易所规则不一致")
        provider: MarketDataProvider
        source_provider: MarketDataProvider
        if config.data.source == "csv":
            if config.data.path is None:
                raise ValueError("CSV 数据源缺少 path")
            source_provider = CSVMarketDataProvider(config.data.path)
        else:
            source_provider = OKXHistoricalDataProvider(client)
        provider = RecordingMarketDataProvider(source_provider)
        strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
        order_notional = Decimal(str(config.position_sizing.parameters["order_notional"]))
        position_sizer = FixedNotionalPositionSizer(order_notional)
        risk_manager = default_risk_manager(
            maximum_order_notional=config.risk.max_order_notional,
            maximum_exposure=config.risk.max_total_exposure,
            maximum_daily_loss=config.risk.max_daily_loss,
            maximum_drawdown_pct=config.risk.max_drawdown_pct,
            maximum_orders_per_minute=config.risk.max_orders_per_minute,
            stale_after_seconds=config.risk.stale_after_seconds,
        )
        portfolio = Portfolio(
            balances={instrument.quote_currency: config.backtest.initial_capital},
            positions={instrument.instrument_id: Decimal("0")},
        )
        broker = BacktestBroker(
            portfolio,
            instrument,
            config.backtest.fee_rate,
            config.backtest.slippage_rate,
        )
        database = Database(settings.database_url)
        database.initialize()
        repository = TradingRepository(database)
        event_bus = EventBus()
        clock = BacktestClock(datetime.min.replace(tzinfo=UTC))
        run_id = uuid4().hex
        run_started_at = datetime.now(UTC)
        start_manifest = build_run_start_manifest(
            run_id=run_id,
            config=config,
            instrument_snapshot=instrument_snapshot,
            run_started_at=run_started_at,
        )
        engine = BacktestEngine(
            run_id=run_id,
            config=config,
            instrument=instrument,
            provider=provider,
            strategy=strategy,
            position_sizer=position_sizer,
            risk_manager=risk_manager,
            broker=broker,
            clock=clock,
            event_bus=event_bus,
        )

        def record_result(result: object) -> None:
            from backtest.engine import BacktestResult

            if not isinstance(result, BacktestResult):
                raise TypeError("回测 Runner 返回了无效结果")
            manifest = build_run_manifest(
                run_id=result.run_id,
                config=config,
                instrument_snapshot=instrument_snapshot,
                provider=provider,
                data_started_at=result.started_at,
                data_completed_at=result.completed_at,
                run_started_at=run_started_at,
                start_manifest=start_manifest,
            )
            write_run_manifest(manifest, config.output.directory / result.run_id)
            repository.save_instrument_snapshot(instrument_snapshot.to_dict())
            repository.save_dataset_snapshot(manifest)
            repository.save_run_manifest(manifest, status="completed")
            repository.save_backtest_run(result.run_id, result.summary)
            dimensions = {
                "run_id": result.run_id,
                "mode": config.mode.value,
                "strategy_name": config.strategy.name,
                "instrument_id": instrument.instrument_id,
                "bar": config.market.bar,
            }
            repository.save_audit_records(
                [
                    {
                        "record_type": "reproducibility_manifest",
                        "payload": manifest,
                        **dimensions,
                    },
                    *[
                        {"record_type": "fill", "payload": asdict(fill), **dimensions}
                        for fill in result.fills
                    ],
                    *[
                        {
                            "record_type": "equity_point",
                            "payload": asdict(point),
                            **dimensions,
                        }
                        for point in result.equity_curve
                    ],
                    {
                        "record_type": "backtest_result",
                        "payload": result.summary,
                        **dimensions,
                    },
                ]
            )

        def record_start() -> None:
            repository.save_instrument_snapshot(instrument_snapshot.to_dict())
            repository.save_run_manifest(start_manifest, status="running")

        def record_failure(_error: BaseException) -> None:
            failed_manifest = {
                **start_manifest,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            repository.save_run_manifest(failed_manifest, status="failed")

        return TradingSession(
            descriptor=SessionDescriptor(
                mode=config.mode.value,
                strategy_name=config.strategy.name,
                instrument_id=instrument.instrument_id,
                bar=config.market.bar,
                config_snapshot=config.model_dump(mode="json"),
            ),
            instrument=instrument,
            runner=engine,
            repository=repository,
            event_bus=event_bus,
            resources=(client,),
            result_handler=record_result,
            before_run=record_start,
            failure_handler=record_failure,
        )
    except Exception:
        client.close()
        raise


def build_demo_evaluation_session(config: RunConfig, settings: Settings) -> TradingSession:
    if config.mode is not TradingMode.DEMO:
        raise ValueError("模拟观察 Session 要求 mode=demo")
    settings.require_demo_credentials()
    client = OkxClient(settings)
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        database = Database(settings.database_url)
        database.initialize()
        repository = TradingRepository(database)
        run_id = uuid4().hex
        coordinator = PrivateStateCoordinator.for_private_account(client, repository, SystemClock())
        sync = coordinator.synchronize_private_account(
            instrument,
            config.market.bar,
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            source="demo_evaluation_startup",
        )

        def require_healthy_reconciliation() -> None:
            result = coordinator.reconcile_private_state(instrument, source="demo_evaluation")
            if result.status is not ReconciliationStatus.HEALTHY:
                raise ValueError(f"模拟盘对账未通过: {result.message}")

        candles = client.get_history_candles(
            instrument.instrument_id, config.market.bar, config.data.limit
        )
        confirmed = [candle for candle in candles if candle.confirmed]
        if not confirmed:
            raise ValueError("无法获得已确认收盘 K 线")
        current_equity = (
            sync.portfolio.cash_balance(instrument.quote_currency) or Decimal("0")
        ) + sync.portfolio.position(instrument.instrument_id) * sync.mark_price
        daily_pnl, drawdown = repository.daily_risk_metrics(
            instrument.instrument_id, sync.captured_at, current_equity
        )
        strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
        position_sizer = FixedNotionalPositionSizer(
            Decimal(str(config.position_sizing.parameters["order_notional"]))
        )
        risk_manager = default_risk_manager(
            maximum_order_notional=config.risk.max_order_notional,
            maximum_exposure=config.risk.max_total_exposure,
            maximum_daily_loss=config.risk.max_daily_loss,
            maximum_drawdown_pct=config.risk.max_drawdown_pct,
            maximum_orders_per_minute=config.risk.max_orders_per_minute,
            stale_after_seconds=config.risk.stale_after_seconds,
        )
        clock = BacktestClock(confirmed[0].timestamp)
        portfolio_source = StaticPortfolioSource(sync.portfolio)
        event_bus = EventBus(repository)
        engine = TradingEngine(
            run_id=run_id,
            mode=TradingMode.DEMO,
            bar=config.market.bar,
            instrument=instrument,
            strategy=strategy,
            position_sizer=position_sizer,
            risk_manager=risk_manager,
            broker=ReadOnlyBroker(sync.open_orders),
            portfolio=portfolio_source,
            clock=clock,
            event_bus=event_bus,
            protective_exits=ProtectiveExitPolicy(
                enabled=config.protective_exits.enabled,
                stop_loss_pct=config.protective_exits.stop_loss_pct,
                take_profit_pct=config.protective_exits.take_profit_pct,
            ),
            repository=repository,
        )
        runner = DemoEvaluationRunner(
            run_id=run_id,
            bar=config.market.bar,
            candles=confirmed,
            engine=engine,
            clock=clock,
            daily_pnl=daily_pnl,
            drawdown_pct=drawdown,
        )
        return TradingSession(
            descriptor=SessionDescriptor(
                mode=config.mode.value,
                strategy_name=config.strategy.name,
                instrument_id=instrument.instrument_id,
                bar=config.market.bar,
                config_snapshot=config.model_dump(mode="json"),
            ),
            instrument=instrument,
            runner=runner,
            repository=repository,
            event_bus=event_bus,
            resources=(client,),
            before_run=require_healthy_reconciliation,
            after_run=require_healthy_reconciliation,
        )
    except Exception:
        client.close()
        raise


def build_public_observe_session(
    config: RunConfig,
    settings: Settings,
    *,
    max_events: int,
    timeout_seconds: float,
) -> TradingSession:
    if config.mode is not TradingMode.DEMO:
        raise ValueError("公共观察 Session 要求 mode=demo")
    client = OkxClient(settings)
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        database = Database(settings.database_url)
        database.initialize()
        repository = TradingRepository(database)
        run_id = uuid4().hex
        strategy = create_strategy(config.strategy.name, config.strategy.parameters, instrument)
        clock = BacktestClock(datetime.now(UTC))
        portfolio_source = StaticPortfolioSource(
            PortfolioSnapshot(
                balances={
                    instrument.base_currency: Decimal("0"),
                    instrument.quote_currency: Decimal("0"),
                },
                positions={instrument.instrument_id: Decimal("0")},
                average_entry_prices={},
            )
        )
        event_bus = EventBus(repository)
        websocket_provider = OKXPublicWebSocketProvider(
            gap_provider=OKXHistoricalDataProvider(client),
            stale_after_seconds=config.risk.stale_after_seconds,
        )
        engine = TradingEngine(
            run_id=run_id,
            mode=TradingMode.DEMO,
            bar=config.market.bar,
            instrument=instrument,
            strategy=strategy,
            position_sizer=FixedNotionalPositionSizer(
                Decimal(str(config.position_sizing.parameters["order_notional"]))
            ),
            risk_manager=default_risk_manager(
                maximum_order_notional=config.risk.max_order_notional,
                maximum_exposure=config.risk.max_total_exposure,
                maximum_daily_loss=config.risk.max_daily_loss,
                maximum_drawdown_pct=config.risk.max_drawdown_pct,
                maximum_orders_per_minute=config.risk.max_orders_per_minute,
                stale_after_seconds=config.risk.stale_after_seconds,
            ),
            broker=ReadOnlyBroker(),
            portfolio=portfolio_source,
            clock=clock,
            event_bus=event_bus,
            protective_exits=ProtectiveExitPolicy(
                enabled=config.protective_exits.enabled,
                stop_loss_pct=config.protective_exits.stop_loss_pct,
                take_profit_pct=config.protective_exits.take_profit_pct,
            ),
            repository=repository,
        )
        runner = PublicObserveRunner(
            run_id=run_id,
            instrument_id=instrument.instrument_id,
            bar=config.market.bar,
            engine=engine,
            clock=clock,
            provider=websocket_provider,
            max_events=max_events,
            timeout_seconds=timeout_seconds,
        )
        return TradingSession(
            descriptor=SessionDescriptor(
                mode=config.mode.value,
                strategy_name=config.strategy.name,
                instrument_id=instrument.instrument_id,
                bar=config.market.bar,
                config_snapshot=config.model_dump(mode="json"),
            ),
            instrument=instrument,
            runner=runner,
            repository=repository,
            event_bus=event_bus,
            resources=(client,),
        )
    except Exception:
        client.close()
        raise
