# Unknown 订单恢复接口合同

本流程只使用只读 OKX V5 接口。证据完整性按目标提交时间窗口是否被已完成的查询覆盖判断，而不是要求所有代码路径都成功。

| 接口 | 官方合同 | 当前作用 | 保留范围 | 当前案件是否必需 |
| --- | --- | --- | --- | --- |
| `/api/v5/trade/orders-pending` | 是 | 当前挂单 | 当前 | 是 |
| `/api/v5/trade/orders-history` | 是 | 最近已完成订单 | 最近 7 天 | 是 |
| `/api/v5/trade/orders-history-archive` | 是 | 已完成订单归档 | 最近 3 个月 | 仅窗口超出最近范围时 |
| `/api/v5/trade/fills` | 是 | 最近成交 | 最近 3 天 | 可选 |
| `/api/v5/trade/fills-history` | 是 | 成交历史 | 最近 3 个月 | 是 |
| `/api/v5/trade/fills-history-archive` | 否 | 不调用 | 不适用 | 否 |
| `/api/v5/account/bills` | 是 | 最近账单 | 官方最近范围 | 是 |
| `/api/v5/account/bills-archive` | 是 | 账单归档 | 最近 3 个月 | 仅需要补足时间窗口时 |

`fills-history-archive` 不是官方 V5 REST 合同：历史请求保留为审计记录，标记为 `unsupported_endpoint_contract`、`not_applicable`、`blocking=false`，并由 `/api/v5/trade/fills-history` 取代。

每个分页查询保存请求窗口、页数、记录数、首末记录时间和完成状态。空结果只有在 HTTP 成功、OKX `code=0`、分页完成且请求窗口覆盖目标提交时间时，才是有效的无证据结果。
