# 模拟盘验收流程

## 无凭证也能执行

```powershell
.venv\Scripts\python.exe -m app.cli db-status
.venv\Scripts\python.exe -m app.cli demo-doctor --config configs/btc_ma_demo.yaml
.venv\Scripts\python.exe -m app.cli observe-demo --config configs/btc_ma_demo.yaml --strategy buy_and_hold --bar 1m --max-events 1 --timeout-seconds 75
```

期望结果：数据库与公共接口通过；凭证和订单门禁被阻断；公共观察能连接、只接收确认 K 线、执行信号和风控、`submitted_order=false`。

## 有模拟盘凭证后

```powershell
.venv\Scripts\python.exe -m app.cli demo-doctor --config configs/btc_ma_demo.yaml
.venv\Scripts\python.exe -m app.cli sync-demo-account --config configs/btc_ma_demo.yaml
.venv\Scripts\python.exe -m app.cli run-demo --config configs/btc_ma_demo.yaml
```

只有 `demo-doctor` 的私有检查、恢复和 `order_allowed` 都为 PASS 后，才可以人工决定是否运行测试订单。系统不会自动运行此命令：

```powershell
.venv\Scripts\python.exe -m app.cli place-demo-test-order --config configs/btc_ma_demo.yaml --side buy --price <符合当前价格精度的价格> --confirm-demo-order
```

安全条件：限价单、现货、模拟交易头、单笔不超过 20 个计价币单位、统一风控、提交前后对账。不要使用明显偏离市场的价格来规避真实成交逻辑；模拟盘测试也应按正常订单状态处理。

## 当前验收边界

若运行环境没有完整模拟盘凭证，真实私有 REST、私有 WebSocket 和测试订单均必须阻断。这不是通过，也不会发送订单。曾在聊天、工单或其他外部位置明文暴露的凭证必须先轮换，程序不会自动创建、删除或修改 API Key。

受控私有状态检查：

```powershell
.venv\Scripts\python.exe -m app.cli check-demo-private-state --config configs/btc_ma_demo.yaml
```

只有账户总额、可用余额、冻结余额、持仓成本、挂单、私有订阅和首次 REST 对账全部可确认时，人工测试订单门禁才允许继续。单笔名义金额不得超过 5 个计价币单位。
