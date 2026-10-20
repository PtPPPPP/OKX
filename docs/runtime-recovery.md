# 持续运行：状态、事件与受控恢复

本轮继续在隔离 Git 副本开发，保留 P0 研究快照、前序策略契约和存储/CLI 重构。
没有改写原冻结目录、历史数据、报告和样本外消费记录，也没有新增依赖、表或数据库迁移。
运行观测不参与下单授权，所有状态 JSON 都带 `trading_permission=false`。

## 统一只读入口

```powershell
python -m app.main runtime-status --database artifacts/p2_runtime_20261020/demo/runtime.db --run-id <run_id>
```

命令只用 SQLite `mode=ro` 和只读事务读取一个一致快照，不加载 Settings、凭证或网络，不创建数据库、
迁移、续租、解冻或启动任务。查询成功返回 JSON/退出码 0；数据库缺失、schema 不兼容、证据格式损坏
返回 `health=unknown`/退出码 2。未知 run ID 同样明确失败。旧命令参数和输出保留。

新状态格式为 `format_version=1`，复用 `continuous_demo_run_events` 中的一条可更新观测记录：

| 字段 | 含义 |
| --- | --- |
| run_id、mode、strategy_name/version | 运行身份；旧记录无法证明版本时显示 unknown |
| lifecycle / legacy_run_status | 观测生命周期 / 原运行表状态，保留两者便于审计 |
| heartbeat_at、process_heartbeat_fresh | 最近持久化心跳；默认 30 秒以内视为新鲜，不能证明 OS 进程仍活着 |
| last_market_received_at、market_arriving | 最近收到行情的本地 UTC 时间；默认 120 秒，WS pong 不算行情 |
| last_confirmed_processed_at、business_progressing | 最近成功处理并保存确认 K 线的本地时间；1H 默认 7,320 秒 |
| public/private_connection、reconnects | 连接状态和成功重连次数（已建立连接数减一）；失败尝试仍由 Provider 自身预算统计。未创建私有连接为 not_applicable，未证明的信息为 unknown/null |
| latest_reconciliation、account_trusted | 私有检查路径还要求新鲜心跳、已确认私有连接、60 秒内健康对账；公共 Shadow 不声明账户可信 |
| last_persisted_checkpoint | 直接来自业务运行表的版本、K 线开盘时间、状态哈希和更新时间 |
| blocking_reasons、stop_reason、recovery_actions | 阻塞证据、停止原因及本地操作建议 |

K 线的时间戳仍为开盘时间；接收/处理时间为本地观测时刻，两者不能混用。
连接字段是最后一次持久化观测，不是查询时的在线探测；停止后保留的 connected 不能解释为当前仍连接。
旧 Smoke 命令的 `observation_outcome` 也不能替代新版业务健康判断，应以 `runtime-status` 的三类证据为准。
停止运行的历史心跳不代表当前还有进程。只有心跳、行情和业务进度都有新鲜证据，且无阻塞或未解决告警，
才报告 healthy。旧运行没有新版证据时报告 unknown；查询不会把它“补成”健康状态。

`ContinuousVWAPShadowRunner.run_events` 在等待行情时每 10 秒续租、保存观测并独立检查行情和业务停滞。
正常重复/旧行情不会推进业务；持续收到未确认行情也不能掩盖确认 K 线处理停滞。
`ContinuousDemoEngine` 使用相同模型，保留其私有协调与熔断门禁；离线 Replay 按真实墙钟约每 10 秒
更新观测，在结束时保存最后进度，业务检查点仍逐根提交。有限 candle-only 兼容回放不是一个新常驻服务。

## 事件可靠性和保留

| 记录 | 保存和保留规则 | 写入失败 |
| --- | --- | --- |
| 订单、成交、一次性授权、账户与协调门禁 | 原 FULL 事务与原所有者；没有改成异步队列 | 保持原 fail closed，不重发未知订单 |
| 持续 EventBus 的完整领域事件 | publish 返回前，将完整事件和幂等键在同一个 FULL 事务保存；绑定运行 mode/bar | 异常传给调用方，不消费幂等键、不发布未落盘事件 |
| VWAP 信号、Shadow 提案、检查点、确认 K 线身份 | 原同一业务事务；恢复再核验哈希、版本、数量和检查点边界 | 全部回滚，当前 Runner 阻塞，使用新实例显式恢复 |
| 生命周期和恢复事件 | 发生状态变化/故障恢复时保存，保留审计链 | 停止业务；无法落盘时保留错误日志，后续查询只能依赖最后成功记录 |
| 最新运行观测 | 每个 run 一条可更新记录 | 不以陈旧状态伪装健康 |
| 本地告警 | 11 个固定类别，每个 run/类别一条；重复更新次数、首次/末次时间，恢复计数及事件 | 异常上传监督路径，不静默丢弃 |
| 内存和文本诊断 | 持续 EventBus 事件尾部与去重缓存各最多 256；Demo MA 收盘缓存最多 max(warmup,35)，VWAP 26 根 | 只有可靠保存后才能淘汰内存尾部 |
| 轮转日志 | 既有 3 类日志，每类当前文件及 3 份备份，每份最大 5 MiB；重复配置关闭旧文件句柄 | 文本日志不作为订单/授权的唯一证据 |

完整关键审计、信号、提案、生命周期及恢复记录**不自动删除**，数据库会随业务增长。
需要由操作人员监控磁盘；对已停止、无未决订单的运行先使用现有备份/恢复校验流程归档。
本轮没有新增自动清理命令，不删除在线关键记录来控制容量。故障反复出现会增加同一告警的计数，
不会为每次相同检测新增告警行；每次实际恢复单独保留恢复事件。相同已冻结熔断原因只记录一次。

有限回测仍可使用默认 store-less EventBus 和结束时批量审计，内存随有限输入增长；这不适合常驻路径。
持续模式必须显式启用可靠存储及保留上限；没有 append_event 能力时拒绝有界模式。
旧 processed_events 只证明幂等键，不证明完整审计存在，不能冒充新版可靠事件；不重写旧哈希或补造历史事件。
事件持久化与之前的订单事务仍是不同业务边界，不声称是一个端到端事务。

离线 Shadow Replay 继续使用原 NORMAL 耐久性作用域连接。这些是可从校验输入重建的数据，系统级故障
可能丢失末尾已确认提交；不用于账户、授权或资金记录。本轮保留其连接数与逐根提交性能约束。

## 故障处置

| 情况 | 系统动作 | 恢复条件 |
| --- | --- | --- |
| 公共断线 | stale -> reconciling，拒绝提前输入新代际 K 线 | 有界重连后从持久化检查点恢复，验证历史并完整补齐缺口才 ready |
| 重复、乱序、旧代际行情 | 不生成重复业务记录；确认前输入不产生交易 | 后续按连续的确认行情推进 |
| 心跳在但行情/进度停滞 | 本地告警、blocked，停止业务推进 | 检查源/任务后显式新建 Runner，校验并恢复原检查点 |
| 检查点哈希/版本/历史 OHLCV 不符、业务记录缺失 | 拒绝恢复，保留原记录 | 人工审计和修复证据；不能自动清空状态或更换策略继续 |
| 私有流或对账异常、账户变化 | 原协调器与熔断器冻结 | 按既有协调器只读查询和对账处理，观测模块没有解冻权 |
| 未知订单 | 状态查询给出 query_original_client_order_id...never_resubmit | 沿用原 recover-demo-order 与对账流程；不通过重新发送恢复 |
| 数据库失败 | 传播异常并停止；尽力保存故障证据和释放租约 | 修复存储后核对最后提交；数据库本身不可写时不可能保证故障事件也写入 |
| 子任务异常、超时、取消、意外正常结束 | 监督者得到失败，取消并等待兄弟任务；即使没有新行情，主循环也会结束 | 现有任务 restart_policy=never，必须人工检查后重启 |
| 原进程异常消失、租约未释放 | 拒绝直接覆盖，包括相同 run ID | 现有 recover-vwap-continuous-shadow 检查租约过期、原 owner 不存在、DB 完整性及无订单/私有活动，再登记中断恢复 |

公共连接保留原指数退避（上限 60 秒）和连续失败预算（默认 20），新增 Provider 生命周期总重连预算
（默认 100）。成功订阅不再能规避总预算。任何预算耗尽都停止；没有后台自动重启或无限 worker 重试。

受控恢复示例（以下公共行情命令本轮没有在线执行）：

```powershell
python -m app.main runtime-status --database <db> --run-id <id>
python -m app.main recover-vwap-continuous-shadow --database <db> --run-id <id>
python -m app.main run-vwap-continuous-shadow --database <db> --resume-run-id <id> --config configs/btc_vwap_shadow.yaml
```

恢复不改变 run ID、参数或数据区间。启动和重连会核对检查点完整滚动窗口的行情内容，当前公共补数
查询最多 300 根；数据源不能提供所需窗口/连续缺口时拒绝，不保证任意时长断线都能自动恢复。
纯公共 Shadow 检查点若关联订单或成交，同样拒绝套用该恢复流程。

停止会取消并等待待接收行情、监督任务和重连对账任务，再释放资源。`to_thread` 取消不会停止线程，
因此对账线程被 shield 并等待结束，结束前不释放租约；停止标记阻止其继续逐根写入。
Provider 自身必须提供有界 I/O 超时，Python 无法强制终止一个永不返回的外部线程。

## 可复现的本地演示

```powershell
python -m scripts.demo_runtime_recovery --output artifacts/p2_runtime_demo_new
```

输出目录必须不存在，避免覆盖原记录。脚本只生成确定性合成行情、使用虚拟时钟和本地 FaultPlan：
正常处理 -> 在检查点事务 after_runtime 注入 StorageError -> 检查回滚与 blocked 状态 ->
新 Runner 显式恢复同一 run -> 与不中断参考运行逐项比较信号、提案和状态。
`demonstration.json` 保存三阶段 JSON 及数据哈希。实际演示处理 31 根业务 K 线，网络和 Broker 写入均为零；
这不是实际运行了对应小时或天数。运行 ID、审计 ID、接收/处理墙钟及观测时间允许变化，业务结果不因此改变。

## 验证边界

新增测试覆盖心跳/行情/处理分离、重复/缺口/重连、检查点损坏拒绝、告警合并、同 run 租约保护、
未知订单不重发、子任务取消/超时/异常、启动失败清理、原子事件写入及写入中进程退出。
加速事件测试保存 1,500 个完整关键事件，而内存事件和去重缓存各保持 32 个测试上限；不是 1,500 天实测。

完整恢复适配优先完成只读 VWAP；V3 显式逐根契约、bounded Demo 和其他旧运行尚未统一到此检查点恢复入口。
前序发现的 PrivateEventProcessor 成交与库存跨阶段事务缺口没有被本轮隐藏或改写，仍须单独处理。
本轮未连接私有账户、下单、迁移既有数据库、创建常驻服务、推送 Git 或调用外部通知渠道。
尚需真实长时间观察网络波动、时钟偏差、磁盘容量/故障和资源稳定性；测试通过不代表已具备生产运行能力。

租约只约束同一个 SQLite 文件，复制数据库不会提供跨机器的全局锁；本地主机 PID 检查不能证明异地主机
进程已退出。共享盘、异地主机的 owner 判定和自动恢复不在本轮验证范围，操作前仍需人工核对。

## 2026-10-20 实际验收

- Windows / Python 3.12.14：全量 **1,011 passed**，579.18 秒；相对前序 971 项新增 40 项。
- Ruff check、Ruff format check（320 文件）、Mypy strict（198 源文件）及 Git diff 空白检查通过。
- 首轮完整回归为 1,005 passed / 2 failed；两项失败是旧耐久性黄金计数。新增起止观测明确增加
  2 次运行级提交、4 次 SQL 写入，两种模式均为 8 个连接、127 次提交、625 次写入；连接上限和逐根业务约束未放宽。
- 最终源码演示目录：`artifacts/p2_runtime_20261020/demo_complete/`。健康状态为
  healthy -> blocked -> healthy，31 根业务 K 线，信号/Shadow 提案/检查点与不中断参考运行一致；网络和订单写入均为 0。
- 原目录 2,324 个受保护文件未变；前序存储拆分的 56 个方法体、策略源码、参数文件、依赖锁与正式迁移实现保留。
- 完整日志、状态查询与保护性核对证据位于 `artifacts/p2_runtime_20261020/`，没有进入原冻结研究目录。
