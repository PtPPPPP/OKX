# Phase 2B4 Replay-Scoped NORMAL Implementation Report

## 1. DURABILITY BOUNDARY

```text
default_database=WAL + FULL(2)
replay_session=WAL + NORMAL(1), reconstructible offline state only
continuous_shadow=WAL + FULL(2)
critical_order_paths=WAL + FULL(2)
migration=FULL(2)
```

生产源码调用关系已用 AST guard 固化：`replay_session()` 的唯一生产调用方是
`app/services/shadow_replay.py`；replay 专用连接 factory 的唯一调用方是
`ShadowReplaySession`。continuous runtime、Demo、订单和迁移均不能进入该连接。

NORMAL 不具备 FULL 相同的断电保证。系统级耐久性丢失后，Replay 可能需要从完整 candle
事务边界确定性重放。本项目没有验证物理断电实际会丢失多少事务。

## 2. CODE CHANGES

| file | change | reason | scope |
| --- | --- | --- | --- |
| `app/storage/database.py` | 增加封闭的 FULL / RECONSTRUCTIBLE_REPLAY profile、共享连接配置和 replay-only factory；设置后立即读回 | 防止 NORMAL 成为通用参数，并保证默认显式 FULL | 数据库连接初始化 |
| `app/services/continuous_shadow_repository.py` | `ShadowReplaySession` 改用 replay-only factory；补充正式耐久性限制 | 只让离线 Replay 使用 NORMAL | Shadow Replay |
| `docs/database-write-durability.md` | 固化 reconstructible、恢复和未验证边界 | 防止把 NORMAL 描述为断电安全 | 文档 |
| `tests/test_replay_durability_boundary.py` | 增加默认/replay/continuous/migration 实际设置和静态 scope guard | 防止 NORMAL 泄漏 | 结构测试 |
| `tests/test_shadow_replay_lost_tail.py` | 增加 K=1/5/10 整事务 snapshot 丢尾恢复 | 验证确定性收敛与资金隔离 | 恢复测试 |
| `benchmarks/durability_experiment.py` | NORMAL 改为正式 replay 默认；FULL 仅由 benchmark harness 覆盖 replay factory | 获得同路径可复现对照 | benchmark-only |
| `benchmarks/persistence_metrics.py` 及相关测试 | 补齐严格类型 | 使 `mypy --strict .` 覆盖全仓库 | 质量门，无业务变化 |

## 3. EFFECTIVE SQLITE SETTINGS

均为实际连接读回：

```text
replay:
  journal_mode=wal
  synchronous=1

default:
  journal_mode=wal
  synchronous=2

continuous_shadow:
  synchronous=2

critical_sample:
  default/order repository connection=2
  migration connection=2
```

## 4. TRANSACTION INVARIANTS

1000-candle 正式 Replay 实测：

```text
one_candle_one_transaction=true
connections_per_candle=0.006
commits_per_candle=1.005
writes_per_candle=4.887
cross_candle_transaction=false
nested_transaction=false
atomicity=true
```

FULL 与 NORMAL 均为 6 connections、1005 commits、4887 writes。没有合并 candle，SQL
write set 和事务频率未变。一个 candle 是设计恢复单元，不是物理断电丢失上限承诺。

## 5. LOST-TAIL RECOVERY

测试在第 N-K 根成功提交后使用 SQLite backup 取得一致 snapshot，不执行跨表 DELETE。恢复时
使用真实 committed checkpoint、真实策略计算、真实 replay session 和 canonical candle
transaction 重放 tail。

```text
K=1:  converged=true
K=5:  converged=true
K=10: converged=true

duplicate_processed_candles=0
duplicate_signal_events=0
duplicate_proposals=0
duplicate_runtime_transition=0
broker_writes=0
```

比较范围包含 processed candles、runtime final state、signals、proposals、proposal events、
run counters、last timestamp 和 final run state。

## 6. FUNDS-SAFETY ISOLATION

```text
replay_order_intent_executable=false
replay_can_reach_ControlledDemoWriteService=false
replay_can_reach_broker_submit=false
submission_performed=0
quantity=0
notional=0
orders_created=0
demo_order_proposals_created=0
```

## 7. FAILURE MODELS

```text
python_exception=whole candle rolled back; connection remains reusable
process_before_commit=partial_transaction false; zero rows after restart
process_after_commit=database readable; two committed probe rows present
OS_CRASH=NOT_TESTED
POWER_LOSS=NOT_TESTED
```

Process crash 不等于 OS crash，也不等于 power loss。没有增加自动重试、静默重连或当前事务
透明重放。

## 8. PERFORMANCE

首次正式完整执行；100/1000 各 10 repeats，10000 为单次方向性结果：

| candles | FULL median / p95 | NORMAL median / p95 | speedup | FULL/NORMAL mean commit |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0.698s / 0.779s | 0.434s / 0.540s | 1.61x | 2.732ms / 0.351ms |
| 1000 | 3.324s / 3.545s | 0.833s / 0.856s | 3.99x | 2.657ms / 0.264ms |
| 10000 | 29.084s / 29.084s | 4.332s / 4.332s | 6.71x | 2.628ms / 0.262ms |

```text
same_connections=true
same_commits=true
same_writes=true
same_business_outputs=true
effective_synchronous_verified=true
```

原始结果：`artifacts/performance/phase_2b4_durability_ab.json`。

## 9. WAL OBSERVATION

```text
wal_autocheckpoint=1000
journal_size_limit=-1
NORMAL wal_size_after_clean_close:
  100=4,132,392 bytes
  1000=0 bytes
  10000=0 bytes
checkpoint_observation=not sampled; PASSIVE/FULL checkpoint would mutate observed WAL
```

0 表示最后连接关闭后的 checkpoint/清理结果，不表示运行期间没有 WAL。未调整
`wal_autocheckpoint`、`journal_size_limit` 或其他 WAL 参数。

## 10. BEHAVIOR EQUIVALENCE

```text
strategy_results_identical=true
signals_identical=true
proposals_identical=true
runtime_final_state_identical=true
trading_behavior_changed=false
```

FULL/NORMAL 的 candle、BUY、proposal、连接、提交、写入和逐表写入计数完全一致。

## 11. PRODUCTION CHANGE

```text
production_behavior_changed=true
change_scope=replay_durability_only

production_replay_durability_changed=true
critical_durability_changed=false
trading_behavior_changed=false
strategy_behavior_changed=false
```

## 12. QUALITY GATES

```text
ruff=PASS
mypy --strict .=PASS (287 source files, 0 errors)
pytest=PASS (698 passed, 0 failed)
security_smoke=PASS (102 passed, 0 failed)
fresh_clone=PASS
replay_convergence=PASS
connection_lifecycle=PASS
```

## 13. PERFORMANCE PROFILE AFTER

使用 1000-candle、10-repeat NORMAL 中位数作为稳定画像：

```text
database_time_share=42.52%
commit_time_share=31.75%
sql_time_share=10.29%
strategy_python_share=57.48%
```

10000 单次方向性结果中 DB share 为 69.87%，但不是重复统计样本；不能据此继续引入 batching、
index 或全局连接池。

## 14. PERSISTENCE OPTIMIZATION DECISION

```text
replay_persistence_optimization=COMPLETE
```

连接复用与 replay-scoped NORMAL 已兑现主要收益。1000-candle 重复样本中策略/Python 已是
最大占比，继续改变持久化语义缺少收益与风险依据。

## 15. NEXT STEP

```text
STRATEGY_COMPUTE_PROFILING
```

只推荐，不在本阶段实施。continuous shadow durability 必须另立阶段重新证明，不能继承本轮
Replay 结论。

## 16. FINAL STATUS

```text
replay_scope_isolated=true
default_FULL_preserved=true
critical_FULL_preserved=true
NORMAL_effective_verified=true
lost_tail_replay_converges=true
funds_side_effects=0
one_candle_atomicity_preserved=true
all_tests_pass=true
durability_limits_documented=true

READY_FOR_NEXT_PHASE=true
PHASE_2B4_REPLAY_NORMAL_COMPLETE
```
