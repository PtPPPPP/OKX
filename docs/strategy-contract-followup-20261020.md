# P1 策略契约补全：2026-10-20

## 工作位置和既有成果

原 `C:/program1/coding/OKX` 仍与既有冻结绑定，保留其全部源码、研究数据、报告和消费记录。
此前 P1 主体及缓存修复已完成，基线为 `f1b8805`，原有逐根特征、注册入口、预热、状态恢复和
只读适配器均继续复用。v1 的历史验收见 [原记录](strategy-contract-validation.md)。

本轮只在已有 Git 副本 `C:/program1/coding/OKX-github-upload-20261016` 中补全，不合回冻结目录，
不改写原 `OKX-p1-strategy-contract` 副本及 2026-10-16 的报告或 D 盘备份。
验证复用原环境的解释器，不安装依赖；未调用私有账户、提交订单、推送或部署。

## 实际复现的缺口与修复

复现：先处理至 03:00 的已确认收盘行情，再将已知订单的 02:30 反馈直接送入核心策略。
v1 核心接受并改变了状态；只读适配器对同一事件拒绝，报 `out-of-order Shadow feedback`。
因此“乱序反馈如何处理”仍依赖调用入口。另一个风险是迟到反馈的处理时间不等于交易所
更新时间；仅保存后者，不能在恢复后还原已经推进到的逻辑时刻。

现在由 V3 核心保存 `last_event_at`，行情和反馈都检查有时区的 UTC 逻辑时间且不允许倒退。
校验失败不改变状态；相同时刻仍允许按既有顺序处理反馈，重复反馈不重复推进业务状态。
恢复时核心和适配器时钟必须一致。状态版本由 Strategy 扩展和注册元数据统一声明，报告、
运行清单和配置不再硬编码状态格式 1。

实现显式升级为 `v3_step_v2`，策略状态格式为 2。旧格式 1 缺少处理时刻，不自动迁移或重置；
旧实现、旧结果及旧状态可以在保留的 v1 副本或 Git 基线中继续使用。外层 Shadow 封装格式仍为 1。
有效连续行情的候选公式、候选参数、手续费、滑点、止损、24H 持仓政策和晋升门槛均不变。
版本绑定的信号 ID 与元信息会变化，不要求它们等于 v1；业务动作需单独核对。

## 使用与验证

```powershell
Set-Location C:/program1/coding/OKX-github-upload-20261016
$python = 'C:/program1/coding/OKX/.venv/Scripts/python.exe'
& $python -m scripts.demo_strategy_contract --output artifacts/p1_contract_demo_20261020
```

示例仅使用固定开发期行情，分别执行批量 replay、逐根 on_candle、中途保存和恢复，比较完整
信号及最终策略状态。输出目录必须是新目录，不覆盖前次证据；不推测成交，不作为样本外研究。

新增回归覆盖直接核心/Shadow、连续/恢复、跨行情的倒退反馈、迟到反馈的处理时间、UTC、
同刻重复反馈，以及格式和时钟不兼容时的原子拒绝。

## 实际运行结果

- 本轮相关回归：**95 passed**，包括原 68 项 V3 契约测试、23 项新增事件时钟测试和策略注册测试。
- 完整 Pytest：**898 passed，0 failed，0 skipped，425.25 秒，退出码 0**。使用现有 uv 路径，
  实际运行了快照离线环境恢复测试，没有通过跳过集成来获得通过。
- `ruff check .` 通过；`ruff format --check .` 检查 **305 个文件**通过；`mypy` 检查 **189 个源码文件**通过。
- 599 根固定开发行情，300 根预热，输出 299 条信号，其中 9 条可行动意图；批量、逐根、恢复后
  的完整信号及最终策略状态一致。`trading_permission=false`，没有推测或提交订单。
- 与保留的 v1 开发集输出逐项核对，299 条信号的策略名、标的、动作、时间、原因、置信度完全相同。
  新信号指纹为 `c2c777140ce10d70f574787b43704d1eb11fb62750e8b2c80a9899d049169ceb`；
  版本、ID 变化是显式升级结果，旧指纹及旧报告保留。
- 输入 SHA-256 仍为 `d931461c41d28ab8748aa51a637488e08ddf86540c1303370f98545d0571960c`。
- 原目录保护清单共 2324 个文件，工作前后 SHA-256 全部不变；候选 JSON、uv.lock、pyproject.toml、
  原 VWAP Shadow 源码字节及冻结执行政策保持不变。原 P0 快照 verify 通过，快照哈希仍为
  `95d363a627960a78ca6ad07acd0c2482ecde3fa77682505a877183c459c49368`。

[完整测试日志](../artifacts/p1_contract_full_20261020.log)、
[示例报告](../artifacts/p1_contract_demo_20261020/report.json)、
[批量信号](../artifacts/p1_contract_demo_20261020/batch_signals.json)、
[逐根信号](../artifacts/p1_contract_demo_20261020/step_signals.json)、
[v1/v2 动作对照](../artifacts/p1_contract_demo_20261020/compatibility.json)、
[机器可读验收](../artifacts/p1_contract_acceptance_20261020/verification.json)。
产物均在本 Git 副本中新增，未覆盖历史产物。本轮未提交或推送代码，没有新的远端 CI 结论。

## 保留的兼容边界

优先覆盖五类 V3；MA、buy-and-hold、VWAP mean reversion 和原 VWAP Shadow 专用状态流程
维持原范围。本轮不泛化 WebSocket 服务，不实现账户/撮合器原子恢复，不迁移旧 V3 状态。
信号一致不等于不同执行环境的成交一致；不宣称长期实况运行或跨平台恢复已经验证。
