# OKX 模拟盘只读鉴权诊断

`diagnose-okx-auth` 只调用公共时间、公开现货规则、账户配置和账户余额接口。它不会查询订单、提交订单、撤单或启动交易循环。

```powershell
.venv\Scripts\python.exe -m app.cli diagnose-okx-auth --config configs/btc_ma_demo.yaml
```

诊断输出只包含凭证是否存在、来源类别、首尾空白和换行状态；绝不输出 API Key、Secret、Passphrase、签名或授权请求头。

模拟盘 REST 使用 `https://www.okx.com`，私有请求必须包含 `x-simulated-trading: 1`。REST 签名是 `timestamp + METHOD + requestPath + body` 的 HMAC-SHA256 后 Base64 编码；GET 查询参数属于 `requestPath`。时间戳使用 UTC，OKX 文档说明与服务器相差超过 30 秒会被拒绝。

官方依据：[REST 鉴权与模拟盘](https://www.okx.com/docs-v5/en/)；[账户配置接口](https://www.okx.com/docs-v5/en/#rest-api-account-get-account-configuration)。

若诊断仍失败，请仅在 OKX API 管理页面核对：该 Key 是否为模拟盘 Key、是否具有 Read 权限、Key/Secret/Passphrase 是否同组，以及出口 IP 是否符合白名单。不要把任何凭证粘贴到命令行、代码或聊天中。
