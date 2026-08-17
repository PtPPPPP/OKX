# OKX SPOT cash 能力按账户模式审计

官方交易模式表明确：Futures 账户模式下，SPOT 使用 `tdMode=cash`。这不代表保证金、合约或期权能力被启用；本项目只审计 `SPOT + cash`。

本项目以 `GET /api/v5/account/max-avail-size?instId=BTC-USDT&tdMode=cash` 的 `availBuy` 和 `availSell` 作为当前可买、可卖数量的权威证据。对于 SPOT，前者为计价币金额，后者为基础币数量。

账户余额中的 `eq`、`availEq`、未实现盈亏和保证金字段不作为 SPOT cash 可用数量。`cashBal`、`availBal`、`frozenBal` 只作为余额快照和交叉核验来源。

```powershell
.venv\Scripts\python.exe -m app.cli audit-spot-capability --config configs/btc_ma_demo.yaml
.venv\Scripts\python.exe -m app.cli plan-demo-spot-order --config configs/btc_ma_demo.yaml
```

第二个命令只写入 dry-run 审计记录，固定 `submission_performed=false`，不会调用 Broker 或订单接口。

官方依据：[交易模式对照和最大可用交易额度](https://www.okx.com/docs-v5/trick_en/)；[账户最大可用额度接口](https://www.okx.com/docs-v5/en/)。
