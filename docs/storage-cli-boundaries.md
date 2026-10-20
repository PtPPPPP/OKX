# 存储与 CLI 职责边界

本轮只在 `OKX-github-upload-20261016` 隔离 Git 副本开发，保留前序未提交的策略契约工作。
原研究目录、冻结源码、数据、报告和样本外消费记录不参与修改。数据库 schema、迁移版本、
金额口径、策略、交易参数和授权条件不变；没有新增依赖。

## 职责与调用关系

原来所有存储实现都位于 `app/storage/repositories.py:TradingRepository`；现在按业务所有者委托：

| 原职责 | 当前实现 | 主要调用方与边界 |
| --- | --- | --- |
| 订单、状态历史、成交去重和成本成交查询 | `storage/orders.py:OrderRepository` | Runner、受控写服务、对账与私有事件处理；保留状态回退检查、孤立成交隔离、稳定成交 ID |
| 账户快照、成本字段、日内风险值、策略所属库存 | `storage/accounts.py:AccountRepository` | 账户同步、风控、持续运行和私有事件处理；账户持仓不等于可卖的策略库存 |
| 提案、复核审计、提交槽位、一次性授权、未知订单恢复 | `storage/proposals.py:ProposalRepository` | Preflight/Revalidator、受控提交应用服务、ControlledDemoWriteService、UnknownOrderRecoveryService；跨表提交操作仍由该所有者负责 |
| 私有暂态、事件幂等、连接 epoch/水位、对账和冻结门禁 | `storage/private_state.py:PrivateStateRepository` | 继续经 `PrivateStateCoordinator` 及其内部 Processor/Reconciler 推进；拆分没有增加业务层直接写入口 |
| 信号、风控审计、运行清单、数据与规则快照、系统事件和通用事件去重 | `storage/audit.py:AuditRepository` | TradingSession、EventBus、Runner、数据与研究入口；bootstrap 中两个持久化 EventBus 已直接注入 `repository.audit` |

`TradingRepository` 保留原方法名、注解、位置/关键字参数及默认值，显式委托，没有 SQL、连接或提交逻辑。
`database` 属性以及 `canonical_fill_id`、`PrivateStateFenceDeferred` 的原导入位置仍然可用。
新代码可使用 `repository.orders/accounts/proposals/audit`；私有状态仍服从协调器单写规则。
辅助序列化集中到无 I/O 的 `_codec.py`。没有多重继承、动态 `__getattr__` 或通用 Repository 基类。

## 事务所有权

各实现共享同一个 `Database` 对象，**这本身不代表共享一个事务**。`Database.connect()`
仍是唯一普通连接/提交/回滚/关闭所有者，使用原来的 SQLite FULL、外键与 WAL 配置。
每个原子业务方法完整迁入同一个模块；方法内部只打开一次连接，所有 SQL 使用该连接。
跨表写入保留在事务所有者内部，不通过另一个会自动提交的 Repository 方法完成。

| 事务方法 | 必须一起提交的内容 |
| --- | --- |
| `save_demo_order_proposal` / `transition_demo_order_proposal` | 提案内容或状态与对应提案事件 |
| `fence_demo_order_proposal` | 读取健康私有状态并绑定版本、记录 fence 事件；原 `BEGIN IMMEDIATE` 保留 |
| `begin_controlled_demo_submission` | 再检查 fence/预算、消费提案、建立本地父订单、提案审计、保留提交槽位；原 `BEGIN IMMEDIATE` 保留 |
| `authorize_controlled_demo_write` | 检查提案/订单/健康状态及已签发事件，原子记录一次性授权；原 `BEGIN IMMEDIATE` 保留 |
| `complete_controlled_demo_submission` | 订单结果、提案结果与提交事件 |
| `mark_controlled_demo_submission_unknown` | 订单 unknown、提案 unknown、审计及私有门禁冻结 |
| `resolve_unknown_order_from_authoritative_read` | 权威订单状态、提案关联、未决数量/门禁、恢复审计；恢复后仍需对账 |
| `apply_private_state_event` | 幂等标记、较新的暂态快照、门禁版本、系统审计 |
| `confirm_private_state_snapshots` | 截止水位内的快照确认与门禁状态 |
| `save_order` / `apply_managed_fill` | 订单与状态历史 / 单个策略库存的创建和更新 |

`save_fill`、通用 `claim_event`、独立审计、运行/规则/数据清单可以按原接口独立持久化。
**独立持久化不代表可以任意组合成一个原子业务操作**。未来如果一个操作需要多个职责共同提交，
应由明确的业务事务所有者传递同一连接；不能顺序调用多个自动提交的方法并声称具有原子性。
本轮没有引入跨 Repository 新事务，也没有改变原提交顺序。

数据库原本对 SQLite 异常显式 rollback 并 safe-stop；其他异常由 finally 中的连接关闭回滚未提交事务。
这两条路径均做故障注入验证，未调整 `database.py`。仅离线 Shadow replay 可以使用 NORMAL
耐久性专用连接，授权、账户、订单和对账继续使用 FULL。

## CLI 与资源

本轮提取 `submit-demo-order`、`revalidate-demo-order-proposal` 两个命令的业务流程：

- CLI：解析和显式确认参数、加载配置、调用服务、保持 JSON/错误文本和退出码。
- `services/demo_proposal_application.py:DemoProposalApplication`：复核、预留提交槽位、调用受控写服务、
  分类保存拒绝/未知/成功结果、要求提交后对账；不创建/关闭资源，不依赖 Typer。
- `bootstrap.open_demo_proposal_application`：取得传入 client 的关闭责任，用 `ExitStack`
  创建并启动会话，退出时先关闭 session、再关闭 client。构造或启动失败、中断、业务异常及
  session.close 异常时仍关闭 client；CLI 不再重复关闭。同步提交和原异步复核的运行方式保持不变。

复核仍经过原 Revalidator；写能力仍只由 `ControlledDemoWriteService` 签发。应用服务通过
session 协调入口对账，不能直接归并或解冻私有状态。网络结果不明确仍冻结为 unknown，
不重试、不替换订单、不退还预算；恢复入口继续只读。

明确修复的资源生命周期缺口：这两个命令过去在 try/finally 之前构造 session，构造失败会漏关
client；过去 session.close 抛异常也会跳过 client.close。此次收敛资源所有者直接覆盖这两种情况，
不改变交易决策。其他命令的资源管理未统一迁移。

## 既有边界与后续范围

以下是代码梳理发现的既有行为，不能把本轮测试理解为它们已经解决：

- `PrivateEventProcessor` 的 ORDER 路径依次调用 `save_order`、`save_fill`、`apply_managed_fill`、
  `claim_event`；这些原来就是独立事务。整条链路不具备崩溃时的全量原子性，成交已保存后库存更新失败
  可能留下阶段性不一致。此问题需要单独设计事件记账事务/恢复规则，不能借结构调整静默改变。
  已在重构前后分别用临时库注入库存更新异常并重放事件，两者均为成交 1 条、库存 0 条，
  重放未补记库存；证据为 `preexisting_transaction_gap.json`，没有修改现有业务代码。
- `AccountSync.sync` 的 REST 账户快照和逐条挂单也按原方法分别提交；协调器门禁仍必须完成对账
  才放行。本轮没有声称远端读取和本地批次是单个数据库事务。
- `apply_managed_fill` 本身不提供独立成交 ID 去重；现有调用方依赖 `save_fill` 的插入结果控制调用。
  保留此约定，不新增自动重试或授权能力。

兼容门面仍用于多职责服务和多数原调用点；本轮没有重写所有 CLI、持续 Shadow 存储或数据库迁移工具。
后续优先独立处理私有订单事件的跨阶段记账恢复，再按调用需求迁移其余命令。

## 本地验证

新增 `test_repository_transactions.py`、`test_demo_proposal_application.py`、`test_storage_boundaries.py`：
9 类事务的 26 个写入阶段分别注入 RuntimeError 与 SQLite 错误，比较失败前后完整数据库 dump，
验证单连接、全部回滚及连接关闭；另覆盖跨兼容入口去重、CLI JSON/退出码、授权门禁和资源异常路径。

对照重构前文件的 56 个方法 AST 和签名，均相同；独立离线比较 43 组 CLI 帮助输出、8 种提案命令
输出/退出码一致。对照材料和结果保存在忽略目录 `artifacts/p1_storage_cli_20261020/`，不进入冻结研究。
只在本机 Windows/Python 环境执行，没有连接真实或模拟账户、发送远端订单、迁移已有数据库或测试跨平台运行。

2026-10-20 实际结果：全量 **971 passed**，耗时 455.44 秒，无失败或跳过；本轮新增 73 个用例。
Ruff check、Ruff format check（315 文件）、Mypy（196 源文件）及 Git diff 空白检查通过。
原目录 2,324 个受保护文件、前序策略契约的 11 个独立文件哈希未变；架构文档在既有内容之后追加本轮说明。
P0 历史快照 verify 通过，快照哈希仍为
`95d363a627960a78ca6ad07acd0c2482ecde3fa77682505a877183c459c49368`。
本次没有执行 Git commit、push、发布或部署。
