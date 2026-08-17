# OKX Quant MVP

一个面向 OKX 模拟盘与离线回测的量化交易框架。当前基线只允许 Demo 环境写入，
不代表实盘可用，也不提供实盘订单入口。

## 环境

- Python 3.11 或 3.12
- [uv](https://docs.astral.sh/uv/)

```powershell
uv sync --extra dev
```

真实凭证只放在本机 `.env`；仓库仅提交 `.env.example`。不要把 API Key、Secret、
Passphrase、账户信息或内部审计记录加入 Git。

## 质量门

```powershell
uv run python scripts/quality_gate.py
```

该命令依次检查格式、Ruff、Mypy、完整测试和安全 smoke；任一检查失败都会返回非零退出码。

## CLI

正式入口是 `app.cli`。先查看总帮助：

```powershell
uv run python -m app.cli --help
```

常用命令均可先用 `--help` 查看参数，不会发起 Demo 写入：

```powershell
uv run python -m app.cli show-config --help
uv run python -m app.cli validate-config --help
uv run python -m app.cli db-status --help
uv run python -m app.cli db-backup --help
uv run python -m app.cli db-migrate-plan --help
uv run python -m app.cli db-migrate-authorized --help
uv run python -m app.cli run-shadow-replay --help
uv run python -m app.cli backtest --help
uv run python -m app.cli observe-demo --help
```

`db-migrate-authorized` 只能配合已生成且仍与目标数据库匹配的迁移计划、确认信息和验证备份使用。
不要对真实运行数据库做试验性迁移。

## 安全边界

- OKX 写请求只允许 Demo 环境，并强制发送 `x-simulated-trading: 1`。
- 下单与撤单需要受控服务签发的一次性授权；未知提交结果禁止盲目重试。
- 默认、Continuous Shadow、订单和迁移路径使用 SQLite `FULL`。
- 只有可确定性重放的 Replay Session 使用 `WAL + NORMAL`。
- Phase 2 性能工作已经封板，详见 `docs/phase-2-performance-closure.md`。

## CI

GitHub Actions 在 Windows 与 Ubuntu 上运行同一质量门。工作流只申请 `contents: read`，
不部署、不发布、不读取凭证。
