# P0 研究快照实际验收记录

验证日期：2026-10-15。环境：Windows、CPython 3.12.14、uv 0.12.3。
当前目录没有 `.git`，无法提供 Git diff 或干净 clone 证明。没有新增第三方依赖。
未修改策略参数、候选选择和既有冻结窗口，未调用交易 API。

实现入口是 `scripts/research_snapshot.py`（标准库启动器）和
`backtest/research_snapshot.py`（快照内计算 worker）。原冻结/执行模块复用数据质量门、
训练决定与同一个样本外计算函数。操作说明见 [research-snapshots.md](research-snapshots.md)。

| 验收项 | 实际结果 |
|---|---|
| 开发代码后续变化 | 独立 fixture 的研究执行和风险源码改成直接抛错，快照仍成功运行 |
| 迁移及项目外启动 | 复制到项目外带空格目录，从项目外 cwd 启动，成功 |
| PYTHONPATH 污染 | 同时含被破坏的开发副本及原项目路径，仍使用快照代码 |
| 源码、参数、锁文件、数据篡改 | 测试逐项修改，运行前拒绝，不生成成功回执 |
| 数据/依赖缺失、版本错误 | 明确失败；覆盖外部数据、缺包、包元数据版本和 Python 版本错误 |
| 结果一致性 | 两个小型离线运行的完整信号语义指纹、成交、资金曲线和经济指标相同 |
| 消费约束 | 已消费批次拒绝首次验证；复制首次验证后的包仍拒绝再次首次验证 |
| 敏感文件边界 | sentinel 测试证明 `.env`、账户 DB、日志、虚拟环境等不打包；实际归档禁用文件检查为空 |

小型演示使用 **120 根训练、36 根样本外合成 K 线**，仅验证工程复现，不构成行情或收益证据。
保持五个原参数候选和训练拒绝决定，两个候选分别出现 2、4 笔成交，其余为零。
创建 → 校验 → 独立环境恢复 → 运行 → 复制 → 项目外恢复并再次运行均实际完成。

- [完整演示命令和结果](../artifacts/research_snapshot_p0/demonstration_final/demonstration.json)
- [小型快照 manifest](../artifacts/research_snapshot_p0/demonstration_final/snapshot/snapshot.json)
- 独立副本：`C:/Users/PtP/AppData/Local/Temp/okx_snapshot_p0_demo_awdwoxoh/portable experiment`

还对既有批次的 **1,505 根历史样本外 K 线**登记运行了完整复现，成交、资金曲线和经济指标
与原发布报告一致。原冻结哈希仍为
`0900bba983c0ce3d1476364d8e0970ee410319a9cd44fe252078dc69f1054136`。
原研究产物和公共历史数据共 **343 个文件**在工作前后逐文件 SHA-256 不变。

- [既有批次的事后归档](../artifacts/research_snapshot_p0/legacy_snapshot/snapshot.json)
- [原报告复现的成功回执](../artifacts/research_snapshot_p0/legacy_snapshot/registrations/original-oos_complete.json)
- [汇总验收证据](../artifacts/research_snapshot_p0/final_verification.json)

该旧批次明确标注“事后归档”：32 个文件与原清单匹配、3 个原清单文件已修改、154 个文件
没有原冻结时的哈希证据。数值结果一致不证明原完整源码或原环境被恢复；这两个证明标识
仍为 false。原消费记录、训练拒绝、报告和未结束的前向窗口均保留。

实际检查：

- 全量 Pytest：**804 passed，399.03 秒**，包括实际 uv 隔离环境集成测试，没有跳过。
- 最后对解释器检查和清单提示的修改补测 **5 passed**；随包文档/清单补测 **1 passed**。
- `ruff format --check .`：297 个文件通过；`ruff check .` 通过。
- `mypy`：183 个源码文件通过。
- [全量日志](../artifacts/research_snapshot_p0/full_pytest.log)、[最后定向日志](../artifacts/research_snapshot_p0/final_targeted.log)。

第一次无缓存离线依赖恢复明确失败；随后仅在快照 `.runtime` 中联网安装锁定包，之后的离线
恢复使用了已填充缓存。没有改变项目 `.venv` 的依赖安装。未验证跨平台、无缓存离线恢复、
另一台机器或其他 CPython 构建。本地哈希与独占文件无法阻止人为删改重算，也不能让
验证前分叉的离线副本共享全局唯一消费状态；各副本始终属于同一个冻结批次。
