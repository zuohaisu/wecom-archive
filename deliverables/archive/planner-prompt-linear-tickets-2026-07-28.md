# Planner 提示词 — 为 Crowntime WeCom Archive v1 UI 建立后端 Linear 工单

> 本文件交给 **planner**（建票角色）执行。目标：依据已完成的 v1 界面设计（`design/ui-v1/`），为**每一个前端页面**建立对应的**后端程序工单**（数据模型 + API + 鉴权 + 集成点）。前端视觉已由设计团队完成，本次只建"后端"工单。

---

## 0. 背景与关键事实（建票前必读）

- **设计交付物**：`design/ui-v1/`（设计系统 `ds/`、`tokens/`、品牌资源、`pages/` 下 17 个页面、`brief/` 原提示词、`README.md`）。这是**设计参考镜像**，不是运行代码。
- **现状**：只有 `login` 页的**视觉**被搬进了运行中的 app（`backend/app/web/templates/login.html` + `backend/app/routers/auth.py`，仅改样式不改行为；WeCom OAuth 与密码登录仍按原方式工作）。`styles.css` 已作为附加样式加入 `backend/app/web/static/`。**其余 15 个页面尚无后端。**
- **范围约定**：本次只建**后端**工单。每个页面最终 = 一条路由 + 一个真实 Jinja 模板（替换 `pages/*.html` 占位），但建票聚焦后端程序。
- **依赖地基 F0（账号体系重构）尚未做**——`admin_users` 现仅有 `id/tenant_id/wecom_user_id/name/avatar_url/last_login_at` + 时间戳，**无 password / role / status / email / phone / department**。当前密码模式是 env 单 hash（dev 兜底），非 per-user。这是解锁一切账户/权限功能的前置项。
- **已存在的可复用模型**（建票前先 `Read backend/app/db/models.py` 核实，勿重复建表）：
  - `Tenant`、`TenantWecomConfig`（含 corp_id 唯一性守卫、is_active）——租户配置模型已就位，缺开通流程/邮件激活/连通性自检。
  - `AdminUser`（见上，缺 F0 字段）、`AdminSession`（session 表已存在）。
  - `ArchiveMessage` + `ArchiveMessageRecipient`、`MediaFile`（含 download_status / thumbnail / playback 状态）、`Contact`（**仅内部员工**，非外部联系人）、`SyncState`（同步健康）、`KeyVersion`。
  - **不存在、需新建**：`AuditLog`、`ExternalContact`、`PlatformAdmin`（或 role 方案），以及密码重置令牌表。
- **参考文档**：`deliverables/feature-roadmap-2026-07-28.md`（每项 Effort 人周 + 优先级 + 依赖）、`deliverables/design-agent-prompt-2026-07-28.md`（设计约束）、`design/ui-v1/README.md`（设计状态说明）、`DEV_AGENT_RULES.md` + `docs/AGENTS.md`（工程纪律）。

---

## 1. Planner 必须做的事

1. **先核实再建票**：`Read` 现有 `backend/app/db/models.py`、`backend/app/routers/`、`backend/app/services/`、`backend/app/routers/auth.py`，确认哪些已存在、哪些需新增。**不要凭假设建表**。
2. **在 Linear 建票**：项目「365企微会话存档」(id `cfe726ec-810c-4848-8455-9b3607bf1fc1`)，team **Builder (RND)**。新 issue 默认进 Backlog，需显式设 `status`（本仓库 Linear 约定）。
3. **按 Epic 分组**：每个功能域 = 1 个 Epic（父 issue）+ 若干子 issue。后端范围为主。
4. **每张工单字段**：标题 / Why（解决什么用户问题·对应哪个设计页）/ Scope（后端：数据模型变更 + API 端点 + 鉴权/会话 + 集成点）/ Non-goals（明确不做什么，防范围蔓延）/ Dependencies（前置 issue 或 F0）/ Acceptance Criteria（可观测、可验证，引用所服务的页面）/ Estimate（人周，取自路线图）/ Labels。
5. **标注依赖**：所有依赖账户/角色/审计归因的工单，必须 `blocked-by: F0` 或对应 Epic。
6. **遵守工程纪律**（写进每张工单的 Non-goals/约束或描述）：Agent 不 git commit/push；实现交付 = 开发提示词 + QA 提示词两份文件（`.workbuddy/prompts/`）；架构冻结 D1（SSR + 原生 JS，不引 React）；Linear markdown 中代码标识符加反引号。

---

## 2. 设计页 → 后端程序 映射（建票清单）

### Epic 0 — F0 账号体系重构（Foundation，必须最先建）
- **服务页面**：login（已视觉化，需换底层）、users、settings、forgot-password、audit 归因、RBAC。
- **后端 Scope**：
  - 扩展 `AdminUser`：加 `password_hash`、`role`（枚举：owner/admin/compliance/legal/readonly_audit 等，先定最小集）、`status`（active/disabled）、`email`、`phone`(可空)、`department`(可空)、`last_active_at`（由活动更新，区别于 `last_login_at`）、邀请相关字段（`invite_token`/`invited_by`/`invite_status`）。
  - `auth.py` 由 env 单 hash 升级为 **per-user 密码鉴权**，保留 WeCom OAuth。
  - 新建 `password_reset_tokens` 表（token / admin_user_id / expires_at / used）。
  - Alembic migration；会话清理自动化（复用 `AdminSession`）。
- **Acceptance**：per-user 登录可用；可分配角色；停用用户无法登录；重置邮件流程可用。
- **Estimate**：3.5w（路线图 F0）。

### Epic A1 — 概览首页 / Dashboard
- **页面**：`pages/dashboard.html`
- **后端 Scope**：聚合统计 API——归档天数（tenant 创建→首条消息或 now）、消息总数（count `archive_messages`）、存储用量（sum `media_files.file_size` + DB 估算）、被监控员工数（去重 wecom_user_id）、同步健康（读 `SyncState`）；"最近活动"= 近期 `AuditLog`；快捷入口=导航。建议抽一个 `UsageService` 聚合模块供 A2 复用。
- **Dependencies**：F0（作用域/鉴权）、Audit 基础设施（最近活动）。
- **Acceptance**：仪表盘渲染真实聚合；14/30/90 天分段切换改变查询区间。

### Epic A2 — 用量分析
- **页面**：`pages/analytics.html`
- **后端 Scope**：消息量趋势（按日）、消息类型构成（group by 类型）、存储构成（按类型/大小）、会话活跃分布（按小时）——均为聚合查询。复用 A1 的 `UsageService`。
- **Dependencies**：F0。
- **Acceptance**：图表由真实数据渲染；不暴露消息内容。

### Epic A3 — 用户管理
- **页面**：`pages/users.html`（含邀请弹窗）
- **后端 Scope**：列 `admin_users`（角色/状态/最后活跃/近30天消息数）、邀请（建邀请令牌 + 发邮件）、启用/停用（status）、重置密码（管理员触发）。近30天消息数 = 查 `archive_messages`。
- **Dependencies**：F0（role/status/email）、邮件发送能力。
- **Acceptance**：CRUD + 邀请邮件；"最后活跃"标记；超 N 天静默视觉（计算所得，不建独立离职功能）。

### Epic A4 — 外部联系人（含详情）
- **页面**：`pages/contacts.html`、`pages/contact-detail.html`
- **后端 Scope**：**新建 `ExternalContact` 实体**（姓名/企业/标签/来源/归属员工/最后互动/会话数）+ 从 WeCom 外部联系人 API 同步；列表 API（筛选）；详情 = 该外部 userid 跨员工的全部聊天时间线（复用消息查询）；导出取证弹窗（落点见 C2）。
- **Dependencies**：F0（租户作用域）、WeCom 外部联系人 API 集成。
- **Acceptance**：列表由 WeCom 同步填充；详情展示完整时间线。

### Epic A5 — 高级检索增强
- **页面**：`pages/search-advanced.html`
- **后端 Scope**：扩展现有 `app/routers/search.py`——加筛选（时间范围 / 员工 / 外部联系人 / 消息类型）+ 结果导出（导出动作落点见 C2）。保留现有 `search.html` 风格。
- **Dependencies**：A4（按外部联系人筛选）、C2（导出）。
- **Acceptance**：筛选生效；结果可导出。

### Epic A6 — 媒体资源库
- **页面**：`pages/media.html`
- **后端 Scope**：列 `media_files`（按类型/时间筛选）、下载端点（复用现有）、每次下载写入审计。
- **复用**：`MediaFile` 模型 + 现有下载路由。
- **Dependencies**：Audit 基础设施、F0。
- **Acceptance**：网格浏览 + 下载 + 审计钩子。

### Epic A7 — 审计日志（含 Audit 基础设施）
- **页面**：`pages/audit-log.html`
- **后端 Scope**：**新建 `AuditLog` 表** + 在"查看/检索/导出/配置变更"处写入钩子 + 列表/筛选/分页 API。只读追加，任何人（含超管）不可删改。
- **Dependencies**：F0（操作人 = admin_user）、各动作来源。
- **Acceptance**：每次查看/检索/导出/配置均记录；可筛选；不可篡改（immutable）。

### Epic A8 — 设置
- **页面**：`pages/settings.html`
- **后端 Scope**：修改密码（F0）、绑定手机（**本期 defer，UI 为占位禁用**）、外观与语言偏好持久化（per-user：theme + locale，放 `AdminUser` 或 `user_preferences` 表）、租户策略（留存）——留存见 C3。
- **Dependencies**：F0。
- **Acceptance**：改密可用；主题/语言偏好持久化。

### Epic A9 — 首次配置向导
- **页面**：`pages/onboarding.html`（留存策略）、`onboarding-invite.html`、`onboarding-done.html`
- **后端 Scope**：留存策略配置（存储）、邀请合规团队（复用 A3 邀请）、标记首次完成。
- **Dependencies**：F0、C3（留存配置）。
- **Acceptance**：首跑流程存储留存策略 + 邀请。

### Epic B1 — 平台总控台（super-admin）
- **页面**：`pages/platform.html`
- **后端 Scope**：**新建 `PlatformAdmin`**（独立于 `admin_users`，隔离更清晰）；跨租户鉴权作用域（平台角色绕过 tenant_id 过滤）；跨租户聚合（复用 `UsageService`，tenant=None）；租户启停（`tenants.is_active`）；"发起内容访问申请"→ 建审计门控请求（记录）。**隐私：默认仅元数据+聚合，内容查看需显式授权 gate。**
- **Dependencies**：F0（角色）、Audit 基础设施、UsageService、Tenant 模型。
- **Acceptance**：超管看租户清单+聚合；默认不可看内容；内容申请记入审计。

### Epic B2 — 多租户开通配置
- **页面**：`pages/tenant-provisioning.html`
- **后端 Scope**：创建租户 + `TenantWecomConfig`（secret 与 RSA 私钥**加密存储**）、连通性自检（调 WeCom API）、向首位管理员发激活邮件（建 `AdminUser` 邀请）、已开通租户列表。
- **复用**：`Tenant`/`TenantWecomConfig` 模型已存在；需加密落盘 + 邮件 + 自检。
- **Dependencies**：F0（首位管理员用户）、邮件能力。
- **Acceptance**：表单创建租户+配置；自检运行；激活邮件发出。

### Cross-cutting C2 — 证据导出（PDF/Excel）
- **入口**：`contact-detail.html`、`search-advanced.html`、审阅台（现有 `review_console.html`）。
- **后端 Scope**：导出服务（生成选定消息的 PDF/Excel）；**需安全审批**（现状 P3，排期前走合规评审）；导出记入审计。
- **Dependencies**：Audit 基础设施、消息查询。
- **Acceptance**：导出产出文件且被记录。

### Cross-cutting C3 — 数据留存策略
- **后端 Scope**：留存配置（表/租户设置）+ 到期后锁定/清理的任务。
- **Dependencies**：F0。
- **Acceptance**：策略可配；任务按期执行。

---

## 3. 依赖顺序（建议建票与排期顺序）

1. **F0 账号体系**（Epic 0）— 一切前置。
2. **Audit 基础设施**（并入 A7）+ **UsageService 聚合**（A1/A2 共享）— 多页依赖。
3. 租户内：A3 用户管理 → A1 Dashboard → A2 Analytics → A4 外部联系人 → A5 检索 → A6 媒体 → A8 设置 → A9 向导。
4. 平台：B1 总控台（需 PlatformAdmin + 跨租户作用域）→ B2 开通配置。
5. 横切：C2 导出、C3 留存（可并行，依赖 Audit + F0）。

---

## 4. 待产品拍板的开放决策（planner 采用推荐默认值并在工单注明，标注"待用户确认"）

- **找回密码通道**：推荐**邮箱链接**（省成本），不做短信。→ forgot-password 工单按邮箱实现。
- **绑定手机**：本期 **defer**（UI 占位禁用），后端暂不建。→ settings 工单 Non-goals 注明。
- **超级管理员模型**：推荐**独立 `PlatformAdmin` 表**，与 `admin_users` 隔离。→ B1 按独立表建。
- **超管能否看会话内容**：默认**仅元数据+聚合**，内容查看需显式授权 gate（记录审计）。→ B1 默认实现该分层。
- **租户开通**：本期做**完整创建流程**（表单 + 配置加密存储 + 邮件激活 + 连通性自检），因总控台需有租户可看。
- **角色最小集**：先定 owner/admin/compliance/legal/readonly_audit，后续可调。→ F0 按此枚举建。

> 上述决策若产品方有变，planner 在工单中标注冲突点并 @ 相关人确认，不要阻塞建票。

---

## 5. 明确不在本期建票（Out of Scope）

- 移动端适配（已砍）。
- 敏感词 / 风险监控告警 UI（优先级极低，暂缓）。
- 独立"离职员工会话继承"功能（已并入 A3 用户管理的"最后活跃/静默"标记）。

---

## 6. 建票产出要求

- 每个 Epic 建 1 个父 issue（label `epic`），子 issue label `backend` + 对应功能 label。
- 依赖关系用 Linear 的 issue relation（`blocks` / `blocked by`）或标题前缀标注（如 `[BLOCKED:F0]`）。
- 工单描述中**引用所服务的设计页路径**（`design/ui-v1/pages/xxx.html`）与对应路线图 Effort。
- 建完后给一份清单：Epic → 子 issue 数 → 各自状态/依赖，便于后续派发实现。
