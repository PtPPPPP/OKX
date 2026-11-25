# CI 说明（Phase 3B）

## 设计

```text
workflow=.github/workflows/ci.yml
platforms=windows-latest + ubuntu-latest（矩阵；fail-fast=false 以便同时看到两平台结果）
python=3.12（pyproject 要求 >=3.11；开发实测 3.12.11；两平台固定一致，暂不扩展版本矩阵）
dependency_manager=uv（与本地一致：uv sync --extra dev → uv run ...）
quality_command=uv run python scripts/quality_gate.py（本地与 CI 同一入口，无重复 YAML 逻辑）
triggers=push + pull_request（无 schedule/dispatch/deploy）
permissions=contents: read（显式最小化；无任何写权限动作）
timeout=45 分钟/job（本地 gate 约 10 分钟，留出 CI 冷启动与安装余量）
cache=仅 uv 依赖缓存（setup-uv enable-cache）；绝不缓存 data/、artifacts/、pytest 状态
```

## 步骤分离

`uv sync --extra dev`（依赖安装失败）与 `scripts/quality_gate.py`（质量失败）是独立
step，CI 日志可直接区分两类失败。

## 第三方 actions（官方/广泛使用，版本已记录）

```text
actions/checkout@v4
actions/setup-python@v5
astral-sh/setup-uv@v6（uv 官方 setup action）
```

## CI 不需要任何 secret

默认测试套件 offline、deterministic、fresh-clone 自包含：无 OKX API key、无 .env、
无任何 GitHub Secret。任何默认测试需要凭证都属工程缺陷（fresh-clone invariant 的
延伸），不得用 Secrets 绕过。

## CI 明确不做的事

```text
不运行 benchmark（10000-candle / cProfile / durability A/B 属本地研究工具）
不上传 performance artifacts（phase_2*.json/.pstats 本就是 gitignored 本地产物）
不上传 pytest report（失败时 GitHub log 足够）
不对任何真实数据库做 migration（测试只建临时合成库）
不创建 .env、不连 OKX、不下单
无 release/deploy/publish
```

## 已知平台相关代码（分类）

```text
intentional Windows-only：
  app/services/demo_session.py 的 sys.platform=='win32' 事件循环分支（双分支，Linux 走 else）
  app/services/legacy_quarantine.py 的 os.name=='nt' winerror 处理（Linux 自然跳过）
  codex-loop.ps1（开发辅助 PowerShell 脚本；无需 Linux 等价物，不参与 CI）
portable：其余全部（gethostname/socket 等跨平台 API）
行尾：.gitattributes `* -text` 禁用全部转换——仓库字节即提交字节，
  保证 pinned fixture sha256 跨平台一致
```

## 本地等价命令

```powershell
uv run python scripts/quality_gate.py
```
