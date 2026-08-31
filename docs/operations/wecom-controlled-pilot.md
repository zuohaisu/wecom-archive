# 企业微信受控测试企业试点运维手册（RND-421）

> **状态：** 已批准文档（PR 合并后生效）。本手册是「受控测试企业试点」的
> **唯一权威 runbook**，覆盖试点定位、名额治理、客户准入、知情说明、台账、
> onboarding / smoke / offboarding、数据导出、正式应用迁移、回滚与 Go/No-Go 门禁。
>
> **执行边界：** 本手册**只定义流程与判定标准，不构成执行授权**。任何真实客户
> 试点动作（安装/移除测试企业、联系客户、收费、退款、修改生产配置）都必须
> 单独获得 Haisu 的明确批准，并满足第 16 节 Go/No-Go 门禁。
>
> **证据等级约定（贯穿全文）：**
> - **Confirmed** — 官方文档/官方书面答复或仓库内可执行代码/已合并证据。
> - **Inferred** — 多源交叉、官方未逐字确认的合理推断。
> - **Unknown** — 官方未明确或仓库无证据，**不得推断，默认 fail-closed**。

---

## 1. Purpose

在正式应用完成其余发布门禁（企业微信账号认证、应用上线审核、定价与线下试运营
合同等）之前，用**受控、有限、知情、可停止、可迁移、可回滚**的方式验证真实外部
客户。本手册确保：

1. 真实外部客户试点不会被包装成正式公开 SaaS 发布；
2. 测试企业名额（官方上限与 90 天自动删除约束）被持续治理，不超发、不空占；
3. 每个试点客户都经过可判定的准入门禁并签署知情确认；
4. 单客户从进入到退出的每一步都有 runbook、失败回滚与审计记录；
5. 正式应用路径启用后，试点客户可平滑迁移或重新授权，且迁移失败可回滚；
6. 最终 Go/No-Go 明确区分文档、平台、技术与外部试点四个层面，不因文档完成
   而自动批准真实客户试点。

## 2. Scope / Non-scope

### In scope

- 试点定位、名额治理与试点台账（ledger）规则。
- 客户准入门禁与客户知情说明模板。
- 单客户 onboarding、smoke、incident、offboarding、数据导出 runbook。
- 正式应用迁移预案与回滚预案。
- Go/No-Go 判定框架。

### Non-scope（未获授权不得执行）

- 安装、移除或替换测试企业（需 Haisu 逐次批准）。
- 联系客户、发送客户通知、收取或退还试点费用。
- 修改企业微信服务商后台、生产密钥、`.env`、systemd、nginx、数据库、回调配置
  或生产 tenant 状态。
- 签署协议、提交正式应用发布。
- 把测试应用描述为正式发布、无限容量或长期稳定承诺。
- 重新研究、要求或授权缴纳已由 RND-420/#95 排除的 3 万元产品销售合作保证金。

## 3. Ground truth

以下事实来自 `tasks/archive/RND-420-research-report.md`（#95 / RND-420，已合并
PR #96）与仓库当前代码。证据等级见 RND-420 报告附录 A。

### 3.1 保证金（Confirmed）

- 3 万元「服务商保证金」仅面向**产品销售合作**（代销企业微信官方会话内容存档
  产品、结算返利）模式。
- 官方客服书面确认（2026-08-27）：**「下单数据专区不需要」「应用服务商不需要缴纳」**。
- 本项目路径（上架自研 SaaS 应用 + 数据与智能专区提供会话存档服务）**不触发该保证金**。
- **因此：不允许以「没有 3 万元保证金预算」作为 #81 或任何试点/上架决策的 No-Go 理由。**

### 3.2 测试企业名额（Confirmed / Inferred / Unknown）

| 事项 | 事实 | 等级 |
|---|---|---|
| 名额上限 | 每个测试应用**同时**最多 **10 个**测试企业 | Confirmed |
| 占用/释放 | 名额按「同时存在」计；移除测试企业后名额释放，可再配置其他企业 | Confirmed（删除行为）/ Inferred（名额恢复） |
| 自动删除 | 测试授权安装 **90 天自动删除**（官方答复；另有「移除才删除」口径并存，**保守按 90 天治理**） | Confirmed（保守口径） |
| 接口许可 | 测试企业购买接口许可为 **0 元免支付单** | Confirmed |
| 同装限制 | 同一企业微信账号**不能同时**安装测试应用和正式应用 | Confirmed |
| 配置快照 | 测试授权使用**安装时配置快照**；后续配置修改不同步，更新需重新授权 | Confirmed |
| 收费 | 测试企业是否允许收费：**官方未明确 → Unknown，不得收费** | Unknown |
| 测试企业主体 | 官方表述「需服务商自行注册」，未明确禁止真实外部主体 | Inferred |
| 会话存档 | 测试企业开通数据与智能专区/会话存档**需完成企业微信认证**；专区 30 天免费试用后需购买 | Confirmed |

### 3.3 迁移到正式应用（Confirmed / Inferred / Unknown）

| 事项 | 事实 | 等级 |
|---|---|---|
| 重新授权 | 必须：删测试授权 → 正式授权 → **新 permanent code** | Confirmed |
| 配置重配 | 回调/域名/IP/Token/AESKey 为应用级配置，随正式应用生效，需在服务商后台重新核对 | Inferred |
| 订单残留 | 测试期互通账号订单残留可导致正式开通报错 `existed effective test order, can't create order`，需清理/走正式下单 | Confirmed（社区案例） |
| 平台侧数据 | 测试租户的平台侧归档数据继承/迁移：**官方未规定，属平台自有设计** | Unknown / Inferred |

### 3.4 平台侧既有能力（Confirmed，仓库代码）

- Tenant 生命周期：`provisioning → active / frozen / suspended`（`Tenant.lifecycle_status`，
  `models.py`；`service_access.py` 执行门禁）。
- 激活门禁：`tenant_activation.py`，状态 `not_started / blocked / ready`；平台侧明确激活
  前租户只能访问 provisioning 受限面。
- 第三方授权：`wecom_org_authorization.py`（install → OAuth callback → proof/claim →
  `ThirdPartyOrganizationBinding` → Owner + provisioning session）。
- 订阅：`trial / active / grace / expired / canceled`（`billing_lifecycle.py`，ADR-0005）。
- 支付：微信支付 + 支付宝（`payment_orders.py`、`wechat_pay.py`、`alipay.py`；
  生产支付启用已由 #76/RND-418 验收）。
- 导出：文本/媒体导出 + 审批 + 审计（`export_service.py`、`export_approval.py`、
  `export_audit.py`）。
- 数据删除/保留：回收站 30 天 + 永久清理 + 合规保留锁（`message-deletion.md`）。
- 平台运营：受控操作层（`platform_operations.py`），幂等授权 + 审计。

### 3.5 尚未具备（Missing，本手册只设计流程不实现）

- 平台侧**无**「测试→正式应用迁移」自动化 hook（依赖企业微信后台手动删测试授权
  与重新正式授权）。
- 平台侧**无**「取消/解除企业微信授权」API；offboarding 的授权撤销依赖企业微信
  服务商后台手动操作 + 平台侧数据处置。
- **无**试点台账（ledger）存储；本手册第 8 节定义最小台账 schema，落地载体由
  后续实现票决定（当前建议运维私有表格/文档，禁止入库敏感字段）。

## 4. Pilot governance（试点定位）

### 4.1 定位声明

受控测试企业试点**只能**是：

- **invitation-only**：不接受公开注册、自助申请或市场推广入口；
- **明确测试属性**：客户书面知悉当前为测试应用，不是正式发布；
- **容量有限**：同时在线试点客户数 ≤ 官方上限（10）且由台账治理，实际可用容量
  更保守（见第 5 节）；
- **可随时暂停**：平台可基于风险、故障或合规原因随时暂停试点服务；
- **可迁移**：正式应用路径启用后按第 14 节迁移或重新授权；
- **可退出**：客户可按第 12 节退出，平台也可按第 11 节强制退出；
- **不宣传为正式公开 SaaS**：任何对外材料不得把测试应用描述为正式发布；
- **不承诺长期使用测试应用**：明确 90 天自动删除与迁移义务。

### 4.2 测试应用与正式应用的边界

| 维度 | 测试应用（试点） | 正式应用（后续） |
|---|---|---|
| 发布状态 | 未提交上线 | 已认证 + 上线审核通过 |
| 授权方式 | 服务商后台「测试企业」添加 | 正式授权安装 |
| 名额 | 官方 10 个同时上限 | 无此限制（按平台规则） |
| 收费 | **Unknown → 不得收费** | 可配置定价（需线下试运营合同等） |
| 生命周期 | 90 天自动删除约束 | 正常订阅生命周期 |
| 迁移关系 | 同一企业**不能同时**安装两者 | — |

## 5. Slot governance（名额治理）

### 5.1 名额模型

- 总额度：**官方 Confirmed 上限 = 10 个同时测试企业**（第 3.2 节）。
- **治理上限 = min(官方上限, 运营批准上限)**。默认运营批准上限 **3**，由 Haisu
  在试点启动前批准调整；超过 3 个同时在线客户必须逐次批准并记录。
- 一个「占用名额」的定义：台账中该 slot 处于 `reserved / onboarding / active /
  offboarding` 任一状态（见 5.2）。
- 一个「释放名额」的定义：台账中该 slot 进入 `released`，且企业微信服务商后台
  对应的测试企业已移除（移除后官方名额释放）。
- **占用即算名额**：从 `reserved` 起算，即使客户尚未完成授权也占用名额。

### 5.2 Slot 状态机

```text
vacant → reserved → onboarding → active → offboarding → released
             │            │          │
             └────────────┴──────────┴─→ (任何阶段可因失败/暂停) → released
```

| 状态 | 含义 | 进入条件 | 离开条件 |
|---|---|---|---|
| `vacant` | 名额空闲 | 初始化/释放后 | 批准保留 |
| `reserved` | 名额已锁定给特定客户 | 准入门禁通过 + 双人批准 | 客户放弃/超时（见 5.4） |
| `onboarding` | 正在执行第 9 节 onboarding | 发出邀请 | onboarding 全部 PASS → `active`；失败 → 回滚 → `released` |
| `active` | 客户在线使用测试应用 | smoke 测试 PASS | offboarding 启动 / 暂停 / 90 天治理 |
| `offboarding` | 正在执行第 12 节退出 | 客户退出 / 平台强制 / 迁移 | 退出完成 → `released` |
| `released` | 名额已释放 | offboarding 完成 + 后台移除确认 | — |

### 5.3 90 天自动删除治理

- 每个 `active` slot 记录**测试授权安装日期**（台账 `authorization_at`）。
- **自动治理线：授权满 80 天** → 台账标记 `age_warning`，operator 必须启动
  offboarding 或迁移动作；**满 90 天前**必须完成移除，避免官方自动删除导致
  平台侧状态与后台不一致。
- 官方自动删除**不可逆**（测试授权删除后需重新添加），因此 90 天窗口是硬约束，
  不得依赖「测试企业没有时间限制」的口径。

### 5.4 替换与冷却

- **允许替换**：`released` 的名额可被新客户占用（官方确认移除即释放）。
- **替换前核查**：同一客户若此前安装过测试应用（含自动删除后），需确认服务商
  后台无残留测试授权记录，避免重复占用/冲突。
- **冷却期**：官方未规定冷却期 → 默认不设硬冷却，但同一客户**被移除后 7 天内
  不得重新占用新名额**（运营冷却，防止 90 天窗口滚动规避）；该冷却期为运营
  策略，非官方规则。
- **不可逆限制**：测试授权删除后**无恢复路径**（官方未提供），涉及数据的处置
  必须在删除前完成导出（第 13 节）。

### 5.5 Unknown 处理（fail-closed）

- 测试企业**收费状态 Unknown** → 试点阶段一律**免费**，任何收费意向必须先由
  Haisu 与官方书面确认后才可能放开；未确认前按「不可收费」治理。
- 测试企业主体限制 Unknown → 每新增客户前由 Haisu 确认该客户主体可作为测试
  企业添加；未确认前不得添加。
- 90 天删除双口径 → 一律按更保守的 90 天治理。

## 6. Admission criteria（客户准入门禁）

每个候选客户必须逐项判定 **PASS / FAIL / NOT APPLICABLE（NA）**。任一项
**FAIL 或 Unknown**（即证据不足以判定 PASS）→ **不得进入试点**。不接受
「原则上可以」。

| # | 条件 | 判定 | 证据要求 |
|---|---|---|---|
| A1 | 候选客户是合法注册的企业主体，且使用企业微信组织 | PASS/FAIL | 客户提供企业名称 + 企业微信组织可验证信息（不含 CorpID） |
| A2 | 客户企业管理员愿意配合授权，且该管理员在应用管理员列表中有管理权限 | PASS/FAIL | 客户书面确认 + 授权时系统只接受管理员授权模式 |
| A3 | 客户满足会话存档权限要求（如适用：需完成企业微信认证才能开通数据与智能专区） | PASS/FAIL/NA | 若客户不使用会话存档功能则 NA；否则需认证凭证（不记录敏感值） |
| A4 | 客户理解这是**测试应用**，不是正式发布 | PASS | 客户签署知情确认（第 7 节模板） |
| A5 | 客户接受测试服务**可能随时暂停** | PASS | 知情确认中勾选 |
| A6 | 客户接受未来**可能需要重新授权**（迁移到正式应用时） | PASS | 知情确认中勾选 |
| A7 | 客户接受**正式应用迁移流程**（含数据导出/继承安排） | PASS | 知情确认中勾选 |
| A8 | 客户理解**支持范围**（测试期支持边界，见第 11 节） | PASS | 知情确认中勾选 |
| A9 | 客户理解**退款/退出/数据导出边界**（试点免费；退出时按第 13 节导出） | PASS | 知情确认中勾选 |
| A10 | 试点容量检查：当前 `active+onboarding+reserved` < 治理上限 | PASS/FAIL | 台账快照 |

**准入记录**：第 8 节台账记录每一项的 PASS/FAIL/NA 与判定时间、判定人；知情确认
文件本身**不进入仓库**，仅记录其安全引用（见第 18 节）。

## 7. Customer disclosure（客户知情说明模板）

以下模板是内部可执行基线。发给客户前由 Haisu 批准，且**不得**添加任何未经
平台确认的能力描述。模板中的「我方」指 Crowntime。

---

### 受邀测试服务说明（模板）

尊敬的客户：

感谢您受邀参与我方「会话存档与协作分析」服务的**受控测试**。请仔细阅读以下
内容，确认理解后签署。

**1. 测试属性**

- 您当前使用的是**测试应用**，不是公开正式发布版本；我方不将测试应用宣传为
  正式产品。
- 测试期**免费**，不产生订阅费用。

**2. 已知限制（截至本说明签署日）**

- 测试应用可同时服务的企业数量有限（官方上限 10 个），我方将控制同时在线
  客户数。
- 测试授权存在 **90 天自动删除** 的平台约束；我方会在到期前联系您安排退出
  或迁移。
- 测试应用使用安装时的配置快照；配置调整可能需要**重新授权**。
- 同一企业微信账号**不能同时**安装测试应用与正式应用；迁移到正式应用时需
  重新授权。
- 会话存档能力依赖企业微信「数据与智能专区」，该专区有官方试用期与续费规则；
  我方将按官方规则执行。

**3. 当前未知限制**

- 企业微信平台对测试企业的部分规则（如收费、主体要求）官方尚未明确；我方
  按最保守方式处理，**在官方确认前不会向您收费**。
- 其他未列明限制可能存在，我方将及时告知。

**4. 数据处理**

- 您的会话存档数据将按我方隐私与安全策略处理（详见我方 SLA/隐私文档，若适用）。
- 测试期结束后，您可按第 5 条导出全部已有数据；我方按约定保留或删除副本。
- 我方不记录您的 CorpID、用户标识、密钥或消息内容到任何运营台账。

**5. 退出与授权撤销**

- 您可随时书面要求退出；我方将在 **5 个工作日** 内停止服务、协助导出数据并
  撤销授权。
- 我方也可基于风险/故障/合规原因随时暂停或终止测试服务，并提前（除紧急情况外）
  通知您。

**6. 迁移到正式应用**

- 正式应用上线后，您可能需要：**撤销测试授权 → 重新正式授权 → 重新配置**；
  我方将提供迁移指引并尽力保障数据连续性。
- 测试期开通的某些平台订单/许可在正式开通时可能需清理（平台已知案例），我方
  将协助处理。

**7. 退款与权益**

- 测试期免费，无已付款权益；正式应用上线后的订阅与退款规则届时另行约定，
  不受本说明约束。

---

## 8. Pilot ledger（试点台账 schema）

台账是试点运营的唯一事实源（人数、名额、状态、审计）。允许/禁止字段如下。

### 8.1 允许记录字段

| 字段 | 说明 |
|---|---|
| `pilot_id` | 内部试点编号（如 `P-001`），唯一 |
| `customer_alias` | 客户别名/内部引用（如「客户A」「测试企业-华东」），**不得为可公开识别主体名**（如需记录主体名，仅存于 Haisu 私密处，台账用安全引用） |
| `invitation_status` | `not_invited / invited / accepted / declined / expired` |
| `admission_state` | 第 6 节各项 PASS/FAIL/NA 汇总：`pending / passed / failed` |
| `slot_state` | 第 5.2 节：`vacant / reserved / onboarding / active / offboarding / released` |
| `activation_state` | `not_started / blocked / ready / activated`（对应 `TenantActivationCheck`） |
| `tenant_state` | 平台 tenant 状态：`provisioning / active / frozen / suspended / n/a` |
| `migration_state` | 第 14 节：`pilot_active / migration_scheduled / pilot_frozen / formal_reauthorization / formal_validation / formal_active / pilot_decommissioned` |
| `support_state` | `active / attention / escalated / closed` |
| `offboarding_state` | `not_started / requested / exporting / revoking / released / archived` |
| `authorization_at` | 测试授权安装日期（用于 90 天治理） |
| `slot_reserved_at / activated_at / offboard_requested_at / released_at` | 各阶段时间戳 |
| `operator` / `approval_ref` | 操作人与批准记录引用（谁做的、谁批准的） |
| `last_smoke_result` | 最近一次 smoke 结果摘要（PASS/FAIL + 时间） |

### 8.2 禁止记录字段（无论是否加密）

- CorpID、UserID、suite id、suite secret、permanent code、archive secret、
  私钥、access token、suite ticket、授权 code/state。
- 消息内容、会话内容、文件内容。
- 完整支付交易号、订单号、退款号、银行账号。
- 完整回调 URL、完整域名内敏感路径。
- 客户密钥、密码、双因素凭据。

> 若业务确需引用敏感对象（如某笔授权），只能使用**不可逆内部标识或安全引用**
> （如散列引用 `ref#a1b2c3` 指向 Haisu 私密记录），台账本身不得出现敏感值。

### 8.3 台账载体与审计

- 当前实现无 ledger 存储 → 建议载体：Haisu 批准的私密表格/文档（如加密笔记），
  由 operator 维护，**禁止进入 git、GitHub、聊天或截图**。
- 每次状态变更记录 `operator + approval_ref + timestamp`。
- 若后续实现 ledger 存储，须单独立票并满足敏感数据限制（本 schema 即最小化基线）。

## 9. Onboarding runbook（单客户进入）

> 本 runbook 中涉及**外部平台动作或客户动作**的步骤均为 operator procedure，
> 不得由 agent 自动执行；每步记录脱敏证据。

### 9.1 Pre-onboarding（进入前）

1. [operator] 容量检查：台账 `active+onboarding+reserved <` 治理上限 → PASS 才继续。
2. [Haisu] 客户资格初筛：第 6 节 A1–A3 判定。
3. [Haisu] 发送知情说明（第 7 节）并取得客户签署确认（安全引用入库）。
4. [operator] 环境就绪检查：非生产/试点环境配置（`wecom-third-party-authorization.md`
   第 3.2 节回滚前提）、`suite_ticket` fresh、回调可达。
5. [operator] 回滚就绪检查：确认可在失败时移除测试授权、撤销租户、保留数据副本。

### 9.2 Onboarding（进入）

1. [Haisu] 批准占用名额 → 台账 `reserved`（记录批准引用）。
2. [operator] 服务商后台添加测试企业（**外部平台动作，需批准**）。
3. [客户] 企业微信管理员完成授权（只接受管理员授权模式；成员授权失败关闭）。
4. [系统] OAuth callback → proof/claim → `ThirdPartyOrganizationBinding` →
   组织确认 → Owner 创建 → 租户进入 `provisioning`。
5. [operator] 租户身份验证：官方组织名称来自授权结果且只读确认；与客户声明主体
   一致；不与既有租户冲突（同一 CorpID 安全冲突分支，不重复建租户）。
6. [operator] 激活门禁检查：`TenantActivationCheck` 处于 `ready` 前租户停留在
   `provisioning`，受限入口全部失败关闭。
7. [operator] 平台明确激活（**受控平台操作**，需批准 + 审计）。
8. [operator] 归档能力配置：会话存档凭证、回调、连通性按
   `wecom-third-party-authorization.md` 验证。
9. [operator] 执行第 10 节 smoke 测试；全部 PASS → 台账 `active`；任一 FAIL →
   第 9.3 节回滚。

### 9.3 Onboarding 失败回滚

| 失败点 | STOP 条件 | 回滚动作 | 证据 | 升级 |
|---|---|---|---|---|
| 授权失败 | OAuth callback 未产生合法 claim | 不创建/不激活租户；清理 proof/claim（若已产生）；台账回 `vacant` | HTTP 状态 + 错误类型（脱敏） | operator → Haisu |
| 组织冲突 | 同一 CorpID 已存在租户 | 走安全冲突分支，不新建；告知客户走既有租户 | 冲突类型（脱敏） | Haisu |
| provisioning 失败 | 组织创建事务异常 | 按既有幂等规则重试；不重复建租户/Owner；确认无半成品 | 幂等检查结果 | Haisu |
| smoke FAIL | 第 10 节任一 FAIL | 停用激活；移除测试授权（需批准）；平台侧数据导出备份后处置；台账 `released` | 失败项清单 | Haisu + 对应实现票 |

## 10. Smoke test（单客户验收）

> 任何可能触碰真实生产数据或外部平台的验证动作：写成 operator procedure，
> 不实际执行。smoke 必须覆盖（每项 PASS/FAIL + 时间 + operator）：

| # | 验证项 | 通过标准 | 触碰外部平台？ |
|---|---|---|---|
| S1 | 授权成功 | 授权结果只读确认官方组织名称；binding 创建成功 | 是（只读） |
| S2 | tenant mapping | 租户 ↔ 企业微信授权绑定唯一且正确 | 否 |
| S3 | dashboard 访问 | 激活后 tenant 控制台可访问；provisioning 期间失败关闭 | 否 |
| S4 | 首条归档消息路径 | 显式 tenant worker 使用该租户凭据完成一次 sync/decrypt；`sync_states` 游标推进 | 是（只读，测试消息） |
| S5 | worker 行为 | 归档/媒体 worker 按 tenant-scoped 凭据运行，无全局凭据回退 | 否 |
| S6 | entitlement 状态 | 订阅/权益投影与测试期策略一致（免费，无收费状态） | 否 |
| S7 | subscription 状态 | `trial`（若启用）或明确 free 状态；无意外 `expired/frozen` | 否 |
| S8 | audit event | 关键动作（授权、激活、配置、归档触发）有审计记录且脱敏 | 否 |
| S9 | 无跨租户泄露 | 该租户会话/查询不返回其他租户数据；导出/搜索隔离 | 否 |

smoke 结果记入台账 `last_smoke_result`；S1/S4 涉及外部只读交互时保留脱敏时间戳
与结果类型。任一 FAIL → 第 9.3 节回滚或第 11 节 incident。

## 11. Incident handling（故障处理）

### 11.1 支持范围（测试期）

- 支持窗口与 SLA 由 Haisu 在试点启动前批准；默认仅工作时间人工支持。
- 测试期不承诺生产级 SLA；不承诺 24/7 恢复时间。

### 11.2 故障分类与处置

| 故障 | STOP 条件 | 回滚 | 证据 | 升级 |
|---|---|---|---|---|
| 授权失败（新增/续期） | 授权流程 FAIL，租户无合法 binding | 保持 `provisioning`，不激活；必要时移除测试授权 | 脱敏错误类型 | operator → Haisu |
| provisioning 失败 | 组织创建异常 | 幂等重试；不重复建租户 | 幂等结果 | Haisu |
| 归档失败 | 归档 worker 连续失败（如凭据/专区不可用） | 冻结该租户归档；保留已归档数据；诊断凭据/回调 | worker 日志摘要（脱敏） | operator → Haisu |
| entitlement 不匹配 | 订阅/权益与测试期策略不符（如意外冻结） | 按 billing_lifecycle 规则修复；不手工改库 | 生命周期执行摘要 | operator → Haisu |
| 支付/订阅不匹配 | 出现任何收费状态（试点免费） | **立即暂停该租户**，核对是否误收费；走退款流程（若已发生） | 支付/订阅状态摘要（脱敏） | Haisu（紧急） |
| 数据隔离疑虑 | 疑似跨租户泄露 | **立即暂停**（`suspended`，不依赖支付）；隔离取证；按泄露流程 | 审计记录 | Haisu + 安全负责人 |
| 企业微信 API 错误 | 平台侧错误持续 | 按官方错误码受控重试；不猜测资金/授权状态 | 错误类型（脱敏） | Haisu |
| 客户中止（abort） | 客户要求退出/撤销 | 立即转第 12 节 offboarding | 客户请求记录 | operator |

**通用原则**：任何涉及资金、授权或数据隔离的异常 → **fail-closed**，先暂停/隔离，
再取证，不猜测。运维纪律：一次只改一个变量，改完验证再继续。

## 12. Offboarding runbook（单客户退出）

1. [operator] 台账 `offboarding_state=requested`，记录客户请求/平台决策与批准。
2. **停止服务**：按需将租户 `lifecycle_status` 置 `frozen`（保留数据）或
   `suspended`（风险场景）；停用归档/同步/媒体 worker 对该租户的调度。
3. **禁用 tenant**：平台侧停用后，租户控制台与业务入口失败关闭
   （`service_access.py` 门禁）。
4. **导出**：按第 13 节执行数据导出；导出完成并交付客户后，才进入下一步。
5. **撤销授权**：企业微信服务商后台移除该测试企业（**外部平台动作，需批准**）；
   确认平台侧 binding/授权记录按数据处置策略清理或保留（**平台无自动撤销 API，
   为人工操作**）。
6. **计费处置**：试点免费 → 无退款；若出现任何已收款项（异常），走既有退款流程。
7. **保留与清理**：按数据保留策略（默认导出后按约定删除平台副本；若客户有合规
   保留需求，保留但隔离）；媒体对象按 `message-deletion.md` 清理规则执行。
8. **审计**：offboarding 全程动作记审计，含操作人、时间、批准引用。
9. **名额释放**：确认后台移除后台账 `released`，名额回到池子。

**OFF-STOP**：导出未完成前不得撤销授权/删除数据；授权撤销后测试租户的平台侧
数据不可再通过企业微信同步，但平台侧已存副本仍可导出（需在撤销前确认导出完成）。

## 13. Data export（数据导出）

- 平台已有导出能力：文本导出 + 媒体导出 + 审批 + 审计（`export_service.py`、
  `export_approval.py`、`export_audit.py`；`/api/exports/*`）。
- **offboarding 前导出流程**：
  1. 租户 owner/平台操作员发起导出（文本 + 媒体）；
  2. 导出审批（`export_approval`）通过；
  3. 导出 job 完成，交付客户（安全通道，不落 git/聊天）；
  4. 台账记录导出完成时间与 job 引用（**不记录完整交易号/内容**）。
- 导出范围边界：仅该租户数据；系统不导出其他租户数据（隔离由 S9 验证）。
- 导出后处置：默认按约定删除平台副本（`message-deletion.md` 永久清理路径）；
  合规保留（`deletion_locked`）时保留但不对外。
- 企业微信侧原始数据不受平台导出/删除影响（删除只影响本系统存档副本）。

## 14. Formal application migration（正式应用迁移预案）

### 14.1 迁移模型

官方确认：迁移必须**删测试授权 → 正式授权 → 新 permanent code**；测试授权删除
会同步删除该企业测试应用配置；正式应用使用自己的应用级配置（回调/域名/IP/
Token/AESKey）。**不存在透明迁移**——必须重新授权。

### 14.2 迁移对象清单

| 对象 | 处理 | 标记 |
|---|---|---|
| 企业授权 | 撤销测试授权 → 正式授权（新 permanent code） | reauthorization required |
| tenant identity | 平台侧租户主体不变（同一客户企业） | reusable unchanged |
| tenant mapping（binding） | 重新绑定到正式授权的新 permanent code | remap required |
| WeCom 应用身份 | 测试应用 → 正式应用（不同 suite） | remap required |
| 凭据（suite secret/token/AESKey 等） | 正式应用独立配置，**测试期凭据作废** | reauthorization required（新凭据） |
| callback 配置 | 正式应用回调/指令 URL 重新核对 | manual confirmation required |
| 归档配置 | 正式应用归档凭证/专区配置重新核对 | manual confirmation required |
| subscription | 测试期免费 → 正式订阅（新定价） | manual confirmation required |
| entitlement | 正式套餐权益按新订阅授予 | remap required |
| 已存归档数据 | 平台侧数据：**官方未规定继承** → 平台自定；本预案默认**保留并随租户映射到正式状态**，需逐客户确认数据连续性安排 | unknown（官方）/ manual confirmation required（平台） |
| owner/admin 账号 | 平台侧 AdminUser 可保留（同一租户） | reusable unchanged |
| audit records | 保留（审计不因迁移删除） | reusable unchanged |
| exported data | 客户已导出数据不受影响 | reusable unchanged |
| support state | 迁移期间支持状态提升为 `migration` 专项 | manual confirmation required |

### 14.3 客户迁移状态机

```text
pilot_active
   │ (Haisu 批准 + 正式应用就绪)
   ▼
migration_scheduled        ← 台账记录迁移计划、日期、客户确认
   │ (暂停新归档/冻结写入，保留读取)
   ▼
pilot_frozen               ← 测试期冻结：归档/同步暂停，数据只读保留
   │ (客户完成正式授权；平台完成正式配置)
   ▼
formal_reauthorization     ← 正式应用授权成功；新 permanent code 绑定
   │
   ▼
formal_validation          ← 正式应用 smoke（复用第 10 节清单，S1/S2/S4/S9 必查）
   │
   ├─ PASS ───────────────► formal_active → pilot_decommissioned
   │
   └─ FAIL ─► formal_validation_failed ─► 第 14.4 节回滚
```

失败路径（**不设计成一次性 cutover**）：

```text
formal_validation_failed
   ├─ 可恢复 → 修复正式配置后重试 formal_reauthorization/formal_validation
   └─ 不可恢复 → 回滚到安全试点状态（pilot_frozen → pilot_active 或 service paused）
```

### 14.4 迁移失败回滚

| 步骤 | 保持 authoritative | 动作 |
|---|---|---|
| 正式验证失败 | 平台侧租户数据（试点期归档副本）为 authoritative | 恢复试点状态：重新激活测试授权（若测试授权已删，需后台重新添加——**受官方 90 天与删除约束**，可能不可行，因此**回滚决策必须在删测试授权前做出**） |
| 双应用冲突 | 同一企业不能同时装测试+正式 | 任一时刻只保留一条授权路径；若正式已授权成功，测试不可恢复 → 数据走正式路径 |
| 重复 ingestion | 归档游标（`sync_states`）为去重权威 | 迁移期间不并行运行测试/正式 worker；恢复后以游标为准补跑 |
| 重复 entitlement | 订阅权威（`Subscription`/`SubscriptionActivation`） | 只授予一次正式订阅；测试期免费状态不产生权益 |
| 订单残留 | 支付/订单权威（`PaymentOrder`） | 按官方案例处理 `existed effective test order`；不手工改库 |

**关键顺序约束**：正式授权成功前**不得**删除测试授权（删除不可逆）；正式验证
PASS 后才允许 decommission 测试路径。**迁移窗口内客户服务可能中断**，须在
migration_scheduled 时告知客户。

## 15. Rollback plan（回滚预案汇总）

| 场景 | 触发 | 回滚动作 | 恢复判据 |
|---|---|---|---|
| Pilot onboarding 回滚 | 客户尚未成功进入（第 9.3 节） | 不激活/移除测试授权/清理半成品 | 名额回 `vacant`，无半成品租户 |
| Runtime 回滚 | 客户已使用测试应用但出故障（第 11.2 节） | `frozen`（保数据）/ `suspended`（风险）；停调度 | 故障根因确认后可恢复或转 offboarding |
| Migration 回滚 | 正式验证失败（第 14.4 节） | 恢复试点状态或暂停服务；数据以平台副本为 authoritative | 数据连续、无重复 ingestion、无双授权 |

**通用约束**：
- 绝不自动 `alembic downgrade`；destructive schema 动作需独立批准 runbook
  （`wecom-third-party-authorization.md` §部署与回滚）。
- 删除/撤销授权前必须先完成导出（OFF-STOP）。
- 回滚动作逐项审计，记录 operator + approval_ref。

## 16. Go / No-Go gate（最终判定框架）

四层独立判定，**每一层都有明确 PASS / FAIL**；任一层 FAIL → 该层 No-Go。
**只有四层全部 PASS 才允许真实外部客户试点（GO）**。

### A. Documentation Ready

| 判据 | 状态 |
|---|---|
| 本 runbook（pilot 治理/准入/知情/台账/onboarding/smoke/incident/offboarding/导出/迁移/回滚）合并到 main | 本文档 PR 合并后 = PASS |
| 台账 schema 已批准，操作者明确 | 待 Haisu 确认 |
| 知情说明模板已批准 | 本文档包含模板；发送前 Haisu 逐次批准 |

### B. Platform Ready（官方名额规则与关键 Unknown）

| 判据 | 状态 |
|---|---|
| 官方名额上限（10 同时）、90 天删除、配置快照、同装限制：Confirmed（RND-420） | PASS |
| 测试企业收费状态：**Unknown → 按不可收费治理**，已由 Haisu 显式接受 | 待 Haisu 确认 |
| 测试企业主体要求：Inferred（倾向自有账号）→ 每客户前 Haisu 确认 | 待确认 |
| 迁移需重新授权 + 订单残留处理：Confirmed | PASS |

### C. Technical Ready

| 判据 | 状态 |
|---|---|
| #79 / RND-352（非生产端到端授权验收）**PASS 证据存在** | **当前 FAIL（#79 OPEN，无 QA 结论）** |
| 第 10 节 smoke 清单在非生产环境全部通过 | 待 #79 后执行 |
| 激活门禁、provisioning 门禁、日志脱敏验证通过 | 属 #79 范围，待证据 |

### D. External Pilot Ready

| 判据 | 状态 |
|---|---|
| A + B + C 全部 PASS | 当前 FAIL（C 未过） |
| Haisu 对本批客户 + 名额上限（默认 3）显式 Go | 待批准 |
| 每客户准入（第 6 节）+ 知情确认（第 7 节）+ 台账初始化完成 | 逐客户执行 |

### 当前结论

> **DOCUMENTATION READY（本文档合并后）/ EXTERNAL PILOT NO-GO**
>
> 原因：#79 / RND-352 仍 OPEN，无独立 QA PASS 证据 → **Technical Ready = FAIL**。
> 在 #79 明确 PASS 前，**不得**为真实外部客户占用新的测试企业名额。
> 本判定不以任何形式依赖 3 万元保证金（该障碍已由 RND-420/#95 排除）。

## 17. Evidence requirements（证据要求）

- 每项 runbook 步骤记录：时间、operator、结果（PASS/FAIL/NA）、批准引用。
- 外部平台动作只记录脱敏结果类型与时间戳；**不记录**请求体、回调内容、
  授权材料或凭据。
- Go/No-Go 判定必须引用具体证据（issue 状态、QA 报告、台账快照），不允许
  「记忆中的状态」。
- 若某检查无法运行，如实报告「未验证」并说明原因，不得谎报 PASS。

## 18. Sensitive-data restrictions（敏感数据限制）

本手册及试点台账**不得**包含：CorpID、UserID、suite id/secret、permanent code、
archive secret、私钥、access token、suite ticket、授权 code/state、消息内容、
完整支付/退款/订单号、银行账号、客户密钥、完整回调 URL。

- 台账只允许第 8.1 节字段；敏感对象只能用不可逆内部标识或安全引用。
- 知情确认文件与客户往来**不进入仓库**；仅记录安全引用。
- 文档、issue、聊天、截图、日志中出现任何上述敏感值即视为违规，立即通知 Haisu
  并按仓库 secrets 流程处理。

---

## 引用

- `docs/operations/wecom-third-party-authorization.md` — 第三方授权、suite_ticket
  生命周期、FIELD_ENCRYPTION_KEY 核查、非生产验收。
- `docs/operations/nonproduction-deployment.md` — 受控非生产部署。
- `docs/operations/payment-billing-runtime.md` — 计费/订阅/支付运行时。
- `docs/operations/message-deletion.md` — 回收站、永久清理、合规保留。
- `docs/architecture/current-state.md` — 架构与权威边界。
- `tasks/archive/RND-420-research-report.md` — 平台规则证据（gitignored，本地）。
- ADR-0004 / ADR-0005 / ADR-0006 — 支付、订阅生命周期与服务门禁决策。
