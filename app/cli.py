from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from app.bootstrap import (
    build_backtest_session,
    build_demo_evaluation_session,
    build_public_observe_session,
)
from app.config.run_config import RunConfig, load_run_config
from app.config.settings import Settings, TradingMode
from app.domain.market import Instrument
from app.domain.order import Order
from app.exchange.exceptions import ExchangeError
from app.exchange.okx_client import OkxClient
from app.market.historical_data import load_candles_csv, save_candles_csv
from app.monitoring.alerts import WeComAlertNotifier
from app.monitoring.logging_config import configure_logging
from app.runtime.clock import SystemClock
from app.services.demo_orders import DemoOrderService
from app.services.demo_session import DemoTradingSession
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.storage.database import Database, StorageError
from app.storage.repositories import TradingRepository
from app.strategies.registry import strategy_descriptions
from backtest.engine import BacktestResult
from backtest.report import write_backtest_report

app = typer.Typer(
    no_args_is_help=True,
    help="OKX 现货量化回测、模拟交易与双重开关受控实盘 CLI",
)
logger = logging.getLogger(__name__)


def _fail(message: str) -> NoReturn:
    typer.echo(json.dumps({"error": message}, ensure_ascii=False), err=True)
    raise typer.Exit(code=1)


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def _load(
    command: str,
    config_path: Path | None,
    *,
    mode: TradingMode | None = None,
    instrument: str | None = None,
    bar: str | None = None,
) -> tuple[RunConfig, Settings]:
    overrides: dict[str, Any] = {}
    if mode is not None:
        overrides["mode"] = mode.value
    if instrument is not None:
        overrides["instrument_id"] = instrument
    if bar is not None:
        overrides["bar"] = bar
    try:
        config = load_run_config(config_path, cli_overrides=overrides or None)
        settings = Settings(trading_mode=config.mode)
        configure_logging(
            settings.log_level,
            redact_values=(
                settings.okx_api_key.get_secret_value(),
                settings.okx_secret_key.get_secret_value(),
                settings.okx_passphrase.get_secret_value(),
            ),
        )
        logger.info(
            "命令启动",
            extra={
                "command": command,
                "trading_mode": config.mode.value,
                "instrument_id": config.market.instrument_id,
                "strategy": config.strategy.name,
            },
        )
        return config, settings
    except ValidationError as exc:
        _fail(_validation_message(exc))
    except (ValueError, StorageError) as exc:
        _fail(str(exc))


def _repository(settings: Settings) -> TradingRepository:
    database = Database(settings.database_url)
    database.initialize()
    return TradingRepository(database)


def _demo(
    command: str,
    config_path: Path | None,
) -> tuple[RunConfig, Settings, TradingRepository, OkxClient]:
    config, settings = _load(command, config_path, mode=TradingMode.DEMO)
    try:
        settings.require_demo_credentials()
    except ValueError as exc:
        _fail(str(exc))
    repository = _repository(settings)
    return config, settings, repository, OkxClient(settings)


def _instrument_dict(instrument: Instrument) -> dict[str, Any]:
    return {
        "instrument_id": instrument.instrument_id,
        "base_currency": instrument.base_currency,
        "quote_currency": instrument.quote_currency,
        "instrument_type": instrument.instrument_type.value,
        "price_tick": str(instrument.price_tick),
        "quantity_step": str(instrument.quantity_step),
        "minimum_quantity": str(instrument.minimum_quantity),
        "minimum_notional": str(instrument.minimum_notional),
        "status": instrument.status.value,
    }


def _order_summary(order: Order) -> dict[str, object]:
    return {
        "client_order_id": order.request.client_order_id,
        "exchange_order_id": order.exchange_order_id,
        "state": order.state.value,
        "filled_quantity": str(order.filled_quantity),
        "average_price": str(order.average_price) if order.average_price is not None else None,
    }


@app.command("show-config")
def show_config(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    config, settings = _load("show-config", config_path)
    payload = {
        "config": config.model_dump(mode="json"),
        "settings": settings.safe_dict(),
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@app.command("validate-config")
def validate_config(
    config_path: Annotated[Path, typer.Option("--config")],
) -> None:
    config, _ = _load("validate-config", config_path)
    typer.echo(
        json.dumps(
            {
                "valid": True,
                "mode": config.mode.value,
                "strategy": config.strategy.name,
                "instrument": config.market.instrument_id,
                "bar": config.market.bar,
            },
            ensure_ascii=False,
        )
    )


@app.command("list-strategies")
def list_strategies() -> None:
    typer.echo(json.dumps(strategy_descriptions(), ensure_ascii=False, indent=2))


@app.command("describe-strategy")
def describe_strategy(name: Annotated[str, typer.Argument()]) -> None:
    for item in strategy_descriptions():
        if item["name"] == name:
            typer.echo(json.dumps(item, ensure_ascii=False, indent=2))
            return
    _fail(f"未注册策略: {name}")


@app.command("inspect-instrument")
def inspect_instrument(
    instrument_id: Annotated[str, typer.Argument()],
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    _, settings = _load("inspect-instrument", config_path)
    client = OkxClient(settings)
    try:
        instrument = client.get_instrument(instrument_id)
    except ExchangeError as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(json.dumps(_instrument_dict(instrument), ensure_ascii=False, indent=2))


@app.command("download-data")
def download_data(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    instrument: Annotated[str | None, typer.Option("--instrument")] = None,
    bar: Annotated[str | None, typer.Option("--bar")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    config, settings = _load("download-data", config_path, instrument=instrument, bar=bar)
    target_limit = limit if limit is not None else config.data.limit
    target_output = output if output is not None else config.data.output
    client = OkxClient(settings)
    try:
        candles = client.get_history_candles(
            config.market.instrument_id,
            config.market.bar,
            target_limit,
        )
    except ExchangeError as exc:
        _fail(str(exc))
    finally:
        client.close()
    save_candles_csv(candles, target_output)
    reloaded = load_candles_csv(target_output, bar=config.market.bar)
    typer.echo(
        json.dumps(
            {
                "instrument_id": config.market.instrument_id,
                "bar": config.market.bar,
                "count": len(reloaded),
                "output": str(target_output),
                "started_at": reloaded[0].timestamp.isoformat() if reloaded else None,
                "completed_at": reloaded[-1].timestamp.isoformat() if reloaded else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("backtest")
def backtest(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    instrument: Annotated[str | None, typer.Option("--instrument")] = None,
    bar: Annotated[str | None, typer.Option("--bar")] = None,
) -> None:
    config, settings = _load(
        "backtest",
        config_path,
        mode=TradingMode.BACKTEST,
        instrument=instrument,
        bar=bar,
    )
    session = build_backtest_session(config, settings)
    result = session.run()
    if not isinstance(result, BacktestResult):
        raise TypeError("回测会话未返回 BacktestResult")
    output_dir = config.output.directory / result.run_id
    write_backtest_report(result, output_dir)
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "strategy": config.strategy.name,
                "instrument": config.market.instrument_id,
                "bar": config.market.bar,
                "output": str(output_dir),
                "summary": result.summary,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@app.command("portfolio-backtest")
def portfolio_backtest(
    config_path: Annotated[Path, typer.Option("--config")],
) -> None:
    from app.domain.market import InstrumentStatus, InstrumentType
    from app.reproducibility import InstrumentSnapshotStore
    from backtest.multi_asset import MultiAssetBacktestEngine

    config, settings = _load("portfolio-backtest", config_path, mode=TradingMode.BACKTEST)
    target_instruments = config.market.effective_instruments
    store = InstrumentSnapshotStore()
    client = OkxClient(settings)
    instruments_map: dict[str, Instrument] = {}
    candles_map: dict[str, list[Any]] = {}
    try:
        for inst_id in target_instruments:
            base, quote = inst_id.split("-", 1)
            snapshot_path = (
                config.data.instrument_snapshot
                if len(target_instruments) == 1
                else Path(f"configs/snapshots/{inst_id}.json")
            )
            if snapshot_path is not None and snapshot_path.is_file():
                instruments_map[inst_id] = store.resolve(
                    inst_id,
                    configured_path=snapshot_path,
                    fetch=client.get_instrument,
                ).instrument
            else:
                instruments_map[inst_id] = Instrument(
                    instrument_id=inst_id,
                    base_currency=base,
                    quote_currency=quote,
                    instrument_type=InstrumentType.SPOT,
                    price_tick=Decimal("0.01"),
                    quantity_step=Decimal("0.0001"),
                    minimum_quantity=Decimal("0.0001"),
                    minimum_notional=Decimal("1"),
                    status=InstrumentStatus.LIVE,
                )
            if config.data.source == "csv":
                csv_path = config.data.paths.get(inst_id, config.data.path)
                if csv_path is None:
                    _fail(f"多品种 CSV 回测缺少品种 {inst_id} 的数据文件路径")
                candles_map[inst_id] = load_candles_csv(csv_path, bar=config.market.bar)
            else:
                candles_map[inst_id] = client.get_history_candles(
                    inst_id, config.market.bar, config.data.limit
                )
    except (ExchangeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()

    engine = MultiAssetBacktestEngine(
        config=config,
        instruments=instruments_map,
        candles_by_instrument=candles_map,
    )
    res = engine.run()
    typer.echo(
        json.dumps(
            {
                "run_id": res.run_id,
                "strategy": res.strategy_name,
                "instruments": list(res.instruments),
                "bar": res.bar,
                "summary": res.summary,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@app.command("check-okx-connection")
def check_okx_connection(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    config, settings = _load("check-okx-connection", config_path)
    client = OkxClient(settings)
    try:
        server_time = client.get_server_time()
        instrument = client.get_instrument(config.market.instrument_id)
        candles = client.get_history_candles(config.market.instrument_id, config.market.bar, 5)
    except ExchangeError as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "simulated": config.exchange.simulated,
                "server_time_ms": server_time,
                "instrument": _instrument_dict(instrument),
                "recent_candle_count": len(candles),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("sync-demo-account")
def sync_demo_account(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    config, _settings, repository, client = _demo("sync-demo-account", config_path)
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        coordinator = PrivateStateCoordinator.for_private_account(client, repository, SystemClock())
        sync = coordinator.synchronize_private_account(
            instrument,
            config.market.bar,
            run_id="cli-sync",
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            source="cli_sync_demo_account",
        )
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(
        json.dumps(
            {
                "instrument_id": instrument.instrument_id,
                "captured_at": sync.captured_at.isoformat(),
                "mark_price": str(sync.mark_price),
                "open_order_count": len(sync.open_orders),
                "balances": {k: str(v) for k, v in sync.portfolio.balances.items()},
                "positions": {k: str(v) for k, v in sync.portfolio.positions.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("observe-demo")
def observe_demo(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    max_events: Annotated[int, typer.Option("--max-events")] = 1,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    config, settings = _load("observe-demo", config_path, mode=TradingMode.DEMO)
    session = build_public_observe_session(
        config,
        settings,
        max_events=max_events,
        timeout_seconds=timeout_seconds,
    )
    result = session.run()
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))


def _execute_trading_cycles(
    config: RunConfig,
    settings: Settings,
    *,
    execute_orders: bool,
    cancel_pending: bool,
    loop: bool,
    poll_interval_seconds: float,
    max_cycles: int,
) -> None:
    from app.runners.demo import DemoEvaluationRunner

    if poll_interval_seconds <= 0:
        _fail("poll_interval_seconds 必须大于 0")
    if max_cycles < 0:
        _fail("max_cycles 不能为负数")
    target_instruments = config.market.effective_instruments
    if not loop and max_cycles <= 1 and len(target_instruments) == 1:
        session = build_demo_evaluation_session(
            config,
            settings,
            execute_orders=execute_orders,
            cancel_pending_before_submit=cancel_pending,
        )
        result = session.run()
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        return

    last_candle_ts_by_inst: dict[str, str] = {}
    cycle = 0
    try:
        while True:
            cycle += 1
            for inst_id in target_instruments:
                inst_config = config.with_instrument(inst_id)
                session = build_demo_evaluation_session(
                    inst_config,
                    settings,
                    execute_orders=execute_orders,
                    cancel_pending_before_submit=cancel_pending,
                )
                runner = session.runner
                duplicate_bar = False
                if isinstance(runner, DemoEvaluationRunner) and runner.candles:
                    current_bar_ts = runner.candles[-1].timestamp.isoformat()
                    if current_bar_ts == last_candle_ts_by_inst.get(inst_id):
                        duplicate_bar = True
                        runner.submit_latest_bar = False
                        runner.cancel_pending_before_submit = False
                result = session.run()
                if hasattr(result, "latest_candle_timestamp") and result.latest_candle_timestamp:
                    last_candle_ts_by_inst[inst_id] = str(result.latest_candle_timestamp)
                payload = {
                    "cycle": cycle,
                    "mode": inst_config.mode.value,
                    "instrument_id": inst_id,
                    "duplicate_bar_skipped": duplicate_bar,
                    **asdict(result),
                }
                typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
            if not loop and max_cycles <= 1:
                break
            if max_cycles > 0 and cycle >= max_cycles:
                break
            time.sleep(poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("收到中断信号，常驻循环已安全停止", extra={"cycles": cycle})


@app.command("run-demo")
def run_demo(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    execute_demo_orders: Annotated[
        bool,
        typer.Option(
            "--execute-demo-orders",
            help="在最新已确认收盘 K 线上向 OKX 模拟盘真实提交通过风控的限价单（默认只读干跑）",
        ),
    ] = False,
    confirm_demo_order: Annotated[
        bool,
        typer.Option(
            "--confirm-demo-order",
            help="明确确认允许提交受控 OKX 模拟盘限价单",
        ),
    ] = False,
    cancel_pending: Annotated[
        bool,
        typer.Option(
            "--cancel-pending/--no-cancel-pending",
            help="提交新轮次信号前自动撤销未成交的旧限价挂单",
        ),
    ] = True,
    loop: Annotated[
        bool,
        typer.Option("--loop", help="开启常驻守护循环，按轮次自动对账、撤旧单并评估新收盘 K 线"),
    ] = False,
    poll_interval_seconds: Annotated[
        float,
        typer.Option("--poll-interval-seconds", help="常驻循环轮询间隔秒数"),
    ] = 60.0,
    max_cycles: Annotated[
        int,
        typer.Option("--max-cycles", help="常驻循环最大轮数（0 表示持续运行直到中断）"),
    ] = 0,
) -> None:
    if execute_demo_orders and not confirm_demo_order:
        _fail("启用 --execute-demo-orders 时必须同时显式提供 --confirm-demo-order")
    config, settings = _load("run-demo", config_path, mode=TradingMode.DEMO)
    _execute_trading_cycles(
        config,
        settings,
        execute_orders=execute_demo_orders,
        cancel_pending=cancel_pending,
        loop=loop,
        poll_interval_seconds=poll_interval_seconds,
        max_cycles=max_cycles,
    )


@app.command("run-live")
def run_live(
    config_path: Annotated[Path, typer.Option("--config")],
    execute_live_orders: Annotated[
        bool,
        typer.Option(
            "--execute-live-orders",
            help="在最新已确认收盘 K 线上向 OKX 实盘真实提交通过风控的限价单（默认只读对账与信号评估）",
        ),
    ] = False,
    confirm_live_trading: Annotated[
        bool,
        typer.Option(
            "--confirm-live-trading",
            help="明确确认已开启 TRADING_MODE=live 与 ALLOW_LIVE_TRADING=true 并授权实盘限价单",
        ),
    ] = False,
    cancel_pending: Annotated[
        bool,
        typer.Option(
            "--cancel-pending/--no-cancel-pending",
            help="提交新轮次信号前自动撤销未成交的旧限价挂单",
        ),
    ] = True,
    loop: Annotated[
        bool,
        typer.Option("--loop", help="开启实盘常驻守护循环，同一根 K 线去重不重复发单"),
    ] = False,
    poll_interval_seconds: Annotated[
        float,
        typer.Option("--poll-interval-seconds", help="常驻循环轮询间隔秒数"),
    ] = 60.0,
    max_cycles: Annotated[
        int,
        typer.Option("--max-cycles", help="常驻循环最大轮数（0 表示持续运行直到中断）"),
    ] = 0,
) -> None:
    if execute_live_orders and not confirm_live_trading:
        _fail("启用 --execute-live-orders 时必须同时显式提供 --confirm-live-trading")
    config, settings = _load("run-live", config_path, mode=TradingMode.LIVE)
    if not settings.allow_live_trading or config.exchange.simulated:
        _fail(
            "实盘模式要求同时设置 TRADING_MODE=live、ALLOW_LIVE_TRADING=true 且 exchange.simulated=false"
        )
    _execute_trading_cycles(
        config,
        settings,
        execute_orders=execute_live_orders,
        cancel_pending=cancel_pending,
        loop=loop,
        poll_interval_seconds=poll_interval_seconds,
        max_cycles=max_cycles,
    )


@app.command("query-demo-order")
def query_demo_order(
    client_order_id: Annotated[str, typer.Argument()],
    config_path: Annotated[Path, typer.Option("--config")],
) -> None:
    config, settings, repository, client = _demo("query-demo-order", config_path)
    session = DemoTradingSession(config, settings, client, repository)
    try:
        session.start()
        order = DemoOrderService(config, client, repository, submission_gate=session).query(
            client_order_id
        )
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        session.close()
        client.close()
    typer.echo(json.dumps(_order_summary(order), ensure_ascii=False))


@app.command("cancel-demo-order")
def cancel_demo_order(
    client_order_id: Annotated[str, typer.Argument()],
    config_path: Annotated[Path, typer.Option("--config")],
    confirm_demo_cancellation: Annotated[
        bool,
        typer.Option(
            "--confirm-demo-cancellation",
            help="明确确认撤销一笔受控 OKX 模拟盘订单",
        ),
    ] = False,
) -> None:
    if not confirm_demo_cancellation:
        _fail("必须显式提供 --confirm-demo-cancellation")
    config, settings, repository, client = _demo("cancel-demo-order", config_path)
    session = DemoTradingSession(config, settings, client, repository)
    try:
        session.start()
        order = DemoOrderService(config, client, repository, submission_gate=session).cancel(
            client_order_id
        )
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        session.close()
        client.close()
    typer.echo(json.dumps(_order_summary(order), ensure_ascii=False))


@app.command("test-alert")
def test_alert(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    webhook_url: Annotated[
        str | None,
        typer.Option(
            "--webhook-url",
            help="可选：显式指定企业微信 Webhook URL（默认读取环境变量 ALERT_WEBHOOK_URL）",
        ),
    ] = None,
) -> None:
    config, settings = _load("test-alert", config_path)
    target_url = (webhook_url or settings.alert_webhook_url.get_secret_value()).strip()
    if not target_url:
        _fail(
            "未配置企业微信 Webhook 地址：请在 .env 中设置 ALERT_WEBHOOK_URL 或传入 --webhook-url"
        )
    notifier = WeComAlertNotifier(
        target_url,
        provider=config.monitoring.provider,
        min_level=config.monitoring.min_level,
        timeout_seconds=config.monitoring.timeout_seconds,
        redact_values=(
            settings.okx_api_key.get_secret_value(),
            settings.okx_secret_key.get_secret_value(),
            settings.okx_passphrase.get_secret_value(),
        ),
    )
    ok = notifier.send_test_alert(
        instrument_id=config.market.instrument_id,
        strategy_name=config.strategy.name,
        mode=config.mode.value,
    )
    if not ok:
        _fail("企业微信 Webhook 告警推送失败，请检查机器人 Webhook URL 与网络连接")
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "provider": config.monitoring.provider,
                "instrument_id": config.market.instrument_id,
                "strategy": config.strategy.name,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
