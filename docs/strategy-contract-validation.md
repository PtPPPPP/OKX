# P1 V3 策略契约实际验收

验证日期：2026-10-16。Windows，CPython 3.12.14，既有 uv 0.12.3 缓存。
实现及本次产物位于 `C:/program1/coding/OKX-p1-strategy-contract`，原 `OKX` 目录保持不变。
未增加依赖，未安装或升级原研究环境，未调用私有账户或交易接口，未执行实盘、部署或推送。

## 实现与职责

| 位置 | 统一后的职责 |
|---|---|
| `app/strategies/base.py`、`registry.py`、`v3_parameters.py` | 保留 Strategy；可选 StatefulStrategy 扩展、五类 V3 注册、版本绑定和参数校验 |
| `app/strategies/v3_features.py`、`v3_timeframes.py` | 统一逐根因果特征、已完成 4H 聚合和保守预热长度；原批量公式仅供对照 |
| `app/strategies/v3.py` | 同一版本的 episode 信号、24H 持仓政策、逐 ID 订单反馈和版本化 checkpoint |
| `app/domain/market.py`、`context.py`、`runtime/clock.py` | 已确认行情、UTC 开盘/收盘语义、连续性、显式预热及只读上下文 |
| `backtest/engine.py`、`research_execution.py`、`strategy_v3_candidates.py` | 注册对象贯通研究及回测，逐根预热，真实策略配置与输出身份；兼容旧特征导入入口 |
| `app/trading_engine.py`、`reproducibility.py`、`bootstrap.py` | 组装、事件和运行清单使用同一策略身份；执行撮合及风控保持各自职责 |
| `app/services/strategy_shadow.py` | 显式只读的逐根行情/反馈适配，不推断成交、不创建 broker 或交易客户端 |
| `app/services/vwap_continuous_shadow.py` | 原专用流程保留，版本常量引用注册表；原策略文件字节及持久化格式不变 |

契约细节、配置示例和时间/恢复规则见 [strategy-contract.md](strategy-contract.md)。

## 实际命令与结果

以下命令的工作目录均为隔离副本，Python 只读复用原目录的已有解释器；已核实 `app`、
`backtest` 实际从隔离副本导入，不从原开发目录导入。快照集成测试显式提供 uv 路径，
在临时目录恢复隔离环境，使用已填充缓存离线运行。

```powershell
Set-Location C:/program1/coding/OKX-p1-strategy-contract
$python = 'C:/program1/coding/OKX/.venv/Scripts/python.exe'
$env:RESEARCH_TEST_UV = 'C:/Users/PtP/AppData/Local/hermes/tools/uv-0.12.3-win32-x64/uv.exe'
& $python -m pytest -o addopts='' -q
& $python -m ruff check .
& $python -m ruff format --check .
& $python -m mypy
```

- 完整 Pytest：**872 passed，0 failed，0 skipped，684.22 秒，退出码 0**。
  包含 68 项新增契约测试及全部原有订单授权、风控、私有状态、恢复、研究治理和快照测试。
- Ruff check 通过；格式检查 **304 个文件**通过；Mypy **189 个源码文件**通过。
- [完整回归日志](../artifacts/p1_full_pytest_final_20261016.log)。测试运行期间没有修改源码。

| 验收项 | 实际覆盖 |
|---|---|
| 公式/批量一致性 | 十个原变体 × 三个 4H 起点，逐项比较 candidate 与两个消融序列 |
| 逐根/批量/恢复信号 | 十个变体，完整信号字段和最终状态相同；保存到文件后新实例恢复 |
| 因果性 | 修改未来 OHLC/volume，之前的信号不变 |
| 高周期边界 | 第十二根完整 4H 收盘前不能启用对应指标，收盘后才能启用 |
| 预热 | 更新上下文与 episode 前态，不发出信号、不创建验证区间外订单 |
| 持仓和挂单恢复 | 两张挂单、部分成交、持仓、成交时间及 24H 退出计时，在恢复后保持一致 |
| 执行反馈贯通 | 捕获真实 BacktestEngine 的行情、订单和持仓反馈，逐项输入只读适配器，完整信号及最终策略状态一致；测试中有实际模拟成交和已平仓交易 |
| 非法行情 | 重复、乱序、缺口、未确认、未对齐输入拒绝且状态不变，补齐后可继续 |
| 不兼容状态 | 版本、参数、身份、哈希、历史长度、门控、持仓或挂单不匹配时拒绝，不静默重置 |
| 身份和模式 | 配置、注册表、对象、清单、事件、报告一致；不可用版本、缺少版本、Demo 接入被拒绝 |

前次暂停前的完整测试曾有两项失败：旧 VWAP 文件字节变化，以及测试进行中编辑元信息源码导致
父进程缓存与快照子进程不一致。旧 VWAP 文件已按原字节恢复，固定哈希预期未变；没有降低
复现指纹检查。前次日志保留，本次稳定源码的完整回归已覆盖并通过这两项。

## 离线回放与只读逐根示例

实际执行：

```powershell
& $python -m scripts.demo_strategy_contract --output artifacts/p1_strategy_contract_demo_20261016
```

再次演示请换一个尚不存在的输出目录，避免覆盖本次证据。使用既有固定开发期 CSV，
`relative_volume_breakout` / `v3_step_v1` / `volume_breakout_20`，参数为原 PRIMARY 的
`breakout=20, volume_window=24, volume_multiple=1.5`，未覆盖任何候选参数。

- CSV 实际 **599 根**（文件名虽含 600），预热 **300 根**，产生 **299 条信号**、**9 条可行动意图**。
- 批量 `replay`、逐根 `on_candle` 和中途 checkpoint 恢复输出相同。
- 数据 SHA-256：`d931461c41d28ab8748aa51a637488e08ddf86540c1303370f98545d0571960c`。
- 信号指纹：`b235ceb5c5ed57f37ae0bb77f74de318711a4d732bbbe3c22e9eb1e587793ff6`。
- 三条路径使用不同 run ID，信号不依赖 run ID；示例没有撮合或推测订单与成交。
- 这是工程开发集验收，不是样本外收益证据，报告中 `trading_permission=false`。

[示例报告](../artifacts/p1_strategy_contract_demo_20261016/report.json)、
[批量信号](../artifacts/p1_strategy_contract_demo_20261016/batch_signals.json)、
[逐根信号](../artifacts/p1_strategy_contract_demo_20261016/step_signals.json)、
[恢复状态](../artifacts/p1_strategy_contract_demo_20261016/checkpoint.json)。

## 旧研究保护及行为差异

- 对 `development-origin.json` 的 **360 个原始来源文件 + 1964 个保护文件**逐个 SHA-256
  核对，共 2324 个，无缺失或变化；包括原数据、冻结、报告、消费记录和 P0 快照及其记录。
- 候选 JSON、uv.lock、pyproject.toml、原 VWAP Shadow 策略文件精确字节不变，完整执行政策
  与原 cohort 相同。未重新选候选，未调整费用、滑点、止损、24H 政策、晋升门槛或窗口。
- 再次运行原 `legacy_snapshot/snapshot.py verify` 成功，快照哈希仍为
  `95d363a627960a78ca6ad07acd0c2482ecde3fa77682505a877183c459c49368`。
  P0 的旧批次仍是明确标注证据边界的事后归档，不升级其历史证明力度。
- 旧 `_active_order` 布尔值可能因 A 单结束而误清除 B 单阻塞；新版本按 ID 管理。
  这是明确的新版本行为差异，旧结果不重算、不覆盖。另统一了真实策略名称、版本绑定的信号 ID
  和不规则行情拒绝规则；有效连续行情的候选公式对照未发现不一致。
- 新开发实现只能形成新产物和新批次，不能借迁移或复制解除原样本外消费约束。

[机器可读验收与保护复核](../artifacts/p1_contract_acceptance_20261016/verification.json)。
运行产物被 Git 忽略，需要随本地交付另行保留；不能仅靠源码仓库链接宣称已备份运行证据。

## 尚未兼容或验证的范围

新完整状态契约优先覆盖五类 V3。MA、buy-and-hold、VWAP mean reversion 沿用旧接口；
VWAP Shadow 仍用其原专用 checkpoint 和 SQLite 生命周期，未强制升级格式。
没有将原 VWAP 的 WebSocket/重连/落库协调器泛化成 V3 持续服务，没有旧 V3 私有适配器
状态迁移或 broker/账户原子恢复。调用方仍负责公共数据缺口补齐、反馈核对和信号/状态原子落库。

同一行情和反馈产生一致信号，不保证不同成交模型的成交或经济结果一致。
本次没有验证真实公共 WebSocket 长期运行、交易所执行、另一台机器、跨平台恢复或无缓存的
离线依赖恢复；没有新增实盘能力。原冻结目录没有合入本次修改。
