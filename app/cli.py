from __future__ import annotations

import json
import logging
from dataclasses import asdict
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
    help="OKX 现货量化回测与受控模拟交易框架（默认 fail-closed，不支持实盘下单）",
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


@app.command("run-demo")
def run_demo(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    config, settings = _load("run-demo", config_path, mode=TradingMode.DEMO)
    session = build_demo_evaluation_session(config, settings)
    result = session.run()
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))


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


if __name__ == "__main__":
    app()
