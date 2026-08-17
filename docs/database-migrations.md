# 数据库迁移

## 原则

- 结构变化只能通过有序 Migration；
- 每项记录版本、名称、SHA-256 校验和、执行时间和结果；
- 旧库普通启动不自动修改，必须显式运行 `db-migrate`；
- 旧库升级前使用 SQLite 在线备份生成一致快照；
- 每项迁移在 `BEGIN IMMEDIATE` 事务内运行；
- 失败回滚业务结构，再记录 `failed`；
- 已应用迁移校验和变化或未知高版本会被判为不兼容并拒绝运行；
- 旧表不删除。

## 命令

### 受控正式迁移（两阶段，fail-closed）

```powershell
# 1) 只读查看状态与迁移计划（不修改数据库、不产生授权）
.venv\Scripts\python.exe -m app.cli db-status
.venv\Scripts\python.exe -m app.cli db-migrate-plan --output plan.json

# 2) 显式授权后执行（无授权一律 BLOCKED）
.venv\Scripts\python.exe -m app.cli db-migrate-authorized --plan plan.json --operator-confirmation-id <审批标识>
```

`db-migrate-plan` 输出数据库路径、sha256、当前/目标 schema 版本、待执行迁移、兼容性状态、备份要求、风险摘要和 `plan_sha256`，可选写入计划文件。它绝不修改数据库。

`db-migrate-authorized` 在执行前重新对现场数据库验证计划绑定（TOCTOU 防护）：数据库路径、字节哈希、起始版本、目标版本、`plan_sha256` 任一不一致即 BLOCKED 并要求重新生成计划。通过验证后依次执行：正式库 verified backup（失败即阻止迁移）→ 恢复演练 → 副本上的完整迁移演练（事务回滚/故障恢复/硬中断恢复/幂等重放全部真实执行，任一失败即阻止）→ 预检门禁 → 一次性执行授权 → 真实迁移。每次尝试都会向 `<数据库目录>/migration_audit.jsonl` 追加审计记录（时间、数据库身份、版本、迁移集合、授权标识、结果、失败原因），不记录任何凭证。

无待执行迁移时（`v23 → target v23`）授权命令返回 `NO_OP`：成功退出、不改 schema、不消费授权。

以下情况一律 BLOCKED，不会自动修复：数据库版本高于应用程序（future schema）、迁移历史/校验和不兼容、schema 元数据缺失或损坏、备份或恢复演练失败、检测到仍在运行的 continuous/shadow 任务。不存在 `--force`/`--yes`/`--unsafe` 类逃生口。

### 数据库副本（非正式库）

正式库写迁移不接受裸 CLI 调用。数据库副本可通过显式 `DATABASE_URL` 使用原流程：

```powershell
$env:DATABASE_URL = "sqlite:///data/copy.db"
.venv\Scripts\python.exe -m app.cli db-migrate --target-version 23
.venv\Scripts\python.exe -m app.cli db-backup --output data/backups/manual.db
```

## v0.2 新结构

- `schema_migrations`：迁移历史；
- `runs`：应用版本、Git、配置、数据和交易规则哈希；
- `instrument_snapshots`：交易规则快照；
- `dataset_snapshots`：回测数据集快照；
- `processed_events`：持久化幂等键；
- `legacy_tables`：不再使用但必须保留的旧表标记；
- `orders`：增加运行和策略维度；
- `fills`：增加本地与交易所成交 ID。

旧 `account_snapshots`、`position_snapshots` 如果存在，只写入 `legacy_tables` 标记，不改名、不删除、不清空。

## v0.3 模拟订单闭环结构

- `orders.order_source`：区分回测、策略模拟、人工测试、保护性退出和对账；
- `order_state_changes`：每次状态变化保存运行、模式、策略、品种、周期、信号和来源；
- `fills.fee_currency`：用于按手续费币种恢复成本；
- `portfolio_snapshots.asset_balances_json`：保存总额、可用、冻结和权益；
- `portfolio_snapshots.position_costs_json`：保存平均成本、来源和可靠性；
- `private_state_snapshots`：保存 WebSocket 暂态，等待 REST 确认。
