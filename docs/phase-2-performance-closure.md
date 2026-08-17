# Phase 2 性能工作正式封板

```text
STOP_PHASE_2_PERFORMANCE_WORK
```

## 为什么开始

Phase 1 审计发现 replay/持久化路径每根 K 线开 4-5 个独立连接与事务（"逐信号写库"），
且缺乏数据支撑的性能画像。Phase 2 以 MEASURE FIRST 为原则逐轮推进。

## 各阶段结论

| 阶段 | 结论 |
|---|---|
| 2A 画像 | legacy replay 1000 candles=67s（4.45 conn+commit/candle）；soak 引擎对照 0.46s；写路径分级（CRITICAL/IMPORTANT/TELEMETRY） |
| 2B1 事务收敛 | replay 收敛到唯一 canonical 原子提交（1 candle=1 transaction；conn/commit 4.45→1.005；FULL 下 3.9×） |
| 2B2 durability 实验 | 每 candle 新连接形态下 NORMAL 单独仅 1.0-1.4×（fsync≈2.7ms，其余为每连接 WAL 生命周期成本） |
| 2B3 连接生命周期 | path-scoped 会话连接（conn/candle→0.006，commits 不变；FULL 5.06×；commit 7.5→2.7ms 证实 2B2 推断） |
| 2B4 Replay NORMAL | 离线 Replay 会话唯一允许 WAL+NORMAL（K=1/5/10 丢失尾部重放收敛、零重复、零订单副作用） |
| 2C1 策略计算画像 | compute 仅占端到端 ~6.7%；rolling_vwap 为最大单热点但端到端占比 ~2.5%（天花板 1.026×） |

## 最终吞吐

```text
replay benchmark：10000 candles ≈ 4.3s ≈ 2300 candles/s
实际配置 1h（最细 5m）candle ⇒ 实时裕量 ≈10^6-10^7×
```

## 为什么停止

```text
performance already sufficient        —— 实时裕量百万倍级
realtime headroom extremely large     —— 2310 candles/s vs 0.00028 candles/s（1h）
remaining optimization ceiling small  —— 最大单热点端到端收益 ≈1.026×
additional semantic risk not justified —— incremental VWAP 涉及 Decimal 求和顺序/parity 风险
```

## 明确放弃 / 延后的项（避免未来 agent 重复发起）

| 项 | 状态 | 原因 |
|---|---|---|
| incremental VWAP（O(24)→O(1)） | REQUIRES_SEPARATE_SAFETY_STUDY（实质 DEFERRED，不建议启动） | Decimal 默认精度下加法无结合律 → VWAP 末位可能变化；端到端收益仅 ~1.03× |
| Decimal → float / 数值表示变更 | REQUIRES_SEPARATE_SAFETY_STUDY | rounding/复现/阈值边界/回测 parity |
| telemetry batching（strategy_signal_events 等） | NOT_JUSTIFIED | NORMAL 后 SQL 仅 ~89ms/1000 candles，占比小 |
| 全局 Database.connect 连接复用 | NOT_JUSTIFIED | replay 已解决；订单链低频不值得生命周期风险 |
| 额外索引（shadow_order_proposals(run_id)、signals(instrument_id,timestamp)） | DEFERRED（仅当查询频率上升） | 当前仅两处低频全表扫描 |
| continuous shadow 引擎 NORMAL | REQUIRES_SEPARATE_SAFETY_STUDY | 运行时 scope 与离线 replay 不同；须独立评审 |
| Decimal("3") 等常量提升 | NOT_WORTH_OPTIMIZING | 端到端 <0.5% |

**再次强调：看到 `rolling_vwap` 为 O(window) 重扫不应重新发起优化**——它在端到端
占比 ~2.5%，任何增量化都必须先解决 Decimal parity，收益/风险完全不成比例。
