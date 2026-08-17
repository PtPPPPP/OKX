# 阶段 6A 连续模拟交易

默认关闭。连续交易只能在 Shadow 已通过后，以显式确认参数启动。

策略库存与 OKX 账户总 BTC 分离：启动时的 1 BTC 记为 `external_or_preexisting`，阶段 5 验证成交记为 `manual_validation`。连续策略只允许卖出同一 `run_id` 的 `strategy_managed` 库存，绝不使用账户 `max_sell` 作为策略可卖数量。

首轮上限：单笔 5 USDT、策略敞口 10 USDT、最多 2 笔、最多 60 分钟、最多一个未完成订单。任何 unknown、对账、数据库或私有 WebSocket 异常都会冻结运行。
