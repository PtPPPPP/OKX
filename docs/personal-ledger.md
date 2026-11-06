# 本地个人持仓台账

该功能记录用户手工提供的现货数量、成本、现金与已发生流水，完全离线，不加载 `.env`、
不读取凭证、不连接公共或私有接口、不下单。`transfer_in/out` 仅登记现金变动，绝不执行资金转账。
台账独立于交易数据库、策略持仓和 Demo 授权链路，也不作为下单依据。

## 快速验证（全部为虚构数据）

在项目根目录运行。以下币价和数量只是算术示例，不代表实际行情或个人账户。

```powershell
.venv\Scripts\python.exe -m scripts.personal_ledger --database data/personal-positions/example.db init --opening examples/personal-ledger/opening.json
.venv\Scripts\python.exe -m scripts.personal_ledger --database data/personal-positions/example.db import --transactions examples/personal-ledger/transactions.json
.venv\Scripts\python.exe -m scripts.personal_ledger --database data/personal-positions/example.db import --transactions examples/personal-ledger/transactions.json
.venv\Scripts\python.exe -m scripts.personal_ledger --database data/personal-positions/example.db show --quotes examples/personal-ledger/quotes.json --at 2026-01-01T06:00:00Z
```

第二次导入应为 `added=0, duplicates=6`。该示例期末数量为 2，平均成本 111，剩余成本 222，
现金 864，已实现盈亏（含独立手续费）36，未实现盈亏 98，完整台账估值 1184 USDT。
删除 `--at` 会使用当前时间，这份固定历史行情会过期，总估值为 `null`。
`--at` 只改变估值时间，不倒推历史持仓；若估值时间早于已登记流水，不能得到完整估值。

## 建立个人台账

将示例期初文件复制到 `data/personal-positions/`，填写自己的实际数据，再运行 `init`。
所有个人输入、流水、行情和输出都应放在这个被 Git 整目录忽略的本地目录。
CLI 的数据库和报告输出强制限制在此目录（包括解析符号链接后的路径），不会覆盖已有文件。
默认数据库为 `data/personal-positions/ledger.db`，不要对个人数据库运行示例初始化命令。

期初格式：

```json
{
  "schema_version": 1,
  "currency": "USDT",
  "as_of": "2026-01-01T00:00:00Z",
  "source": "manual_opening",
  "cash": "1000",
  "positions": [
    {
      "instrument": "BTC-USDT",
      "quantity": "0.01",
      "average_cost": "100",
      "updated_at": "2026-01-01T00:00:00Z",
      "source": "manual_position"
    }
  ]
}
```

`quantity` 是实际净持有数量，`average_cost` 是每单位含买入手续费的账面成本。
不知道的数量、成本或现金填 `null`，不能用 0 代替未知。`positions: []` 表示用户声明期初没有持仓。
未填写单项来源/更新时间时继承期初来源/时间；原始历史快照的时间未知仍保持未知。
仅支持同一个计价币的无杠杆现货产品（如 `BTC-USDT`）；不处理合约、借贷、空头或跨币种折算。
数量、价格、金额建议使用十进制字符串；JSON 数字也按十进制解析，不经过 float。
拒绝非有限数、负数、重复产品、未知字段、无时区时间及不匹配的币种。

## 手动登记与批量导入

每条流水必须有稳定且唯一的 `id`、实际发生时间 `at` 和数据来源 `source`。
重复导入时保留原 ID：相同 ID/内容跳过，不同内容拒绝整批导入。
不同 ID 会被当成不同流水，系统不能判断两个不同 ID 是否其实是同一笔交易。

```powershell
.venv\Scripts\python.exe -m scripts.personal_ledger record --id manual-in-001 --kind transfer_in --amount 1000 --at 2026-01-02T00:00:00Z --source manual
.venv\Scripts\python.exe -m scripts.personal_ledger record --id manual-buy-001 --kind buy --instrument BTC-USDT --quantity 0.01 --price 100 --fee 0.01 --at 2026-01-02T01:00:00Z --source manual
.venv\Scripts\python.exe -m scripts.personal_ledger record --id manual-sell-001 --kind sell --instrument BTC-USDT --quantity 0.005 --price 120 --fee 0.01 --at 2026-01-02T02:00:00Z --source manual
.venv\Scripts\python.exe -m scripts.personal_ledger record --id manual-fee-001 --kind fee --amount 0.02 --at 2026-01-02T03:00:00Z --source manual
.venv\Scripts\python.exe -m scripts.personal_ledger record --id manual-out-001 --kind transfer_out --amount 10 --at 2026-01-02T04:00:00Z --source manual
.venv\Scripts\python.exe -m scripts.personal_ledger import --transactions data/personal-positions/transactions.json
.venv\Scripts\python.exe -m scripts.personal_ledger history
```

`buy/sell` 使用 `instrument, quantity, price, fee`，`amount` 必须为 0；
`fee/transfer_in/transfer_out` 使用 `amount`，其他交易字段必须为空或 0。
费用只能以台账计价币记录；基础币或第三币种手续费必须先核实实际净数量与成本，不可直接冒充计价币费用。
同一笔手续费不能既填交易 `fee` 又重复登记独立 `fee`。

流水按导入顺序计算，时间必须不早于期初和上一条非重复流水；相同时刻按输入顺序。
应先按实际发生顺序整理文件再导入。超量卖出、已知现金不足、冲突 ID、乱序时间均整批回滚。
未知数量或成本的持仓不能买卖；未知现金保持未知，不会因一笔转入变成已知余额。

## 成本、盈亏和行情口径

- 使用 Decimal，输入最多 40 位有效数字、数量级指数范围 ±40，内部固定 50 位有效数字，不转换成二进制浮点。循环小数及超出内部精度的结果按该精度舍入；不猜测交易所精度或舍入单位。
- 买入：新增成本 = 数量 × 成交价 + 买入手续费；移动平均成本 = 累计剩余成本 / 实际数量。
- 卖出：已实现盈亏 = 数量 × 成交价 − 卖出手续费 − 按移动平均分摊的成本；剩余平均成本不变，清仓剩余成本精确归零。
- 独立手续费：扣现金并计入台账已实现盈亏，不再调整产品成本。转入转出只影响现金，不算收益。
- 已实现盈亏从期初之后累计，不声称覆盖期初前的历史；产品已实现与独立费用分别展示，汇总含独立费用。
- 未实现盈亏 = 实际数量 × 有效价格 − 剩余成本。未扣除将来卖出费用，不含税、分红、利息、空投等未登记事项。

行情文件为 `Quote` 数组，见示例：每项必须包含产品、价格、带时区 `at` 与来源 `source`。
由用户手工提供或从已有公共行情工具取得后整理，不自动抓取。

```powershell
.venv\Scripts\python.exe -m scripts.personal_ledger show --quotes data/personal-positions/quotes.json --max-age-seconds 300 --output data/personal-positions/report.json
```

报告包含实际数量、平均成本、现金、来源、更新时间、行情时间和行情状态。
缺失、过期、未来或早于持仓更新时间的行情不能用于当前估值；300 秒阈值可显式调整。
数量、现金、成本未知或所需报价无效时，`complete=false`，`account_value=null` 并列出原因。
可计算部分仅作为部分值；`complete=true` 也只表示已登记台账内部数据齐全，不能证明账户无遗漏或完成账户对账。
显式 0 持仓无需报价，未知数量不会被当作空仓。

## 兼容旧文件

```powershell
.venv\Scripts\python.exe -m scripts.personal_ledger --database data/personal-positions/legacy.db import-legacy --positions examples/personal-ledger/legacy.json --as-of 2026-01-01T00:00:00Z --source legacy_manual_import
.venv\Scripts\python.exe -m scripts.personal_ledger --database data/personal-positions/legacy.db show --quotes examples/personal-ledger/quotes.json --at 2026-01-01T06:00:00Z
```

旧数组中的 `cost` 是单位平均成本，`market_value_usdt` 仅保留为历史市值快照。
若原文件没有时点，历史市值时间保持未知；导入时间不会冒充历史市值时间。
数量缺失仍为 `null`，绝不使用旧市值除以新价格。旧文件没有现金，所以现金未知，总估值不输出。
原文件不修改；[长周期持仓分析](position-review.md) 也遵循此限制。

核实实际数量和现金后，可据此建立一个新的期初文件和新数据库。保留旧数据库及流水作为审计材料；
本版本不提供直接改写流水、自动纠错或多份数据库合并，避免静默重写历史。

## 存储、恢复与验证

独立 SQLite schema/version/application_id；金额持久化为十进制文本，期初不可覆盖。
批量导入使用 `BEGIN IMMEDIATE` 与 `synchronous=FULL`，同一数据库并发导入串行校验，
重启后从期初和已提交流水重新计算。失败批次不会部分记账；重试使用原 ID。
只读查询使用只读连接，不会因路径拼错创建新库；不接受交易数据库或其他未知 schema。
本机文件没有加密，需自行保护操作系统访问权限。停止写入后可复制数据库备份；恢复至新文件后用
`history` 和 `show` 核对。未进行断电硬件试验，不宣称断电安全。

```powershell
.venv\Scripts\python.exe -m pytest tests/test_personal_ledger.py tests/test_personal_ledger_storage.py tests/test_personal_ledger_cli.py tests/test_position_review.py -q
uv run python scripts/quality_gate.py
```

质量门包括 Ruff format/check、Mypy、完整 Pytest 与安全 smoke。测试只用虚构输入和临时数据库。
不新增依赖或环境变量，不改变回测、Demo 或实盘开关。

### 本次验收记录（2026-11-06）

- Windows / Python 3.12；当前目录无 `.git`，无法核对提交基线或未提交差异，未初始化或修改项目 Git 历史。
- `uv` 不在 PATH，使用 `.venv\Scripts\python.exe -m ruff format --check .`、`-m ruff check .`、`-m mypy` 等价执行静态质量门：315 个文件格式检查通过、Ruff 通过、195 个源码文件 Mypy 通过。
- 89 项台账及持仓分析相关测试通过。完整 Pytest 在不含 `.env`、个人数据或运行数据库的临时源码副本中执行：940 项通过、6 项跳过。跳过项为需要 `uv` 或 `RESEARCH_TEST_UV` 的研究快照隔离运行集成测试，本次未验证这些集成场景。
- 按 `scripts/quality_gate.py` 定义的九个测试文件单独执行安全 smoke：81 项通过（包含新增的台账 FULL 持久性检查）。
- 初轮隔离副本漏复制 `docs/research-snapshots.md`，补齐后重跑通过；新增台账连接也已纳入持久性连接所有者检查，并新增实际连接 `synchronous=FULL` 回归测试，没有降低原安全要求。
- 文档示例已实际运行：重复导入、历史/过期行情保护和金额结果均符合上文；Git 忽略规则在临时 Git 仓库中验证，个人目录全被忽略、示例目录不被忽略。已核对最终代码与全量测试使用的副本逐字节一致。
- 未连接账户、未读取凭证、未发起交易或资金操作；未进行真实断电验证。
