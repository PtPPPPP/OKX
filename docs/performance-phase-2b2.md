# Phase 2B2 — SQLite Durability Experiment（WAL+FULL vs WAL+NORMAL）

纯实验阶段：`production_synchronous=FULL` 未变，`Database.connect()` 未变。NORMAL 仅通过
benchmark 进程内的 `sqlite3.connect` 包装注入（`benchmarks/durability_experiment.py`），
两个变体走完全相同的注入代码路径，并在**每个连接**上读回 `PRAGMA synchronous` 验证
（`effective_synchronous_verified_on_every_connection=true`，否则实验无效）。

历史基线 `performance-phase-2a.md` / `performance-phase-2b1.md` 保留未动；
机器数据 `artifacts/performance/phase_2b2_durability_ab.json`（gitignored）。

## 1. 实测有效配置（真实连接上执行 PRAGMA，非代码推断）

```text
Database.connect()（canonical replay/订单/迁移共用）  journal=wal synchronous=2(FULL) busy_timeout=10000 foreign_keys=1
ShadowSoakStore._new_connection()                      journal=wal synchronous=1(NORMAL) busy_timeout=30000 foreign_keys=1
MigrationManager._connect()                            journal=delete synchronous=2 busy_timeout=1000 foreign_keys=0
raw sqlite3 默认（本构建）                              journal=delete synchronous=2 busy_timeout=5000 foreign_keys=0
```

注意：ShadowSoakStore（研究 soak 引擎）一直是 NORMAL；生产 app 路径全部 FULL。
本实验对象是"若把 canonical shadow/replay 也切成 NORMAL"。

## 2. A/B 结果（canonical replay workload，同 seed/同 schema/同事务数）

```text
replay_100（10 repeats median）:
  FULL   0.88s  114 candles/s  commit mean 3.22 / p95 6.57 / p99 7.44 ms
  NORMAL 0.65s  155 candles/s  commit mean 0.36 / p95 0.40 / p99 4.69 ms
  speedup=1.36×

replay_1000（10 repeats median）:
  FULL   18.11s 55 candles/s  commit mean 7.85 / p95 10.19 / p99 11.99 ms
  NORMAL 16.72s 60 candles/s  commit mean 5.11 / p95 5.49  / p99 6.41  ms
  speedup=1.08×

replay_10000（单次方向性验证，非统计样本）:
  FULL 165.1s / NORMAL 164.9s，commit mean 7.42 → 5.10 ms，speedup=1.00×

计数等价（§8 硬校验，全部通过）: candles/signals/proposals/writes/commits/connections
在两变体间逐 repeat 完全一致（assert 失败即实验无效）。
```

成本分解（1000 档 median，ms）：

```text
            connect   SQL 语句   COMMIT   其他(Python/策略)
FULL          591       2567      7761        7190
NORMAL        582       2542      5172        8429
```

## 3. 核心发现：fsync 只占 commit 成本的一小部分

```text
FULL commit ≈7.9ms，NORMAL commit ≈5.1ms ⇒ synchronous fsync ≈2.7ms/commit
NORMAL 下每提交仍要 ≈5ms —— 与规模弱相关（100 档 0.36ms → 1000/10000 档 5.1ms）
对照：soak 引擎（长连接 + NORMAL）commit 实测 0.23ms（Phase 2A）
```

结论：在当前"每 candle 新开一条连接"的形态下，每连接的 WAL 生命周期成本
（连接建立时 WAL/shm 恢复、随 WAL 增长的 autocheckpoint 均摊）是 commit 延迟的主导
成分；NORMAL 无法单独兑现收益（1000/10000 档 speedup≈1.0）。NORMAL 的价值
（0.23ms 级 commit）只有在**长连接形态**下才能兑现——与 Candidate #2 相互依赖。

## 4. 故障模型实验

```text
A. Python exception / rollback（两模式，注入 after_signal）:
   全回滚，processed/signal 0 行 —— 两种模式一致（tests 固化）

B. Process termination（子进程 terminate；4/4，两模式一致）:
   before_commit: DB consistent，事务连同 CREATE TABLE 全部回滚（表不存在），0 行，无部分提交
   after_commit:  DB consistent，已提交 2 行完好
   （进程终止 ≠ OS crash：OS page cache 未丢失）

C. OS crash:     NOT_TESTED（无刷 OS 页缓存的基础设施）
D. Power loss:   NOT_TESTED（无磁盘/VM 级故障注入）
```

不声称"NORMAL 断电安全"；WAL+NORMAL 的已知语义是：断电可能丢最近已提交事务但
数据库不损坏——该语义在真实断电基础设施验证前按文档语义对待。

## 5. Durability contract（shadow/replay 路径，代码级验证）

```text
shadow_state_replayable=true   processed_candles PK + INSERT OR IGNORE 幂等；resume 从 checkpoint 重放
signal_replayable=true         策略确定性（reproducibility/soak 幂等测试固化）
proposal_replayable=true       shadow 提案确定性重建（quantity=0/notional=0/submission_performed=0）
real_order_side_effect_possible=false
  —— shadow_order_proposals 的唯一读方是恢复检查（submission_performed 恒 0）；
     受控下单链（ControlledDemoWriteService/begin_controlled_demo_submission）只读
     demo_order_proposals（不同表）；validate_shadow_proposal 强制 read_only/blocked/0 数量
loss_window_safe_for_shadow=true
  断电最多丢最近若干已提交 shadow 事务 → 重放窗口内的确定性重建，无资金动作语义
```

## 6. 路径分级结论（本轮只分类，不实施）

```text
CRITICAL（订单/授权/迁移/恢复）: recommendation=KEEP_FULL
IMPORTANT（shadow/replay 状态）: recommendation=NEEDS_MORE_EVIDENCE
  —— 单独 NORMAL 收益 1.0-1.4×，不足以证明改动；与连接生命周期改造组合后需重测
TELEMETRY（strategy_signal_events 等）: recommendation=BATCH_CANDIDATE
  —— NORMAL 后 SQL 语句执行（≈2.5s/1000 candle）仍是最大 DB 成分
```

## 7. Candidate 重评估

```text
global_connection_reuse_priority=HIGH（由 MEDIUM 上调）
  证据：NORMAL 变体中 commit 仍 5.1ms × 1000 + connect 0.58s —— 每连接 WAL 生命周期
  成为主导；消除后（对照 soak 形态）commit 可到 0.23ms 级，预期 1000 档 4-8×。

telemetry_batching_priority=MEDIUM
  语句执行 2.5s/1000 占 NORMAL 总时长 15%；连接修复后占比更高，届时收益上升。
```

## 8. 生产影响

```text
production_durability_changed=false
production_behavior_changed=false
新增：benchmarks/durability_experiment.py（实验工具）、
tests/test_durability_experiment_tools.py（7 测试）、本文档。
```

## 9. 复现

```powershell
uv run python -m benchmarks.durability_experiment            # ~12 分钟（含 10000 方向性单次）
uv run python -m benchmarks.durability_experiment --skip-10000
uv run pytest tests/test_durability_experiment_tools.py -q
```
