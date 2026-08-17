# First Commit Manifest（Phase 3C）

本仓库当前 0 commits。本清单描述首次提交的最终候选集合；真实仓库未执行
`git add`、`git commit` 或 `git push`。

## INCLUDE

| 类别 | 路径 | 文件数 | 说明 |
|---|---|---:|---|
| SOURCE | `app/`、`backtest/` | 150 | 113 + 37 个 Python 源文件 |
| DEV_TOOL | `scripts/`、`codex-loop.ps1` | 27 | 26 个 Python 工具和 1 个 PowerShell 循环脚本 |
| TEST / FIXTURE | `tests/` | 112 | 106 个测试/辅助 Python 文件和 6 个脱敏 fixture |
| BENCHMARK_SOURCE | `benchmarks/` | 5 | 离线确定性画像工具源码 |
| CONFIG | `configs/` | 15 | 11 YAML、3 JSON、1 CSV；无凭证 |
| DOC | `docs/`、`README.md`、`agent.md`、`CODEX_LOOP_PROMPT.md` | 31 | 28 个 docs 文件和 3 个根目录文档；包含性能封板与本终审 |
| CI | `.github/workflows/ci.yml` | 1 | Windows/Ubuntu 只读质量门 |
| BUILD | `.env.example`、`.gitignore`、`.gitattributes`、`pyproject.toml`、`uv.lock` | 5 | 环境示例、Git 策略和可复现依赖 |
| RUNTIME_PLACEHOLDER | `data/.gitkeep`、`data/research/strategy_v3/*/README.md` | 3 | 空目录占位与研究说明 |

最终合计：349 个文件。该数字必须与 disposable Git index 的 `git ls-files` 一致。

## EXCLUDE

以下文件仅保留在本地，不进入首次 Git 历史：

- `docs/audits/`：内部审计记录，包含凭证衍生标识和真实运营上下文。
- `PROJECT_HANDOFF.md`：包含生产数据库指纹和历史运行证据。
- `scripts/phase_6c3b_order_attribution.py`：内部归因工具，包含真实订单/运行标识。
- `app/services/legacy_inventory_cleanup.py` 与 `tests/test_legacy_inventory_cleanup.py`：
  已退役、无生产调用者的历史清理模块及其专用测试，包含固定历史运营标识。
- `.env`、运行数据库、备份、日志、缓存、构建物、性能输出和本地图谱。

## 明确决策

- `docs/audits/`：EXCLUDE
- `PROJECT_HANDOFF.md`：EXCLUDE
- `agent.md`、`CODEX_LOOP_PROMPT.md`：INCLUDE
- `uv.lock`：INCLUDE
- `benchmarks/`：INCLUDE
- 性能文档：INCLUDE
- `.gitattributes`：INCLUDE，保持 `* -text`

本清单没有待人工选择项。
