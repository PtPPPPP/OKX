# 长周期现货持仓分析

该入口在每日参考模型之外，读取用户主动提供的现货单位平均成本和实际数量，结合最多两年的日线
以及最近 15 天小时行情，输出可复核的数据参数。它不访问私有交易账户或提交订单。
现货采用“无固定期限、投资逻辑成立就持有”的政策，小时形态只作观察。
投资逻辑与失效条件由用户登记，未登记时标记待补充，见 [个人交易规则](personal-trading-policy.md)。

运行：

```powershell
uv run python -m scripts.review_positions --positions data/personal-positions/review-positions.json --days 730 --max-quote-age-seconds 300
```

若已按 [现货长期逻辑台账](spot-thesis.md) 登记理由、条件、材料与复核，可显式只读加载：

```powershell
uv run python -m scripts.review_positions --positions data/personal-positions/review-positions.json --thesis-database data/personal-theses/theses.db --days 730
```

`--thesis-database` 省略时不会寻找或读取个人逻辑台账；原有持仓文件仍可独立运行，逻辑状态为
`MISSING_INPUT`（待补充），买入理由标记待补充，`action=null`。显式指定不存在的逻辑数据库会在
公共行情采集前失败，不会创建数据库。不会从持仓 JSON 或 ticker 的自报状态字段认定逻辑成立。

指定台账时，报告显示由本地登记记录重新评估的状态、理由、依据和 UTC 评估时点：待补充、
待复核、材料支持、存在冲突或逻辑失效。公共历史行情采集完成后会重新评估一次，避免沿用采集前
已过期的逻辑状态。只读取台账，不登记复核或改写历史；不自动连接私有账户、付费信息源。
材料支持不构成买入或继续持有建议，冲突和失效也不生成自动卖出动作。
小时趋势、ATR、成本价及持仓超过 72 小时不会单独改变长期逻辑状态或触发退出。

持仓文件为 JSON 数组。以下为纯虚构示例，数量、价格和金额建议写成十进制字符串：

```json
[
  {
    "instrument": "BTC-USDT",
    "cost": "100.1",
    "quantity": "0.03",
    "updated_at": "2026-10-29T00:00:00Z",
    "source": "synthetic_example"
  }
]
```

`cost` 是单位平均成本；是否已含买入手续费由输入决定。`quantity` 必须是用户明确登记的实际数量，
允许零；缺失或 `null` 表示未知。此入口只计算单项持仓的市值和未实现盈亏，不核算现金、
历史已实现盈亏和账户总估值。此入口不提供完整流水和费用核算。

旧格式每项只有 `instrument`、`cost`、`market_value_usdt` 时仍可读取。
旧 `market_value_usdt` 仅保留为 `historical_snapshot`，不是当前市值或买入本金；记录时间缺失时标为未知。
禁止用旧市值除以新价格生成数量。数量未知时，当前市值、成本总额、未实现盈亏均输出 `null`，
Markdown 标为“未知/不可用”。旧输出中的 `estimated_*` 字段已移除。

个人持仓文件保存在 Git 忽略的 `data/personal-positions/`，报告默认保存在已忽略的
`artifacts/position-review/` 下。不能将账户信息加入源码或提交记录。

输出：

- `analysis.json`、`analysis.md`：长期逻辑状态及依据、报价及时间戳、行情新鲜度、实际数量/未知状态、按有效报价计算的单项市值和未实现盈亏、相对成本涨跌、回本所需涨幅、SMA20/50/200、ATR、观察价位。
- `*-1d.csv`、`*-1h.csv`：已收盘行情；日线使用 `1Dutc`，保证 UTC 零点切分。
- JSON 内的 CSV 哈希：用于核对分析所用数据。

不足 200 天的产品不输出 SMA200；不同产品不拼接历史。三日历史收益使用不重叠样本，
其分位数仅描述过去，不能解释为未来预测区间。报价默认最多有效 300 秒，可用
`--max-quote-age-seconds` 设置正整数秒数；有效期边界仍视为新鲜。未来报价、时间戳缺失或无效、
过期报价均不能计算当前市值和未实现盈亏。输出 `quote_status` 与 `valuation_status` 明确状态，
时间统一记录为 UTC；技术指标仍可作为历史观察显示。本入口不提供账户对账或含分红的总收益核算。

离线回归测试：`uv run pytest tests/test_position_review.py -q`，使用合成持仓和 MockTransport，
不读取凭证、不连接私有账户、不下单。历史结果目录保持原样；旧报告中的推算金额不得用于当前估值。

旧输出目录的 `trading-review.md` 使用了已被更正的三天现货假设，已标记为历史建议，
不得作为当前长期现货的默认退出计划。事件需重新查询，过期价位不能直接作为订单。

2026-10-29 实际运行结果：XAUT、BTC、ARB 各取得 730 根日线，XINTC、XQQQ 各取得 105 根日线。
全项目测试 856 项通过、6 项跳过；Ruff format、Ruff check、Mypy 通过。
