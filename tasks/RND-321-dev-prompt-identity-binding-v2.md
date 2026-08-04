# [PROMPT:DEV:v2] RND-321 — 企业微信扫码登录：账号与登录身份绑定

> 已批准的返工范围。开发 agent 直接执行本提示词；不得把历史的“未知扫码即创建 `AdminUser`”方案继续实现。
> 不 commit、不 push、不修改 Linear、不读取或输出任何生产数据、secret、token 或授权 code。

## 任务身份

- Linear：RND-321「企业微信扫码登录（PC 端管理后台）」
- 目标：PC 企业微信扫码既能登录已绑定账号，又不会把同一人的邮箱账号和扫码身份创建成两个后台账号。
- 风险：R2。这里控制的是会话存档后台的访问权，身份错误绑定会导致权限错误继承。

## [Goal check]

本工作推进“安全的扫码身份接入”闭环。可测证据是：一个控制台账号在同租户中最多对应一个企业微信登录身份；未绑定的扫码人产生独立的待处理访问申请而不是 `AdminUser`；只有 owner/admin 的明确动作才能将申请关联到既有账号或创建新账号；所有会话、角色、租户和审计语义保持正确。

## 已定产品决策（不再讨论）

1. `AdminUser` 表示可进入控制台的**账号/授权主体**，不是一次扫码发现的企业微信成员。
2. 企业微信 `UserId` 是登录身份；邮箱是账号属性和人工核验线索，**不是**无条件的同人证明。企业邮箱可能离职交接或复用。
3. 不允许“邮箱相同即自动合并、自动绑定或自动登录”。管理员仅可看到“疑似匹配”提示，必须明确确认关联。
4. 未绑定的有效企业微信扫码人不得获得 session，也不得创建 `AdminUser`；应只创建幂等的“待处理访问申请”。
5. 访问申请的处理权限限 owner/admin，且必须保留既有 owner 层级保护：admin 不能操作 owner 账号或授予 owner。
6. 已绑定身份命中 `active` 账号才可签发 session；命中 disabled、pending invite 或其他非 active 账号一律拒绝登录，保持原账号状态，不创建新账号。

## 前置阅读与现场核实

按顺序阅读 `AGENTS.md`、`DEV_AGENT_RULES.md`、`docs/AGENTS.md`、`docs/ticket-autopilot-workflow.md`、`docs/agent-data-minimization.md`、`tasks/WAVE-ownership.md`、本提示词和 RND-321 历史 artifacts。执行 `git status --short --branch`，分离本票与他票改动。

不要沿用历史 prompt 的行号、route count、migration head 或“零 migration”判断。先核实当前 `AdminUser`/`AdminSession`、扫码 callback、用户管理页、RBAC、审计 catalog、Pydantic schema、路由契约测试和 Alembic head。

`tasks/WAVE-ownership.md` 高于本提示词；如果其当前内容把必改文件分配给并行工单，停止产品代码改动并报告 `BLOCKED_NEEDS_HUMAN`。

## 交付范围

### 1. 数据模型与迁移

新增最小、可逆的持久化模型（名称可贴合仓库现有命名，但语义必须满足）：

- **登录身份**：tenant、provider（本票仅 `wecom`）、规范化 subject（企业微信 `UserId`）、关联的 `AdminUser`、验证时间与必要审计字段。数据库必须保证同一 tenant + provider + subject 最多绑定一个账号；同一账号至多一个 `wecom` 身份，除非代码中有明确、已测试的支持理由。
- **访问申请**：tenant、provider、subject、来自企微的显示名、可选的企业邮箱提示、pending/resolved 状态、解析目标账号和时间。相同 tenant + provider + subject 的重复扫码必须幂等，不能生成多条申请。

新增一条从**实际唯一** Alembic head 线性延伸的 migration，并在一次性测试数据库上验证 upgrade/downgrade。不得 drop、truncate 或自动删除任何 `AdminUser`、session 或审计记录。

向新身份表回填既有的真实企业微信身份；密码模式 sentinel、`invited:` 之类的占位值不得被当成企业微信身份。若现有 `access_requested` `AdminUser` 行存在，绝不自动把它们合并到邮箱账号或删除；只报告数量，并提供由 owner 明确处理的安全迁移/处理路径。

旧 `AdminUser.wecom_user_id` 在本票中可保留为兼容字段，但新的扫码查找必须以登录身份表为事实来源。成功关联时，只能在唯一性校验通过后同步兼容字段，不能让旧 sentinel 继续成为登录身份的事实来源。

### 2. 扫码 callback 身份决策

保留现有 PC QR、企微内 OAuth、state 单次消费、`user/get` 在职校验、CorpID→tenant 解析、cookie 安全属性和管理后台跳转；不得再造第二套 OAuth/session 流程。

在已验证的企业微信成员和 tenant 后：

| 条件 | 必须结果 |
| --- | --- |
| 身份已绑定到 active `AdminUser` | 仅为该既有账号签发 session，保留原 role/password/审计主体，不新建账号。 |
| 身份已绑定但账号非 active | 无 cookie、无新账号，返回既有的停用/待邀请错误语义。 |
| 身份未绑定 | 幂等创建或读取 `pending` 访问申请；无 cookie、无 `AdminUser`、页面显示待管理员处理。 |

如果当前合法的企业微信响应提供 `biz_mail`（优先）或 `email`，可仅作为申请上的可选匹配线索保存并标准化。不得改造二维码授权以绕过企业微信敏感信息同意，不得假设字段一定返回；缺失邮箱不得阻断扫码申请。

禁止用 `.first()`、邮箱相等或显示名相等自动关联。任何候选匹配都必须 tenant-scoped；多候选是冲突而非任意选择。日志、redirect、cookie、公开 API 和 audit `detail` 均不得包含原始邮箱、`UserId`、授权 code、token 或 secret。

### 3. owner/admin 的申请处理

在“员工与坐席”管理界面新增与正式用户列表**分离**的“待处理访问申请”区域或等价筛选。仅 owner/admin 可见和操作；普通角色不能通过 API 或前端看到申请、邮箱提示或身份信息。

每条申请至少支持：

1. **关联既有账号**：操作者从同租户既有 `AdminUser` 中显式选择主账号。服务端原子地校验请求仍 pending、身份未绑定、目标账号权限层级、tenant 和唯一性；绑定后申请 resolved，原账号 role/status/password 不变。只有目标账号 active 时下一次扫码才能登录。
2. **创建新账号并关联**：操作者显式选择允许的 role 和启用状态，创建一个正式账号并绑定身份。不得由扫码者自行选 role 或获得默认 admin；admin 不得创建/授予 owner。

将“疑似邮箱匹配”展示为提示而非默认选择。明确文案应让操作者知道邮箱复用风险；不得自动合并。新增或修改的文案覆盖 `zh-CN`、`zh-TW`、`en`。

对“申请创建”“身份关联”“从申请创建账号”（以及需要的拒绝/冲突）写入可读的审计动作。审计记录只含动作、对象和非敏感结构化字段，不含邮箱、企业微信 UserId、消息内容、凭证或 token；同步 audit log UI/i18n catalog。

### 4. 既有重复记录与兼容性

本票的目标是阻止**新**重复账号。不得删除历史 `AdminUser` 或改写既有审计记录。若本地/测试库中有旧版 RND-321 创建的 `invite_status=access_requested` 账号，测试并记录其不被自动登录、自动绑定或自动删除；真实数据清理必须由 owner 逐项确认后执行。

保留密码登录、既有 invite/reset、企微内 OAuth、QR UI（含加载/失败/过期/重试）、session TTL/cookie、租户隔离和已实现的角色管理行为。不要改 CI/CD、部署、Ruff/toolchain（RND-342 范围）、前端框架或无关认证流程。

## 文件所有权

预期可写文件（先现场确认；新增的本票测试/迁移文件可写）：

- `backend/app/db/models.py` — 新模型及约束。
- `backend/alembic/versions/<actual-next-revision>_*.py` — 本票唯一 schema migration。
- `backend/app/routers/auth.py` — QR/OAuth 共用身份解析改为查登录身份和创建申请。
- `backend/app/routers/users.py`、相关 schema — tenant-scoped 申请查询/处理 API 与 RBAC。
- `backend/app/audit.py`、`backend/app/web/templates/audit_log.html` — 新审计动作 catalog/render。
- `backend/app/web/templates/users.html`、`backend/app/assets/i18n.js` — 管理申请 UI 和三语文案。
- `backend/tests/test_rnd321_qr_login.py`、`backend/tests/test_rnd286_user_admin.py`、`backend/tests/test_users_page.py`、`backend/tests/test_http_contract.py` 及因本票真实失败而必须更新的相邻 guard tests。

`backend/app/main.py`、CI/CD、部署配置、`.gitignore`、Ruff 配置均为只读。若新增路由迫使与此清单冲突的共享所有权变更，停止并报告。

## 验收标准

- **AC-1 单一账号语义**：未知扫码只产生一条幂等 pending 访问申请，不产生 `AdminUser`、session 或 cookie；重复扫码不重复建申请。
- **AC-2 已绑定登录**：已绑定且 active 的账号扫码成功后复用该账号的角色、状态和主体 ID，不创建重复账号；disabled/pending 账号扫码绝不取得 session。
- **AC-3 明确关联**：owner/admin 只能在同租户内关联 pending 申请到一个明确选择的账号；邮箱/姓名匹配不能自动关联，多候选不能任意选择；所有越权、跨租户、重放和竞态都 fail closed。
- **AC-4 新账号处理**：owner/admin 可从申请显式创建并关联新账号；权限层级与 role/status 规则和既有用户管理一致，扫码人无法自行获得权限。
- **AC-5 管理可见性**：申请和邮箱提示与正式用户列表区分；仅 owner/admin 可见/操作；三 locale 完整；“待处理/已停用/已关联”状态不含歧义。
- **AC-6 审计与数据最小化**：申请/关联/创建动作可审计；敏感字段不泄漏到 audit detail、日志、API、DOM、cookie 或 redirect。
- **AC-7 回归与外部 E2E**：QR UI/state/重试、企微内 OAuth、密码登录、invite/reset、session/cookie/tenant isolation 全部回归。真实企业微信员工 PC 扫码 E2E 是最终人工门槛；mock 不能替代。
- **AC-8 工程门槛**：架构边界、migration upgrade/downgrade、聚焦测试和 `make verify` 全绿。若默认环境仍被 RND-342 的 Ruff 版本漂移阻塞，如实报告为外部 blocker，禁止在本票改 tooling 或把替代命令伪报为默认 gate 成功。

## 强制测试矩阵

至少覆盖：已绑定 active/disabled、未绑定、重复扫码、同 tenant 唯一邮箱提示、重复邮箱提示、无邮箱、跨 tenant、身份已经绑定到其他账号、并发/重复 resolve、owner/admin/普通角色权限、admin 操作 owner 拒绝、密码登录与企微内 OAuth 回归、审计脱敏、migration 回填与 downgrade、三 locale、HTTP route contract。

浏览器检查必须验证申请区、角色控制、成功/停用/待处理提示和 console/network；真实康冠时代员工扫码须在安全配置完成后人工执行。不要打印二维码授权 code 或任何 secret。

## 完成定义与报告

运行聚焦测试、`backend/tests/test_architecture_boundary.py`、migration round trip 和 `make verify`；报告每条命令与 exit code。最终报告逐条列 AC-1 至 AC-8、文件变化、浏览器/真实 E2E 证据、风险、无 secret、仅本票改动、最终 `git status --short --branch`，并明确未 commit、未 push、未修改 Linear。

真实 E2E 未完成或 RND-342 导致默认 `make verify` 失败时，不得宣称 RND-321 PASS；应给出精确的 `BLOCKED_NEEDS_HUMAN` / 外部 blocker 证据。
