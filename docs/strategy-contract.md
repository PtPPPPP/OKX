# V3 统一策略契约 v1

本次实现位于隔离开发目录 `C:/program1/coding/OKX-p1-strategy-contract`。
原 `OKX` 目录仍被旧冻结的部分源码哈希绑定，没有修改其中的源码、参数、快照、数据、报告或消费记录。
`development-origin.json` 保存复制来源、初始源码哈希及受保护文件清单。没有复制凭证、账户数据、
历史研究产物或虚拟环境；验证复用原环境的 Python，不向其安装包。

## 统一范围和入口

保留 `Strategy.on_start/on_bar/on_order_update/on_stop`，新增其可选扩展 `StatefulStrategy`。
现有注册表登记五个 V3 架构，不另建注册体系：

- `htf_pullback_recovery`
- `htf_breakout`
- `relative_volume_breakout`
- `htf_volume_momentum`
- `volume_exhaustion_reversal`

实现版本统一为 `v3_step_v1`，参数来自原 `strategy_v3_candidate_specs.json`。
参数模型禁止未知字段、布尔数值、无效窗口和非有限阈值；不复制一套默认参数。
运行对象名称及参数不可通过公共属性重写；参数字典返回独立副本。

V3 配置必须显式指定版本，无法解析的版本直接报错，不自动换用当前版本：

```yaml
mode: backtest
market:
  instrument_id: BTC-USDT
  bar: 1h
strategy:
  name: relative_volume_breakout
  implementation_version: v3_step_v1
  parameters:
    breakout: 20
    volume_window: 24
    volume_multiple: 1.5
```

上例是原 PRIMARY 参数，不代表候选通过训练或获准交易。五个架构在注册表标记 `research_only`，
普通 Demo 配置/执行拒绝接入；观察信号用显式的 `ConfirmedStrategyShadow`，它没有 broker、
交易客户端、账户查询或授权对象，不会把 BUY 意图当作成交。

研究成交复核通过注册表创建对象；配置不再伪装成 `buy_and_hold`。
`BacktestEngine` 在执行前检查 V3 配置、参数、版本与实际对象一致。
运行 manifest、回测 summary、信号/订单事件携带相同 `strategy_identity`：
策略 ID、实现版本、规范参数、状态契约版本。成交记录继续使用真实策略名称。

## 时间、预热和逐步更新

| 项目 | 契约 |
|---|---|
| Candle.timestamp | 有时区的 UTC **开盘时间**，对齐 1H |
| 行情确认 | `confirmed is True`，决策时钟等于开盘时间 + 1H；实况调用方须等待公共源确认 |
| OHLC/volume | 价格有限且为正，OHLC 合法，成交量有限且非负；零成交量按原 V3 公式处理 |
| 4H | 仅包含完整且连续的四根 1H，直到其收盘才能参与指标；跳过开头不完整桶 |
| 预热 | `StrategyContext.is_warmup=True`，只建立指标与 episode 前态，返回空信号列表，不创建订单 |
| 预热之后 | 不允许再次进入预热；不能用恢复来清除已消费的 episode 前态 |
| 重复 | 包括完全相同的重复 K 线，明确拒绝；无第二次信号且状态不变 |
| 乱序/缺口 | 明确拒绝、状态不变；调用方补齐缺失已确认数据后才能继续，不排序、不插值、不重置 |
| 反馈 | 在生成后续收盘信号前应用已知订单更新，携带同一时点的持仓快照 |

输入必须按逻辑决策时间递增；时间相同时也必须保持相同的反馈/收盘事件顺序。
不能把收盘后才获知的成交反馈回填到更早的收盘决策前。只读适配器拒绝时钟倒退，
不负责推测迟到反馈对历史信号的影响；需要由调用方先核对状态，再明确恢复。
历史撮合在开盘或收盘阶段产生的反馈沿用该模型的逻辑时间，不被描述为交易所真实成交时间。

`V3FeatureStream` 使用有界历史窗口，调用与旧公式相同的组件函数。
`required_history` 是按参数计算的保守预热长度：一小时最大窗口加一，或 `4 * htf_slow + 6`
中的较大值，额外 6 根覆盖首尾不完整 4H 桶。它是 bootstrap 指引，不是人为延后所有原公式的
硬门槛；各公式仍按自己的必要数据量启用。研究验证保留原有最多 300 根 warmup 政策。

`generate_v3_signals` 现在逐根调用该核心；传入的 4H 数据必须与同一 1H 序列的完整聚合一致。
`generate_v3_signals_reference` 保留旧批量计算供一致性测试，生产研究入口不再调用它。
十个原变体在三个不同 4H 起点上逐项比较 candidate、without-HTF、without-volume 序列。
研究中的事件收益模型仍是事件研究；共享候选门控不意味着旧信号事件研究的收益口径与
OHLC 限价成交模型相同。

## 状态与恢复

新实例先 `on_start`，初始必须空仓、无未知挂单，再逐根预热。
`on_start` 不用于恢复，已经初始化的对象拒绝再次初始化；`on_stop` 保留业务状态以便存档。
恢复必须创建新对象并调用 `restore_checkpoint_state(state, context, open_order_ids=...)`，
不再调用一次 `on_start`。非空仓首次启动必须提供兼容 checkpoint。

策略 checkpoint 的 `format_version=1`，包含：

- ID、实现版本、规范参数、标的、1H 时间尺度和内容哈希；
- 有界已确认行情窗口、累计数量、上一个候选门控、是否已经产生验证段信号；
- 持仓数量、首次已知买入成交时间；
- 全部未完成订单及最近 128 个终态反馈，保留订单 ID、方向、数量、状态和时间。

恢复校验版本、参数、窗口连续性、最后一个门控、数量、时钟及持仓；挂单 ID 必须由调用方
显式提供，逐项匹配 checkpoint。缺失、损坏或不兼容时拒绝，不自动升级/丢弃状态。
订单反馈不允许数量倒退、旧时间覆盖或终态重新打开；超出终态审计尾部的未知历史反馈要求
重新核对，不能被解释成一张新挂单。持仓不得在没有订单反馈的情况下悄悄改变。
哈希用于内容一致性，不证明来自可信第三方，也不替代真实账户对账。

`ConfirmedStrategyShadow` 只包装同一注册策略和重放时钟。`replay` 是 `on_candle` 的顺序循环。
它只保留最后一个信号，完整轨迹由调用方持久化；`save_checkpoint` 独占创建，避免覆盖旧状态。
恢复时仍须传入持仓快照和已核对的挂单 ID；不会查询私有账户，也不会根据意图推测持仓。
传输层的重复消息过滤、公共数据缺口补齐以及信号/状态原子落库，仍由调用方负责。

## 已发现的行为差异

1. 旧研究配置名称和实际对象不一致；新配置/报告统一真实 ID 和版本。
2. 旧 V3 适配器只有一个 `_active_order` 布尔值。A、B 同时挂起后，A 的终态反馈可能清除
   B 的挂单约束。新实现按订单 ID 管理；该情形会从可能的 BUY/CLOSE 变为 HOLD。
   有单独回归测试，视为明确的新实现版本行为，不改写旧结果。
3. 信号 ID 现在绑定版本、参数和标的，避免不同参数实验撞 ID；相同新版本输入的 ID 稳定。
4. 不规则数据以前可能由批量排序/跳桶掩盖；新 V3 入口明确拒绝。

没有改变候选参数、费用、滑点、止损/止盈、24 小时持仓政策或晋升门槛。
有效连续行情的候选公式未发现不一致。旧快照继续运行其原代码，不更新冻结哈希来兼容新代码；
新版本只能产生新的开发产物和研究批次，不能继承旧批次的未消费资格。

## 离线/只读示例

在隔离开发目录执行；该命令只读取已固定哈希的开发期 fixture，输出目录必须是新目录：

```powershell
Set-Location C:/program1/coding/OKX-p1-strategy-contract
$python = 'C:/program1/coding/OKX/.venv/Scripts/python.exe'
& $python -m scripts.demo_strategy_contract --output artifacts/new_contract_demo
```

命令对同一个 PRIMARY 变体分别调用批量 `replay` 和逐根 `on_candle`，中途保存和恢复再比对。
报告记录数据哈希、身份、信号指纹和只读标识，不是样本外证据。
另有真实 `BacktestEngine` 测试捕获其逐笔订单反馈及持仓，将同一反馈流传入只读适配器后
逐条比较完整信号与最终策略状态。不同反馈或不同执行环境的成交不要求相同。

## 兼容范围

原 MA、buy-and-hold、VWAP mean reversion 沿用原行为，未宣称它们支持新状态恢复协议；
注册元数据将其标记为 `legacy_unversioned`。旧 VWAP Shadow 保留自己的 checkpoint、
生命周期与 SQLite 事务，版本常量改为引用注册表，保持原值，策略源码字节及状态格式不变。
通用只读适配器对不支持新契约的策略明确拒绝，要求使用原 runner。

本次未将原 VWAP 专用 WebSocket/重连/SQLite 协调器泛化成 V3 服务，也未实现完整撮合器/
账户的原子恢复、旧 V3 适配器状态迁移或交易所执行集成。V3 的逐根状态恢复已验证，不能据此
宣称生产持续运行、跨平台恢复或不同执行环境下的成交等价。

本次真实执行的命令、覆盖范围和证据见 [P1 验收记录](strategy-contract-validation.md)。
