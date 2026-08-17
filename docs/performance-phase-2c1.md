# Phase 2C1 — Strategy Compute Profiling & Hotspot Attribution

PROFILE FIRST。本轮零生产改动：只新增 `benchmarks/strategy_profiler.py`、
`tests/test_strategy_profile_tools.py` 与本文档。历史性能文档（2A/2B1-2B4）未动。
机器数据：`artifacts/performance/phase_2c1_strategy_profile.json` + `.pstats`（gitignored）。

## 1. 冻结基线验证

```text
pytest=698/698 PASS（进入本轮前实测，exit=0）  ruff=PASS  mypy --strict=PASS
Replay session=WAL+NORMAL(1)（Database.open_reconstructible_replay_connection，强制 WAL 校验）
Default Database.connect=FULL(2)  continuous shadow/critical order/migration=FULL
```

## 2. Strategy Call Graph（静态）

```text
candle → VWAPShadowStrategy.on_bar
  ├─ 校验（confirmed/volume>0；违规清窗）
  ├─ deque[maxlen=24].append(bar)                      O(1)
  ├─ rolling_vwap(bars, 24)：                           ★O(window) 全窗重扫
  │    list(deque)[-24:]  → 24 元素拷贝
  │    sum(volume)                          + 24 Decimal 加
  │    sum((h+l+c)/Decimal("3") * volume)   + 24×(2加+1除+1乘+1加)  ← 生成器内每 bar 构造 Decimal("3")
  │    weighted/total                        + 1 除
  ├─ deviation=(vwap-close)/vwap*10000  threshold=vwap*(1-100/10000)   ~6 Decimal 运算（含 2 常量构造）
  └─ _signal：sha256(identity) 1 次 + isoformat 2 次 + Signal dataclass + metadata dict
replay 侧/candle：state_snapshot→json.dumps（1 键）+ signal_value→json.dumps（6 键，5 次 Decimal→str）
                 + runtime_state json.dumps + session.commit（SQL 侧）
```

**VWAP 复杂度结论：每 candle O(window=24) 重扫，非增量 O(1)**（两个生成器共 23.6 次调用/candle，cProfile 实测）。

## 3. 测量方法与开销

```text
Method A = 显式 per-bar 计时器（on_bar/两个 json.dumps 分段；instrumentation-only，非 app 代码）
Method B = cProfile + pstats（标准库；top50 cumulative/self 已存 .pstats）
显式计时器开销 ≤ ~0.2µs/bar（4 次 perf_counter_ns），<1%
cProfile 开销 = 92%（对 20ms 级 compute-only 循环；固定成本主导）→ cProfile 只用于结构归因，
绝对时间以 Method A 为准。两方法方向一致：rolling_vwap 主导 on_bar。
```

## 4. 时间分解与 per-candle 成本（Method A，10 repeats，计数全 deterministic）

```text
compute-only（无持久化，真实策略/Decimal/Signal/序列化）:
  Workload A warmup-heavy(124)  18.3 µs/candle   on_bar 12.1 | sig_json 2.7 | state_json 1.6
  Workload B steady-no-signal(300) 17.0 µs/candle  on_bar 11.7 | 2.4 | 1.4
  Workload C signal-gen(424, 16 buys) 17.4 µs/candle on_bar 11.9 | 2.5 | 1.4
  mixed_1000（seed 合成）   20.9 µs/candle（median 0.0202s）
  mixed_10000               26.6 µs/candle（median 0.2079s）

端到端（scoped NORMAL replay，本机快态）:
  mixed_10000: compute 0.208s + persistence 2.898s ≈ 3.11s
  ⇒ compute ≈ 6.7%，persistence ≈ 93%，orchestration 计入 compute 侧
```

## 5. 调用频率（cProfile，C workload，424 bars）

```text
on_bar=1.0/candle   rolling_vwap=1.0/candle   _signal=1.0/candle   state_snapshot=1.0
json.dumps=2/candle（signal_value + state）  encoder.encode=4/candle（两阶段）
sha256=1/candle（signal identity；self 0.3ms/424 bars ≈ 1.5% compute —— 明确排除）
isoformat=2.04/candle（identity + metadata）
Decimal 构造/运算：CPython C-decimal 在 cProfile 下不可见 → NOT_SEPARATELY_MEASURED（动态）；
静态推导：~150 次 Decimal 算术/candle + 24 次 Decimal("3") 常量构造/candle（生成器内）
对象分配：Signal(dataclass,slots) 1 + metadata dict 1 + 2 个 genexpr 迭代器/candle（无 pydantic 验证热路径）
```

## 6. SIGNAL vs NO_SIGNAL（Method A per-bar 中位数）

```text
no_signal on_bar = 12.1 µs   signal on_bar = 12.6 µs   ⇒ 增量 ≈ +0.5µs（~4%）
signal 的真实额外成本在持久化侧（proposal INSERT ×2 同事务），Python 构造侧几乎免费
无重复序列化：每 candle 恰 1 次 signal_value dumps + 1 次 state dumps（无同载荷重复）
```

## 7. TOP COMPUTE HOTSPOTS（cProfile cumulative，占 compute wall %）

```text
#1 on_bar              424 calls  cum 62.4%  （VWAP+判定+Signal）     VWAP/SIGNAL
#2 rolling_vwap        424 calls  cum 37.7%  self 0.73ms              VWAP
#3 builtins.sum        802 calls  self 2.46ms                          VWAP(DECIMAL)
#4 _signal             424 calls  cum 19.8%  self 1.34ms              SIGNAL
#5 <genexpr>×2      10025 calls  self 4.1ms                           VWAP/ALLOCATION
#6 json.dumps          850 calls  cum 16.4%                           SERIALIZATION
#7 encoder.encode      850 calls  cum 12.1%                           SERIALIZATION
#8 iterencode          850 calls  self 1.3ms                          SERIALIZATION
#9 datetime.isoformat  865 calls  self 0.9ms（4.5%）                  TIMESTAMP
#10 sha256             424 calls  self 0.3ms（~1.5%，排除）           HASHING
```

## 8. Optimization Ceiling（Amdahl）

```text
最大单热点 rolling_vwap：占 compute 37.7% ⇒ 端到端占比 ≈ 37.7% × 6.7% ≈ 2.5%
完全消除它 max_speedup ≈ 1/(1-0.025) ≈ 1.026×
compute 内全部热点（VWAP+json+signal）一并消除 ≈ 1.07×（端到端）
persistence 已在 2B4 判定 COMPLETE（NORMAL scoped）
```

## 9. Realtime Headroom

```text
configs/btc_vwap_shadow.yaml：bar=1h ⇒ required ≈ 1/3600 = 0.00028 candles/s
（最细配置 5m ⇒ 0.0033 candles/s）
benchmark（2B4 NORMAL 10000）≈ 2310 candles/s；本机快态 ≈ 3200 candles/s
headroom ≈ 8×10^6（1h）/ ~10^6（5m）
⇒ PERFORMANCE_ALREADY_SUFFICIENT（裕量百万倍级）
```

## 10. 候选清单（本轮只登记）

```text
Candidate 1: rolling_vwap 增量化 O(window)→O(1)
  Current cost: 17.8µs/candle（compute 的 ~60%）；Frequency: 每 candle
  Direction: 窗口滑动时减去出窗 bar、加入新 bar（维护增量和）
  Expected benefit: compute ~2×；端到端 ≈1.03×
  Semantic risk: 中——Decimal 加法在默认 28 位精度下不满足结合律，增量重排求和顺序
                 可能改变 VWAP 末位；必须与现实现做逐 bar parity 验证（独立阶段）
  Complexity: M；Regression surface: 策略信号序列/回测 parity 全集
  Classification: SEMANTICS_SENSITIVE（requires independent correctness study）
Candidate 2: Decimal("3") 与阈值常量提升为模块级常量
  Expected benefit: ~1µs/candle；端到端 <0.5%；逐值等价（同字符串构造）
  Classification: SAFE_LOCAL_OPTIMIZATION，但 NOT_WORTH_OPTIMIZING（端到端收益微）
Candidate 3: signal_value 序列化——已确认无重复，无候选（明确排除）
Candidate 4: sha256/isoformat——占比 ~1.5%/4.5%，明确排除（§18/§19 纪律）
Candidate 5: 无架构级候选（compute 仅 6.7%，任何重构收益天花板 1.07×）
```

## 11. 结论

```text
should_we_keep_optimizing = NO_PERFORMANCE_ALREADY_SUFFICIENT
realtime headroom 百万倍级；最大单热点端到端占比 2.5%，天花板 1.03×。
persistence（2B4 完成）与 compute（本轮画像）均不再构成现实瓶颈。
```

## 12. 复现

```powershell
uv run python -m benchmarks.strategy_profiler --repeats 10
uv run pytest tests/test_strategy_profile_tools.py -q
```
