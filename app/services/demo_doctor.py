from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from app.config.run_config import RunConfig
from app.config.settings import Settings, TradingMode
from app.exchange.exceptions import ExchangeError
from app.exchange.okx_client import OkxClient
from app.runtime.clock import SystemClock
from app.services.private_state_coordinator import PrivateStateCoordinator
from app.storage.database import Database, StorageError
from app.storage.migrations import MigrationManager
from app.storage.repositories import TradingRepository


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        if any(check.status is CheckStatus.FAIL for check in self.checks):
            return 1
        if any(check.status is CheckStatus.BLOCKED for check in self.checks):
            return 2
        return 0

    @property
    def order_submission_allowed(self) -> bool:
        return any(
            check.name == "order_allowed" and check.status is CheckStatus.PASS
            for check in self.checks
        )


class DemoDoctor:
    def __init__(self, config: RunConfig, settings: Settings) -> None:
        self.config = config
        self.settings = settings

    def run(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        checks.append(
            DoctorCheck(
                "local_config",
                CheckStatus.PASS if self.config.mode is TradingMode.DEMO else CheckStatus.FAIL,
                "mode=demo" if self.config.mode is TradingMode.DEMO else "配置不是 demo",
            )
        )
        checks.append(
            DoctorCheck(
                "live_disabled",
                CheckStatus.PASS if not self.settings.allow_live_trading else CheckStatus.FAIL,
                "实盘开关关闭",
            )
        )
        checks.append(
            DoctorCheck(
                "simulated_header",
                CheckStatus.PASS if self.config.exchange.simulated else CheckStatus.FAIL,
                "配置要求模拟交易",
            )
        )
        database = Database(self.settings.database_url)
        db_status = MigrationManager(database.path).status()
        db_ok = db_status.compatible and not db_status.pending
        checks.append(
            DoctorCheck(
                "database",
                CheckStatus.PASS if db_ok else CheckStatus.FAIL,
                "数据库迁移已是最新版" if db_ok else "数据库迁移未就绪",
            )
        )
        client = OkxClient(self.settings)
        instrument = None
        try:
            client.get_server_time()
            checks.append(DoctorCheck("public_api", CheckStatus.PASS, "公共时间接口可用"))
        except ExchangeError as exc:
            checks.append(DoctorCheck("public_api", CheckStatus.FAIL, str(exc)))
        try:
            instrument = client.get_instrument(self.config.market.instrument_id)
            checks.append(DoctorCheck("instrument", CheckStatus.PASS, "现货规则可用且品种可交易"))
        except (ExchangeError, ValueError) as exc:
            checks.append(DoctorCheck("instrument", CheckStatus.FAIL, str(exc)))
        credentials = self.settings.demo_credentials_configured
        checks.append(
            DoctorCheck(
                "credentials",
                CheckStatus.PASS if credentials else CheckStatus.BLOCKED,
                "模拟盘凭证已配置" if credentials else "缺少模拟盘凭证",
            )
        )
        prerequisites = (
            db_ok
            and instrument is not None
            and credentials
            and self.config.mode is TradingMode.DEMO
        )
        if not prerequisites:
            for name in ("private_api", "account_sync", "open_orders", "recovery"):
                checks.append(DoctorCheck(name, CheckStatus.SKIPPED, "前置检查未通过"))
            checks.append(DoctorCheck("order_allowed", CheckStatus.BLOCKED, "禁止提交模拟盘订单"))
            client.close()
            return DoctorReport(tuple(checks))
        if instrument is None:
            raise RuntimeError("诊断内部状态错误：品种规则缺失")
        repository = TradingRepository(database)
        try:
            coordinator = PrivateStateCoordinator.for_private_account(
                client, repository, SystemClock()
            )
            snapshot = coordinator.synchronize_private_account(
                instrument,
                self.config.market.bar,
                run_id=uuid4().hex,
                mode=self.config.mode.value,
                strategy_name=self.config.strategy.name,
                source="demo_doctor",
            )
            checks.append(DoctorCheck("private_api", CheckStatus.PASS, "模拟账户鉴权成功"))
            checks.append(DoctorCheck("account_sync", CheckStatus.PASS, "账户快照已保存"))
            checks.append(
                DoctorCheck(
                    "open_orders",
                    CheckStatus.PASS,
                    f"已同步 {len(snapshot.open_orders)} 笔挂单",
                )
            )
            reconciliation = coordinator.reconcile_private_state(instrument, source="demo_doctor")
            account_model_safe = snapshot.portfolio.trusted_for_trading
            checks.append(
                DoctorCheck(
                    "recovery",
                    CheckStatus.PASS
                    if reconciliation.order_submission_allowed and account_model_safe
                    else CheckStatus.BLOCKED,
                    reconciliation.message
                    if account_model_safe
                    else "账户模式或持仓字段不支持当前现货自动交易",
                )
            )
            bounded = self.config.risk.max_order_notional <= Decimal("5") and Decimal(
                str(self.config.position_sizing.parameters["order_notional"])
            ) <= Decimal("5")
            position = snapshot.portfolio.position(instrument.instrument_id)
            cost_reliable = snapshot.portfolio.position_cost(
                instrument.instrument_id
            ).cost_is_reliable
            cost_safe = position <= 0 or cost_reliable
            allowed = (
                reconciliation.order_submission_allowed
                and account_model_safe
                and bounded
                and cost_safe
            )
            checks.append(
                DoctorCheck(
                    "order_allowed",
                    CheckStatus.PASS if allowed else CheckStatus.BLOCKED,
                    "对账健康、成本可靠且单笔不超过 5 个计价币单位"
                    if allowed
                    else "账户模型、对账、持仓成本或金额上限未通过",
                )
            )
        except (ExchangeError, StorageError, ValueError) as exc:
            checks.append(DoctorCheck("private_api", CheckStatus.FAIL, str(exc)))
            for name in ("account_sync", "open_orders", "recovery"):
                checks.append(DoctorCheck(name, CheckStatus.SKIPPED, "私有接口失败"))
            checks.append(DoctorCheck("order_allowed", CheckStatus.BLOCKED, "禁止提交模拟盘订单"))
        finally:
            client.close()
        return DoctorReport(tuple(checks))
