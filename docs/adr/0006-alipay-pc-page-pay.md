# ADR-0006: 支付宝电脑网站支付与服务端权益确认

**状态**：已决策（RND-416）  
**日期**：2026-08-23  
**关联任务**：RND-416  
**接续**：[ADR-0004](0004-annual-plan-wechat-pay-gates.md) 的 provider-neutral 支付边界

## Context

年度套餐已有 provider-neutral 订单、可信付款事件、幂等订阅激活和期限授予。支付宝电脑网站支付要求 PC 浏览器进入支付宝收银台；收银台可让客户用支付宝 App 扫码。浏览器同步跳回商户页不能证明付款结果。

## Decision

- 新订单在 `ALIPAY_ENABLED=true` 时优先选择 provider code `alipay`，既有订单永久按数据库中的 provider code 查询和关闭，不能因配置切换而误投其他渠道。
- 服务端以 RSA2 签名生成 `alipay.trade.page.pay` URL；只经受鉴权、`no-store` 的 303 checkout endpoint 转交浏览器。JSON、HTML、日志和异常正文不得包含签名 URL、私钥或其他密钥材料。
- PC 页面不自行生成或信任「已支付」结论：它跳转支付宝收银台，收银台展示 QR。`ALIPAY_RETURN_URL` 固定为 `/admin/billing`，仅重新展示订单投影。
- 可信付款只来自两条路径：
  1. `POST /api/payments/alipay/notify` 的表单字段去除 `sign`/`sign_type` 后按字典序 RSA2 验签；
  2. `alipay.trade.query` 返回的精确 `alipay_trade_query_response` JSON 节点 RSA2 验签。
- 两条路径均必须核验 AppID、seller ID、`out_trade_no`、`trade_no`、`total_amount` 与订单的 CNY 分金额；只有 `TRADE_SUCCESS` 或 `TRADE_FINISHED` 能形成 `TrustedPaymentEvent`。通知 event ID 和交易号继续由既有持久化幂等边界约束。
- `WAIT_BUYER_PAY` 和尚未在支付宝创建的订单保持 pending，`TRADE_CLOSED` 关闭；订单超时或用户关闭会调用 `alipay.trade.close`。未知或验签失败状态必须失败关闭。
- 生产配置只由密钥管理/EnvironmentFile 注入：`ALIPAY_ENABLED`、`ALIPAY_APP_ID`、`ALIPAY_SELLER_ID`、`ALIPAY_MERCHANT_PRIVATE_KEY`、`ALIPAY_PUBLIC_KEY`、`ALIPAY_NOTIFY_URL`、`ALIPAY_RETURN_URL`。`ALIPAY_PUBLIC_KEY` 可为支付宝 RSA 公钥或包含该公钥的 PEM X.509 证书。启用后任一缺失、PEM 不合法、密钥非 RSA/小于 2048 bit，或 URL 不是固定 HTTPS 路径，应用启动必须失败。
- 本票不接支付宝退款。微信退款控制面必须拒绝支付宝支付订单，不能把支付宝交易号提交给微信。

## Consequences

- 支付宝和微信共享订单、订阅、权益及回放处理，不复制任何生命周期规则。
- 运营人员必须分别完成支付宝产品签约、AppID/卖家 PID/公钥配置、HTTPS callback 注册和非生产实付验收；代码合入不是生产付款授权。
- 未来支付宝退款、账单对账、H5/小程序或服务商模式需要独立决策和工单。
