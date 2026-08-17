# Phase 2B3 — Path-Scoped Connection Lifecycle

Phase 2B2 的遗留问题（"每连接 ~5ms 是 WAL 生命周期成本"是推断）在本轮得到实测证实并
实施修复：`run_shadow_replay` 的执行域现在持有**一条**专用连接，每根 candle 仍是
**一个**显式 `BEGIN IMMEDIATE ... COMMIT` 事务。历史基线（2A/2B1/2B2 文档）未动。

## 1. Connection ownership 设计

```text
connection_owner = ShadowReplaySession（app/services/continuous_shadow_repository.py）
  - __enter__: sqlite3.connect(timeout=10) + row_factory + PRAGMA foreign_keys=ON（仅一次）
  - __exit__:  若 in_transaction → rollback；close exactly once（容忍外部已关闭）
  - 不重连、不重试：任何 sqlite 失败 → StorageError fail closed

transaction_owner = canonical 事务体 _commit_vwap_shadow_candle_tx（唯一 SQL 实现）
  - 入口断言 not connection.in_transaction（禁止嵌套事务，RuntimeError）
  - 显式 BEGIN IMMEDIATE → writes → COMMIT；异常 → rollback → 原样传播
  - 两个入口共享同一实现：public commit_vwap_shadow_candle（自管 Database.connect，
    生产引擎路径不变）与 session.commit_vwap_shadow_candle（借用 scoped 连接）
  - 未 claim（重复 candle）→ rollback + return False

connection_scope = 一次 replay run（单线程，check_same_thread 默认 True 未动）
transaction_scope = 严格 1 candle（connection lifetime ≠ transaction lifetime）
```

未建立第二套持久化实现；未改 `Database.connect()`；未触碰订单/授权/迁移/恢复路径。
生产 continuous shadow 引擎（不同 execution scope，asyncio）保持每 candle 自管连接（§14）。

## 2. Before 成本分解（改造前，1000 candles FULL，instrumented）

```text
connect=511ms  close=162ms  BEGIN=543ms  PRAGMA=15ms  （合计每连接固定 ≈1.2ms）
sql(other)=645ms  commit=2502ms（本次快态；2B2 慢态 7.8ms/commit，本机 wall-clock 波动达 3×）
WAL 增至 4.2MB；wal_autocheckpoint=1000、journal_size_limit=-1（未调参数）
```

## 3. Before / After（FULL，C_1000，5 repeats median）

```text
              before(2B1)   after(2B3)
elapsed       17.04s*       3.37s（p95 4.25 / min 3.20）  *本机波动，快态基线 4.6s
connections   1005          6                             （-99.4%；conn/candle 1.005→0.006）
commits       1005          1005                          （不变：1.005/candle）
writes        4887          4887                          （不变）
mean_commit   7.50ms        2.70ms                        （每连接 WAL 成本消失；2.7ms≈纯 FULL fsync，
                                                            与 2B2 差值 7.85-5.11=2.74ms 精确吻合）
db_share      62.3%         84.1%（绝对量骤降）
```

结构目标全部达成：connection↓↓↓、commit≈unchanged、writes=unchanged、
1 candle = 1 transaction、行为等价（Phase 2B1 golden 测试 9/9 直接通过，零新 semantic diff）。

## 4. Post-reuse FULL/NORMAL A/B（benchmark-only，回答 2B2 遗留问题）

```text
                FULL(scoped)   NORMAL(scoped)   speedup
replay_100      0.67s          0.42s           1.58×
replay_1000     3.18s          0.80s           3.96×
replay_10000†   29.50s         4.23s           6.98×
commit mean     2.63ms         0.25ms          （0.25ms = soak 形态实测值）
                † 单次方向性，非统计样本
```

**分叉点判定**：连接复用后 NORMAL 收益重新显著（整体 4-7×，commit 10×）。这是
"path-scoped NORMAL 值得单独决策讨论"的情形（对照 2B3 规范的两种预设情形，属于后者）。
本轮未实施，生产默认仍 FULL。

## 5. 故障语义（测试固化，tests/test_shadow_replay_connection_lifecycle.py）

```text
after_processed/after_signal/after_proposal 注入 → 失败 candle 全回滚（2B1 套件复用通过）
nested transaction → RuntimeError（防嵌套断言）
rollback → connection 干净可复用（失败后下一 candle 成功，仅第二根持久化）
database locked → StorageError fail closed；锁释放后同一 session 继续可用
unexpected close → StorageError，无静默重连（§10）
close exactly once：正常/异常/KeyboardInterrupt 路径均验证；重复 exit 安全
PRAGMA 生命周期：整个 session 期间 wal/FULL(2)/foreign_keys=ON 逐 candle 采样不变
线程绑定：其他线程使用底层连接 → ProgrammingError（check_same_thread=True 保持）
```

## 6. 质量门槛

```text
ruff=PASS  mypy --strict=PASS（178 files）
pytest=689/689 PASS（680 基线 + 9 lifecycle 测试，exit=0，零失败）
security_smoke=PASS（demo write boundary / submission fence / migration workflow /
fresh clone / replay convergence / continuous shadow 引擎与 CLI 隔离，59 项）
```

## 7. 剩余瓶颈与候选重评

```text
global_connection_reuse_priority=LOW
  （含义：是否推广到整个 repository 系统。replay 已解决；订单链低频（0.44s/生命周期）
   不值得为其承担生命周期风险；生产 shadow 引擎可未来独立评估 session 化）
telemetry_batching_priority=LOW
  （scoped NORMAL 后 SQL 语句仅 ~89ms/1000 candles，占比小）
新主导：scoped NORMAL 下 strategy/Python ≈0.46s/1000（57%）；FULL 下 commit 2.6ms 仍是大头
```

## 8. 复现

```powershell
uv run pytest tests/test_shadow_replay_connection_lifecycle.py tests/test_shadow_replay_convergence.py -q
uv run python -m benchmarks.persistence_profiler --repeats 5 --output artifacts/performance/phase_2b3_connection_lifecycle.json
uv run python -m benchmarks.durability_experiment --output artifacts/performance/phase_2b3_post_reuse_ab.json
```
