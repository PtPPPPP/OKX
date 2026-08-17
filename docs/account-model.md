# 账户余额与持仓成本模型

## OKX 字段口径

项目使用 OKX V5 `GET /api/v5/account/balance` 的 `details`：

- `cashBal` → `total_balance`：币种现金总余额；
- `availBal` → `available_balance`：当前可用于现货订单的余额；
- `frozenBal` → `frozen_balance`：交易所明确返回的冻结余额；
- `eq` → `equity`：币种权益；
- `eqUsd`，缺失时使用 `disEq` → `usd_equity`；
- `uTime` → UTC `updated_at`。

官方文档：[OKX V5 Get balance](https://www.okx.com/docs-v5/#rest-api-account-get-balance)。

不能假定 `cashBal = availBal + frozenBal`。当前真实模拟账户响应已经证明该等式并非对所有币种恒成立，因此冻结余额直接采用 OKX 字段；总持仓和权益不再使用 `availBal`。

## 使用规则

- 权益和风险敞口使用总余额；
- 买单能力使用计价币可用余额；
- 卖单数量使用基础币可用余额；
- 已冻结基础币仍属于总持仓；
- 未完成买单的剩余名义金额计入风险敞口。

## 平均成本

恢复顺序：

1. 项目持久化的模拟盘成交；
2. OKX 最近三个月现货成交历史；
3. OKX 账户 `openAvgPx`，仅在 USD、USDC 或 USDT 计价时采用；
4. 无法验证数量一致时标记为 `unknown`。

成交重放考虑多次买入、部分卖出以及手续费币种。只有重放后的数量与当前总持仓在交易规则步长内一致，成本才标记为可靠。存在持仓但成本不可靠时，不执行自动保护性退出，也不允许阶段 5 测试订单。

## 私有 WebSocket

私有账户和持仓推送只更新 `private_state_snapshots` 暂态。重复事件由幂等键拒绝，旧事件不会覆盖新事件。REST 对账成功后将暂态标记为已确认；不得用一次推送永久覆盖权威账户快照。
