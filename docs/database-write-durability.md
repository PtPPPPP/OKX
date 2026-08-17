# 数据库写入持久性边界

本项目按“崩溃后能否无歧义恢复”决定写入时机，不按表名或写入频率决定。

必须立即持久化并与同一根业务事件原子提交：

- 已处理 K 线的幂等标识、策略运行时检查点、对应信号；
- Shadow Proposal 及其阻断原因；
- Demo 写授权、Proposal 状态、submission 状态和订单状态；
- Broker/REST 对账结果、unknown 冻结状态和恢复审计。

可批量写入：

- 不参与恢复判断的会话汇总审计；
- 已经由权威业务状态完整表达的重复诊断记录。

可只保留在内存直到下一次原子检查点：

- 能从已提交 K 线和运行时状态重新计算的临时计数；
- 不影响风控、幂等和恢复结论的展示数据。

当前 VWAP continuous Shadow 每根确认 K 线通过 `commit_vwap_shadow_candle` 使用一个 `BEGIN IMMEDIATE` 事务提交处理标识、运行时状态、信号、可选 Proposal 和 heartbeat。任一步失败时整根 K 线回滚，恢复后可安全重放。该路径不存在“每个字段各开一次连接”的问题。

## 离线 Replay 耐久性契约

`ShadowReplaySession` 保存的是可由固定输入和固定策略配置确定性重建的离线 Replay
状态。它是唯一允许使用 `WAL + synchronous=NORMAL` 的正式数据库路径。

`synchronous=NORMAL` 下 SQLite 返回成功的 `COMMIT`，不具备与 `FULL` 相同的系统级
断电耐久性保证。如果操作系统或存储设备丢失了最近已提交的数据，必须从完整的 candle
事务边界重新执行确定性 Replay。一个 candle 仍对应一个事务；这里的恢复单元是一个
candle transaction，但这不是对物理断电时实际丢失上限的声明。

该放宽只适用于离线、只读市场输入且不会产生可执行订单意图的 Replay。它不得用于：

- continuous shadow runtime；
- Demo 写授权、订单或 submission 状态；
- broker/private-state 对账；
- migration、backup 或其他影响资金和恢复判定的状态。

默认 `Database.connect()`、continuous shadow、订单关键路径和 migration 继续使用
`FULL`。本项目尚未验证 OS crash durability 或 power-loss durability，不能把 `NORMAL`
描述为“断电安全”。

本轮不对 Demo/订单安全状态做批处理，也不改变旧 continuous Demo 的逐事件持久化。其频率由 K 线周期限制，降低持久性换取的收益没有证据支持。以后只有在可复现 profile 证明数据库提交是瓶颈，并且新增崩溃矩阵覆盖批次边界后，才允许调整。
