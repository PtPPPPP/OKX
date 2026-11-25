# P1：V3 策略运行契约

本次实现把五类 V3 候选的十组既有参数接入统一策略注册表，并提供逐根行情输入和状态恢复。
参数、24 小时持仓限制、成交模型和候选晋升标准没有调整。原研究文件、快照及消费记录保留。

## 入口与身份

- `app/strategies/v3.py`：`V3Parameters`、`V3Strategy` 和版本化检查点。
- `app/strategies/registry.py`：以各候选原有 `candidate_id` 注册，参数内必须声明相同身份。
- `backtest/research_execution.py`：配置使用实际策略名称和参数；报告保存 `strategy_definition`。
  `V3ExecutionStrategy(candles, variant, warmup_count=...)` 保留兼容入口，只读取明确指定的预热前缀，
  不再预先计算全部未来行情的交易信号。
- `app/runners/strategy_observer.py`：无 Broker、无网络连接的逐根只读观察入口。
- `app/strategies/v3_rules.py`、`v3_features.py`：研究与流式执行共用的指标和条件计算。
  原 `backtest/strategy_v3_*` 路径继续导出原有公共接口。

策略参数结构包含 `candidate_id`、`variant_id`、`parameters`、`maximum_hold_hours`。
实现版本为 `v3-stream-1`。参数缺失、多余、非法窗口、非有限数值或身份不一致会被拒绝。
研究候选不加入原有默认可用策略列表；可通过注册表显式创建。
交易引擎拒绝这些候选在非回测模式下提交订单，只读观察不产生订单。

## 时间和状态

只接受带时区、整点对齐、已确认的 1H K 线，时间戳表示开盘时刻；信号时间为该根 K 线收盘时间。
4H 条件仅使用已经完成的 4H K 线。行情统一为 UTC，预热仅更新指标和 episode 状态。
相同的最后一根 K 线重复到达时不再发信号；内容冲突、乱序、缺失、未确认行情会明确报错。
不自动补数据或跳过缺口。调用方应先修复连续性，再继续输入。

逐根计算使用有界历史窗口（现有参数为 300 根），不会保存未来行情或无限累积历史。
批量信号研究保留原来的批量路径，使用相同的条件和指标函数；测试对十组参数逐根比较，
覆盖高周期边界、预热和历史窗口淘汰后的情况。

`on_start` 初始化策略；然后可调用 `restore_state`。检查点包含实现版本、完整参数、标的、
行情窗口、上一 episode 状态、挂单 ID、首次成交时间和持仓数量。恢复必须与调用方已经核对的
持仓和未完成订单一致；版本错误、参数变化、订单/持仓不一致、未来行情等情况会拒绝恢复。
失败的恢复不会部分覆盖运行中的策略状态。检查点不是账户总账，也不自行查询账户。

## 离线检查

以下命令使用明确指定的 CSV，不下载数据，不访问私有账户；输出目录必须不存在。
CSV 使用 `app.market.historical_data` 的标准格式（包含 `confirmed` 列）。

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_v3_contract `
  --csv <开发集或合成数据.csv> `
  --variant-id volume_breakout_20 `
  --output artifacts/my_v3_contract_check
```

命令检查批量信号与逐根信号、不中断与中途恢复的信号是否一致，并用同一实现运行离线撮合。
只读观察保持给定的组合状态，没有模拟成交；回测的成交会改变持仓，因此不能直接要求它们
在不同订单反馈条件下输出完全相同的买卖序列。相同输入、相同状态与反馈才是一致性比较的前提。

## 范围与证据

本轮只统一 V3 候选的研究执行和只读观察入口；已有 MA/VWAP 的策略接口继续兼容，
没有重写专用 VWAP Shadow 服务，也没有接入真实 WebSocket 进行长期运行验收。
检查点需要调用方保存和在启动后显式恢复；尚未提供自动重启调度。
Repository/CLI 职责拆分和多策略资金管理仍属后续任务。

独立快照中的旧代码与产物没有修改。旧结果按旧版本解释，新版本报告附带策略定义；
不能修改旧冻结哈希来混用版本。本次工程验证使用合成数据，不重新消费历史样本外数据。

本轮验证输出位于 `artifacts/strategy_contract_p1/`；该目录不作为新的收益研究结果。
实际验证细节见 [本轮验收记录](strategy-contract-p1-validation.md)。
