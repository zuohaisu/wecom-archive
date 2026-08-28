# ADR-0004: 年度套餐、微信支付与自助接入门禁

**状态**：已决策；订阅生命周期、退款与服务门禁由 [ADR-0005](0005-saas-billing-lifecycle-refunds-and-service-gates.md) 接续（2026-08-16）
**日期**：2026-08-12
**作者**：Haisu / Codex
**关联任务**：RND-382

---

## 变更记录

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1 | 2026-08-12 | 冻结首个年度套餐、Native 支付、订阅生命周期和生产启用门禁 |
| v2 | 2026-08-16 | 标注订阅生命周期、退款与服务门禁由 ADR-0005 接续；套餐、支付安全、身份和生产配置契约保持有效 |

---

## 1. Context

目标不是“接通一个支付 API”，而是让企业管理员可以自行完成：

`可信创建组织 → 购买年度套餐 → 微信扫码付款 → 自动开通权益 → 自行完成企业微信配置`

支付成功只是资金事实。Tenant、套餐、订阅、权益和企业微信运行状态必须分别有权威事实源，
不能由前端字段、支付路由或某个环境变量混在一起推断。

本决策延续 ADR-0003 已公开的价格，不重新发明 SKU。真实支付商户凭据仍是生产外部门禁；
它们只允许通过部署环境注入，缺失时支付能力必须显示为未配置并失败关闭。

## 2. Decision

### 2.1 唯一可购买套餐

| 字段 | 冻结值 |
|---|---|
| stable code | `annual_base_cny_99` |
| 展示名 | `年度基础套餐` |
| 价格 | CNY 99.00，即服务端整数 `9900` 分 |
| 周期 | 12 个日历月，一次性付款；不是免密代扣 |
| 存储额度 | 5 GiB，即 `5 * 1024^3` bytes |
| 座席 | 不限座席 |
| 超额口径 | 1 元/GB/月；首期支付闭环不自动售卖或自动扣取超额包 |
| 腾讯费用 | 企业微信会话存档接口的开通和腾讯官方费用另计 |

客户端只能提交稳定的 plan code；订单金额、币种、周期和权益必须从服务端 Plan 读取。
Plan 失活后不得创建新订单，但既有订单、支付和订阅历史必须保持可追溯。

### 2.2 订阅生命周期

> 本节的套餐期限和 Native 单次扫码续费原则继续有效；`grace`、`frozen`、独立 `suspended`、
> 到期不续费和 provider 退款后的期限回退，以 [ADR-0005](0005-saas-billing-lifecycle-refunds-and-service-gates.md) 为准。

- 只有可信支付结果可以激活或续费；创建订单、展示二维码或浏览器声称“支付成功”都不可以。
- 首次激活：`starts_at = trusted_payment_succeeded_at`，`ends_at` 为其后 12 个日历月，
  使用 UTC 保存，结束时间为排他的有效上界。
- 有效期内续费：不重置 `starts_at`，从当前 `ends_at` 再延长 12 个日历月。
- 已到期后续费：从新的可信支付成功时间重新开始 12 个日历月。
- 闰日规则：按日历年增加；2 月 29 日在非闰年的对应到期日为 2 月 28 日。
- 当前版本没有自动扣款。续费由 owner 再次创建订单并扫码支付；不得宣传“自动续费”。
- 支付失败、取消或二维码超时不改变现有订阅和权益。
- 支付已成功而领域激活失败时，订单进入可追踪的 `paid_activation_pending`，由同一幂等服务重试；
  不得静默丢单，也不得创建第二笔订阅。
- 用户取消未支付订单时调用微信关单；已支付订阅不因“取消续费”提前失效。
- MVP 不提供自助退款。经人工审核的全额退款以微信退款最终成功为准；成功后立即取消对应新增期限，
  若不能无歧义地回退续费期限则停止自动处理并进入人工恢复。退款申请被受理不等于退款成功。

### 2.3 购买身份与 Tenant 绑定顺序

固定顺序如下：

1. 企业微信第三方**管理员授权**产生可信组织证明。
2. 一次性 claim 原子创建 `provisioning` Tenant 和唯一首位 owner。
3. 已认证 owner 的服务端 session 创建购买意向和订单；`tenant_id` 只取 session，不接收客户端传值。
4. 订单保存不可变的 plan、金额、币种和 Tenant 快照，再向支付渠道下单。
5. 可信回调或验签成功的主动查单结果驱动支付状态；付款人的 OpenID 只能作为支付证据，
   不能替代企业管理员身份或变更订单 Tenant。
6. 统一订阅激活服务写入 Subscription、Entitlement 和 Audit。
7. 付款后 Tenant 仍为 `provisioning`；只有企业微信配置、连通性和运行时检查全部通过才变为 `active`。

普通员工扫码、用户输入的 CorpID/企业名、邮箱相似度或支付付款人身份都不是 owner 证明。

### 2.4 微信支付产品与安全契约

首个渠道选择普通商户 **微信支付 API v3 Native 支付**。官方将其定义为 PC 网页浏览器收款能力；
服务端调用 `POST /v3/pay/transactions/native` 获得 `code_url`，浏览器只接收由该 URL 生成的二维码，
不接触商户私钥、API v3 key 或原始支付签名材料。

订单约束：

- `out_trade_no` 由服务端生成，在同一商户号下唯一；不得复用已超时订单号。
- `amount.total = 9900`，`amount.currency = CNY`；回调和查单必须逐项核对。
- `appid` 必须与 `mchid` 有绑定关系；两者都必须与解密后的通知一致。
- `notify_url` 为 HTTPS 固定部署地址，不接受请求参数覆盖。
- 二维码默认 15 分钟过期；超时后先关单，再以新商户订单号创建新订单。
- `attach` 不是授权边界；即使使用也只能放不可逆、非敏感的内部关联值。

安全约束：

- 请求使用商户 API 证书私钥签名；微信响应和回调必须验签。
- 新接入采用微信支付当前推荐的**微信支付公钥模式**；根据 `Wechatpay-Serial` 精确匹配配置的
  `PUB_KEY_ID_*`，未知 ID 一律失败关闭。
- 回调验签必须使用原始请求体以及 `Wechatpay-Timestamp`、`Wechatpay-Nonce`、
  `Wechatpay-Signature`、`Wechatpay-Serial`；验签前不解密、不更新订单。
- 验签后使用 API v3 key 对 `resource` 执行 AEAD_AES_256_GCM 解密。
- 只接受 `TRANSACTION.SUCCESS`，并核对 appid、mchid、out_trade_no、transaction_id、
  trade_state、amount.total 和 amount.currency；重复或乱序通知必须幂等。
- 回调时间戳必须在受控容差内，通知 ID/交易号/订单状态形成持久防重放边界。
- 回调在 5 秒内完成验证和持久化应答；合法重复通知持续返回 200/204。业务激活可在持久化后重试。
- 没有回调时必须主动查单；只有响应验签成功且 `trade_state=SUCCESS` 才能补记支付成功。
- T+1 账单对账和异常恢复属于 RND-390 的生产启用门禁。

用户的零钱、借记卡或信用卡是否可选，由微信支付确认页、用户账户、银行、商户权限和风控共同决定。
产品文案只能写“支持微信支付页面显示的零钱/银行卡等支付方式”，不得保证某张卡一定可用，
也不得在商户端收集银行卡信息。

### 2.5 生产环境变量契约

实现不得写入真实值。生产启用时由 owner 在受控密钥环境中配置：

| 变量 | 用途 | 敏感 |
|---|---|---|
| `WECHAT_PAY_ENABLED` | 显式启用支付渠道 | 否 |
| `WECHAT_PAY_APP_ID` | 已认证且与商户号绑定的应用 ID | 否，但日志仍最小化 |
| `WECHAT_PAY_MCH_ID` | 普通商户号 | 否，但日志仍最小化 |
| `WECHAT_PAY_MERCHANT_SERIAL_NO` | 商户 API 证书序列号 | 否 |
| `WECHAT_PAY_MERCHANT_PRIVATE_KEY` | 商户 API 证书私钥 PEM | **是** |
| `WECHAT_PAY_API_V3_KEY` | 回调资源解密 key | **是** |
| `WECHAT_PAY_PUBLIC_KEY_ID` | 微信支付公钥 ID | 否 |
| `WECHAT_PAY_PUBLIC_KEY` | 微信支付公钥 PEM | 否，仍按受控配置管理 |
| `WECHAT_PAY_NOTIFY_URL` | 固定 HTTPS 支付通知地址 | 否 |

启用时必须同时完整提供全部变量；任一缺失、PEM 无效、API v3 key 长度无效、notify URL 非 HTTPS，
应用启动/支付 readiness 必须失败关闭。配置值不得出现在前端、异常正文、日志、测试 fixture、
GitHub Issue、聊天或 git diff 中。

### 2.6 支付渠道抽象与支付宝边界

> **更新（RND-416，2026-08-23）**：本节原先「仅注册 `wechat_pay`」的阶段性限制已由
> [ADR-0006](0006-alipay-pc-page-pay.md) 取代。支付宝电脑网站支付现通过同一 provider-neutral
> 边界接入；本节其余的领域隔离原则仍然有效。

领域层只依赖 provider-neutral contract：

- `create_payment(order) -> checkout_artifact`
- `verify_and_parse_notification(headers, raw_body) -> trusted_payment_event`
- `query_payment(order) -> trusted_payment_state`
- `close_payment(order)`

Order 保存稳定 provider code、provider transaction reference 和原始状态的最小必要摘要；
Subscription 激活服务不 import 微信 client。`checkout_artifact` 当前类型为 QR code，未来可增加跳转链接。

RND-416 之前的阶段只注册 `wechat_pay` provider。支付宝当前实现范围、RSA2 验签、PC 收银台
跳转与环境变量契约以 [ADR-0006](0006-alipay-pc-page-pay.md) 为准；未定义的支付宝产品模式和退款
仍不应被宣称为已支持。

### 2.7 “全自助”与首个真实客户 E2E

“全自助”定义为**平台运营人员零手工改库、零手工开通、零手工复制凭据**；客户企业管理员按
腾讯官方要求登录管理后台、确认授权、开通会话存档接口、配置密钥/可信 IP/回调等步骤仍然允许。

首个真实客户的可判定步骤：

1. 使用非生产测试企业先完成第三方授权、owner、支付和配置全链路；生产入口此前保持关闭。
2. 生产商户完成 Native 产品权限、API v3、公钥模式、证书和 HTTPS notify URL 配置。
3. 新客户以管理员授权创建 provisioning Tenant，不由平台人员代建。
4. owner 看到 99 元/年、5 GiB、不限座席和“腾讯费用另计”，确认后创建 9900 分订单。
5. 网页显示二维码；客户在微信内选择其账户实际可用的零钱/银行卡并支付。
6. 回调或主动查单经完整验证后只激活一次，页面显示订单与年度到期日。
7. 客户自行完成企业微信配置；自动检查通过后 Tenant 才 active，并成功归档首条非敏感测试消息。
8. 重复回调、页面刷新、重新登录、支付超时和激活重试均不重复收费或重复延长期限。
9. T+1 对账无“微信成功、商户未知”或“商户成功、微信未知”；异常有可追踪处置记录。

任何真实生产付款、退款或商户后台配置都需要独立受控验收；本地 fake provider 的 PASS 不能替代它。

## 3. Consequences

- RND-376 可以只实现一个年度 Plan 和权威 entitlement，不等待复杂 price book。
- RND-384 负责幂等激活/续费；RND-380 只负责支付订单、渠道和网页状态。
- 一次性 Native 支付满足扫码购买，但不等于自动续费；若未来需要免密续费，必须另立产品、合规和授权决策。
- 支付回调不是唯一真相入口；主动查单和 T+1 对账是生产完整性的一部分。
- 采用公钥模式减少平台证书轮换风险，但真实商户必须先在微信支付商户平台取得匹配的公钥和 ID。

## 4. Alternatives Considered

- **JSAPI 支付**：适合微信内网页，不符合当前 PC 管理后台展示二维码的主路径。
- **付款码支付**：需要商户扫描用户付款码，方向与“客户扫网页二维码”相反。
- **先信任前端支付结果**：无法证明资金事实，拒绝。
- **微信回调直接改 Subscription**：会把渠道逻辑和套餐规则耦合，未来支付宝会复制业务规则，拒绝。
- **在 RND-380 阶段同时实现支付宝**：当时会扩大上线面且无真实需求证据，故拒绝；RND-416 已在独立范围、测试与配置门禁下重新决策，见 ADR-0006。
- **自动续费/代扣**：Native 单次扫码没有该授权语义，拒绝冒充。

## 5. References

- [ADR-0003：已公开定价与产品策略](0003-product-strategy-hosted-only.md)
- [ADR-0005：SaaS 收费生命周期、退款与服务门禁](0005-saas-billing-lifecycle-refunds-and-service-gates.md)
- [企业微信第三方企业授权运维说明](../operations/wecom-third-party-authorization.md)
- [微信支付：Native 支付产品介绍](https://pay.weixin.qq.com/doc/v3/merchant/4012791874)
- [微信支付：支付确认页可选择零钱或银行卡](https://pay.weixin.qq.com/doc/v3/merchant/4012062524)
- [微信支付：Native 下单](https://pay.weixin.qq.com/doc/v3/merchant/4012791877)
- [微信支付：Native 调起支付](https://pay.weixin.qq.com/doc/v3/merchant/4012791878)
- [微信支付：Native 支付成功回调通知](https://pay.weixin.qq.com/doc/v3/merchant/4012791882)
- [微信支付：签名与验签](https://pay.weixin.qq.com/doc/v3/merchant/4012365342)
- [微信支付：从平台证书切换至微信支付公钥](https://pay.weixin.qq.com/doc/v3/merchant/4012154180)
- [微信支付：支付回调和查单实现指引](https://pay.weixin.qq.com/doc/v3/merchant/4012075249)
- [微信支付：关闭 Native 订单](https://pay.weixin.qq.com/doc/v3/merchant/4012791881)
- [微信支付：Native 退款申请](https://pay.weixin.qq.com/doc/v3/merchant/4012791883)

---

_Last updated: 2026-08-16_
