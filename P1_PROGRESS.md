# P1 策略契约进度（已暂停）

2026-10-15，按用户“先暂停，保存现有进度，待会再继续任务”的要求停止开发。此文件是恢复入口；尚未交付完成，不应将下述阶段性结果表述为全部验收通过。

## 工作位置与保护要求

- 原项目：`C:\program1\coding\OKX`。
- **P1 全部改动位于隔离副本：`C:\program1\coding\OKX-p1-strategy-contract`。后续继续在这里工作。**
- 原目录仍有被既有冻结绑定的源码；没有将 P1 修改合回原目录，没有更新旧冻结哈希。
- `development-origin.json` 保存原始文件哈希、复制来源及受保护文件清单。最近核对原目录共 2324 个来源及研究相关文件，内容哈希均保持不变。
- 未更改候选参数、uv.lock、手续费、滑点、止损、晋升门槛或验证窗口。新的执行政策与原冻结政策逐项相同。
- 未访问私有交易 API、下单、操作资金、推送、部署或提交 PR；未向原项目 .venv 安装依赖。
- 两个目录均未发现 Git 仓库；不要用 Git 提交代替现有文件保护。

## P0 检查

原项目已有版本化、哈希清单、隔离运行及数据绑定的研究快照能力，已实际验证原快照入口：

```powershell
C:/program1/coding/OKX/.venv/Scripts/python.exe -I C:/program1/coding/OKX/artifacts/research_snapshot_p0/legacy_snapshot/snapshot.py verify
```

结果通过。旧档案属于事后归档，证据边界和本地复制限制保持原文。P1 未修改这些已有快照、报告、数据或样本外消费记录。

## 已实现

1. 复用 Strategy 接口及注册表，增加可选 StatefulStrategy 契约、策略实现版本、参数校验、身份信息和版本校验。五类 V3 候选通过同一注册入口创建，研究配置不再以 buy_and_hold 冒充 V3。
2. 将 V3 特征和高周期聚合移入 app/strategies，研究批量信号通过同一个逐根 V3FeatureStream 更新。保留旧向量化公式作为一致性参考，兼容旧 backtest 导入入口。
3. 统一 UTC 开盘时间、收盘决策时刻、已确认行情、完整高周期、预热和重复/乱序/缺失行情拒绝规则。
4. V3Strategy 管理信号、持仓和订单反馈；提供带版本、身份、参数、哈希、滚动行情和订单账本的状态保存/恢复，恢复时核对持仓及挂单，不兼容时明确拒绝。
5. BacktestEngine/TradingEngine 接入同一预热及逐根路径；配置、运行对象、事件、清单和报告记录相同策略身份。
6. 新增 ConfirmedStrategyShadow，只接收已确认行情和外部显式反馈，不含账户查询或下单能力。
7. 提供开发集离线示例及文档，不使用已消费样本外数据调试。

主要文件：

- app/strategies/base.py、registry.py、v3.py、v3_parameters.py、v3_features.py、v3_timeframes.py
- app/domain/context.py、market.py；app/runtime/clock.py；app/config/run_config.py；app/bootstrap.py
- app/trading_engine.py、app/reproducibility.py
- app/services/strategy_shadow.py、vwap_continuous_shadow.py
- backtest/engine.py、research_execution.py、research_governance.py、strategy_v3_candidates.py、strategy_v3_features.py
- scripts/demo_strategy_contract.py、scripts/research_snapshot.py
- tests/test_v3_strategy_contract.py、test_strategy.py、test_research_cycle.py
- docs/strategy-contract.md，以及 README、架构与研究流程文档链接

重要：`app/strategies/vwap_shadow.py` 已恢复为原文件的精确字节，原有固定哈希测试未放宽。其版本元信息由注册表统一提供，不更改旧策略实现或状态格式。

## 实际验证结果

- 新增策略契约测试：68 passed，覆盖批量/逐根、未来数据不影响过去、高周期边界、状态恢复、持仓及挂单、异常行情、身份一致和真实回测反馈重放。
- 最近相关回归组合：74 passed（prospective architecture、V3 contract、strategy）。
- Ruff check 通过；Ruff format --check 检查 304 文件通过；Mypy 检查 189 源码文件通过。
- 初次完整回归：**870 passed，2 failed，384.66 秒**，日志 `artifacts/p1_full_pytest.log`。
  - 第一项是旧 VWAP 文件固定哈希失败；已恢复原文件字节，未更改预期哈希。
  - 第二项为临时合成快照复现的指纹比较失败：完整测试运行期间修改了身份元信息函数，父进程缓存旧函数、快照子进程读取新函数。比较证据显示仅 buy_hold_control.strategy_identity 中 parameters 字段的有无不同，未发现经济指标差异。未放宽指纹比较。
- 修复后在源码稳定状态重新执行上述两项：**2 passed，22 deselected，23.87 秒**。命令见下。**尚未再次完成整套测试，不能声称全部 872 项通过。**
- 用户暂停时，已启动的针对性测试刚完成；没有留置测试或开发后台任务。

## 已运行示例

```powershell
C:/program1/coding/OKX/.venv/Scripts/python.exe -m scripts.demo_strategy_contract --output artifacts/p1_strategy_contract_demo
```

在隔离副本根目录运行成功，使用固定开发 CSV，原有 volume_breakout_20 参数：

- 输入实际 599 根，预热 300 根，输出 299 条信号，其中 9 条可行动信号。
- 批量与逐根输出相同；中途保存恢复与连续运行相同。
- 输入哈希：`d931461c41d28ab8748aa51a637488e08ddf86540c1303370f98545d0571960c`。
- 信号指纹：`b235ceb5c5ed57f37ae0bb77f74de318711a4d732bbbe3c22e9eb1e587793ff6`。
- 输出：`artifacts/p1_strategy_contract_demo/report.json`、两份信号文件和 checkpoint.json。
- 这是工程开发集验证，不是样本外策略证据；示例不推断成交、不授予交易权限。

## 恢复用命令与环境

所有后续命令工作目录必须是隔离副本根目录。现有 Python 可只读复用；已核对 app/backtest 实际导入路径属于隔离副本。

```powershell
Set-Location -LiteralPath 'C:\program1\coding\OKX-p1-strategy-contract'
$env:RESEARCH_TEST_UV='C:/Users/PtP/AppData/Local/hermes/tools/uv-0.12.3-win32-x64/uv.exe'
C:/program1/coding/OKX/.venv/Scripts/python.exe -m pytest tests/test_prospective_architecture.py tests/test_research_snapshot.py -o addopts='' -q -k 'external_data_can or production_strategy_is_untouched'
```

上面的最后一次针对性命令已通过。快照测试使用测试临时目录的隔离 .runtime 和已有 uv 缓存，不安装到原研究环境。

## 待完成（用户恢复后再执行）

1. 阅读本文件，检查隔离副本和保护清单是否有后续人工改动。
2. 冻结本次开发源码后，重新运行完整回归，日志另存 `artifacts/p1_full_pytest_final.log`。**完整快照测试运行期间不要修改源码**，避免父进程缓存与子进程源码不一致。
3. 若有新增修改，再执行相应 Ruff、格式和 Mypy 检查；必要时重新生成新目录中的演示产物。
4. 再核对原项目受保护文件、候选配置、uv.lock 和执行政策没有变化。
5. 写出最终 P1 验收记录，中文交付说明实际测试数量、行为差异及兼容范围；目前尚未写最终验收报告。

## 已知兼容边界

- MA、buy_and_hold、VWAP mean reversion 保持旧接口，版本标为 legacy_unversioned，不宣称具有新的完整状态恢复保证。
- 原 VWAP Shadow 策略字节和专用持久化流程保留。新增通用只读适配器尚未替换其 WS/重连/SQLite 协调器。
- 不迁移旧 V3 私有适配器运行状态，不提供完整 broker/account 原子恢复，不增加实盘或交易所接入能力。
- 历史撮合与交易所成交语义仍不同，信号一致不能等同于成交收益一致。
- 旧冻结保留原代码及原结果，新契约不会静默重算或覆盖旧研究；尚未声称跨平台或离线环境恢复已验证。
