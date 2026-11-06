# 个人组合风险分析

入口为 `python -m scripts.personal_risk`。只读用户明确提供的本地输入或个人持仓台账，
输出 JSON / Markdown 风险报告，不读取凭证、不访问私有账户、不下单，也不生成减仓比例或交易计划。
没有接入信息源、行情下载或后台定时任务。

个人台账、现货逻辑及风险分析三个 CLI 的标准输出和错误输出统一使用 UTF-8，包括 Windows
管道/重定向、JSON、Markdown 和帮助文本。调用方捕获输出时应按 UTF-8 解码；无需配置系统代码页。

用户已确认现货没有固定持有期限，合约一般最多 72 小时。长期资金、合约预算、备用现金和风险上限
尚未确定，因此模板全部保持未设置；没有把项目用于 Demo 下单的默认限额当作用户个人风险设置。

## 快速开始

```powershell
.venv\Scripts\python.exe -m scripts.personal_risk template --output data/personal-risk/input-template.json
.venv\Scripts\python.exe -m scripts.personal_risk report --input data/personal-risk/input-template.json --format markdown
```

个人输入、设置、行情和报告保存在被 Git 整目录忽略的 `data/personal-risk/`。输出路径限定于该目录，
拒绝覆盖已有文件。模板生成时不假定现金为零、不假定完整持仓，也不预填预算或冲击幅度。

## 可信输入与金额口径

输入模型 `RiskRequest` 包含 `currency`、`as_of`、`source`、现金、现货、线性合约、行情、
产品分类、风险设置和情景。金额、数量及比例用十进制字符串；JSON 数字按 Decimal 解析，
不经 float。复用台账的 Decimal 校验和行情时间校验，运算使用 50 位有效数字。

- `positions_complete=true` 是用户明确声明已经列出分析范围内所有现货和合约；默认 `false`。
- 每笔数量和现金的 `verified=true` 表示用户已核对实际记录，默认 `false`，不代表交易所已认证或自动对账。
- `updated_at` / `source` 记录数量和现金来源，`as_of` 是输入快照时点。未来记录不能冒充当前数据。
- 实际数量未知填 `null`，不能用 0 代替；不接受以历史市值反推数量。明确的 0 持仓与未知数量不同。
- 行情 `RiskQuote` 必须有产品、正价格、时间 `at` 和来源。默认有效期 300 秒，可显式配置。
  缺行情、行情过期、未来行情或早于该持仓更新时间的行情均不能用于当前完整估值。

仅支持单一计价币、现货多头和以同一计价币结算的线性合约。合约使用 `base_quantity`，即实际基础资产
等价数量，**不是合约张数**。如果手头只有张数，必须先根据可信合约乘数换算后登记；模块不猜测乘数。
每个合约持仓用独立 `id`，明确 `long` 或 `short`；同合约的两个方向可以分别登记，不能使用相同 ID。
本版本不支持反向合约、期权、借贷或跨币种折算，不把不支持的资金结构强行纳入准确估值。

## 总资产、现金与合约敞口

本报告的总资产口径是登记范围内的**组合净权益**：

```text
现货市值 = 实际数量 × 有效报价
合约未实现盈亏 = 方向符号 × 基础资产等价数量 ×（有效报价 − 入场均价）
组合净权益 = 已结算现金余额 + 现货市值 + 合约未实现盈亏
合约有符号名义敞口 = 方向符号 × 基础资产等价数量 × 有效报价
```

方向符号：多头 `+1`，空头 `−1`。合约名义敞口用于风险分析，不是可再次加入账户的资产。
当前 `entry_price` 缺失时不能算合约未实现盈亏，因此不能给出完整组合净权益。

`cash.balance` 是已经包含保证金、但尚未包含合约未实现盈亏的已结算现金。
有合约时必须明确 `cash_scope="all_settled_excluding_derivative_pnl"`；纯现货可使用 `spot_only`。
`unknown` 或含合约却仅有现货现金时，总资产不可完整计算。不要填已经包含未实现盈亏的“账户权益”作为现金。
不同钱包现金必须先核对无重叠后汇总一次，不能把子账户总额和它的明细再次相加。

`contract_collateral` 是全组合只登记一次的合约保证金，用于检查合约预算，**不再次加入资产**。
`cash.available` 是未占用的可用现金；不知道时保持 `null`，不能把全部含保证金现金当成备用现金。
已知数据必须满足 `available <= balance`、`contract_collateral <= balance`，以及
`available + contract_collateral <= balance`，矛盾输入直接拒绝。

- `cash_ratio`：可用现金 / 组合净权益。
- `cash_balance_ratio`：包含保证金的已结算现金 / 组合净权益，不能视为自由资金比例。
- 现货资产权重：现货市值 / 组合净权益。
- 产品风险权重：该产品的总名义敞口 / 组合净权益；合约的这个比例不是资产占比，可大于 1。
- 毛敞口：各持仓名义敞口绝对值之和；净敞口：各持仓有符号名义敞口之和。

现货与合约逐项展示，并按产品、标的和资产类别汇总。不能因为多空抵消而省略毛敞口。
`products` 映射由用户明确提供 `underlying` 和 `asset_class`，例如一个现货代理产品与某合约是否应
归于同一个经济标的，不能仅凭名称相似自动认定。缺失分类会报告未分类及所需信息，分组结果不能标为完整。
同标的聚合只是登记的名义敞口，不代表不同产品之间不存在基差、跟踪偏差或发行人风险。
跨不同标的相加的净敞口仅表示名义方向余额；不能把两个不同资产一多一空的抵消视为可靠对冲。

缺少可信数量、完整性声明、现金口径或有效报价时，完整总资产为 `null`；可计算部分明确标为部分值。
净权益为零或负数时可以展示金额，但不计算权重/比例，也不能报告风险检查已完整通过。
可用现金未知可以不影响已知总权益，但现金比例和备用金检查保持未知。

## 资金用途及限制

所有设置默认 `null` 或空字典；未设置显示 `NOT_SET / 待设置`，不能解释成“无限制”或“已通过”。
数值 0 是明确设置的零额度，与未设置不同。比例是小数，例如 `0.1` 表示 10%，不是 `10`。

| 设置 | 检查口径 |
|---|---|
| `long_term_budget` | 现货市值不超过登记的长期资金预算 |
| `contract_budget` | 全组合合约保证金不超过登记的合约预算 |
| `reserve_cash_target` | 可用现金不低于备用现金目标 |
| `product_weight_limits` | 各产品毛名义敞口 / 净权益不超过其单独上限，缺项待设置 |
| `contract_trade_loss_budget` | 每笔合约在用户指定止损价和费用假设下的亏损金额预算 |
| `max_gross_exposure_ratio` | 组合毛敞口 / 净权益上限 |
| `max_abs_net_exposure_ratio` | 组合净敞口绝对值 / 净权益上限 |
| `min_available_cash_ratio` | 可用现金 / 净权益下限 |
| `max_scenario_loss` | 已配置冲击情景下的组合亏损金额阈值 |

三类资金用途只是同一组合资金的预算标签，不会给资产增加三笔现金；预算合计不能超过组合净权益。
没有独立钱包分配与转账功能，也不自动把资金划入某种用途。

合约单笔亏损假设为 `max(0, 方向 × 数量 × (入场价 − 止损价) + estimated_trade_cost)`。
`stop_price` 和 `estimated_trade_cost` 均需用户提供，费用不默认 0；缺失则提示补充，不能据预算倒推出止损价。
这是假定能按指定价格成交时、从入场开始的损益测算，**不是实际亏损上限**；跳空、滑点、资金费和强平会使结果不同。
每个合约 ID 代表用户登记的一笔持仓；多笔交易合并为一个 ID 后不能再声称进行逐笔成交风险核算。

报告逐项显示 `NOT_SET / UNKNOWN / PASS / EXCEEDED`、当前值、阈值和原因。
存在超限就报告超限；缺少设置或数据保持不完整，检查通过也不代表投资安全。
合约超过 72 小时仅提示复核，不清除持仓、不自动平仓；现货不设置持有期限。

## 价格冲击情景与历史相关性

`scenarios` 默认空数组，不替用户选择压力幅度。每个情景有 `name` 及按明确标的指定的 `shocks`，
如 `{"BTC":"-0.1"}` 仅表示假定 BTC 价格下跌 10%。同标的现货与线性合约采用相同的百分比冲击，
分别计算盈亏后汇总，方向相反时损益符号相反。遗漏持仓标的的冲击幅度时，不能默认为 0 或输出完整情景总盈亏。

情景增量盈亏 = 当前有符号名义敞口 × 价格冲击。现金计价币价值假定不变；不模拟基差变化、
资金费、交易费用、强平、流动性和实际成交路径。情景结果是**假设下的敏感度**，不是预测概率、VaR、
最大损失或收益承诺。即使情景结果盈利，也不代表组合没有风险。

本版本不计算历史相关性，报告明确标为未计算。未提供统一时间窗口和可对齐的历史价格，不用少量
报价强行计算相关系数。未来增加该功能时须披露窗口、收益率频率、时间戳对齐方式、缺失处理及样本数。

## 复用个人台账

```powershell
.venv\Scripts\python.exe -m scripts.personal_risk report --input data/personal-risk/overlay.json --ledger data/personal-positions/ledger.db --confirm-ledger --available-cash 800 --format markdown --output data/personal-risk/report-001.md
```

`--ledger` 只读显式指定的本地 SQLite 台账，通过现有 `LedgerStore.load()` 和 `replay()` 取得实际数量与现金。
不会访问交易账户，不会创建不存在的库。`--confirm-ledger` 仅标记用户已核对账本数量/现金；未提供时仍可运行，
但不能显示为完整可信估值。`--available-cash` 是用户确认的可用现金，可省略为未知。

叠加文件仍需提供行情、分类、完整性声明、合约以及现金口径，不能因为台账存在就默认账户已完整。
它必须省略 `cash` 并保持 `spot_positions=[]`，否则拒绝合并，防止同一笔资产重复导入。
台账中的历史市值不参与当前估值；旧文件数量未知仍保持未知。台账成本不是风险数量的替代数据。

## 虚构示例与验算

```powershell
.venv\Scripts\python.exe -m scripts.personal_risk report --input examples/personal-risk/synthetic-portfolio.json --at 2026-01-02T00:00:00Z --format markdown --output data/personal-risk/synthetic-report.md
```

示例的价格、数量、资金预算和限制全部虚构，并非用户设置或建议。其结果应为：现金 1000（可用 800，
含保证金 200）、现货市值 400、合约未实现盈亏 20，因此组合净权益 **1420**。
合约毛名义敞口 200，组合毛敞口 600、净敞口 400；不能把这些名义额或保证金再次加到 1420 上。
下跌情景增量盈亏为 −70，上涨情景为 +50；示例故意配置部分超限，以便核对报告原因。
不带 `--at` 使用当前时间时，这份固定历史行情会过期，不能给出当前完整估值。

已有[台账示例](personal-ledger.md)数据库时，可以用不含预算的叠加示例：

```powershell
.venv\Scripts\python.exe -m scripts.personal_risk report --input examples/personal-risk/ledger-overlay.json --ledger data/personal-positions/example.db --confirm-ledger --available-cash 864 --at 2026-01-01T06:00:00Z
```

预期净权益 1184，预算保持待设置，不会把“能估值”等同于“风险限制完整”。

## 检查

```powershell
.venv\Scripts\python.exe -m pytest tests/test_personal_risk.py tests/test_personal_risk_cli.py tests/test_risk_ledger_adapter.py tests/test_personal_ledger.py -q
uv run python scripts/quality_gate.py
```

不增加依赖或环境变量，不修改订单风控参数、交易开关、持仓台账数据或逻辑复核历史。

### 验收记录（2026-11-06）

- Windows / Python 3.12。项目目录没有 `.git`，因此不能从本目录取得提交基线；原目录未初始化 Git。
- 148 项风险分析、台账适配、CLI 及共享台账估值相关测试通过。包括多空方向、资金不重复计入、缺预算、
  未核实数量、未知数量、过期/未来行情、矛盾时间线、非正权益、72 小时复核与情景覆盖缺失。
- 本地全项目：1,114 项通过、6 项跳过；按 `scripts/quality_gate.py` 的九个文件单独执行安全 smoke：82 项通过。
- Ruff format/check 通过（327 个文件），Mypy 通过（201 个源码文件）。当前环境没有 `uv`，使用已有虚拟环境
  的 `python -m` 执行质量门对应命令；跳过的 6 项研究快照隔离运行测试需要 `uv` 或 `RESEARCH_TEST_UV`，未宣称已验证。
- 完整测试在不含 `.env`、个人数据和运行数据库的临时源码副本中执行。命令行示例实际验证：净权益 1420、
  冲击盈亏 −70/+50；按当前时间执行时旧行情使完整净权益为 `null`。台账叠加结果 1184，读取前后数据库字节一致，
  重复导入为 `added=0, duplicates=6`。个人目录的 Git 忽略规则及示例可跟踪性均已验证。
- 未读取凭证、未连接私有账户、未执行交易或资金操作。没有历史相关性或真实账户对账验证。
- GitHub 发布副本基于前一轮现货逻辑分支，保留远端现有架构，并补齐该分支缺少的台账依赖。
  发布副本完整回归为 1,353 项通过、8 项跳过，安全 smoke 为 82 项通过；格式/Lint 通过（349 个文件），
  Mypy 通过（215 个源码文件）。8 项跳过同样是缺少 `uv` 的研究快照隔离运行测试。
