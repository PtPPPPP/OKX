# 独立研究快照 v1

快照固定研究代码、参数、环境约束、数据和证据。它只执行既有 V3 冻结队列的离线成交验证，
不提供下载、调参、重新选候选或交易入口。没有新增第三方依赖。

## 格式与证据

`snapshot.json` 的 `format_version=1`。`snapshot_hash` 是去掉自身字段后的规范 JSON SHA-256；
`files` 列出所有不可变文件及原始字节 SHA-256。建议另存该哈希到可信审计系统。

```text
snapshot.py                  仅用 Python 标准库的独立启动器
snapshot.json                格式版本、原冻结哈希、环境、数据映射、源码证据、文件清单
USAGE.md                     随包保存的操作说明
code/app/**/*.py             整个源码目录的 Python 文件
code/backtest/**/*.py
code/scripts/**/*.py
code/configs/research/**/*.json
code/configs/snapshots/**/*.json
code/{pyproject.toml,uv.lock,README.md}
code/docs/research-snapshots.md
datasets/historical/         历史训练数据的 manifest 和 normalized/candles.csv
datasets/<purpose>/          已绑定的历史样本外或前向数据（可稍后追加到派生包）
evidence/cohort.json         原冻结文件，字节不变
evidence/training/           原训练决策和其 manifest 发布的全部报告资源
evidence/*_consumed.json     原消费记录，字节不变（包括另一 purpose 的既有记录）
evidence/*_complete.json     原完成记录，字节不变
evidence/*_pending.json      原等待记录，字节不变
evidence/original_report/    已消费样本外的原发布报告
.runtime/                   本机专用、可重新建立的依赖环境，不属于可迁移归档内容
state/                      快照内首次验证的消费、完成记录
registrations/              显式运行登记、信号轨迹、成功或失败的复现比对记录
runs/<run-id>/               新运行结果；不覆盖 evidence 或之前的 runs
```

新冻结的 `schema_version=2` 按目录收集源码，包含 `app/risk/position_sizing.py` 等间接依赖，
同时绑定 `pyproject.toml`、锁文件、Python 精确补丁版本、解释器实现、系统、架构和字节序。
数据/训练路径相对于项目根目录。旧 `schema_version=1` 的逐文件清单仅用于读取兼容，
不再扩展。未知格式拒绝读取。

不递归打包整个工作目录。只收集上述源码/JSON 规则和已发布报告 manifest 中的
JSON/CSV/Markdown；忽略隐藏目录、缓存。不会收集 `.env`、`.venv`、日志、账户数据库、
行情缓存 SQLite、原始下载响应或其他配置目录。源文件/资源不能是符号链接或 junction。
若凭证被错误地硬编码在允许的源码或研究 JSON 中，目录规则无法识别这种内容；归档者仍须
保证这些输入本身是可归档的研究材料。

## 创建、校验、迁移和复现已完成研究

项目根目录中使用现有 Python 创建，不需要安装或改变现有研究环境：

```powershell
$python = (Resolve-Path '.venv/Scripts/python.exe').Path
& $python -m scripts.run_research_cycle snapshot-create `
  --freeze artifacts/research_cycle_v1/freeze/cohort.json `
  --validation-cache data/research/rebuilt_v1/historical_oos `
  --output artifacts/my_research_snapshot
& $python -I artifacts/my_research_snapshot/snapshot.py verify
& $python -I artifacts/my_research_snapshot/snapshot.py read-report
```

等价独立创建入口为 `python -I scripts/research_snapshot.py create --source-root . ...`。
项目入口也提供 `snapshot-verify/prepare/read-report/attach-validation/validate-once/reproduce`。
创建目录必须不存在；失败保留现场，不能把没有 `snapshot.json` 的半成品当作成功归档。

旧记录中的绝对路径作为**原始证据**保留，执行不解析这些路径。原数据或报告已经搬迁时，
创建命令可用 `--historical-cache`、`--training-report`、`--validation-report` 指定实际目录，
仍须匹配原文件哈希、训练 artifact 集合哈希、消费记录的数据哈希与原报告集合哈希。
已消费但缺少完成记录的批次拒绝归档为可重跑研究，必须保留中断证据并审计。

复制完整快照到独立目录；后续入口使用该目录中的启动器，不再需要开发项目：

```powershell
Copy-Item artifacts/my_research_snapshot D:/research-archive/experiment-001 -Recurse
Set-Location $env:TEMP
$snapshot = 'D:/research-archive/experiment-001/snapshot.py'
& $python -I $snapshot verify
# 精确版本的 CPython 必须已经安装；若 uv 未在 PATH，增加 --uv C:/tools/uv.exe。
& $python -I $snapshot prepare --python $python
& $python -I $snapshot reproduce --run-id offline-001 --reason '迁移后离线核对'
& $python -I $snapshot read-report
```

`read-report` 校验并输出原报告，不执行实验，不要求安装研究依赖，也不写运行记录。
`reproduce` 必须提供新的 `--run-id` 和 `--reason`，先登记再计算，并保留原冻结哈希、
数据哈希、原报告集合哈希、父快照哈希（若有）、信号指纹和经济结果指纹。
已有 run ID、输出目录不复用。比对失败保留结果和 `_failed.json`，返回非零，不发布成功回执。

## 环境恢复与导入隔离

`prepare` 只向快照自己的 `.runtime` 安装，执行
`uv sync --locked --no-dev --no-install-project --no-config --no-python-downloads --reinstall --link-mode copy`。
不会改锁文件，不安装当前项目的 editable 包，也不退回别的 Python 或依赖版本。
可显式给 `--python` 指定解释器，但必须与归档中的补丁版本完全一致。
系统/架构/解释器实现不符时拒绝恢复；v1 未承诺跨平台可复现。
快照不封装操作系统、CPU、系统库和解释器二进制；也未证明不同系统补丁或不同解释器构建完全等价。

默认恢复允许下载锁文件指定的依赖包；`prepare --offline` 禁止联网，只使用已有 uv 缓存。
包、Python 或 uv 不存在时明确失败。包中不包含 Python 安装器、uv 二进制或 wheelhouse，
所以“保存锁文件”不等于保证日后离线安装成功。本次没有验证无缓存离线恢复。

每次执行先 `uv sync --check --locked --offline`，再验证安装元数据版本及安装文件字节清单，
不在执行过程中自动修复依赖。删除模块、修改安装文件或版本都会失败；用显式 `prepare`
重新安装后才可运行。迁移时不应复制 `.runtime`，应在目标目录重新恢复。

计算子进程使用 `.runtime` 的 Python，启用 `-I -S -B`，只显式加入快照 `code` 和其依赖目录。
`PYTHONPATH`、当前目录、用户 site、`.pth`、`sitecustomize` 不参与导入；另检查已加载研究模块
确实来自 `code`。只传递必要的操作系统环境变量及 `UV_CACHE_DIR` 缓存位置，不转发 OKX 凭证、
索引、解释器、安装策略等 uv/pip 环境覆盖。缓存位置沿用依赖准备阶段的设置；缓存不足时
离线恢复仍明确失败，不改用其他缓存、联网或切换版本。
计算入口不调用网络下载器或交易 API。这是研究入口隔离，不是对恶意 Python 代码的操作系统沙箱。

## 数据方案

默认随包保存历史训练数据和所选 purpose 的完整归一化 CSV、manifest。
每次运行先校验文件字节哈希，再使用原有数据质量门重新计算内容哈希，检查区间、已确认性、
下载时间和冻结时间。训练数据用于原来的 warmup；未来窗口仍只接受冻结的完整 720 根小时线。
既有参数、政策、冻结窗口不变。不允许缺数据时下载替代数据或缩短区间。

大数据可在创建时增加 `--external-data`。快照仍记录每个数据文件 SHA-256、逻辑 ID、
规范数据哈希和起止时间，运行时增加 `--data-root <目录>`；布局固定为：

```text
<data-root>/datasets/historical/data_manifest.json
<data-root>/datasets/historical/normalized/candles.csv
<data-root>/datasets/historical_oos/data_manifest.json
<data-root>/datasets/historical_oos/normalized/candles.csv
```

前向包的逻辑 ID 是 `prospective`。外部目录可以搬迁，但缺失或任何字节不符都返回非零，
不能切换同区间的另一版数据。只需复制这些指定文件，无需携带原 SQLite。

## 冻结之后数据才到达

新批次可在冻结后立即创建源码/训练证据包，省略 `--validation-cache`，用 `--purpose`
固定 `historical_oos` 或 `prospective`。归档后日常开发可以继续；数据未到齐时验证明确失败。
窗口结束、外部流程准备完整公共数据后，用**旧包自己的启动器和环境**生成一个派生包：

```powershell
python -I D:/frozen/snapshot.py prepare --python C:/Python312/python.exe
python -I D:/frozen/snapshot.py attach-validation `
  --cache D:/public-history/prospective `
  --output D:/frozen-with-data `
  --reason '冻结窗口结束后绑定完整数据'
python -I D:/frozen-with-data/snapshot.py prepare --python C:/Python312/python.exe
python -I D:/frozen-with-data/snapshot.py validate-once `
  --run-id original-validation --reason '首次完整前向验证'
```

追加操作不改父包，复制同一源码、冻结、训练决定和既有消费证据，记录父 manifest 原件、
父快照哈希及新数据哈希。已有数据或已消费时拒绝追加/替换。
首次 `validate-once` 在计算前独占写入 `state/*_consumed.json`；中断仍算已消费。
复制包时必须带上 `state`、`registrations`、`runs`。已完成后只能读报告或明确登记 `reproduce`。

## 既有研究与本地约束的边界

`artifacts/research_cycle_v1` 不被改写。v1 事后创建的包明确标为 `POST_HOC_ARCHIVE` / **事后归档**，
环境只是归档时观察值，不能声称是原冻结时的环境。`source_evidence` 逐项列出：

- `MATCHES_FROZEN_HASH`：当前字节与原清单相同。
- `CHANGED_SINCE_FREEZE`：原清单记录过，但当前内容已不同。
- `NOT_COVERED_BY_ORIGINAL_FREEZE`：原清单没有证据，例如 `app/risk/position_sizing.py`。

旧配置和静态交易规则必须匹配原哈希，不能以事后归档名义改参数。对于原清单未覆盖或
后来修改的源码，本包只固定当前归档内容；即使计算结果一致，也不能证明曾恢复原完整环境。
v2 则要求全量规则收集的源码及 Python/平台记录与冻结完全匹配后才允许创建包。

样本外已经消费的包不能重新运行首次验证，不能晋升训练拒绝者；复现是单独的审计动作，
不构成新的样本外证据。一个包固定一个 purpose，另一 purpose 的既有消费原件仍保留。
哈希及独占创建可以发现内容变化、防止正常入口覆盖和重置已携带的消费记录；**不能**
阻止拥有文件系统写权限的人删除记录并重算哈希，也不能让验证前分叉的多个离线副本拥有
全局唯一的消费状态。本项目未引入中央登记或第三方时间戳，离线复制不提供全局防重跑保证。
同一冻结哈希的所有副本应视为同一研究批次，审阅时合并各副本登记，不把副本当作新实验。

经济比对覆盖完整 summary、逐笔成交、闭仓周期和资金曲线。允许变化的是登记时间、完成时间、
外层 run ID、随机 `client_order_id`，以及买入持有对照的随机 `signal_id`；V3 候选的确定性
信号 ID 仍参与成交比对。新运行还保存逐条信号的时间、动作、理由、置信度和 metadata，
仅去掉随机身份字段。旧报告未保存全部信号轨迹，因此与旧报告只能比对信号计数及已有成交
信号字段；不能补称已经核实了旧运行每个未成交信号。两个新快照运行可以比较完整信号指纹。

## 验证

```powershell
# uv 应在 PATH，或用 RESEARCH_TEST_UV 指定；集成测试只用已有缓存恢复独立环境。
$env:RESEARCH_TEST_UV = 'C:/tools/uv.exe'
.venv/Scripts/python.exe -m pytest tests/test_research_cycle.py tests/test_research_snapshot.py -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy
```

测试使用显式合成 K 线，不将其当作真实行情或收益证据；不访问交易 API。
没有 uv 时只跳过需要实际环境恢复的集成测试，常规完整测试结果须报告跳过数。
