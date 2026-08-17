# OKX 账户字段合同

来源：OKX V5 `GET /api/v5/account/balance`、`GET /api/v5/account/config`、私有 `account` 与 `balance_and_position` 通道（2026-07 核对）。官方字段定义见：<https://www.okx.com/docs-v5/zh/#rest-api-account-get-balance> 和 <https://www.okx.com/docs-v5/zh/#rest-api-account-get-account-configuration> 。

| 字段 | OKX 定义 | 项目用途 |
|---|---|---|
| `cashBal` | 币种现金余额 | 原样保存为 `cash_balance`，不称为总余额 |
| `availBal` | 币种可用余额 | 仅现货模式作为可交易数量 |
| `frozenBal` | 币种冻结余额 | 原样保存；不推导为 `cashBal-availBal` |
| `eq` | 币种权益 | 原样保存为 `equity` |
| `eqUsd` | 币种美元权益 | 原样保存为 `equity_usd` |
| `disEq` | 折扣后美元权益 | 原样保存为 `discount_equity` |
| `totalEq` | 美元层面账户权益 | 仅账户权益展示，不等同策略组合权益 |
| `adjEq` | 美元层面有效保证金 | 仅保证金账户语义 |
| `availEq` | 美元层面可用保证金 | 不用于现货可买数量 |
| `isoEq` | 美元层面逐仓权益 | 仅逐仓/保证金语义 |
| `ordFrozen` | 挂单占用保证金 | 仅适用模式下的保证金字段 |
| `upl` | 未实现盈亏 | 原样保存，可为负 |
| `liab` / `interest` | 负债 / 利息 | 非零即不支持当前纯现货自动交易 |

账户模式来自 `acctLv`：`1` 现货、`2` 合约、`3` 跨币种保证金、`4` 组合保证金。当前项目只支持模式 `1` 且无负债、无衍生品仓位的 BTC-USDT 现货。其他模式只能同步和审计，固定禁止订单提交。

`available + frozen = cash` 不是通用不变量。字段缺失使用 `None`；没有可靠字段合同时使用 `insufficient_data`，不会猜测或补零。

Futures 账户模式不等于 FUTURES 交易品种。官方交易模式表说明 Futures 模式下 SPOT 使用 `tdMode=cash`；当前可交易数量必须由 `max-avail-size` 的 `availBuy` / `availSell` 单独验证，不能仅依赖 `cashBal` 或账户权益。
