# 现货投资逻辑登记与复核

适用产品固定为 BTC-USDT、ARB-USDT、XINTC-USDT、XQQQ-USDT、XAUT-USDT。
遵循[个人交易规则](personal-trading-policy.md)：现货没有固定持有期限，投资逻辑成立就持有；
不能从这一规则推导出任一产品的投资逻辑已经成立。用户尚未给出具体买入理由，初始五项均为待补充。

本模块只做本地登记、读取、人工复核和报告。没有账户、行情、付费信息源或大模型接口，
不加载 `.env`，不生成买卖、加减仓或自动退出指令。证据被标为事实表示登记人的分类，软件不自动核实其真实性。

## 初始化与待填写模板

也可参考[五产品空白模板](../examples/spot-thesis/blank-templates.json)，其中没有任何预填买入理由。

个人文件放在被 Git 整目录忽略的 `data/personal-theses/`；CLI 的数据库及输出路径强制限制于该目录，
拒绝覆盖已有文件。不将个人理由、证据、复核历史或数据库放进示例目录或 Git。

```powershell
.venv\Scripts\python.exe -m scripts.spot_thesis init
.venv\Scripts\python.exe -m scripts.spot_thesis template
.venv\Scripts\python.exe -m scripts.spot_thesis template --instrument BTC-USDT --output data/personal-theses/btc-v1.json
.venv\Scripts\python.exe -m scripts.spot_thesis report --format markdown
```

`template` 默认输出五个模板；指定产品时输出单个 JSON 对象，自动生成唯一版本 ID，理由为空，条件为空，
复核周期为空。用户填写后用 `register` 登记。空模板也可保存为草稿，此时状态仍为待补充，
其余产品、台账估值和技术分析可正常运行。若输出文件存在，选择新文件名，不覆盖旧版本。

## 数据结构与登记

每个 `ThesisRevision` 有版本 `id`、`instrument`、`recorded_at`、`source`、`buy_reason`、
`check_conditions`、`invalidation_conditions`、`review_interval_days`。
每个条件包含同一版本内唯一 `id`、描述 `text` 和必须显式填写的类别 `kind`：`fundamental`、`technical` 或 `holding_time`。
至少有明确理由、一个基本面检查条件、一个基本面失效条件和正整数天复核周期，才满足登记完整性。
周期只是复核期限，不是持有到期日；不预设各产品的周期或理由。

```powershell
.venv\Scripts\python.exe -m scripts.spot_thesis register --file data/personal-theses/btc-v1.json
.venv\Scripts\python.exe -m scripts.spot_thesis evidence --file data/personal-theses/evidence-001.json
.venv\Scripts\python.exe -m scripts.spot_thesis review --file data/personal-theses/review-001.json
.venv\Scripts\python.exe -m scripts.spot_thesis report --format markdown --output data/personal-theses/report-001.md
.venv\Scripts\python.exe -m scripts.spot_thesis history --output data/personal-theses/history-001.json
```

证据 `Evidence` 必须记录：

| 字段 | 含义 |
|---|---|
| `id`, `statement` | 稳定唯一标识、证据陈述 |
| `instruments` | 明确适用的产品数组，不自动传播至其他产品 |
| `classification` | `fact` 事实、`user_judgment` 用户判断、`model_inference` 模型推断 |
| `direction` | 相对投资逻辑的 `support` 支持、`against` 反面、`neutral` 中性 |
| `kind` | 必须显式填写：`fundamental` 基本面、`technical` 技术观察、`holding_time` 持有时间观察；不默认归为基本面 |
| `source` | 具体来源链接或可复核的本地材料标识，不能为空；登记不会访问链接 |
| `published_at` | 发布时间；不知道时显式 `null`，不能用采集时间冒充 |
| `collected_at` | 采集时间，必须有时区 |
| `expires_at` | 登记人确定的有效截止时间；未知时 `null`，不默认永不过期 |
| `supersedes_id` | 可选，关联被更正的旧证据，旧内容永久保留 |

时区统一为 UTC；拒绝无时区时间、发布时间晚于采集时间、无效产品、重复/冲突 ID、无效引用等。
证据缺发布时间或有效期、未到发布时间/采集时间、超过有效期，都不能作为当前有效事实。
到达证据截止时刻即视为过期。新材料更正旧材料时，对共同适用产品停用旧材料，
不会误删旧材料针对其他产品的适用性。来源可以是用户提供材料，不自动抓取或购买信息。

复核 `Review` 包含 `id`、`instrument`、`revision_id`、`reviewed_at`、`reviewer`、`notes`、
已检查证据集合 `evidence_ids`、条件检查数组 `checks`。
每个检查包含 `condition_id`、`outcome`（`met/not_met/unknown`）和引用的 `evidence_ids`。
这是登记人的显式判断，软件不会从自然语言自动判断条件是否满足。
`evidence_ids` 可记录已经看过但不能支撑条件的材料；条件引用的材料也计入已阅集合。

## 状态与依据

| 状态 | 主要触发条件 |
|---|---|
| 待补充 `MISSING_INPUT` | 缺买入理由、基本面检查/失效条件或复核周期；不代填、不默认成立 |
| 待复核 `PENDING_REVIEW` | 没有当前有效复核、缺有效事实、条件未查清、证据/复核过期、新版本或出现尚未检查的新证据 |
| 有支持证据 `SUPPORTED` | 当前版本有效复核，检查条件有新鲜基本面事实支撑且满足，失效条件有事实支撑且未满足，存在支持事实且无材料冲突、无遗漏新材料 |
| 存在冲突 `CONFLICT` | 当前存在有效基本面反面事实；仅说明登记材料存在反对依据，不自动判定退出 |
| 触发失效条件 `INVALIDATED` | 有效人工复核明确勾选基本面失效条件满足，并引用适用产品的有效基本面事实 |

优先处理缺失登记；完整登记时失效触发优先于材料冲突，其后才可能有支持证据。
新增尚未检查的材料同时阻止继续沿用旧的支持或失效复核结论；若仍有有效反面事实则显示冲突并要求复核。
报告显示状态依据、证据来源及时间、新鲜度、条件检查与引用标识，并保留历史版本信息。
`SUPPORTED` 不是“投资逻辑已客观证实”，也不是持有或买入指令。过期证据仍可见，但不能当成新证据使用。

技术指标、小时破位、持有满 72 小时、用户判断、模型推断均不能单独使长期逻辑成立或失效。
若把技术或时间观察登记成相应类别，系统只将其作为辅助信息。用户输入若将技术事实错误标为基本面，
软件无法可靠地理解并纠正自然语言分类，登记人仍需按上述规则核实分类。

## 历史和更正

独立 SQLite 文件保存只追加的版本、证据、复核；应用层无更新/删除入口，数据库触发器拒绝行更新与删除。
每次复核同时保存当时的计算结果，后来证据过期、新版本或新结论不会覆盖它。
同类同 ID 同内容重复提交返回 `added=false`；同 ID 不同内容拒绝。更正时使用新 ID，并通过版本或
`supersedes_id` 关联。新增复核必须指向当前版本，修订和复核按产品顺序追加，不接受倒序改写。
写入使用事务与 `synchronous=FULL`，并发提交不会丢失记录；这不是防篡改签名或断电硬件保障。

`report --at <带时区时点>` 按当前已登记材料及其声明时间重算，可用于离线复现；后补材料可能改变重算结果，
它不等价于当时已知信息。原始复核结论应查看 `history` 的 `journal[].review_result`；实际入库时间记录在
`journal[].recorded_at`。个人本地文件没有加密，应使用操作系统访问权限保护，并在停止写入后备份。

## 合成示例：支持到失效并保留历史

`examples/spot-thesis/synthetic-*.json` 全部为虚构测试材料，不是用户真实买入理由，也不代表真实 BTC 事件。
使用单独示例数据库：

```powershell
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db init
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db register --file examples/spot-thesis/synthetic-revision.json
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db evidence --file examples/spot-thesis/synthetic-evidence.json
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db review --file examples/spot-thesis/synthetic-review.json
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db report --at 2026-01-02T02:00:00Z
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db evidence --file examples/spot-thesis/synthetic-invalidation-evidence.json
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db review --file examples/spot-thesis/synthetic-invalidation-review.json
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db report --at 2026-01-03T02:00:00Z
.venv\Scripts\python.exe -m scripts.spot_thesis --database data/personal-theses/example.db history
```

预期 BTC 从 `SUPPORTED` 变为 `INVALIDATED`；其余四个产品始终待补充；旧证据和第一次复核的支持结果仍在历史中。
用当前时间查看这份固定历史示例时，材料已过期，不能把历史支持/失效结果冒充当前结论。

## 与现货分析连接及测试

现有长周期分析可显式添加 `--thesis-database data/personal-theses/theses.db`。
缺省不读逻辑数据库，未登记产品正常显示待补充；技术计算不改变登记状态。
该现货分析入口本身可能获取公共行情，逻辑登记模块始终离线。详见[持仓分析](position-review.md)。

```powershell
.venv\Scripts\python.exe -m pytest tests/test_spot_thesis.py tests/test_spot_thesis_storage.py tests/test_spot_thesis_cli.py tests/test_position_review.py -q
uv run python scripts/quality_gate.py
```

质量门覆盖格式、Lint、Mypy、全量测试和安全 smoke；测试使用合成资料与临时数据库，不连接真实账户。

## 本次验收（2026-11-06）

| 验证对象 | 完整 Pytest | 安全 smoke | Ruff 格式 / Lint / Mypy |
|---|---|---|---|
| 用户指定本地源码 | 1017 通过，6 跳过 | 82 通过 | 全部通过（321 文件格式、198 源码类型检查） |
| GitHub main 基线上的集成副本 | 1185 通过，8 跳过 | 81 通过 | 全部通过（337 文件格式、209 源码类型检查） |

95 项本地相关测试通过；包含公共现货分析依赖的远端相关测试 123 项通过。两套完整测试的跳过项均为
需要 `uv` / `RESEARCH_TEST_UV` 的研究快照隔离运行集成场景；本次未验证这些场景。
环境没有 `uv`，实际使用现有 Python 3.12 虚拟环境等价执行质量门各项命令，不宣称执行了缺失的工具。
本地完整测试使用不含 `.env`、个人数据与运行数据库的临时源码副本；没有访问真实账户或下单。

合成示例的支持→失效→证据过期复核、五个空白模板、旧证据与复核快照保留、重复提交及 Git 忽略规则
均已实际验证。新模块的来源、证据分类、时效及状态机另经独立代码审阅。

指定本地目录没有 `.git`；GitHub 集成副本基于远端 `9255abb906efa57e8e07ba944dde1ee04c16867d`，
保留远端已有功能并添加本次模块，以及远端缺少的公共现货分析依赖。远端的聚合函数导入按其现有
`v3_timeframes` 模块适配，没有用旧源码覆盖其策略、存储、运行恢复或组合功能。
这份记录描述本机实测结果，不代表 GitHub Actions 运行结果或真实账户验证。
