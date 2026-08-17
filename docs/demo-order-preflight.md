# 模拟盘订单统一预检

`prepare-demo-order` 是 SPOT/cash 模拟盘订单提案的唯一准备入口。它读取账户、现货规则、最大可用额度、持仓和挂单，复用 SPOT/cash 能力评估后生成带哈希和 30 秒有效期的提案。

```powershell
.venv\Scripts\python.exe -m app.cli prepare-demo-order --config configs/btc_ma_demo.yaml
```

本阶段提案固定为 `submission_performed=false`。即使能力评估通过，提案也会因 `controlled_submission_disabled` 标记为 `blocked`；不会调用 Broker 或 OKX 订单接口。
