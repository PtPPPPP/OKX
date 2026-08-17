# Phase 2B1 — Legacy Replay Persistence Convergence

Phase 2A Candidate #1 的实施：`run_shadow_replay` 的持久化路径收敛到唯一 canonical
原子提交实现 `ContinuousShadowRepository.commit_vwap_shadow_candle`。
Phase 2A 原始基线保留在 `docs/performance-phase-2a.md` 与
`artifacts/performance/phase_2a_baseline.json`；本轮 after 数据在
`artifacts/performance/phase_2b1_after.json`（gitignored）。

## 1. 改造前后路径

```text
BEFORE（legacy，每确认 K 线）:
  claim_candle      → Database.connect → INSERT OR IGNORE processed_candles → commit → close
  save_runtime      → Database.connect → UPSERT strategy_runtime_states → commit → close
  save_signal       → Database.connect → INSERT OR IGNORE strategy_signal_events → commit → close
  save_proposal(BUY)→ Database.connect → INSERT proposals + events → commit → close
  heartbeat         → Database.connect → UPDATE continuous_demo_runs → commit → close
  ⇒ ≈4.45 connections + 4.45 commits / candle（Phase 2A 实测 1000 candles=67.0s）

AFTER（canonical，每确认 K 线）:
  commit_vwap_shadow_candle → 1 Database.connect → BEGIN IMMEDIATE
    → processed_candles + strategy_runtime_states + strategy_signal_events
      (+ shadow_order_proposals + proposal_events) + run heartbeat UPDATE
    → COMMIT → close
  ⇒ 1 connection + 1 transaction / candle
```

生产引擎 `vwap_continuous_shadow._process` 的调用完全不变（它本来就用 canonical 路径）。

## 2. 语义差异表（legacy vs canonical，逐字段）

| 字段 | legacy | canonical | 判定与处理 |
|---|---|---|---|
| processed candle identity（9 列） | source="local_csv_shadow_replay" | 新增 `market_data_source` 参数，replay 传同值 | IDENTICAL |
| duplicate 检测 | INSERT OR IGNORE rowcount==0 → replay 抛 MarketDataError | 同机制，返回 False → replay 抛同一异常 | IDENTICAL |
| strategy_runtime_states.state_json | json.dumps(state_snapshot, sort_keys) | 同（replay 传同一字符串） | IDENTICAL |
| strategy_runtime_states.state_hash | sha256(json 列表[时间/None/None/relation/type/warmup]) | sha256(runtime_state)（直接绑定持久化 state_json） | EQUIVALENT——legacy 哈希含冗余常量；canonical 绑定实际持久化内容，且 hash 仅用于遥测关联（resume 只读 state_json） |
| strategy_signal_events.signal_value | 完整 VWAP json（close/vwap/deviation_bps/vwap_window/window_length/reason） | 同（canonical 接收参数，replay 传同一字符串） | IDENTICAL |
| strategy_signal_events.current_relation | `str(vwap)`（warmup 期为字符串 `"None"`） | 常量 `"vwap"` | **有意差异（LEGACY_ONLY 纠正）**：legacy 把数值塞进为 fast/slow 关系设计的列且 warmup 期写入 `"None"`；vwap 值已逐字保留在 signal_value JSON（等价测试断言信息无丢失） |
| shadow_order_proposals 全部 19 个投影字段 | save_proposal | canonical 硬编码 | IDENTICAL（golden 逐字节一致） |
| shadow_order_proposal_events | (blocked, "shadow_only;not_sized") | 同 | IDENTICAL |
| run heartbeat/counters/status | status=shadow_running + 3 计数 + last_heartbeat_at | 同（同一 UPDATE） | IDENTICAL |
| private/public_stream_status | "not_applicable"/"local_replay" | 新增两个参数，replay 传同值（生产默认 not_created/ready 不变） | IDENTICAL |
| 结果字段 shadow_proposal_ids | save_proposal 返回的 uuid 列表 | 移除（无任何消费方）；提案经 signal_id 可追溯 | 已废弃（重复信息） |

A/B 验证：legacy 方法序列（仍服务于 continuous_demo/bounded_demo）与收敛后 replay
在 119-bar tracked fixture 上对 6 张表的确定性投影做 sha256 对比——除
`current_relation` 外全部逐字节一致（`tests/test_shadow_replay_convergence.py` 固化
golden digest）。

## 3. 改动清单

| file | 改动 |
|---|---|
| `app/services/continuous_shadow_repository.py` | `commit_vwap_shadow_candle` 新增 3 个仅数据标签的关键字参数（`market_data_source` / `private_stream_status` / `public_stream_status`，默认=生产原值，行为零变化） |
| `app/services/shadow_replay.py` | candle 循环重写为单次 canonical 提交；删除 per-candle claim/save_runtime/save_signal/save_proposal/heartbeat 调用；结果 dict 移除无人消费的 `shadow_proposal_ids`（新增 `proposal_signal_ids` 语义别名=buy_signal_ids） |
| `tests/test_shadow_replay_convergence.py`（新） | golden 等价、结构性连接/提交断言、故障注入 A/B/C、锁 D、重复 E、双 run 独立性 |

legacy repository 方法全部保留（continuous_demo / bounded_demo 仍在使用）。

## 4. Atomicity / Failure Injection（测试固化）

```text
one_candle_one_transaction=true      （结构性断言 connections/candle≤1.2、commits/candle≤1.2）
partial_write_possible=false         （注入 after_processed/after_signal/after_proposal：失败 bar 全回滚）
duplicate_replay_idempotent=true     （第二次 commit 返回 False，零新行、心跳不被触碰）
generation_fence_preserved=true      （create_run 仍要求 active generation）
heartbeat_atomic=true                （与 candle 同一事务）
signal_atomic=true / proposal_atomic=true
database locked=fail closed          （外部 BEGIN IMMEDIATE 下 StorageError，零部分状态）
```

## 5. 性能 Before / After（同 workload、同环境、同 profiler、5 repeats median）

```text
C_1000（legacy replay，同 seed 441 buys）:
  elapsed          67.0s  → 17.0s          （3.9×）
  connections      4446   → 1005           （-77.4%）
  commits          4446   → 1005           （-77.4%）
  writes           4887   → 4887           （0%，审计/状态零删除）
  conn/candle      4.446  → 1.005          （✓ ≤1.2 结构验收）
  commits/candle   4.446  → 1.005          （✓ ≤1.2 结构验收）
  throughput       ~15    → ~59 candles/s
  mean_commit_ms   7.001  → 7.505（FULL 不变）
  db_share         66.3%  → 62.3%

C_100（Phase 2A quick 实测 2.290s/458conn/458commit → 0.861s/105conn/105commit，2.7×）
A/B/D workload 与 soak 路径不受影响（A_1000 0.464s→0.469s，波动范围内）。
```

**未达 5× 硬门槛（§18）——已停止并分析**：结构目标（1 conn/1 commit/candle、
写集合不变、原子性、语义等价）全部达成；剩余 17s 的分解 = 每 candle 仍新开 1 条
连接（connect+PRAGMA+close）+ FULL 同步 commit（实测均值 7.5ms × 1005 ≈ 7.5s）
+ 语句执行 ≈1.8s + 策略/Python ≈6s。Phase 2A 中 soak 的 144× 对比同时包含
"运行时连接复用"与"synchronous=NORMAL"两个成分；本轮实验完成了分解：仅事务收敛
（4.45→1.0 conn+commit/candle）= 3.9×。剩余成分正是 Candidate #2（连接生命周期）
与 #4（同步模式）的范畴，本轮规则明确禁止触碰。

## 6. Durability 配置不变

```text
journal_mode: WAL → WAL（未改）
synchronous: FULL(2) → FULL(2)（未改；Database.connect 未触碰）
durability_changed=false
```

改善只来自 transaction convergence + connection reduction；写语句集合保持不变
（processed/runtime/signal/proposal/heartbeat 每类写仍在，仅事务归一）。

## 7. Candidate #2 重评（数据驱动，§28）

```text
global_connection_reuse_priority=MEDIUM_PRIORITY
```

证据：after C_1000 已是 1 conn+1 commit/candle；17s 中 FULL commit ≈7.5s（44%）、
语句 ≈1.8s、连接 open/close+PRAGMA 估 2-4s、策略/Python ≈6s。单独实施 Candidate #2
只消除连接开销（约 1.2-1.4×）；要到 soak 量级（>30×）必须叠加 shadow 路径
`synchronous=NORMAL`（commit 7.5ms→0.23ms，soak 实测），即 Candidate #4。
建议 2B2：先做 DURABILITY_EXPERIMENT（仅 benchmark 层 A/B 验证 shadow 路径 NORMAL
的收益与丢失窗口，CRITICAL 订单路径保持 FULL），再决定是否实施连接生命周期改造。

## 8. 复现

```powershell
uv run python -m benchmarks.persistence_profiler --repeats 5 --output artifacts/performance/phase_2b1_after.json
uv run pytest tests/test_shadow_replay_convergence.py -q
```
