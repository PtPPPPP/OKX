from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import uuid4

import typer
from pydantic import ValidationError

from app.bootstrap import (
    build_backtest_session,
    build_demo_evaluation_session,
    build_public_observe_session,
)
from app.config.run_config import RunConfig, load_run_config
from app.config.settings import Settings, TradingMode
from app.continuous_shadow_cli import app as continuous_shadow_app
from app.domain.market import Instrument
from app.domain.order import OrderSide, OrderState, OrderType
from app.exchange.exceptions import AuthenticationError, ExchangeError, NetworkError, OrderRejected
from app.exchange.okx_client import OkxClient
from app.market.historical_data import save_candles_csv
from app.market.websocket import OKXPublicWebSocketProvider
from app.monitoring.logging_config import configure_logging
from app.runtime.clock import SystemClock
from app.services.bounded_demo import BoundedDemoConfiguration, BoundedDemoEngine
from app.services.continuous_demo import ContinuousDemoConfiguration, ContinuousDemoEngine
from app.services.continuous_runtime_safety import ShadowAccountBaselineRepository
from app.services.continuous_shadow_repository import ContinuousShadowRepository
from app.services.controlled_demo_write import ControlledDemoWriteService
from app.services.demo_doctor import DemoDoctor
from app.services.demo_order_preflight import (
    DemoOrderIntent,
    DemoOrderPreflightService,
    ProposalStatus,
)
from app.services.demo_order_revalidation import DemoOrderProposalRevalidator
from app.services.demo_orders import DemoOrderService
from app.services.demo_session import DemoTradingSession
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.services.shadow_replay import run_shadow_replay
from app.services.spot_cash_capability import SpotCashCapabilityEvaluator
from app.services.unknown_order_recovery import UnknownOrderRecoveryService
from app.storage.database import Database, StorageError
from app.storage.migration_workflow import (
    MigrationWorkflowError,
    build_migration_plan,
    execute_authorized_migration,
    load_plan_file,
)
from app.storage.migrations import MIGRATIONS, MigrationError, MigrationManager
from app.storage.repositories import TradingRepository
from app.strategies.registry import (
    strategy_descriptions,
)
from backtest.report import write_backtest_report

app = typer.Typer(
    no_args_is_help=True,
    help="配置驱动的 OKX 现货回测与模拟盘框架（不支持实盘）",
)
app.add_typer(continuous_shadow_app)
logger = logging.getLogger(__name__)


def _load(
    command: str,
    config_path: Path | None,
    overrides: dict[str, Any] | None = None,
) -> tuple[RunConfig, Settings]:
    try:
        config = load_run_config(config_path, cli_overrides=overrides)
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


def _migration_manager() -> MigrationManager:
    settings = Settings()
    database = Database(settings.database_url)
    return MigrationManager(database.path)


def _database_path() -> Path:
    settings = Settings()
    return Database(settings.database_url).path


def _demo(
    command: str,
    config_path: Path | None,
    overrides: dict[str, Any] | None = None,
) -> tuple[RunConfig, Settings, TradingRepository, OkxClient]:
    config, settings = _load(command, config_path, overrides)
    try:
        if config.mode is not TradingMode.DEMO:
            raise ValueError("模拟盘命令要求配置 mode=demo")
        settings.require_demo_credentials()
        return config, settings, _repository(settings), OkxClient(settings)
    except (ValueError, StorageError) as exc:
        _fail(str(exc))


def _validation_message(exc: ValidationError) -> str:
    messages = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "配置"
        messages.append(f"{location}: {error['msg']}")
    return "; ".join(messages)


def _fail(message: str) -> NoReturn:
    logger.error(message)
    typer.echo(f"错误: {message}", err=True)
    raise typer.Exit(code=1)


def _decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise typer.BadParameter(f"{field_name} 必须是有效数字") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise typer.BadParameter(f"{field_name} 必须是大于 0 的有限数字")
    return parsed


def _overrides(strategy: str | None, instrument: str | None, bar: str | None) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "strategy.name": strategy,
        "market.instrument_id": instrument,
        "market.bar": bar,
    }
    if strategy is not None:
        overrides["strategy.parameters"] = {}
    return overrides


@app.command("show-config")
def show_config(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """显示合并后的运行配置和脱敏基础配置。"""
    config, settings = _load("show-config", config_path)
    typer.echo(
        json.dumps(
            {"run": config.model_dump(mode="json"), "application": settings.safe_dict()},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@app.command("validate-config")
def validate_config(config_path: Annotated[Path, typer.Option("--config")]) -> None:
    """校验 YAML、环境变量和策略参数的最终组合。"""
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


@app.command("db-status")
def db_status() -> None:
    """只读显示数据库迁移状态。"""
    try:
        status = _migration_manager().status()
    except (MigrationError, StorageError, ValueError, ValidationError) as exc:
        _fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "current_version": status.current_version,
                "target_version": status.target_version,
                "pending": status.pending,
                "failed": status.failed,
                "compatible": status.compatible,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("db-migrate")
def db_migrate(
    target_version: Annotated[int, typer.Option("--target-version")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只显示计划，不修改数据库")] = False,
) -> None:
    """事务化升级数据库；旧库升级前自动备份。"""
    try:
        applied = _migration_manager().migrate(
            dry_run=dry_run,
            backup=True,
            target_version=target_version,
        )
    except (MigrationError, StorageError, ValueError, ValidationError) as exc:
        _fail(str(exc))
    typer.echo(
        json.dumps(
            {"dry_run": dry_run, "target_version": target_version, "migrations": applied},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("db-backup")
def db_backup(
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """备份当前 SQLite 数据库。"""
    try:
        destination = _migration_manager().backup(output)
    except (MigrationError, StorageError, ValueError, ValidationError) as exc:
        _fail(str(exc))
    typer.echo(json.dumps({"backup": str(destination)}, ensure_ascii=False))


@app.command("db-migrate-plan")
def db_migrate_plan(
    database: Annotated[
        Path | None,
        typer.Option("--database", help="目标数据库路径，默认读取 DATABASE_URL"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="把迁移计划写入该 JSON 文件（供授权阶段绑定）"),
    ] = None,
) -> None:
    """只读生成受控迁移计划；不修改数据库、不产生授权。"""
    path = database if database is not None else _database_path()
    try:
        report = build_migration_plan(path)
    except MigrationWorkflowError as exc:
        _fail(f"{exc}（reason={exc.reason_code}）")
    payload = report.payload()
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if output is not None:
        if output.exists():
            _fail(f"计划输出文件已存在，拒绝覆盖: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        typer.echo(json.dumps({"plan_file": str(output)}, ensure_ascii=False))


@app.command("db-migrate-authorized")
def db_migrate_authorized(
    plan_file: Annotated[Path, typer.Option("--plan", help="db-migrate-plan 生成的计划文件")],
    operator_confirmation_id: Annotated[
        str,
        typer.Option(
            "--operator-confirmation-id",
            help="操作者显式确认标识（例如审批单号）",
        ),
    ],
) -> None:
    """执行经授权的受控正式迁移；无有效授权时一律 BLOCKED。"""
    try:
        plan = load_plan_file(plan_file)
        outcome = execute_authorized_migration(
            database_path=_database_path(),
            plan=plan,
            operator_confirmation_id=operator_confirmation_id,
        )
    except MigrationWorkflowError as exc:
        current, target = _current_and_target_versions()
        _fail(
            f"受控迁移被阻止（reason={exc.reason_code}）：{exc}；"
            f"当前 schema 版本={current}，应用程序目标版本={target}；"
            "请先运行 python -m app db-migrate-plan --output plan.json 查看计划，"
            "再用 --operator-confirmation-id <审批标识> 重新执行"
        )
    typer.echo(json.dumps(outcome.payload(), ensure_ascii=False, indent=2))


def _current_and_target_versions() -> tuple[str, str]:
    try:
        status = _migration_manager().status()
        return str(status.current_version), str(status.target_version)
    except (MigrationError, StorageError, ValueError, ValidationError):
        return "<unreadable>", str(MIGRATIONS[-1].version)


@app.command("list-strategies")
def list_strategies() -> None:
    """列出注册策略及其参数模型。"""
    typer.echo(json.dumps(strategy_descriptions(), ensure_ascii=False, indent=2))


@app.command("describe-strategy")
def describe_strategy(name: Annotated[str, typer.Argument()]) -> None:
    """显示一个已注册策略的说明。"""
    descriptions = {item["name"]: item for item in strategy_descriptions()}
    if name not in descriptions:
        _fail(f"未注册策略: {name}")
    typer.echo(json.dumps(descriptions[name], ensure_ascii=False, indent=2))


@app.command("list-instruments")
def list_instruments(
    quote: Annotated[str | None, typer.Option(help="按计价币过滤")] = None,
) -> None:
    """从 OKX 公共接口列出当前可交易现货品种。"""
    _, settings = _load("list-instruments", None)
    client = OkxClient(settings)
    try:
        instruments = [item for item in client.list_instruments() if item.tradable]
        if quote:
            instruments = [item for item in instruments if item.quote_currency == quote]
    except ExchangeError as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(
        json.dumps(
            [
                {
                    "instrument_id": item.instrument_id,
                    "base_currency": item.base_currency,
                    "quote_currency": item.quote_currency,
                    "status": item.status.value,
                }
                for item in instruments
            ],
            ensure_ascii=False,
        )
    )


@app.command("inspect-instrument")
def inspect_instrument(instrument_id: Annotated[str, typer.Argument()]) -> None:
    """从 OKX 获取并校验指定现货交易规则。"""
    _, settings = _load("inspect-instrument", None)
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
    strategy: Annotated[str | None, typer.Option()] = None,
    instrument: Annotated[str | None, typer.Option()] = None,
    bar: Annotated[str | None, typer.Option()] = None,
    output: Annotated[Path | None, typer.Option()] = None,
    limit: Annotated[int | None, typer.Option(min=1, max=1000)] = None,
) -> None:
    """按配置从 OKX 下载现货历史 K 线。"""
    overrides = _overrides(strategy, instrument, bar)
    overrides["data.limit"] = limit
    config, settings = _load("download-data", config_path, overrides)
    client = OkxClient(settings)
    try:
        resolved = client.get_instrument(config.market.instrument_id)
        candles = client.get_history_candles(
            resolved.instrument_id, config.market.bar, config.data.limit
        )
        confirmed = [candle for candle in candles if candle.confirmed]
        if not confirmed:
            raise ValueError("OKX 未返回已收盘 K 线")
        destination = output or config.data.output
        save_candles_csv(confirmed, destination)
        repository = _repository(settings)
        repository.save_candle_metadata(
            resolved.instrument_id,
            config.market.bar,
            confirmed[0].timestamp,
            confirmed[-1].timestamp,
            len(confirmed),
            "OKX REST /api/v5/market/history-candles",
        )
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(
        f"已保存 {len(confirmed)} 根 {resolved.instrument_id} "
        f"{config.market.bar} K 线到 {destination}"
    )


@app.command("backtest")
def backtest(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    strategy: Annotated[str | None, typer.Option()] = None,
    instrument: Annotated[str | None, typer.Option()] = None,
    bar: Annotated[str | None, typer.Option()] = None,
    output_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """使用注册策略、动态交易规则和配置数据源运行通用回测。"""
    overrides = _overrides(strategy, instrument, bar)
    overrides["output.directory"] = output_dir
    config, settings = _load("backtest", config_path, overrides)
    try:
        session = build_backtest_session(config, settings)
        result = session.run_backtest()
        destination = config.output.directory / result.run_id
        write_backtest_report(result, destination)
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "strategy": result.strategy_name,
                "instrument": result.instrument_id,
                "bar": result.bar,
                "output": str(destination),
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
    """检查服务器时间、动态交易规则和模拟账户鉴权。"""
    config, _, _, client = _demo("check-okx-connection", config_path)
    try:
        server_time = client.get_server_time()
        instrument = client.get_instrument(config.market.instrument_id)
        client.get_portfolio(instrument)
    except ExchangeError as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(
        json.dumps(
            {
                "status": "ok",
                "server_time_ms": server_time,
                "instrument_id": instrument.instrument_id,
                "demo_authentication": "ok",
            },
            ensure_ascii=False,
        )
    )


@app.command("diagnose-okx-auth")
def diagnose_okx_auth(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """只读诊断 OKX 模拟盘鉴权；不会调用订单接口。"""
    config, settings, repository, client = _demo("diagnose-okx-auth", config_path)
    result: dict[str, Any] = {
        "environment": {
            "mode": client.environment.mode,
            "rest_base_url": client.environment.rest_base_url,
            "simulated_trading": client.environment.simulated_trading,
        },
        "credentials": {
            name: {
                "configured": status.configured,
                "source": status.source,
                "has_leading_or_trailing_whitespace": status.has_leading_or_trailing_whitespace,
                "contains_linebreak": status.contains_linebreak,
            }
            for name, status in settings.credential_diagnostics().items()
        },
        "public_rest": {"status": "not_run"},
        "account_configuration": {"status": "not_run"},
        "account_balance": {"status": "not_run"},
        "derivative_positions": {"status": "not_run"},
        "private_websocket_auth": {"status": "not_run", "reason": "not needed for REST diagnosis"},
        "order_submission_attempted": False,
    }
    try:
        server_time = client.get_server_time()
        instrument = client.get_instrument(config.market.instrument_id)
        result["public_rest"] = {
            "status": "passed",
            "server_time_ms": server_time,
            "clock_skew_ms": client.server_offset_ms * -1,
            "instrument_id": instrument.instrument_id,
        }
        configuration = client.get_account_configuration()
        result["account_configuration"] = {
            "status": "passed",
            "account_mode": configuration.account_mode.value,
        }
        client.get_portfolio(instrument, configuration=configuration)
        result["account_balance"] = {"status": "passed"}
        derivative_positions = client.get_derivative_positions()
        result["derivative_positions"] = {
            "status": "passed",
            "nonzero_position_count": len(derivative_positions),
            "instrument_ids": sorted(derivative_positions),
        }
    except AuthenticationError as exc:
        diagnostic = client.authentication_diagnostic(exc).safe_dict()
        result["authentication"] = diagnostic
        endpoint = diagnostic["endpoint"]
        if endpoint == "/api/v5/account/config":
            result["account_configuration"] = {"status": "failed"}
        elif endpoint == "/api/v5/account/balance":
            result["account_balance"] = {"status": "failed"}
        elif endpoint == "/api/v5/account/positions":
            result["derivative_positions"] = {"status": "failed"}
        else:
            result["private_rest"] = {"status": "failed"}
    except ExchangeError as exc:
        result["public_rest"] = {"status": "failed", "reason": str(exc)}
    finally:
        repository.save_audit_record(
            record_type="okx_auth_diagnostic",
            run_id=uuid4().hex,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            instrument_id=config.market.instrument_id,
            bar=config.market.bar,
            payload=result,
        )
        client.close()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result["account_configuration"]["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("audit-spot-capability")
def audit_spot_capability(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """只读审计当前账户是否具备指定 SPOT/cash 组合的 dry-run 条件。"""
    config, _, repository, client = _demo("audit-spot-capability", config_path)
    run_id = uuid4().hex
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        configuration = client.get_account_configuration()
        portfolio = client.get_portfolio(instrument, configuration=configuration)
        max_size = client.get_max_available_size(instrument.instrument_id)
        derivative_positions = client.get_derivative_positions()
        open_orders = client.get_pending_orders(instrument.instrument_id)
        price = client.get_last_price(instrument.instrument_id)
        report = SpotCashCapabilityEvaluator().evaluate(
            mode=config.mode,
            instrument=instrument,
            portfolio=portfolio,
            max_size=max_size,
            derivative_positions=derivative_positions,
            open_order_count=len(open_orders),
            checked_at=SystemClock().now(),
        )
        result = {
            "account_mode": report.account_mode.value,
            "instrument_id": report.instrument_id,
            "instrument_type": instrument.instrument_type.value,
            "trade_mode": report.trade_mode.value,
            "reference_price": str(price),
            "max_buy_quote_amount": str(max_size.max_buy) if max_size.max_buy is not None else None,
            "max_sell_base_quantity": str(max_size.max_sell)
            if max_size.max_sell is not None
            else None,
            "nonzero_derivative_position_count": len(derivative_positions),
            "open_order_count": len(open_orders),
            "checks": report.checks,
            "blockers": report.blockers,
            "warnings": report.warnings,
            "eligible_for_dry_run": report.eligible_for_dry_run,
            "eligible_for_controlled_order_test": report.eligible_for_controlled_order_test,
            "order_submission_attempted": False,
        }
        repository.save_audit_record(
            record_type="spot_cash_capability_audit",
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            instrument_id=instrument.instrument_id,
            bar=config.market.bar,
            payload=result,
        )
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("plan-demo-spot-order")
def plan_demo_spot_order(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """仅生成 SPOT/cash dry-run 计划，固定不向 Broker 或 OKX 提交订单。"""
    config, _, repository, client = _demo("plan-demo-spot-order", config_path)
    run_id = uuid4().hex
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        configuration = client.get_account_configuration()
        portfolio = client.get_portfolio(instrument, configuration=configuration)
        max_size = client.get_max_available_size(instrument.instrument_id)
        derivative_positions = client.get_derivative_positions()
        open_orders = client.get_pending_orders(instrument.instrument_id)
        price = client.get_last_price(instrument.instrument_id)
        capability = SpotCashCapabilityEvaluator().evaluate(
            mode=config.mode,
            instrument=instrument,
            portfolio=portfolio,
            max_size=max_size,
            derivative_positions=derivative_positions,
            open_order_count=len(open_orders),
            checked_at=SystemClock().now(),
        )
        notional = min(
            Decimal(str(config.position_sizing.parameters["order_notional"])), Decimal("5")
        )
        quantity = (notional / price // instrument.quantity_step) * instrument.quantity_step
        estimated_notional = quantity * price
        result = {
            "instrument_id": instrument.instrument_id,
            "instrument_type": instrument.instrument_type.value,
            "trade_mode": "cash",
            "side": "buy",
            "order_type": "limit",
            "reference_price": str(price),
            "planned_limit_price": str(price),
            "quantity": str(quantity),
            "notional": str(estimated_notional),
            "fee_estimate": str(estimated_notional * config.backtest.fee_rate),
            "minimum_quantity": str(instrument.minimum_quantity),
            "minimum_notional": str(instrument.minimum_notional),
            "price_tick": str(instrument.price_tick),
            "quantity_step": str(instrument.quantity_step),
            "position_sizer": config.position_sizing.name,
            "risk_decision": "not_evaluated: submission path disabled for this stage",
            "capability_decision": capability.status,
            "dry_run": True,
            "submission_performed": False,
        }
        repository.save_audit_record(
            record_type="spot_cash_order_plan",
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            instrument_id=instrument.instrument_id,
            bar=config.market.bar,
            payload=result,
        )
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("prepare-demo-order")
def prepare_demo_order(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """生成不可提交的统一 SPOT/cash 模拟盘订单提案。"""
    config, _, repository, client = _demo("prepare-demo-order", config_path)
    run_id = uuid4().hex
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        configuration = client.get_account_configuration()
        portfolio = client.get_portfolio(instrument, configuration=configuration)
        max_size = client.get_max_available_size(instrument.instrument_id)
        proposal = DemoOrderPreflightService().prepare_order(
            intent=DemoOrderIntent(
                run_id,
                config.strategy.name,
                instrument.instrument_id,
                instrument.instrument_type,
                max_size.trade_mode,
                OrderSide.BUY,
                OrderType.LIMIT,
                min(
                    Decimal(str(config.position_sizing.parameters["order_notional"])), Decimal("5")
                ),
                "manual_demo_test",
                SystemClock().now(),
            ),
            config=config,
            instrument=instrument,
            portfolio=portfolio,
            max_size=max_size,
            derivative_positions=client.get_derivative_positions(),
            open_order_count=len(client.get_pending_orders(instrument.instrument_id)),
            reference_price=client.get_last_price(instrument.instrument_id),
            now=SystemClock().now(),
        )
        repository.save_demo_order_proposal(proposal)
        result = DemoOrderPreflightService.audit_payload(proposal)
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("inspect-demo-order-proposal")
def inspect_demo_order_proposal(
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    _, settings = _load("inspect-demo-order-proposal", config_path)
    proposal = _repository(settings).load_demo_order_proposal(proposal_id)
    if proposal is None:
        _fail("proposal not found")
    typer.echo(
        json.dumps(
            DemoOrderPreflightService.audit_payload(proposal), ensure_ascii=False, default=str
        )
    )


@app.command("inspect-unknown-order-evidence")
def inspect_unknown_order_evidence(
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    _, settings = _load("inspect-unknown-order-evidence", config_path)
    rows = _repository(settings).load_unknown_recoveries(proposal_id)
    typer.echo(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


@app.command("close-unknown-demo-order")
def close_unknown_demo_order(
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    confirm_operational_close: Annotated[bool, typer.Option("--confirm-operational-close")] = False,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    if not confirm_operational_close:
        _fail("must explicitly provide --confirm-operational-close")
    _, settings = _load("close-unknown-demo-order", config_path)
    repository = _repository(settings)
    try:
        repository.close_unknown_order_operationally_not_created(
            proposal_id, reason="explicit_user_authorized_operational_close"
        )
        typer.echo(
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    "status": "operationally_not_created",
                    "submission_performed_preserved": True,
                },
                ensure_ascii=False,
            )
        )
    except (StorageError, ValueError) as exc:
        _fail(str(exc))


@app.command("capture-demo-order-baseline")
def capture_demo_order_baseline(
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/btc_ma_demo.yaml"),
) -> None:
    config, _, repository, client = _demo("capture-demo-order-baseline", config_path)
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        snapshot = PrivateStateCoordinator.for_private_account(
            client, repository, SystemClock()
        ).synchronize_private_account(
            instrument,
            config.market.bar,
            run_id=uuid4().hex,
            mode="demo",
            strategy_name="single_order_acceptance_baseline",
            source="capture_demo_order_baseline",
        )
        typer.echo(
            json.dumps(
                {
                    "captured_at": snapshot.captured_at,
                    "balances": {
                        key: str(value) for key, value in snapshot.portfolio.balances.items()
                    },
                    "open_order_count": len(snapshot.open_orders),
                },
                ensure_ascii=False,
                default=str,
            )
        )
    except (ExchangeError, StorageError, ValueError, RuntimeError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command("bounded-demo-preflight")
def bounded_demo_preflight(
    config_path: Annotated[Path, typer.Option("--config")] = Path(
        "configs/btc_ma_demo_acceptance.yaml"
    ),
) -> None:
    """Read-only startup gates for the bounded acceptance run."""
    config, settings, repository, client = _demo("bounded-demo-preflight", config_path)
    try:
        if not config.strategy.acceptance_only:
            _fail("acceptance config must set acceptance_only=true")
        database = Database(settings.database_url)
        MigrationManager(database.path).require_current()
        with database.connect() as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM continuous_demo_runs WHERE status IN ('starting','warming_up','shadow_running')"
            ).fetchone()[0]
            unknown = connection.execute(
                "SELECT COUNT(*) FROM demo_order_proposals WHERE status='unknown'"
            ).fetchone()[0]
        if active or unknown:
            _fail("active run or unknown proposal exists")
        session = DemoTradingSession(config, settings, client, repository)
        try:
            history = [
                c
                for c in client.get_history_candles(
                    config.market.instrument_id, config.market.bar, 35
                )
                if c.confirmed
            ]
            max_size = client.get_max_available_size(config.market.instrument_id)
            derivatives = client.get_derivative_positions()
            started = session.start()
            readiness = session.readiness_snapshot
            stream_health = session.stream.health
            checks = {
                "reconciliation": started.reconciliation_status.value == "healthy",
                "private_websocket": readiness.stream_ready,
                "private_thread_alive": readiness.monitor_thread_alive,
                "private_state_reconciled": readiness.private_state_reconciled,
                "private_state_received": readiness.private_state_received,
                "private_session_ready": session.order_submission_ready,
                "confirmed_history": len(history) >= int(config.strategy.slow_window or 30),
                "derivative_positions_zero": not derivatives,
                "max_available_buy": max_size.max_buy is not None,
                "max_available_sell": max_size.max_sell is not None,
            }
            ready = all(checks.values())
            telemetry = {
                "readiness_id": session.readiness_id,
                "readiness_stage": readiness.stage.value,
                "network_mode": session.stream.network.mode.value,
                "proxy_configured": session.stream.network.proxy_url is not None,
                "proxy_url": session.stream.network.redacted_proxy_url,
                "private_ws_connect_attempts": stream_health.connect_attempts,
                "private_ws_connections": stream_health.connections,
                "private_ws_tls_ready": stream_health.tls_ready,
                "private_ws_handshake_ready": stream_health.handshake_ready,
                "private_ws_login_sent": stream_health.login_sent,
                "private_ws_authenticated": stream_health.authenticated,
                "private_ws_subscribe_sent": stream_health.subscribe_sent,
                "private_ws_subscriptions_ready": stream_health.subscriptions_ready,
                "private_ws_events_received": stream_health.events_received,
                "private_ws_last_event_timestamp": (
                    stream_health.last_message_at.isoformat()
                    if stream_health.last_message_at is not None
                    else None
                ),
                "private_ws_unsubscriptions": stream_health.unsubscriptions,
                "private_ws_closed_cleanly": stream_health.closed_cleanly,
                "private_ws_failure_stage": stream_health.failure_stage,
                "private_ws_failure_type": stream_health.failure_type,
                "account_snapshot_ready": readiness.account_snapshot_ready,
                "position_snapshot_ready": readiness.position_snapshot_ready,
                "order_snapshot_ready": readiness.order_snapshot_ready,
                "snapshot_freshness_valid": readiness.private_state_reconciled,
                "derivative_positions_count": len(derivatives),
                "non_terminal_orders_count": (
                    len(session.start_snapshot.open_orders)
                    if session.start_snapshot is not None
                    else None
                ),
                "private_state_reconciled": readiness.private_state_reconciled,
                "reconciliation_failure_reason": None,
                "ready_for_bounded_demo": ready,
            }
            repository.save_system_event(
                "private_readiness_preflight_result",
                "read-only bounded demo readiness result",
                telemetry,
            )
            typer.echo(
                json.dumps(
                    {
                        "ready_for_bounded_demo": ready,
                        "checks": checks,
                        "private_stream_health": {
                            "connected": stream_health.connected,
                            "authenticated": stream_health.authenticated,
                            "subscriptions_ready": stream_health.subscriptions_ready,
                            "stale": stream_health.stale,
                        },
                        "private_readiness": telemetry,
                        "environment": "demo",
                        "mode": "bounded_demo",
                        "acceptance_only": True,
                        "database_version": MigrationManager(database.path)
                        .status()
                        .current_version,
                        "confirmed_candles": len(history),
                        "derivative_positions": len(derivatives),
                        "broker_write_calls": 0,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        finally:
            session.close()
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command("run-shadow-replay")
def run_shadow_replay_command(
    config_path: Annotated[Path, typer.Option("--config")],
    data_path: Annotated[Path, typer.Option("--data")],
    maximum_confirmed_candles: Annotated[
        int, typer.Option("--maximum-confirmed-candles", min=1, max=1000)
    ] = 100,
) -> None:
    config, settings = _load("run-shadow-replay", config_path)
    database = Database(settings.database_url)
    try:
        result = run_shadow_replay(database, config, data_path, maximum_confirmed_candles)
    except (StorageError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, default=str))


@app.command("recover-shadow-replay")
def recover_shadow_replay(
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Recover a stopped, local-only replay without any exchange or broker call."""
    settings = Settings()
    database = Database(settings.database_url)
    repository = ContinuousShadowRepository(database)
    row = repository.get_status(run_id)
    if row is None:
        _fail("run_id not found")
    if row["mode"] != "shadow" or row["status"] != "stopped":
        _fail("shadow replay must be stopped before recovery")
    recovery_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO continuous_run_recoveries
            (recovery_id,run_id,status,started_at,completed_at,original_run_status,final_run_status,
             lock_status,database_status,reconciliation_status,external_activity_status,blockers_json,
             warnings_json,closure_type,historical_baseline_available,
             historical_balance_reconciliation_possible,closure_limitations,evidence_level)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                recovery_id,
                run_id,
                "recovered_stopped",
                now,
                now,
                "stopped",
                "stopped",
                "released_or_not_owned",
                "healthy",
                "not_applicable",
                "none",
                "[]",
                "[]",
                "shadow_replay_recovery",
                0,
                0,
                "not_applicable",
                "local_database_only",
            ),
        )
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "result": "recovered_stopped",
                "recovery_id": recovery_id,
                "broker_write_calls": 0,
            },
            ensure_ascii=False,
        )
    )


@app.command("run-continuous-demo")
def run_continuous_demo(
    shadow: Annotated[bool, typer.Option("--shadow")] = False,
    confirm_continuous_demo: Annotated[bool, typer.Option("--confirm-continuous-demo")] = False,
    maximum_runtime_minutes: Annotated[int, typer.Option(min=1, max=180)] = 30,
    maximum_confirmed_decision_candles: Annotated[int, typer.Option(min=1, max=6)] = 6,
    maximum_order_submissions: Annotated[int, typer.Option(min=0, max=2)] = 0,
    maximum_notional_per_order: Annotated[str, typer.Option()] = "5",
    maximum_managed_exposure: Annotated[str, typer.Option()] = "5",
    resume_run_id: Annotated[str | None, typer.Option("--resume-run-id")] = None,
    fault_injection: Annotated[str | None, typer.Option("--fault-injection")] = None,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/btc_ma_demo.yaml"),
    minimum_confirmed_candles: Annotated[int, typer.Option(min=3, max=1000)] = 3,
) -> None:
    if shadow and confirm_continuous_demo:
        _fail("--shadow and --confirm-continuous-demo cannot be used together")
    if not shadow and not confirm_continuous_demo:
        _fail("bounded demo requires --confirm-continuous-demo")
    if shadow and maximum_order_submissions != 0:
        _fail("shadow mode requires zero submissions")
    if not shadow and maximum_order_submissions != 2:
        _fail("bounded demo requires maximum_order_submissions=2")
    if shadow and resume_run_id is not None:
        _fail("shadow mode cannot resume a bounded demo run")
    config, settings, repository, client = _demo("run-continuous-demo", config_path)
    session = DemoTradingSession(config, settings, client, repository)
    stream = OKXPublicWebSocketProvider(stale_after_seconds=15)
    result: Any
    try:
        if shadow:
            shadow_engine = ContinuousDemoEngine(Database(settings.database_url), session, stream)
            result = asyncio.run(
                shadow_engine.run(
                    ContinuousDemoConfiguration(
                        config.market.instrument_id,
                        config.strategy.name,
                        config.market.bar,
                        maximum_runtime_minutes,
                        fault_injection=fault_injection,
                        minimum_confirmed_candles=minimum_confirmed_candles,
                    )
                )
            )
        else:
            if fault_injection is not None:
                _fail("fault injection is only available in shadow mode")
            bounded_engine = BoundedDemoEngine(Database(settings.database_url), session, stream)
            result = asyncio.run(
                bounded_engine.run(
                    BoundedDemoConfiguration(
                        instrument_id=config.market.instrument_id,
                        strategy_name=config.strategy.name,
                        timeframe=config.market.bar,
                        maximum_runtime_minutes=maximum_runtime_minutes,
                        maximum_confirmed_decision_candles=maximum_confirmed_decision_candles,
                        maximum_order_submissions=maximum_order_submissions,
                        maximum_orders_per_hour=2,
                        maximum_open_orders=1,
                        maximum_notional_per_order=_decimal(
                            maximum_notional_per_order, "maximum_notional_per_order"
                        ),
                        maximum_managed_exposure=_decimal(
                            maximum_managed_exposure, "maximum_managed_exposure"
                        ),
                        maximum_runtime_seconds=maximum_runtime_minutes * 60,
                    ),
                    config,
                    resume_run_id=resume_run_id,
                )
            )
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, default=str))
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command("run-legacy-inventory-cleanup")
def run_legacy_inventory_cleanup(
    confirm_legacy_inventory_cleanup: Annotated[
        bool, typer.Option("--confirm-legacy-inventory-cleanup")
    ] = False,
    config_path: Annotated[Path, typer.Option("--config")] = Path(
        "configs/btc_ma_demo_acceptance.yaml"
    ),
    maximum_first_order_wait_minutes: Annotated[int, typer.Option(min=1, max=10)] = 10,
) -> None:
    """Retired write command retained only to fail closed for old operator scripts."""
    if not confirm_legacy_inventory_cleanup:
        _fail("legacy inventory cleanup requires --confirm-legacy-inventory-cleanup")
    _fail(
        "legacy inventory cleanup write path is retired; historical cleanup records remain read-only"
    )


@app.command("continuous-demo-status")
def continuous_demo_status(
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Read a continuous shadow run from the local database only."""
    settings = Settings()
    row = ContinuousShadowRepository(Database(settings.database_url)).get_status(run_id)
    if row is None:
        _fail("run_id not found")
    typer.echo(
        json.dumps(
            {
                **row,
                "environment": "demo",
                "live_trading": False,
                "real_order_submissions": row.get("submitted_order_count", 0),
                "broker_write_calls": row.get("submitted_order_count", 0)
                if row.get("mode") == "bounded_demo"
                else 0,
            },
            ensure_ascii=False,
            default=str,
        )
    )


@app.command("stop-continuous-demo")
def stop_continuous_demo(
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Write a local stop request; no exchange endpoint is called."""
    settings = Settings()
    accepted = ContinuousShadowRepository(Database(settings.database_url)).request_stop(run_id)
    typer.echo(
        json.dumps(
            {"run_id": run_id, "stop_requested": accepted, "broker_write_calls": 0},
            ensure_ascii=False,
        )
    )


@app.command("recover-continuous-demo")
def recover_continuous_demo(
    run_id: Annotated[str, typer.Option("--run-id")],
) -> None:
    """Read-only recovery audit; it never starts a stream or calls a broker."""
    settings = Settings()
    database = Database(settings.database_url)
    repository = ContinuousShadowRepository(database)
    row = repository.get_status(run_id)
    if row is None:
        _fail("run_id not found")
    started = datetime.now(UTC)
    result = "recovered_stopped"
    blockers: list[str] = []
    reconciliation_status = "not_run"
    with database.connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        database_ok = integrity is not None and str(integrity[0]) == "ok"
        baseline = ShadowAccountBaselineRepository(database).load(run_id)
        if not database_ok:
            result = "database_inconsistent"
            blockers.append("database_integrity_check_failed")
        elif baseline is None:
            result = "recovery_required"
            blockers.append("shadow_account_baseline_missing")
            connection.execute(
                "UPDATE continuous_demo_runs SET status='recovery_required',recovery_required=1,stop_reason=? WHERE run_id=?",
                ("shadow_account_baseline_missing", run_id),
            )
        elif row["status"] in {"shadow_running", "starting", "warming_up"}:
            result = "recovery_required"
            blockers.append("run_not_stopped")
            connection.execute(
                "UPDATE continuous_demo_runs SET status='recovery_required',recovery_required=1,stop_reason=? WHERE run_id=?",
                ("stale_run_detected", run_id),
            )
        elif str(row.get("circuit_breaker_code") or "").startswith("external_"):
            result = "recovered_with_external_activity"
            blockers.append(str(row["circuit_breaker_code"]))
        elif database_ok and baseline is not None:
            client: OkxClient | None = None
            try:
                recovery_config = load_run_config(Path("configs/btc_ma_demo.yaml"))
                if recovery_config.mode is not TradingMode.DEMO:
                    raise ValueError("recovery requires demo mode")
                settings.require_demo_credentials()
                client = OkxClient(settings)
                instrument = client.get_instrument(str(row["instrument_id"]))
                account_configuration = client.get_account_configuration()
                portfolio = client.get_portfolio(instrument, configuration=account_configuration)
                pending = client.get_recovery_orders_pending(
                    instrument.instrument_id,
                    datetime.fromisoformat(str(row["started_at"])),
                    datetime.now(UTC),
                )[0]
                orders, order_evidence = client.get_recovery_orders(
                    instrument.instrument_id,
                    datetime.fromisoformat(str(row["started_at"])),
                    datetime.now(UTC),
                )
                fills, fill_evidence = client.get_recovery_fills(
                    instrument.instrument_id,
                    datetime.fromisoformat(str(row["started_at"])),
                    datetime.now(UTC),
                )
                client.get_derivative_positions()
                if not order_evidence.completed or not fill_evidence.completed:
                    raise RuntimeError("recovery query coverage incomplete")
                btc = portfolio.asset_balances.get("BTC")
                usdt = portfolio.asset_balances.get("USDT")
                if btc is None or usdt is None:
                    raise RuntimeError("recovery account fields incomplete")
                local_orders = connection.execute(
                    "SELECT client_order_id,exchange_order_id FROM orders WHERE run_id=?",
                    (run_id,),
                ).fetchall()
                local_client_ids = {str(item[0]) for item in local_orders}
                local_exchange_ids = {str(item[1]) for item in local_orders if item[1]}
                local_fill_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM fills WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                )
                external_orders = [
                    item
                    for item in orders
                    if (item.client_order_id and item.client_order_id not in local_client_ids)
                    or (item.exchange_order_id and item.exchange_order_id not in local_exchange_ids)
                ]
                external_fills = [
                    item
                    for item in fills
                    if (item.client_order_id and item.client_order_id not in local_client_ids)
                    and (
                        item.exchange_order_id and item.exchange_order_id not in local_exchange_ids
                    )
                ]
                quantity_changed = any(
                    abs(current - baseline_value) > Decimal("0.00000001")
                    for current, baseline_value in (
                        (btc.cash_balance or Decimal("0"), baseline.btc_total),
                        (btc.available_balance or Decimal("0"), baseline.btc_available),
                        (btc.frozen_balance or Decimal("0"), baseline.btc_frozen),
                        (usdt.cash_balance or Decimal("0"), baseline.usdt_total),
                        (usdt.available_balance or Decimal("0"), baseline.usdt_available),
                        (usdt.frozen_balance or Decimal("0"), baseline.usdt_frozen),
                    )
                )
                known_run_activity = bool(local_orders or local_fill_count)
                if (
                    external_orders
                    or external_fills
                    or pending
                    or (quantity_changed and not known_run_activity)
                ):
                    result = "recovered_with_external_activity"
                    blockers.append("external_activity_detected")
                reconciliation_status = "healthy"
            except (ExchangeError, RuntimeError, ValueError) as exc:
                result = "reconciliation_failed"
                blockers.append(f"recovery_query_failed:{type(exc).__name__}")
                reconciliation_status = "unhealthy"
            finally:
                if client is not None:
                    client.close()
        if result == "recovered_stopped":
            connection.execute(
                "UPDATE continuous_demo_runs SET status='stopped',stopped_at=?,stop_reason=?,recovery_required=0 WHERE run_id=?",
                (datetime.now(UTC).isoformat(), "recovered_stopped", run_id),
            )
        elif result == "recovery_required" and baseline is None:
            connection.execute(
                "UPDATE continuous_run_locks SET released_at=?,release_reason=? WHERE lock_name='continuous-demo' AND run_id=? AND released_at IS NULL",
                (datetime.now(UTC).isoformat(), "recovery_required_no_baseline", run_id),
            )
            connection.execute(
                "UPDATE continuous_run_locks SET released_at=?,release_reason=? WHERE lock_name='continuous-demo' AND run_id=? AND released_at IS NULL",
                (datetime.now(UTC).isoformat(), "recovery_completed", run_id),
            )
        lock = connection.execute(
            "SELECT lease_expires_at,released_at FROM continuous_run_locks WHERE lock_name='continuous-demo' AND run_id=?",
            (run_id,),
        ).fetchone()
        if (
            lock is not None
            and lock["released_at"] is None
            and datetime.fromisoformat(str(lock["lease_expires_at"])) < datetime.now(UTC)
            and str(row["status"]) == "stopped"
        ):
            connection.execute(
                "UPDATE continuous_run_locks SET released_at=?,release_reason=? WHERE lock_name='continuous-demo' AND run_id=? AND released_at IS NULL",
                (datetime.now(UTC).isoformat(), "expired_stopped_run_recovery", run_id),
            )
        recovery_id = uuid4().hex
        connection.execute(
            """INSERT INTO continuous_run_recoveries
            (recovery_id,run_id,status,started_at,completed_at,original_run_status,final_run_status,
             lock_status,database_status,reconciliation_status,external_activity_status,blockers_json,
             warnings_json,closure_type,historical_baseline_available,
             historical_balance_reconciliation_possible,closure_limitations,evidence_level)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                recovery_id,
                run_id,
                result,
                started.isoformat(),
                datetime.now(UTC).isoformat(),
                str(row["status"]),
                str(row["status"]),
                "released_or_not_owned",
                "healthy" if database_ok else "unhealthy",
                reconciliation_status,
                "external_activity" if "external_activity" in " ".join(blockers) else "none",
                json.dumps(blockers),
                "[]",
                "continuous_demo_recovery",
                int(baseline is not None),
                int(baseline is not None),
                "none" if baseline is not None else "shadow_account_baseline_missing",
                "exchange_and_database" if baseline is not None else "local_database_only",
            ),
        )
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "result": result,
                "recovery_id": recovery_id,
                "reconciliation_status": reconciliation_status,
                "broker_write_calls": 0,
            },
            ensure_ascii=False,
        )
    )


@app.command("recover-demo-order")
def recover_demo_order(
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/btc_ma_demo.yaml"),
) -> None:
    """Read-only recovery; this command cannot submit, amend, or cancel orders."""
    _, settings = _load("recover-demo-order", config_path)
    repository = _repository(settings)
    client = OkxClient(settings)
    try:
        result = UnknownOrderRecoveryService(repository, client).recover(proposal_id)
        started = repository.load_submission_started_at(proposal_id)
        payload = asdict(result)
        payload.update(
            {
                "submission_started_at": started,
                "submission_age_days": (client.clock.now() - started).total_seconds() / 86400
                if started
                else None,
                "fills_history_archive_required": False,
                "new_order_submission_performed": False,
                "total_order_submissions": repository.count_controlled_demo_submissions(),
                "original_client_order_id_unchanged": True,
                "continuous_demo_trading_allowed": False,
            }
        )
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command("revalidate-demo-order-proposal")
def revalidate_demo_order_proposal(
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/btc_ma_demo.yaml"),
) -> None:
    config, settings, repository, client = _demo("revalidate-demo-order-proposal", config_path)
    session = DemoTradingSession(config, settings, client, repository)
    try:
        session.start()
        result = asyncio.run(
            DemoOrderProposalRevalidator(
                repository,
                client,
                websocket_ready=session.order_submission_ready,
                reconcile=lambda _instrument: session.reconcile_result(),
            ).revalidate(proposal_id)
        )
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, default=str))
        if not result.passed:
            raise typer.Exit(code=1)
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        session.close()
        client.close()


@app.command("submit-demo-order")
def submit_demo_order(
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    confirm_demo_order: Annotated[bool, typer.Option("--confirm-demo-order")] = False,
    config_path: Annotated[Path, typer.Option("--config")] = Path("configs/btc_ma_demo.yaml"),
) -> None:
    """Submit the one persisted proposal; no retry or replacement order is possible."""
    if not confirm_demo_order:
        _fail("must explicitly provide --confirm-demo-order")
    config, settings, repository, client = _demo("submit-demo-order", config_path)
    session = DemoTradingSession(config, settings, client, repository)
    try:
        session.start()
        proposal = repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            _fail("proposal not found")
        check = asyncio.run(
            DemoOrderProposalRevalidator(
                repository,
                client,
                websocket_ready=session.order_submission_ready,
                reconcile=lambda _instrument: session.reconcile_result(),
            ).revalidate(proposal_id)
        )
        if not check.passed:
            _fail(f"proposal revalidation failed: {check.reason}")
        proposal = repository.load_demo_order_proposal(proposal_id)
        if proposal is None:
            _fail("proposal disappeared")
        local_order = repository.begin_controlled_demo_submission(proposal)
        try:
            remote_order = ControlledDemoWriteService(repository, client).place_order(local_order)
        except OrderRejected:
            local_order.transition(OrderState.REJECTED, at=client.clock.now())
            repository.complete_controlled_demo_submission(
                proposal_id,
                local_order,
                event_type="submission_failed",
                proposal_status=ProposalStatus.CONSUMED,
            )
            raise
        except NetworkError:
            local_order.transition(OrderState.UNKNOWN, at=client.clock.now())
            repository.complete_controlled_demo_submission(
                proposal_id,
                local_order,
                event_type="submission_unknown",
                proposal_status=ProposalStatus.UNKNOWN,
            )
            raise
        except ExchangeError as exc:
            if any(code in str(exc) for code in ("502", "503", "504")):
                repository.mark_controlled_demo_submission_unknown(
                    proposal_id, error_category="exchange_unavailable", http_status=503
                )
            else:
                # The POST may already have been accepted by the exchange; any
                # ambiguous transport/business outcome must freeze to `unknown`
                # so recovery (read-only, clOrdId-based) can resolve it instead
                # of leaving the proposal stuck in submission_in_progress.
                repository.mark_controlled_demo_submission_unknown(
                    proposal_id,
                    error_category=f"exchange_error_after_submit_attempt:{type(exc).__name__}",
                    http_status=None,
                )
            raise
        repository.complete_controlled_demo_submission(
            proposal_id,
            remote_order,
            event_type="submission_succeeded",
            proposal_status=ProposalStatus.SUBMITTED,
        )
        reconciliation = session.reconcile_result()
        if not reconciliation.order_submission_allowed:
            _fail(f"订单提交后对账未通过: {reconciliation.message}")
        confirmed = repository.load_order(proposal.client_order_id)
        if confirmed is None:
            _fail("订单提交后受控对账未获得本地状态")
        typer.echo(
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    **_order_summary(confirmed),
                    "submission_performed": True,
                    "environment": "demo",
                },
                ensure_ascii=False,
            )
        )
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        session.close()
        client.close()


@app.command("demo-doctor")
def demo_doctor(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """检查公共网络、模拟账户、数据库、恢复状态与下单门禁。"""
    config, settings = _load("demo-doctor", config_path)
    report = DemoDoctor(config, settings).run()
    typer.echo(
        json.dumps(
            {
                "checks": [asdict(check) for check in report.checks],
                "order_submission_allowed": report.order_submission_allowed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


@app.command("sync-demo-account")
def sync_demo_account(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """按配置同步模拟账户、持仓和挂单。"""
    config, _, repository, client = _demo("sync-demo-account", config_path)
    clock = SystemClock()
    run_id = uuid4().hex
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        snapshot = PrivateStateCoordinator.for_private_account(
            client, repository, clock
        ).synchronize_private_account(
            instrument,
            config.market.bar,
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            source="sync_demo_account",
        )
        portfolio = snapshot.portfolio
        orders = snapshot.open_orders
        repository.save_audit_record(
            record_type="account_snapshot",
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            instrument_id=instrument.instrument_id,
            bar=config.market.bar,
            payload={
                "asset_balances": {
                    currency: {
                        "cash_balance": asset.cash_balance,
                        "available": asset.available_balance,
                        "frozen": asset.frozen_balance,
                        "equity": asset.equity,
                        "equity_usd": asset.equity_usd,
                        "fetched_at": asset.fetched_at,
                    }
                    for currency, asset in portfolio.asset_balances.items()
                },
                "positions": dict(portfolio.positions),
                "position_costs": {
                    key: {
                        "average_entry_price": value.average_entry_price,
                        "cost_source": value.cost_source,
                        "cost_is_reliable": value.cost_is_reliable,
                    }
                    for key, value in portfolio.position_costs.items()
                },
                "mark_price": snapshot.mark_price,
                "open_order_count": len(orders),
            },
        )
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(
        json.dumps(
            {
                "status": "ok",
                "quote_currency": instrument.quote_currency,
                "quote_cash_balance": str(portfolio.cash_balance(instrument.quote_currency)),
                "quote_available_balance": str(
                    portfolio.available_balance(instrument.quote_currency)
                ),
                "quote_frozen_balance": str(portfolio.frozen_balance(instrument.quote_currency)),
                "base_currency": instrument.base_currency,
                "base_total_quantity": str(portfolio.position(instrument.instrument_id)),
                "base_available_quantity": str(
                    portfolio.available_position(instrument.instrument_id, instrument.base_currency)
                ),
                "base_frozen_quantity": str(portfolio.frozen_balance(instrument.base_currency)),
                "cost_source": portfolio.position_cost(instrument.instrument_id).cost_source,
                "cost_is_reliable": portfolio.position_cost(
                    instrument.instrument_id
                ).cost_is_reliable,
                "open_order_count": len(orders),
            },
            ensure_ascii=False,
        )
    )


@app.command("audit-account-model")
def audit_account_model(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """只读核验 OKX 账户字段合同；不提交、修改或撤销订单。"""
    config, _, repository, client = _demo("audit-account-model", config_path)
    run_id = uuid4().hex
    try:
        instrument = client.get_instrument(config.market.instrument_id)
        snapshot = PrivateStateCoordinator.for_private_account(
            client, repository, SystemClock()
        ).synchronize_private_account(
            instrument,
            config.market.bar,
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            source="audit_account_model",
        )
        portfolio = snapshot.portfolio
        configuration = portfolio.account_configuration
        assets = {
            currency: {
                "cash_balance": str(asset.cash_balance) if asset.cash_balance is not None else None,
                "available_balance": (
                    str(asset.available_balance) if asset.available_balance is not None else None
                ),
                "frozen_balance": (
                    str(asset.frozen_balance) if asset.frozen_balance is not None else None
                ),
                "equity": str(asset.equity) if asset.equity is not None else None,
                "holding_quantity": (
                    str(asset.holding_quantity) if asset.holding_quantity is not None else None
                ),
                "spendable_quantity": (
                    str(asset.spendable_quantity) if asset.spendable_quantity is not None else None
                ),
                "source": asset.source.value,
                "fetched_at": asset.fetched_at.isoformat(),
                "raw_fields": sorted(asset.raw_field_presence),
                "validation_status": asset.validation_status.value,
            }
            for currency, asset in portfolio.asset_balances.items()
        }
        result = {
            "account_configuration": {
                "account_mode": configuration.account_mode.value if configuration else "unknown",
                "position_mode": configuration.position_mode if configuration else None,
                "auto_loan_enabled": configuration.auto_loan_enabled if configuration else None,
            },
            "field_contract_version": "okx-v5-2026-07",
            "assets": assets,
            "spot_holding_reliable": portfolio.trusted_for_trading,
            "open_order_count": len(snapshot.open_orders),
            "cost_reliable": portfolio.position_cost(instrument.instrument_id).cost_is_reliable,
            "invariants": {
                "available_plus_frozen_equals_cash": "not_assumed",
                "spot_required_fields": (
                    "passed"
                    if portfolio.trusted_for_trading
                    else "insufficient_data_or_unsupported_mode"
                ),
            },
            "order_submission_allowed": False,
            "reason": "此阶段审计命令固定禁止订单提交",
        }
        repository.save_audit_record(
            record_type="account_model_audit",
            run_id=run_id,
            mode=config.mode.value,
            strategy_name=config.strategy.name,
            instrument_id=instrument.instrument_id,
            bar=config.market.bar,
            payload=result,
        )
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()
    typer.echo(json.dumps(result, ensure_ascii=False, default=str))


@app.command("run-demo")
def run_demo(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    strategy_name: Annotated[str | None, typer.Option("--strategy")] = None,
    instrument_id: Annotated[str | None, typer.Option("--instrument")] = None,
    bar: Annotated[str | None, typer.Option()] = None,
) -> None:
    """用模拟账户运行一次策略评估；此命令绝不提交订单。"""
    config, settings = _load("run-demo", config_path, _overrides(strategy_name, instrument_id, bar))
    try:
        result = build_demo_evaluation_session(config, settings).run()
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "submitted_order": result.submitted_order,
                "strategy": config.strategy.name,
                "instrument": config.market.instrument_id,
                "signals": [
                    {
                        "action": evaluation.signal.action.value,
                        "reason": evaluation.signal.reason,
                        "risk_allowed": evaluation.risk_decision.allowed
                        if evaluation.risk_decision is not None
                        else None,
                    }
                    for evaluation in result.evaluations
                ],
            },
            ensure_ascii=False,
        )
    )


@app.command("observe-demo")
def observe_demo(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    strategy_name: Annotated[str | None, typer.Option("--strategy")] = None,
    instrument_id: Annotated[str | None, typer.Option("--instrument")] = None,
    bar: Annotated[str | None, typer.Option()] = None,
    max_events: Annotated[int, typer.Option(min=1, max=100)] = 1,
    timeout_seconds: Annotated[float, typer.Option(min=1, max=3600)] = 30,
) -> None:
    """通过公共 WebSocket 观察已收盘 K 线；结构上禁止提交订单。"""
    config, settings = _load(
        "observe-demo",
        config_path,
        _overrides(strategy_name, instrument_id, bar),
    )
    try:
        result = build_public_observe_session(
            config,
            settings,
            max_events=max_events,
            timeout_seconds=timeout_seconds,
        ).run()
    except (ExchangeError, StorageError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "submitted_order": result.submitted_order,
                "confirmed_candle_count": result.confirmed_candle_count,
                "timed_out": result.timed_out,
                "connected": result.connected,
                "connection_state": result.connection_state,
                "reconnect_count": result.reconnect_count,
                "last_error": result.last_error,
                "evaluations": [
                    {
                        "action": item.signal.action.value,
                        "reason": item.signal.reason,
                        "risk_allowed": item.risk_decision.allowed
                        if item.risk_decision is not None
                        else None,
                    }
                    for item in result.evaluations
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("place-demo-test-order")
def place_demo_test_order(
    side: Annotated[OrderSide, typer.Option()],
    price: Annotated[str, typer.Option()],
    config_path: Annotated[Path, typer.Option("--config")],
    confirm_demo_order: Annotated[
        bool,
        typer.Option(
            "--confirm-demo-order",
            help="明确确认向 OKX 模拟盘提交订单",
        ),
    ] = False,
) -> None:
    """Retired one-step command retained only to fail closed for old operator scripts."""
    if not confirm_demo_order:
        _fail("必须显式提供 --confirm-demo-order")
    _fail(
        "place-demo-test-order is retired; use prepare-demo-order then submit-demo-order "
        "with a persisted Proposal"
    )


@app.command("check-demo-private-state")
def check_demo_private_state(
    config_path: Annotated[Path, typer.Option("--config")],
    timeout_seconds: Annotated[float, typer.Option(min=1, max=30)] = 15,
) -> None:
    """Bounded private WebSocket readiness and REST reconciliation check."""
    config, settings, repository, client = _demo("check-demo-private-state", config_path)
    session = DemoTradingSession(config, settings, client, repository)
    try:
        started = session.start(timeout_seconds=timeout_seconds)
        status = session.reconcile()
    except (ExchangeError, StorageError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    finally:
        session.close()
        client.close()
    typer.echo(
        json.dumps(
            {
                "status": "ok",
                "instrument": started.instrument.instrument_id,
                "private_websocket_ready": True,
                "initial_reconciliation": started.reconciliation_status.value,
                "final_reconciliation": status.value,
                "submitted_order": False,
            },
            ensure_ascii=False,
        )
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


def _order_summary(order: object) -> dict[str, object]:
    from app.domain.order import Order

    if not isinstance(order, Order):
        raise TypeError("订单类型错误")
    return {
        "client_order_id": order.request.client_order_id,
        "exchange_order_id": order.exchange_order_id,
        "state": order.state.value,
        "filled_quantity": str(order.filled_quantity),
        "average_price": str(order.average_price) if order.average_price is not None else None,
    }


if __name__ == "__main__":
    app()
