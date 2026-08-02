[Goal check] This work advances 开发（Development） by 建立可测试的安全活动目录，补齐高风险人类动作、消除空跑解密噪音，并用新会话证明平台审计已持久化。

# RND-335 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`。本票触及鉴权与跨模块事务契约，风险等级 R2；只有 Haisu 明确把本提示词交给开发 agent 后才开始改代码。

## ⚡ 立即执行，不要询问意图

你现在收到的是一个已经批准、待立即执行的任务指令。你就是 RND-335 的开发 agent。不要反问意图，也不要先输出计划等待确认；直接执行。唯一允许停下的情况是触发「人工点位」定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-335「Backend: Small-business security activity policy and audit signal cleanup」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-335/backend-small-business-security-activity-policy-and-audit-signal
- 优先级：High｜风险等级：**R2**（鉴权、跨模块写入与事务语义）
- 所属波次：小微客户安全活动闭环 · 后端契约
- 当前关系：RND-335 blocks RND-336；RND-336 在本票通过独立 QA 前不得开始。

## [Goal check]
本工作推进「开发实现」阶段，证据 = 10 条 AC 均有确定性测试，空跑解密不再写审计，平台状态审计可由新数据库会话读到，分类过滤保持旧客户端兼容。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）

目标客户主要是 1–5 坐席的小微企业。审计是低频、高后果的安全兜底，需要回答“谁登录、谁改账号/设置、谁导出/下载、平台管理员是否访问过”，不需要后台心跳淹没人类活动。

- `backend/app/audit.py:34-50` 有部分 `AuditAction`；`write_audit()` 使用 SAVEPOINT，flush 但不 commit，预期由调用方和主动作同一事务提交。
- `backend/app/routers/audit.py:25-127` 已有租户隔离、只读、分页 API 和 action/object/operator/q/time 过滤，但没有权威 category 或排除 system 的过滤。
- 登录/登出已有写入点（`backend/app/routers/auth.py:663-672`、`:934-963`、`:1085-1108`），失败登录、邀请/接受和密码流程覆盖不足。
- 用户启停/管理员重置在 `backend/app/routers/users.py:134-187`；设置/改密在 `backend/app/routers/settings.py:51-69`、`:174-231`；保留策略在 `backend/app/routers/retention.py:43-76`，均缺完整审计。
- `backend/app/services/decrypt_worker.py:592-602` 每次都写 `decrypt.completed`，即使 `scanned=0`，五分钟轮询可产生每租户每天 288 条噪音。
- `backend/app/routers/platform.py:152-175` 先提交租户状态，再写审计且无第二次提交；同一 Session 测试不能证明请求结束后仍持久化。它还把 `PlatformAdmin.id` 传给 `AuditLog.admin_user_id`，但该列外键指向租户 `AdminUser`；生产形状数据库可能在 SAVEPOINT 内拒绝该行，而 fail-safe 会吞掉异常，形成“主操作成功、审计静默丢失”。
- `backend/app/auth.py:238-263` 的平台租户访问依赖也只 write/flush。导出 writer 已复用常量/helper；媒体下载与保留锁脚本仍有 action 字面量。

**共享工作树基线：**提示词撰写时 `main` 相对 `origin/main` ahead 1，且 `backend/app/web/static/styles.css`、`scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats` 有用户既有改动。开始时重新记录 `git status --short --branch` 与 `git log origin/main..HEAD`；不得覆盖、回滚或把它们算作本票交付。

**本项目已知的高频踩坑点：**
- ❗ **没有模板引擎。** `render_template()` 只做 `__TOKEN__` 单遍替换，不存在 Jinja；本票不改前端。
- ❗ **i18n 有 3 个 locale**：`zh-CN` / `zh-TW` / `en`。本票不新增界面文案，不得碰 i18n。
- ❗ **架构边界是硬闸**：routers 不得 import `app.main`；service 不得 import `app.routers.*`；`app/main.py` 不得加业务逻辑。
- ❗ **架构冻结 D1**：SSR + 原生 JS，禁止引入 React/Vue/build step/SPA 假设。

## 目标（Goal）

建立最小、可信、租户隔离的安全活动流：高风险人类动作有记录，后台空跑不制造噪音，审计写入有可证明的持久性，现有列表 API 向后兼容地支持 RND-336 默认隐藏 system 活动。

## 范围边界

**In scope（交付物）：**

1. **权威生产动作目录。** 在 `backend/app/audit.py` 集中定义 action → category（`security` / `account` / `configuration` / `data_access` / `system`）与稳定 object type，并提供写入端/API 共用的查询函数。至少包含：
   - `auth.login`、`auth.login_failed`、`auth.logout`、`auth.password_reset_requested`、`auth.password_reset_completed`、`auth.password_changed`
   - `user.invited`、`user.invite_accepted`、`user.enabled`、`user.disabled`、`user.password_reset_initiated`
   - `config.changed`、`retention.config_changed`、`retention.config_locked`
   - `export.approval_granted`、`export.approval_denied`、`export.approval_consumed`、`export.executed`、`media.download`
   - `platform.tenant_accessed`、`platform.tenant_activated`、`platform.tenant_deactivated`
   - `decrypt.completed`、`retention.messages_locked`
   平台人工动作归 `security`；真正后台批处理归 `system`。未知历史 action 安全降级为 `system`，新生产 writer 不得用裸字符串绕过目录。
2. **认证、账号与密码。** 成功登录/登出继续保留；当默认租户已安全解析时记录失败密码登录，但不得保存提交的 username/email/password、凭证片段、原始 IP 或可形成账号枚举的信息。邀请/重新邀请、邀请接受、启停、管理员重置、重置完成、主动改密均在成功主事务中写一条；失败、跨租户、无权限和无实际变化不写“成功”事件。
3. **设置与保留策略。** `PUT /api/admin/settings` 只在有效值真实变化时写 `config.changed`，detail 只含排序稳定的 `changed_keys`，绝不含值、密文、mask 或 secret；blank secret no-op 不计。保留天数/锁状态记录安全的旧/新状态，首次永久锁定使用 `retention.config_locked`；423 或 no-op 不写成功事件。
4. **既有数据访问和平台事件归一。** 导出行为保持并纳入目录；媒体下载、平台访问、租户启停、保留锁脚本改用目录常量。平台状态 no-op 不制造事件。平台身份不得写进 tenant `AdminUser` 外键：`admin_user_id=None`，只在安全 detail 中保留稳定 `platform_admin_id`（不存 Basic Auth、密码或不必要的 email）。平台访问/启停记录必须在请求完成前持久化。
5. **worker 降噪。** `scanned=0`、无 repair/reconciliation、无 anomaly/failure 的运行不写 `decrypt.completed`。非空或异常批次最多一条 system 事件，detail 只放聚合计数和安全 public key version。`expected_pubkey_ver` 参数存在本身不算 key-version event；若检测转换需要迁移，停止上报。
6. **事务与持久性。** “主动作 + 审计”优先一次 commit；纯平台访问显式保证审计在依赖/请求完成前提交。保留 `write_audit` fail-safe，不让审计 sink 故障拖垮主业务。平台启停测试必须关闭原 Session 后用新 Session 查询。
7. **向后兼容 API。** `AuditLogOut` 新增只读 `category`（目录计算，不加 DB 列）；新增可选 `category`（逗号分隔）与 `include_system`。无新参数时保持旧集合/字段语义/filters/pagination；`include_system=false` 排除 system，`true` 不放宽 category；total/has_more 基于过滤后查询。
8. **聚焦测试。** 每个高价值 writer 有成功写入、tenant、敏感字段缺失证据；关键失败/no-op 断言不写成功日志；导出和媒体原行为继续通过。

**Out of scope（显式非目标）：**
- 不记录会话打开、时间线、搜索请求/搜索词、缩略图、签名 URL、媒体预览、设置读取、健康检查或内部子请求。
- 不改审计页面 UI；RND-336 单独处理。
- 不做 SIEM、webhook、告警、通知、hash chain、WORM、防篡改或合规报表。
- 不新增 Agent 身份/delegation/run schema。
- 不保存消息正文、解密 payload、凭证、密码、token、invite/reset token、signed URL、storage key、搜索文本或路径。
- 不改数据库列/表/索引或 Alembic migration；认为必须迁移时 `BLOCKED_NEEDS_HUMAN`。
- 不重构无关鉴权流程，不改既有 HTTP 响应来“方便审计”。

**本工单拥有的文件（只许写这些；不要求全部都改）：**
- `backend/app/audit.py`
- `backend/app/auth.py`
- `backend/app/routers/audit.py`
- `backend/app/routers/auth.py`
- `backend/app/routers/users.py`
- `backend/app/routers/settings.py`
- `backend/app/routers/retention.py`
- `backend/app/routers/platform.py`
- `backend/app/routers/media.py`（仅把现有 action 字面量改为目录常量）
- `backend/app/services/decrypt_worker.py`
- `backend/scripts/apply_retention_lock_once.py`（仅目录常量/必要适配，不改锁算法）
- `backend/tests/test_rnd335_security_activity.py`（新建）
- `backend/tests/test_rnd294_audit_hook.py`
- `backend/tests/test_rnd295_audit_list.py`
- `backend/tests/test_password_auth.py`
- `backend/tests/test_rnd278_password_reset.py`
- `backend/tests/test_rnd285_invite_flow.py`
- `backend/tests/test_rnd286_user_admin.py`
- `backend/tests/test_rnd302_change_password.py`
- `backend/tests/test_rnd249_settings_api.py`
- `backend/tests/test_rnd318_retention_config.py`
- `backend/tests/test_decrypt_worker_service.py`
- `backend/tests/test_platform_auth_scope.py`
- `backend/tests/test_rnd310_tenant_activation.py`
- `backend/tests/test_rnd316_export_approval.py`
- `backend/tests/test_rnd317_export_audit.py`
- `backend/tests/test_media_download_audit.py`
- `backend/tests/test_rnd319_retention_lock.py`

**本工单只读、绝不可写的文件：**
- `backend/app/db/models.py`、`backend/alembic/versions/` — 无 schema 变化
- `backend/app/export_approval.py`、`backend/app/routers/export_approval.py`、`backend/app/routers/export_audit.py` — 现有 writer 已复用目录，只读回归
- `backend/app/main.py`、`backend/tests/test_http_contract.py`、`backend/tests/test_rnd280_rbac_scaffold.py` — 不新增 route
- `backend/app/web/templates/audit_log.html`、`backend/app/routers/admin_audit_page.py`、`backend/app/web/sidenav.py`、`backend/app/assets/i18n.js` — RND-336 所有
- `backend/app/web/static/styles.css`、`scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats` — 用户既有改动

若必须改只读文件，先停止，给出调用链、失败测试和最小授权请求，不得“顺手修”。

## 验收标准（Acceptance Criteria）

- **AC-1 权威动作目录**：全部生产 writer 使用同一目录常量/helper；每个 action 恰有一个 category，未知历史 action 归 `system`；无 migration。
- **AC-2 认证活动**：成功 password/WeCom 登录、登出各一条；安全 tenant 上下文中的失败密码登录写 `auth.login_failed`，响应/时序防枚举不变，detail/object_id 不含提交标识或凭证；无法解析 tenant 时不写。
- **AC-3 账号与密码活动**：邀请/接受、启停、管理员重置、重置完成、主动改密各在成功 commit 中恰一条；actor/target/tenant 正确；失败、跨租户、自我停用和 no-op 不写成功事件。
- **AC-4 配置与保留策略**：设置事件仅含真实变化 key 名且无 secret 值/派生表示；保留安全旧/新状态正确；首次永久锁定明确；拒绝/no-op 无事件。
- **AC-5 数据访问与平台**：既有导出/媒体 action 和 tenant 不变；平台访问/启停使用目录，平台人类动作不归 system，状态 no-op 无事件；平台事件 `admin_user_id` 为 null，安全 detail 含 `platform_admin_id`，在启用真实外键的生产形状测试中也能持久化。
- **AC-6 worker 密度**：严格空跑 +0 AuditLog；有扫描、repair/reconciliation 或异常的单批最多 +1；仅 publickey_ver 参数存在的空跑仍 +0；保留锁空跑仍不写。
- **AC-7 持久性**：平台启用、停用、租户访问在关闭原 Session 后，可由新 Session 查询；不存在主状态已 commit、audit 留在未提交 Session 的窗口。
- **AC-8 API 分类过滤**：无参数兼容；每项有 category；category/include_system 组合、tenant、排序、total/offset/has_more 正确；非法 category 确定性 4xx。
- **AC-9 数据最小化**：对所有新 detail 递归断言，禁止密码/token/secret/signed URL/storage key/search/message/decrypted payload/path 及提交登录标识；测试只用假数据。
- **AC-10 回归**：`make verify`、架构硬闸和相关聚焦测试全绿；无前端/schema/migration/route/CI/CD diff。

## 验证方式（Verification — 确定性闸）
- 类型：**automated verification，R2 human-gated execution**
- 命令：
  ```bash
  make verify
  .venv/bin/python -m pytest backend/tests/test_rnd335_security_activity.py backend/tests/test_rnd294_audit_hook.py backend/tests/test_rnd295_audit_list.py -q
  .venv/bin/python -m pytest backend/tests/test_password_auth.py backend/tests/test_rnd278_password_reset.py backend/tests/test_rnd285_invite_flow.py backend/tests/test_rnd286_user_admin.py backend/tests/test_rnd302_change_password.py -q
  .venv/bin/python -m pytest backend/tests/test_rnd249_settings_api.py backend/tests/test_rnd318_retention_config.py backend/tests/test_decrypt_worker_service.py -q
  .venv/bin/python -m pytest backend/tests/test_platform_auth_scope.py backend/tests/test_rnd310_tenant_activation.py backend/tests/test_rnd316_export_approval.py backend/tests/test_rnd317_export_audit.py backend/tests/test_media_download_audit.py backend/tests/test_rnd319_retention_lock.py -q
  .venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
  git diff --check
  ```
- 核心持久性证据不得因 `DATABASE_URL` 缺失而 skip；应使用项目测试 DB 约定或隔离的新 Session fixture。
- 通过 = AC-1 ~ AC-10 全部满足且所有适用命令 exit 0；失败进入最多 2 轮有界修复。

## 依赖（Dependencies）
- 无前置 blocker；Linear 当前 In Progress。
- 本票 PASS 后才解除 RND-336 blocker；不要顺手做前端。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-10 均有测试名/代码位置证据
- [ ] `make verify` 与聚焦测试全绿
- [ ] 新 Session 证据不是 identity map 假阳性
- [ ] 递归敏感字段断言覆盖全部新 detail
- [ ] 无 schema/migration、route、frontend、CI/CD、deploy 改动
- [ ] 本票归因 diff 只在拥有文件，用户既有 dirty diff 原样保留
- [ ] QA Summary 列出最终 action/category 契约和 RND-336 默认 query 示例
- [ ] 未 commit、未 push、未建分支

## 风险与回滚（Risk & rollback）
- 失败登录可能造成枚举/泄露：只用已解析 tenant/内部 ID，不保留提交标识，保留既有响应与验证调用。
- 不得为持久性破坏 `write_audit` fail-safe；优先修调用顺序和 commit 边界。
- category 由目录计算，不新增 DB 列。
- `expected_pubkey_ver` 每次存在不等于 key-version event，防止噪音原样保留。
- 回滚为应用层 writer/目录/API 加法与降噪，无 migration，可逐文件反向应用 diff。

## 人工点位（Human touchpoints）
- **Trigger**：RND-335 已置 In Progress；Haisu 明确下达本提示词后开始 R2 实现。
- **Gate**：Haisu 审阅 action/category、失败登录最小化和新 Session 事务证据后批准 commit。
- **Escalation**：需要 migration、改变鉴权响应、审计故障阻断主动作、修改只读文件，或无法安全审计失败登录时，立即 `BLOCKED_NEEDS_HUMAN`。
- **Escalation**：2 轮仍 FAIL 时附失败命令/测试和最小决策问题，不扩大 scope。

## 开发 agent 执行指引（步骤）
1. 读规则并记录开始 HEAD/ahead/dirty baseline；不得 reset/revert 用户改动。
2. `rg "write_audit|record_export_audit|AuditLog\\(" backend/app backend/scripts` 形成 writer 清单；以 `audit.py` 为唯一目录。
3. 先写目录/API 过滤测试，再改 `audit.py`/`routers/audit.py`，保住无参数旧行为。
4. 按认证 → 账号/密码 → 设置/保留 → 平台 → worker → 既有字面量逐组实现并跑测试。
5. detail 用明确 allowlist；测试递归检查 key/value。
6. 平台测试关闭原 Session 后创建新 Session查询。
7. 跑验证，输出 QA Summary、catalog、前端 query 示例和归因后的 `git status`；不要 commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不改 CI/CD、部署或 `.gitignore`。
- 不碰生产数据/密钥，不记录凭证、token、secret、消息、搜索词、signed URL、storage key 或路径。
- 不扩大到 RND-336、SIEM、告警、Agent 活动或防篡改。
- 不新增 route/migration，不改 route-count/RBAC 契约。
- 复用 `write_audit`/`record_export_audit`，最小正确改动优先于框架重写。
- 证据优先：以 exit 0、新 Session 持久性与递归敏感字段断言为证。
