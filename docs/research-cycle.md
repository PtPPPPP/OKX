# 可复核的研究闭环

本流程只读取 OKX 公共 BTC-USDT 现货 1H 已确认 K 线，在本地执行回测。入口为
`python -m scripts.run_research_cycle`，不需要账户凭证，也不签发模拟盘或实盘授权。

## 数据、历史报告与模型

历史开发集固定为 2023-08-13 00:00 至 2026-08-12 23:00 UTC，共 26,304 根 K 线。
公共 `GET /api/v5/market/history-candles` 下载器保留原始响应、下载时间、SQLite 断点、
归一化 CSV 和 `data_manifest.json`。下载可恢复；每次调用默认最多 128 页、页间隔
0.3 秒。直连只在显式有限页数时开放，原有长期下载的代理门禁保持有效。

完整区间必须满足：缺失、重复、乱序、OHLC 错误、未确认 K 线均为零；首尾覆盖请求
边界；每根 K 线在收盘后下载。验证会重新计算 CSV 内容哈希，不只信任 manifest。
未下载完的数据不能进入冻结或验证。

旧报告依赖链依次是 VWAP episode、fixed-exit、walk-forward、Strategy V2 和 V3。
缺失的原报告无法证明被恢复，本次输出明确标记为重新生成的报告。旧报告沿用各自
的信号事件研究模型，不与新的成交模型直接比较盈亏。

新成交复核只使用既有 V3 五个 PRIMARY 变体，不根据重建结果调参。模型使用收盘信号、
下一根 K 线按上一收盘价挂限价单；触及才成交，考虑手续费、滑点、价格精度和资金
占用。保护性止损使用修复后的模型，同一根 K 线同时触及止损/止盈时先止损。
固定最长持仓 24 小时、止损 1%、止盈 2%；初始资金 10,000 USDT、每单 20 USDT、
最大敞口 100 USDT。手续费 0.1%、滑点 0.05%，另运行二倍成本压力测试。

这些是冻结的研究假设：静态交易规则快照并非完整历史规则，费用并非历史账户费率，
OHLC 不能还原盘口队列和真实成交概率。买入并持有对照也只使用同等 20 USDT 的
一次买入；现金对照净盈亏为零，不将账户资金规模不同的收益直接混比。

## 冻结与独立验证

训练门槛包括旧研究门禁、至少 100 个已平仓交易、扣费后盈利、二倍成本仍盈利、
移除最佳 5/10 笔后仍盈利、至少 60% 有交易年份盈利。门槛写在
`backtest/research_governance.py` 的 `EXECUTION_POLICY` 中。

冻结文件使用实际 UTC 时间并独占创建，绑定候选参数、训练资格/拒绝理由、执行政策、
相关源码与依赖锁文件哈希、训练数据与报告哈希。训练被拒绝的假设可以输出诊断结果，
但不能因为样本外表现较好而成为候选。没有训练合格者时，`selected_candidate` 为 null。

独立验证有两种明确不同的证据：

- **历史样本外**：2026-08-13 00:00 UTC 至本次冻结时最后一根已收盘 K 线。
  起止边界都在查看本轮样本外数据前冻结。数据下载时间必须晚于冻结，并与训练区间
  不重叠。过去区间只能称为 `RETROSPECTIVE_AFTER_FREEZE_ACCESS`；旧配置中的
  `frozen_before_oos` 不能证明当年真的冻结过，也不能保证之前无人看过市场。
- **前向验证**：本次冻结后的下一整点开始，连续 720 根小时 K 线。整个 30 天窗口
  完成后才允许验证，不能缩短窗口或把过去数据改称前向证据。窗口未结束时返回
  `PENDING_FUTURE_WINDOW`。初始指标从窗口自身历史建立，不借用不连续的旧数据。

样本外的 warmup 只用于指标上下文，不产生交易，也不计入该区间收益。
验证前创建一次性消费记录，绑定 dataset/output；完成后发布完整报告文件哈希。
同一输入重复执行只校验并返回已发布报告，替换数据或输出路径会被拒绝。
中途中断后若只有 consumed、没有 complete，会停止而非静默重跑；保留现场，先审计
失败原因，再明确登记新研究批次，禁止删除记录来反复挑选结果。

源码变更、数据变更、训练决策变更或报告文件篡改会使验证失败。哈希提供内容一致性，
不提供独立第三方时间戳证明。所有结果保持 `trading_permission=false`；研究通过也
不等于自动获准交易。

新冻结使用 v2：按源码目录和必要研究资源规则绑定完整清单，记录 Python 精确版本与平台，
使用项目相对路径。冻结后应立即创建独立快照，避免日常开发使当前目录不再匹配冻结。
快照入口支持创建、校验、隔离依赖恢复、原报告读取、首次验证和显式登记复现；未来数据可
由冻结源码追加为派生包。旧 v1 不改写，新增归档明确标注“事后归档”及未覆盖源码的证据边界。
完整格式、命令与本地复制消费约束见 [独立研究快照](research-snapshots.md)。

P1 开发版的 V3 使用注册策略 `v3_step_v1` 和统一逐根更新，不再用 `buy_and_hold` 伪装配置身份。
现有冻结在原快照内保持原代码；新实现产生新产物，不修改旧哈希或重跑已消费样本外来调试。
兼容与已知行为差异见 [统一策略契约](strategy-contract.md)。

## 复现命令

以下命令在项目根目录执行；选择新的输出目录，避免覆盖已发布研究。

```powershell
$cache = 'data/research/new_cycle/historical'
$reports = 'artifacts/new_cycle/historical_reports'
python -m scripts.run_research_cycle download-history --cache $cache
python -m scripts.run_research_cycle rebuild-reports --cache $cache --output-root $reports
# 从 rebuild_index.json 获取 V3 的实际输出目录后执行：
python -m scripts.run_research_cycle historical-execution --cache $cache --legacy-v3-artifact $v3 --output artifacts/new_cycle/development
python -m scripts.run_research_cycle freeze --historical-cache $cache --training-summary artifacts/new_cycle/development/summary.json --output artifacts/new_cycle/freeze/cohort.json
python -m scripts.run_research_cycle download-oos --freeze artifacts/new_cycle/freeze/cohort.json --cache data/research/new_cycle/historical_oos --purpose historical_oos
python -m scripts.run_research_cycle validate --freeze artifacts/new_cycle/freeze/cohort.json --cache data/research/new_cycle/historical_oos --output artifacts/new_cycle/historical_oos_validation --purpose historical_oos
python -m scripts.run_research_cycle download-oos --freeze artifacts/new_cycle/freeze/cohort.json --cache data/research/new_cycle/prospective --purpose prospective
# 仅在冻结的前向窗口结束、完整数据下载成功后运行：
python -m scripts.run_research_cycle validate --freeze artifacts/new_cycle/freeze/cohort.json --cache data/research/new_cycle/prospective --output artifacts/new_cycle/prospective_validation --purpose prospective
```

使用项目虚拟环境的 Python 或 `uv run python`。页数不足时重复同一个下载命令，
继续既有断点；未完成时不要运行下游命令。代理网络用 `--network-mode proxy`，并按
已有网络配置设置显式代理。下载完成与质量失败由 manifest 明确区分。

本次已执行研究的汇总位于 `artifacts/research_cycle_v1/report.md`，原始数据位于
`data/research/rebuilt_v1/`。这些大数据与运行产物被 Git 忽略，应单独保留备份；仓库
本身不包含完整行情及报告。本次前向验证不会因为历史验证完成而被宣称完成。
