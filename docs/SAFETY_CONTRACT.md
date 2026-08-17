# OKX Demo 安全约束

本文件是本项目长期有效的安全边界；与旧报告冲突时，以当前代码、测试和正式数据库为准。

- 只允许 BTC-USDT、SPOT、`tdMode=cash`、限价、long-only 的受控 Demo 路径；live 始终关闭。
- 禁止实盘、SWAP、杠杆、借贷、资金划转、充值、提现、账户或持仓模式修改。
- 只能出售当前 run、当前 runtime generation、`strategy_managed` 范围内且可用的库存；不得使用账户总 BTC、`manual_validation`、`external_or_preexisting` 或 unknown 库存。
- 私有状态、账户、订单或成交存在无法解释的变化时，必须冻结并停止提交。unknown 订单只能通过 ordId/clOrdId 的只读恢复处理，不能再次提交。
- 私有 REST 对账、账户快照持久化和私有 WS 事件归并只能经 `PrivateStateCoordinator`；CLI、诊断、启动恢复和订单服务不得直接调用底层归并组件或解除冻结。
- 每份 Proposal 都要经过预检、状态令牌复核和一次性原子提交栅栏；提交结果不明时预算不返还。
- 未经明确授权，不迁移正式数据库、不启动 Demo、不调用 Broker 或 OKX 交易写接口，也不自动 Git commit 或 push。
- 测试必须使用临时数据库和 Fake Broker；不得用长时间 sleep 猜测并发顺序。
- 故障注入只能使用显式本地 adapter、FaultPlan 和虚拟时钟；非本地 adapter 必须拒绝，禁止以真实网络或真实凭证制造故障。
