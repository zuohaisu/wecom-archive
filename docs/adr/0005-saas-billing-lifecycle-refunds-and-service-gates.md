# ADR-0005: SaaS 收费生命周期、退款与服务门禁

**状态**：已决策（2026-08-16）
**日期**：2026-08-16
**作者**：Haisu / Codex
**关联任务**：RND-398、RND-400、RND-399
**接续**：[ADR-0004](0004-annual-plan-wechat-pay-gates.md) 的订阅生命周期、退款和服务门禁部分

---

## 变更记录

| 版本 | 日期 | 变更内容 |
|---|---|---|
| v1 | 2026-08-16 | 冻结资金、订阅和 Tenant 服务三套状态机，以及续费、宽限期、冻结、暂停、退款、通知和运营规则 |
| v2 | 2026-08-16 | 记录 RND-400 已实现仓库内生命周期模型；线上调度与全入口门禁仍待后续票完成 |
| v3 | 2026-08-16 | 记录 RND-399 已实现 provider-neutral 退款事实、term grant 与保守期限回退；微信传输仍待 RND-403 |

---

## 1. Context

“支付成功”只证明一笔资金事实，不能单独回答以下问题：

- 客户购买的服务期从何时开始、何时结束；
- 到期后是否继续接收会话存档；
- 风险或违规 Tenant 是否应立即停用；
- 退款已受理、退款已到账和权益已回退是否为同一件事；
- 运营台看到的收入是否来自支付渠道还是手工账本。

把这些问题压进一个 `status` 会产生危险的隐式转换。例如，修改 Tenant 状态不能证明微信已经退款，
微信退款成功也不能自动解除安全暂停。前端页面、支付回调和运营操作都不得各自实现一套订阅规则。

因此，本 ADR 在 ADR-0004 的套餐和支付安全契约之上，冻结三套互相引用但不互相冒充的权威状态机。

### 1.1 当前实现与生效边界

本 ADR 是分票实现和上线的目标规则，不表示代码存在即已部署或线上生效：

- RND-400 已在仓库中实现 `grace`、`frozen`、独立 `suspended`、续费投影和可逆迁移；
- 生命周期定时调度、通知和所有路由/Worker 门禁仍分别由 RND-401、RND-402 交付；
- RND-399 已在仓库中实现退款事实、term grant 和期限回退；超管“手工退款”账本仍与 provider 事实严格隔离；
- 当前微信支付 provider 尚无退款申请、查询和通知能力；
- 生产支付仍必须保持关闭，直到 RND-390 的全部门禁通过并获得 Haisu 明确 Go。

只有相应实现票完成、required CI 通过、合并并部署后，某条规则才能被称为线上能力。

## 2. Decision

### 2.1 三套权威状态机

| 状态机 | 回答的问题 | 权威写入者 | 不得替代 |
|---|---|---|---|
| Payment / Refund funds | 渠道是否确认收款或退款 | 验签后的 provider callback、主动查询和对账服务 | Subscription、Tenant service、手工账本 |
| Subscription | 当前是否存在有效服务期及其起止时间 | provider-neutral 订阅领域服务、系统生命周期任务 | 支付渠道原始状态、Tenant 风险暂停 |
| Tenant service | 该 Tenant 当前是否可运行归档和访问产品 | 配置/连通性服务、生命周期任务、受权平台运营员 | 支付或退款是否真实发生 |

浏览器只能发起意图和展示服务端投影。浏览器字段、HTTP 200、二维码展示、客户端倒计时和运营备注
都不是上述状态机的权威输入。

### 2.2 Payment / Refund funds 状态

#### Payment

现有支付订单的业务流程保持：

```text
creating -> pending -> paid_activation_pending -> succeeded
    |          |                  |
    v          +-> closed         +-> 有界幂等重试
  failed       +-> failed
```

- `creating` / `pending` 不是收入，也不授予权益。
- 只有验签并核对商户、订单、金额、币种后的 `SUCCESS` 可以形成可信收款事实。
- `paid_activation_pending` 表示资金成功但订阅事务尚未落地；它是恢复桥接状态，不允许重复收费或重复加期。
- `succeeded` 表示可信收款已由同一幂等激活事务应用到 Subscription。
- 已成功订单不会通过改回 `closed` 或 `failed` 来表示退款；退款有独立记录。

#### Refund

MVP 只允许有平台权限的 superadmin 对一笔已成功支付发起**全额退款**。不开放客户自助退款，
不支持部分退款，也不把手工账本的 `refund` 行当作 provider 退款。

Provider 资金状态至少包含：

```text
created -> processing -> succeeded
    |          |           |
    +-> closed +-> closed  +-> subscription reversal
               +-> abnormal       pending/applied/manual_recovery
```

- `created`：已保存经权限校验的全额退款意图、原因、操作者和幂等键，但尚未形成渠道结果。
- `processing`：微信已受理申请；这不等于退款成功，不改变 Subscription 或 Tenant service。
- `succeeded`：退款通知或主动查询经完整验证后确认 `SUCCESS`，是唯一允许启动期限回退的资金状态。
- `closed` / `abnormal`：保留渠道事实并进入可追踪处置；不得猜测为成功。
- Provider 已 `succeeded` 后，订阅回退另有 `pending` / `applied` / `manual_recovery` 结果。
  这样即使领域事务失败，运营台仍如实显示钱已退，不能把它降级成“退款失败”。

每笔 Refund 必须保存对原 Payment、原订阅激活和对应期限授予的稳定引用；
`out_refund_no` 在商户范围唯一并承担 provider 幂等边界。重复申请、通知、查询和重试必须返回同一结果。

### 2.3 Subscription 状态和时间规则

Subscription 的目标状态为：

```text
trial -> active -> grace -> expired
  |         |        |         |
  +---------+--------+---------+-> active  (可信新付款)
            |
            +-> canceled       (可信退款成功且回退后不再有有效期限)
```

状态含义：

| 状态 | 含义 | 权益结论 |
|---|---|---|
| `trial` | 受信试用期，处于 `[starts_at, ends_at)` | 按试用套餐授予 |
| `active` | 已付款服务期，处于 `[starts_at, ends_at)` | 按已购套餐授予 |
| `grace` | 已到 `ends_at`，但尚未到 `grace_ends_at` | 继续原套餐能力并强提醒续费 |
| `expired` | 已到 `grace_ends_at`，且没有新的可信付款 | 不授予业务能力 |
| `canceled` | 可信退款回退后不再存在有效服务期 | 不授予业务能力；保留历史 |

时间统一以 UTC 保存，边界为半开区间：

- 首次付费：`starts_at = trusted_payment_succeeded_at`；按日历月增加 12 个月得到 `ends_at`。
- 有效期内续费：保持 `starts_at`，从当前 `ends_at` 按日历月增加 12 个月。
- `grace` 内续费：从原 `ends_at` 增加 12 个月，使客户不会因在宽限期付款而丢失剩余宽限天数以外的已购期限；
  新的付费期立即恢复为 `active`。
- 已 `expired` 或 `canceled` 后续费：从新的可信支付成功时间重新开始 12 个月。
- 闰日仍遵循 ADR-0004：2 月 29 日在非闰年的对应日为 2 月 28 日。
- `grace_ends_at = ends_at + 7 * 24 hours`，上界排他；系统时间达到该值即转 `expired`。

当前版本没有代扣授权。所有续费都是 Owner 创建新 Native 订单并再次扫码，不得称为“自动续费”。

Owner 的“到期不续费”只保存 `cancel_at_period_end` 意图：

- 不提前修改 `ends_at`，不关闭已支付权益，不发起退款；
- 到期时仍按 `grace -> expired` 规则处理；
- Owner 可撤销该意图；新的可信续费成功也会清除该意图；
- 因为没有自动扣款，该意图不能被宣传为“取消扣款授权”。

### 2.4 退款后的期限回退

每次可信付款必须对应一条不可变的期限授予记录，至少包含原 Payment、前后 `ends_at`、授予月数、
订阅 revision 和发生时间。退款不得通过“当前日期减 12 个月”猜测原期限。

只有 Refund provider 状态确认 `succeeded` 后，领域服务才可以在一个事务中：

1. 锁定 Refund、Payment、期限授予和当前 Subscription；
2. 验证该 Payment 尚未被退款且授权链仍可无歧义回退；
3. 回退**恰好该 Payment 授予的期限**，重新计算 Subscription 状态和时间；
4. 追加 SubscriptionHistory 和 Audit；
5. 将 subscription reversal 标为 `applied`。

以下任一情况必须标记 `manual_recovery`，不得静默改期：

- 退款对应的期限授予缺失或与当前 revision 不一致；
- 该付款之后已有续费、人工加期或其他变更，MVP 无法证明回退结果唯一；
- Provider 已退款成功，但数据库事务失败或服务中断；
- 重放的退款事件与既有金额、Payment 或 provider reference 冲突。

`manual_recovery` 必须告警并显示在超管异常队列。它不改变“资金已经退回”的事实；
在运营员完成带原因和 Audit 的恢复操作前，不自动猜测 Subscription 或 Tenant service。

### 2.5 Tenant service 状态

Tenant service 的目标状态为：

```text
provisioning -> active <-> frozen
      |           |          |
      +-----------+----------+-> suspended
                                      |
                         operator resume + authoritative reprojection
                                      |
                           provisioning / active / frozen
```

| 状态 | 进入条件 | 离开条件 |
|---|---|---|
| `provisioning` | 已可信创建 Tenant，但企业微信配置/连通性尚未全部通过 | 配置检查通过且 Subscription 有效或处于 grace 时转 `active`；期限失效可转 `frozen` |
| `active` | 配置可运行，Subscription 为 `trial`、`active` 或 `grace` | grace 结束转 `frozen`；平台运营员可立即转 `suspended` |
| `frozen` | Subscription 为 `expired` / `canceled`，且无独立暂停 | 可信付款恢复 Subscription 后转 `active`；若配置仍未完成则转 `provisioning`；运营员可转 `suspended` |
| `suspended` | superadmin 因安全、违规、风控或受控运营原因暂停 | 只能由有权限的 superadmin 恢复；恢复时依据当前 Subscription 和配置重新投影为其他三种状态 |

`suspended` 优先级最高且独立于付款。暂停期间可以继续接收支付/退款通知、主动查单和生命周期对账，
但任何付款都不得自动解除暂停。暂停和恢复都必须有权限、明确原因、幂等键和不可变 Audit。

### 2.6 服务访问矩阵

Subscription 的 `trial` / `active` / `grace` 在 Tenant service 层均投影为可运行的 `active`；
下表以 Tenant service 状态为最终业务门禁。具体路由仍需同时满足 session、角色、Tenant 隔离和 entitlement。

| 能力 | `provisioning` | `active`（含 grace） | `frozen` | `suspended` |
|---|---|---|---|---|
| Owner 登录和配置向导 | 允许 | 允许 | 仅账单/续费与必要账户入口 | 拒绝 |
| Owner 账单、创建续费订单、查询支付 | 允许 | 允许 | 允许 | 拒绝；仅平台侧可处理 |
| 普通成员登录和历史会话访问 | 拒绝 | 允许 | 拒绝 | 拒绝 |
| 新会话同步、解密和归档写入 | 配置完成前拒绝 | 允许；grace 内继续 | 拒绝 | 拒绝 |
| 媒体下载、缩略图、转码等 Worker | 拒绝 | 允许 | 拒绝 | 拒绝 |
| 新建导出及后台导出处理 | 拒绝 | 允许 | 拒绝 | 拒绝 |
| 已归档历史数据保留 | 保留 | 保留 | 保留 | 保留 |
| 支付/退款 callback、主动查询、对账 | 允许 | 允许 | 允许 | 允许，但不得自动解除暂停 |
| 系统生命周期任务 | 允许 | 允许 | 允许 | 允许，只更新权威投影而不解除暂停 |
| superadmin 运营台和受控操作 | 独立平台鉴权 | 独立平台鉴权 | 独立平台鉴权 | 独立平台鉴权 |

“保留”不等于客户可访问。`frozen` / `suspended` 不删除归档、Payment、Refund、SubscriptionHistory、
Audit 或配置数据。任何自动删除、匿名化或法定保留策略必须由独立的数据生命周期决策定义。

### 2.7 允许的状态转换操作者

| 动作 | 允许发起者 | 必要条件 |
|---|---|---|
| 创建/关闭未支付订单 | Owner 经认证 session；系统可关闭超时订单 | Tenant 取自 session；服务端价格；幂等；已付款不可关闭 |
| 确认收款 | 验签后的 callback、主动查询或对账服务 | 商户、订单、金额、币种和防重放全部通过 |
| 激活/续费 Subscription | provider-neutral 订阅服务 | 只消费可信收款事实；事务、历史和 Audit 原子写入 |
| `active/trial -> grace -> expired` | 系统生命周期任务 | 服务端 UTC；可重放；行锁/版本校验；不依赖浏览器在线 |
| 发起全额退款 | 有权限的 superadmin | 已成功 Payment、未全退、原因、确认、幂等键和 Audit |
| 确认退款 | 验签后的退款通知、主动查询或对账服务 | 只有 provider `SUCCESS` 才进入期限回退 |
| 暂停/恢复 Tenant | 有权限的 superadmin | 原因、确认、幂等键和 Audit；恢复时重新投影 |
| 记录到期不续费 | Owner | 只记录意图；不触发资金或提前停服 |

运营员不能手工把 Payment/Refund 标为 provider 成功。需要恢复时只能触发受控查询、重试或记录
manual recovery 处置，不能伪造渠道事实。

### 2.8 通知和异常可见性

通知至少覆盖：

- `ends_at` 前 30、7、1 天；
- 进入 `grace`；
- `grace_ends_at` 临近和进入 `frozen`；
- 支付成功但激活 pending/failed；
- 退款 processing 超时、abnormal、provider succeeded 但 reversal pending/manual recovery；
- Tenant 被 suspended 或恢复。

每个通知使用稳定事件键和受众键幂等发送，记录 queued/sent/failed 等最小状态；
不得把支付凭据、消息正文或客户敏感信息放进通知或日志。通知失败不回滚已确认的资金或订阅事实，
但必须进入可重试异常队列。

### 2.9 超管账务与 Tenant 商业状态

运营台必须把权威 provider 事实和手工账本分开显示：

| 指标/字段 | 口径 |
|---|---|
| `provider_gross_receipts` | 已验证成功的 provider Payment 金额之和 |
| `provider_successful_refunds` | 已验证 `SUCCESS` 的 provider Refund 金额之和 |
| `provider_net_revenue` | `provider_gross_receipts - provider_successful_refunds` |
| `manual_receipts` / `manual_refunds` | 运营员记录的渠道外账本，仅作运营记录 |
| Subscription | plan、状态、`starts_at`、`ends_at`、`grace_ends_at`、cancel intent、revision |
| Tenant service | provisioning/active/frozen/suspended、原因、变更时间 |
| Exceptions | 激活 pending、对账差异、退款 abnormal、reversal manual recovery、通知失败 |

总览可展示 provider 与 manual 的分组和合计，但不得把 manual entry 伪装成微信交易，
也不得让 manual receipt/refund 自动激活、续费、退款或改变 Tenant service。

受控操作包括暂停/恢复、发起全额退款、重试查询/领域应用和人工恢复。每次操作必须校验平台角色，
要求明确原因与确认，使用幂等键，并记录操作者、Tenant、目标对象、前后状态和 Audit event id。

### 2.10 实现与验收顺序

```text
RND-398 本 ADR
   ├─> RND-400 Subscription / Tenant service 权威状态机
   └─> RND-399 Refund 权威模型与期限回退
          └─> RND-403 微信退款申请、查询、通知和恢复

RND-400 + RND-399 ─> RND-401 生命周期与异常通知
RND-400 + RND-387 ─> RND-402 路由与 Worker 门禁
RND-400 + RND-403 + RND-395 ─> RND-404 Owner 体验
RND-400 + RND-399 + RND-403 ─> RND-405 超管账务与受控操作

RND-389 + RND-401..405 ─> RND-406 非生产完整 E2E
RND-380 + RND-389 + RND-406 ─> RND-390 生产受控启用
RND-390 + 自助接入/客户入口前置项 ─> RND-391 真实客户 E2E
```

RND-406 必须证明购买、有效期续费、过期续费、退款受理与成功的差别、精确期限回退、
grace、frozen、恢复、suspended 不被付款解除、两 Tenant 隔离和异常重放。只有非生产证据通过，
RND-390 才能请求生产 Go；只有生产启用和真实客户全链路通过，才能宣称线上端到端完成。

## 3. Consequences

- 状态更明确，但需要迁移现有 `past_due`、Tenant 生命周期和所有业务入口的门禁判断。
- grace 内继续归档会产生有限成本，但避免会话存档出现无法补回的数据空洞。
- frozen 保留历史数据而拒绝业务访问，恢复可逆；它不是删除策略。
- Provider 退款成功与订阅回退分阶段持久化，异常时可能短暂存在“钱已退、权益待恢复”的显式队列，
  优于静默篡改或丢失资金事实。
- MVP 的保守回退会把存在后续续费的歧义情况交给人工恢复；部分退款、自动代扣和自动抵扣留待独立决策。
- 支付入口、回调、生命周期任务、所有 Worker 和运营台都必须消费同一服务端权威投影，不能各自推断。

## 4. Alternatives Considered

- **用一个 Tenant `status` 表示付款、到期、退款和风控**：无法保留资金事实，也会让续费错误解除安全暂停，拒绝。
- **退款申请受理后立即停服**：微信仍可能 processing/abnormal/closed，会造成无资金依据的权益回退，拒绝。
- **客户自助退款或部分退款**：扩大合规、争议处理和期限计算面，MVP 拒绝。
- **到期立即停止归档**：会产生不可恢复的数据缺口；采用 7 天 grace。
- **grace 只允许读、不继续接收会话**：仍会丢失腾讯侧超过可拉取窗口的数据，拒绝。
- **付款自动解除 suspended**：把商业动作置于安全控制之上，拒绝。
- **把手工账本汇总成微信收入**：失去 provider 可核验性，拒绝。

## 5. References

- [ADR-0003：托管版产品策略](0003-product-strategy-hosted-only.md)
- [ADR-0004：年度套餐、微信支付与自助接入门禁](0004-annual-plan-wechat-pay-gates.md)
- [数据模型](../DATA_MODEL.md)
- [系统架构](../ARCHITECTURE.md)
- [微信支付：Native 退款申请](https://pay.weixin.qq.com/doc/v3/merchant/4012791883)

---

_Last updated: 2026-08-16_
