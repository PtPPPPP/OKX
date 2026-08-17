# Phase 2A — Performance Baseline & Persistence Profiling

MEASURE FIRST, OPTIMIZE LATER. 本文档记录 Phase 2A 的画像方法、数据与结论；
机器可读基线在 `artifacts/performance/phase_2a_baseline.json`（gitignored runtime artifact）。
复现方式：`uv run python -m benchmarks.persistence_profiler [--repeats 5]`。
本轮 `production_behavior_changed=false`：所有 instrumentation 仅存在于 benchmark
进程内（包装 `sqlite3.connect`，见 `benchmarks/persistence_metrics.py`），退出即还原。

## 1. 冻结基线

```text
pytest=660/660 PASS   ruff=PASS   mypy --strict=PASS
P0=0  P1=0   GREEN_BASELINE=true
```

## 2. 环境

```text
python=3.12.11   sqlite=3.49.1   journal_mode=WAL   synchronous=2 (FULL, Database.connect 未覆盖)
os=Windows 11   cpu=Intel x86-64 (Raptor Lake)   filesystem=本地 NTFS
db=tempdir 下临时 SQLite   process_count=1
```

开发工作站、单进程：**绝对耗时不可用于容量规划**；写/事务/连接计数是确定性的
（全部 workload 5 次重复计数完全一致，`deterministic_counts=true`），具有高价值。

## 3. 连接与事务模型（静态画像）

- `app/storage/database.py::Database.connect()`：**每个 `with` 块**新开
  `sqlite3.connect(timeout=10)` → 每连接 `PRAGMA foreign_keys=ON` → 块尾无条件
  `commit()`（含只读块，无事务时为廉价 no-op）→ `close()`。未设置
  `synchronous`（SQLite 默认 FULL）。WAL 在 `initialize()` 设置一次（持久）。
- `TradingRepository` / `ContinuousShadowRepository` 的每个方法各自进入一个
  `Database.connect()` 块 ⇒ **每方法 1 连接 + 1 commit**。
- 生产 VWAP continuous shadow 引擎每根确认 K 线调用一次
  `commit_vwap_shadow_candle()`（单 `with` 块 + 显式 `BEGIN IMMEDIATE`）：
  每 bar 1 连接 + 1 事务。
- `run_shadow_replay`（research/测试路径，app/services/shadow_replay.py）每 bar
  调用 `claim_candle` → `save_runtime` → `save_signal` → (BUY 时 `save_proposal`)
  → `heartbeat`：**每 bar 4-5 个连接、4-5 个事务**。
- `ShadowSoakStore`（soak 引擎）：`open_runtime()` 复用一条运行时连接，
  每 bar `BEGIN IMMEDIATE`+commit，且 `PRAGMA synchronous=NORMAL`。

## 4. 基准结果（5 repeats，median）

| workload | 规模 | elapsed(median) | candles/s | conn | commits | writes | writes/candle | commits/candle | conn/candle | db_share |
|---|---|---|---|---|---|---|---|---|---|---|
| A_100 soak | 100 | 0.104s | 958 | 8 | 108 | 359 | 3.59 | 1.08 | 0.08 | 43% |
| A_1000 soak | 1,000 | 0.464s | 2,157 | 8 | 1,008 | 3,447 | 3.45 | 1.01 | 0.008 | 59% |
| A_10000 soak | 10,000 | 5.73s | 1,745 | 17 | 10,026 | 33,935 | 3.39 | 1.00 | 0.002 | 71% |
| C_1000 legacy replay | 1,000 | **67.0s** | 14.9 | **4,446** | **4,446** | 4,887 | 4.89 | **4.45** | **4.45** | 66% |
| B 信号区域 soak | 424 (16 buys) | 0.218s | — | 8 | 432 | 1,294 | 3.05 | 1.02 | — | 50% |
| D 订单生命周期 | 1 次 | 0.444s | — | 35 | 33 | 42 (19 critical) | — | — | — | 85% |

关键单点数字：

```text
mean_db_operation_ms:  A=0.007-0.021   C=1.121 (p95=2.655)
mean_commit_ms:        A=0.227-0.349   C=7.001   D=7.225
```

**同样 1,000 根 K 线、同 seed、同 441 个 buy 信号：soak 引擎 0.46s vs legacy replay
67.0s（≈144×）**。差异来源 = 连接重开（4.45/candle）× 每连接 PRAGMA/文件开销 ×
每事务 commit（FULL 同步模式下平均 7ms）+ legacy 路径 5 个串行事务/bar。
注：soak 另设 `synchronous=NORMAL`，两个变量（连接复用、同步模式）在 C vs A 对比中
同时存在；分解需 Phase 2B 定向实验。

### “逐信号写库”的真实口径

信号遥测与状态写入按 **bar** 而非按 buy 信号发生（与是否产生信号无关）：

```text
soak 每 bar:    signals 行 1 + processed_bars 行 1 + runs 计数 UPDATE 1 (+BUY 时 proposal 1) ≈ 3.4 写/bar, 1 commit/bar
legacy 每 bar:  processed_candles + strategy_runtime_states + strategy_signal_events + heartbeat UPDATE (+BUY 时 proposal+event) ≈ 4.9 写/bar, 4.45 commit+conn/bar
per-buy-signal 口径 (B): writes_per_signal≈80.9, commits_per_signal≈27.0（含全部 bar 的写，非增量成本）
```

## 5. Durability 分类（写路径清单摘要）

CRITICAL（禁止 batching / 延迟 durable；进程 crash 丢失会导致重复下单、错误恢复、错误授权）：
`demo_order_proposals` 状态迁移及其 `demo_order_proposal_events`（prepared/fenced/
started/submitted/unknown/one_use_authorization_reserved）、`orders` 及
`order_state_changes`、`fills`、`bounded_submission_events`、`private_state_control`
（epoch/version/fence）、`schema_migrations`、migration audit（`migration_audit.jsonl`）、
`runtime_generations`、`processed_events` 幂等键。

IMPORTANT（丢失 ⇒ 重复计算/恢复成本/shadow 漂移，无交易安全事故；合并写入需逐项证明）：
`strategy_runtime_states`（断点续跑状态）、`processed_candles`（去重游标）、
`continuous_demo_runs` 状态/心跳字段、`continuous_run_locks`、`shadow_soak_checkpoints`、
`shadow_soak_processed_bars`、`private_state_snapshots` 暂态、`runtime_state`。

TELEMETRY（Phase 2B batching/coalescing 主候选）：
`strategy_signal_events`（**每 bar 一行，含 no_signal**）、`shadow_soak_signals`
（每 bar 一行）、`shadow_soak_runs` 每 bar 计数 UPDATE、`shadow_order_proposal_events`、
`continuous_demo_run_events`、`shadow_soak_run_events`、`system_events`、研究型
`audit_records`/`portfolio_snapshots` 快照。

Workload D 实测关键路径（单次受控下单生命周期，剔除一次性 schema bootstrap 23 写后）
= 19 写 / 33 提交：`demo_order_proposals` 5、`demo_order_proposal_events` 7、
`orders` 2、`fills` 1、`private_state_control` 2、`bounded_submission_events` 1 等。
每步独立事务 = 当前 durability 边界，**不进入性能优化目标**。

## 6. Query Plan 发现（EXPLAIN QUERY PLAN，未改任何索引）

```text
SEARCH（有索引）: processed_candles PK、strategy_runtime_states 唯一索引、
  strategy_signal_events 唯一索引、continuous_demo_runs PK、orders PK(client_order_id)、
  demo_order_proposals PK、private_state_snapshots PK(scope_key)
SCAN（全表扫描）: shadow_order_proposals WHERE run_id=?  ← 索引候选
                  signals WHERE instrument_id=? AND timestamp BETWEEN ← 索引候选
```

INDEX CANDIDATE LIST（本轮不实施）：`shadow_order_proposals(run_id)`、
`signals(instrument_id, timestamp)`。其余热查询全部命中唯一/PK 索引。

## 7. 冗余写候选（只登记，不修改）

| candidate | 现状 | 判断 |
|---|---|---|
| `shadow_soak_runs` 每 bar 计数 UPDATE | 10,000 bar → 10,012 写 | 同事务内、无额外 commit；可随 checkpoint 周期合并，但收益小 — NEEDS_ANALYSIS（低优先） |
| `strategy_signal_events` 每 bar 写（含 no_signal 行） | 1 写/bar，TELEMETRY | SAFE_TO_BATCH（append-only 审计语义保留，仅延后批量插入；恢复窗口内丢失的是观测行，不改变交易/恢复语义） |
| `shadow_soak_signals` 每 bar 写 | 同上 | SAFE_TO_BATCH |
| legacy replay 每 bar heartbeat UPDATE | 1 conn+commit/bar | SAFE_TO_COALESCE（生产引擎已在 `commit_vwap_shadow_candle` 单事务内完成——直接收敛到该实现即可） |
| `strategy_runtime_states` 每 bar UPSERT | 1 写/bar | IMPORTANT；UPSERT 同值也写 — CONDITIONAL（仅在 crash-safe checkpoint 周期内合并才有意义） |
| 订单关键路径 19 写/生命周期 | 每步独立事务 | MUST_KEEP（CRITICAL durability 边界，实测正常） |

## 8. Crash 语义（Phase 2B 候选的丢失分析）

- `strategy_signal_events`/`shadow_soak_signals` 批量插入：batch 未 flush 时 crash ⇒
  丢失最近 N bar 的观测行。可从 K 线源重放重建（确定性策略）；不影响订单恢复
  （recovery 读 `orders`/`fills`/`private_state_control`）、不影响策略结果（状态来自
  `strategy_runtime_states`/checkpoint）⇒ SAFE_TO_BATCH。
- heartbeat/计数 UPDATE 合并：crash ⇒ 心跳陈旧 ⇒ 恢复流程按 stale run 处理（已有
  administrative closure/quarantine 路径）⇒ SAFE_TO_COALESCE，需保留 lease 独立语义。
- `strategy_runtime_states` 延迟写：crash ⇒ 从上一 checkpoint 重放窗口 ⇒ 重复计算但
  结果确定 ⇒ CONDITIONAL（窗口大小 = 重放成本上限）。
- 订单路径任何合并 ⇒ MAY 导致重复下单/错误恢复 ⇒ MUST_REMAIN_SYNCHRONOUS。

## 9. Phase 2B 候选（按 优先级 = 影响×频率÷(风险×复杂度) 排序）

1. **legacy `run_shadow_replay` 收敛到 `commit_vwap_shadow_candle` 单事务实现**
   — Current cost: 4.45 conn+commit/bar，67s/1k bar；Expected benefit: ~50-100×
   （对齐 soak/生产引擎量级）；Safety risk: LOW（生产引擎已验证的同一代码路径）；
   Complexity: LOW；Recommendation: 首选。
2. **`Database.connect()` 连接复用（同线程按需重用或 per-engine 单连接）**
   — Current cost: 每方法 1 connect+PRAGMA+commit+close；benefit: HIGH（全仓库所有
   repository 方法）；risk: LOW-MEDIUM（需保证事务边界不变、异常回滚语义不变）；
   Complexity: MEDIUM；Recommendation: 第二步，配 crash/fault 回归。
3. **信号遥测批量插入（`strategy_signal_events`/`shadow_soak_signals`）**
   — benefit: MEDIUM（1 写/bar → 1 写/N bar）；risk: MEDIUM（crash 丢观测行，
   可重放重建）；Complexity: MEDIUM；Recommendation: 在 1/2 之后，附丢失窗口文档。
4. **`synchronous=NORMAL` 评估（限 shadow/telemetry 库或显式 opt-in）**
   — soak 已用 NORMAL；benefit: MEDIUM（commit 7ms→<1ms 量级）；risk: MEDIUM
   （WAL+NORMAL 下断电可能丢最后事务，但不会损坏库；CRITICAL 路径保持 FULL）；
   Complexity: LOW；Recommendation: 仅对 shadow/telemetry 评估，逐路径审批。
5. **索引 `shadow_order_proposals(run_id)`、`signals(instrument_id,timestamp)`**
   — benefit: LOW-MEDIUM（当前仅两个 SCAN，均为低频查询）；risk: LOW；Complexity: LOW
   （一条 CREATE INDEX migration，经 v24 受控迁移）；Recommendation: 随任一 v24 顺带。

明确不做：订单/授权/恢复关键路径 batching（MUST_REMAIN_SYNCHRONOUS）、任何异步
writer 队列（§17）、为基准删除 audit。

## 10. 复现与测试

```powershell
uv run python -m benchmarks.persistence_profiler --repeats 5   # 完整（~6 分钟）
uv run python -m benchmarks.persistence_profiler --quick --repeats 1   # 快速冒烟
uv run pytest tests/test_persistence_profiler_tools.py -q      # instrumentation 测试
```

instrumentation 测试覆盖：分类/表名提取正确、计数正确、退出后 `sqlite3.connect`
还原、instrumented 与 plain 运行业务结果完全一致（零语义影响）、延迟分位数有序。
默认测试套件不含任何 timing 断言。
