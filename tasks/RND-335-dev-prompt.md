[Goal check] This work advances 开发（Development） by 建立可测试的安全活动目录，补齐高风险人类动作、消除空跑解密噪音，并用新会话证明平台审计已持久化。

# RND-335 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`。本票触及鉴权与跨模块事务契约，风险等级 R2；只有 Haisu 明确把本提示词交给开发 agent 后才开始改代码。

## ⚡ 立即执行，不要询问意图

你现在收到的是一个已经批准、待立即执行的任务指令。你就是 RND-335 的开发 agent。不要反问意图，也不要先输出计划等待确认；直接执行。唯一允许停下的情况是触发「人工点位」定义的 `BLOCKED_NEEDS_HUMAN`。

---

## Preflight（开工第一步，先做完再碰代码）

### P-1 现场采集工作树基线 —— 不要相信任何文档里的快照

本提示词**不记录**工作树状态：提示词是静态的，工作树是易变的，写进来的快照
必然过期。你自己采集：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
(cd backend && .venv/bin/python -m alembic heads)
```

归因规则（稳定，可信）：

1. 采集结果中**不在**下方「本工单拥有的文件」清单里的一切改动 → 标为「非本票」，
   写进 QA Summary 的 notes，**不修改、不回滚、不覆盖、不算作本票交付**。
2. 采集**之后**新增的 commit / push / branch 才归因于你（你不该产生任何一个）。
   采集之前就存在的 ahead commit 不是你的。
3. `backend/app/main.py`、`backend/app/db/models.py`、`backend/tests/test_http_contract.py`
   可能同时被 RND-337 修改（见 `tasks/WAVE-ownership.md` §5）。看到它们有 diff 是
   预期的，不是你的越界，也不要去改。

### P-2 生产形状数据库（AC-5f、AC-7 需要，硬前置）

AC-5f 要求证明平台审计行能穿过**真实外键**落库。本仓唯一的生产形状机制是
`@pytest.mark.skipif(not _DB_AVAILABLE)`，其中
`_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())`
（见 `backend/tests/test_rnd293_audit_log.py:31,58`，全仓 32 个测试文件同此模式）。
`make test` **不设** `DATABASE_URL`；只有 CI 的 PG job 设
（`.github/workflows/deploy.yml:134`），offline job 故意不设（`:121-126`）。

```bash
echo "DATABASE_URL=[${DATABASE_URL}]"
```

- **为空** → 本仓没有 docker-compose，你无法自备 PG。立即输出
  `BLOCKED_NEEDS_HUMAN`：「AC-5f/AC-7a~7c 需要一次性 PostgreSQL 的 `DATABASE_URL`，
  请 Haisu 提供」。**不要**改 `conftest`/CI/测试基础设施来绕过，**不要**用 SQLite
  冒充（`AuditLog.detail` 是 `JSONB`，SQLite 无此类型且默认不强制外键）。
  其余 AC 可继续实现，但 AC-5f/AC-7a~7c 保持未完成并在 QA Summary 中明确标注。
- **非空** → 记录其主机名与库名（不要记录密码），确认是一次性 local/test 库
  再继续。指向共享或生产库时禁止运行，按上一条上报。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-335「Backend: Small-business security activity policy and audit signal cleanup」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-335/backend-small-business-security-activity-policy-and-audit-signal
- 优先级：High｜风险等级：**R2**（鉴权、跨模块写入与事务语义）
- 所属波次：小微客户安全活动闭环 · 后端契约
- 当前关系：RND-335 blocks RND-336；RND-336 在本票通过独立 QA 前不得开始。

## [Goal check]
本工作推进「开发实现」阶段，证据 = 全部子 AC 均有确定性测试，空跑解密不再写审计，平台状态审计可由新数据库会话读到，分类过滤保持旧客户端兼容。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）

目标客户主要是 1–5 坐席的小微企业。审计是低频、高后果的安全兜底，需要回答“谁登录、谁改账号/设置、谁导出/下载、平台管理员是否访问过”，不需要后台心跳淹没人类活动。

- `backend/app/audit.py:34-50` 有部分 `AuditAction`；`write_audit()` 使用 SAVEPOINT，flush 但不 commit，预期由调用方和主动作同一事务提交。
- `backend/app/routers/audit.py:25-127` 已有租户隔离、只读、分页 API 和 action/object/operator/q/time 过滤，但没有权威 category 或排除 system 的过滤。
- 登录/登出已有写入点（`backend/app/routers/auth.py:663-672`、`:934-963`、`:1085-1108`），失败登录、邀请/接受和密码流程覆盖不足。
- 用户启停/管理员重置在 `backend/app/routers/users.py:134-187`；设置/改密在 `backend/app/routers/settings.py:51-69`、`:174-231`；保留策略在 `backend/app/routers/retention.py:43-76`，均缺完整审计。
- `backend/app/services/decrypt_worker.py:592-602` 每次都写 `decrypt.completed`，即使 `scanned=0`，五分钟轮询可产生每租户每天 288 条噪音。
- `backend/app/routers/platform.py:152-175` 先提交租户状态，再写审计且无第二次提交；同一 Session 测试不能证明请求结束后仍持久化。它还把 `PlatformAdmin.id` 传给 `AuditLog.admin_user_id`，但该列外键指向租户 `AdminUser`；生产形状数据库可能在 SAVEPOINT 内拒绝该行，而 fail-safe 会吞掉异常，形成“主操作成功、审计静默丢失”。
- `backend/app/auth.py:238-263` 的平台租户访问依赖也只 write/flush。导出 writer 已复用常量/helper；媒体下载与保留锁脚本仍有 action 字面量。

**共享工作树基线：**见 Preflight P-1。基线由你现场采集，本节不做任何快照断言。

**既有 `operator=system` 参数（本票必须显式处置）：**
`backend/app/routers/audit.py:56-57,78-79` 已有
`operator='system'` → `AuditLog.admin_user_id IS NULL`。本票新增的
`category=system`（后台批处理）与 `include_system` 是**另一个**维度：AC-5c 要求
平台人工动作 `admin_user_id IS NULL` 但 category 归 `security`，所以两者从本票起
必然不等价。**决策：保留 `operator=system` 且语义不变**（它回答「无人类 actor」），
由 AC-8h 用测试把两者的差异钉死。不得改写、废弃或让 `include_system` 顶替它。

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
   **`config.viewed` 的处置（本票必须做，不要自行判断）：**`AuditAction.CONFIG_VIEWED = "config.viewed"`
   已存在于 `backend/app/audit.py:45` 但全仓无 writer（`rg` 可自证）。**保留常量**
   （避免破坏未知的外部引用），**显式登记为 `configuration`**（这样它不会走 unknown
   降级路径、不会被误分到 `system`），且**不新增任何 writer**——「不记录设置读取」
   仍是 Out of scope。由 AC-1e 固定。
2. **认证、账号与密码。** 成功登录/登出继续保留；当默认租户已安全解析时记录失败密码登录，但不得保存提交的 username/email/password、凭证片段、原始 IP 或可形成账号枚举的信息。邀请/重新邀请、邀请接受、启停、管理员重置、重置完成、主动改密均在成功主事务中写一条；失败、跨租户、无权限和无实际变化不写“成功”事件。
3. **设置与保留策略。** `PUT /api/admin/settings` 只在有效值真实变化时写 `config.changed`，detail 只含排序稳定的 `changed_keys`，绝不含值、密文、mask 或 secret；blank secret no-op 不计。保留天数/锁状态记录安全的旧/新状态，首次永久锁定使用 `retention.config_locked`；423 或 no-op 不写成功事件。
4. **既有数据访问和平台事件归一。** 导出行为保持并纳入目录；媒体下载、平台访问、租户启停、保留锁脚本改用目录常量。平台状态 no-op 不制造事件。平台身份不得写进 tenant `AdminUser` 外键：`admin_user_id=None`，只在安全 detail 中保留稳定 `platform_admin_id`（不存 Basic Auth、密码或不必要的 email）。平台访问/启停记录必须在请求完成前持久化。
5. **worker 降噪。** `scanned=0`、无 repair/reconciliation、无 anomaly/failure 的运行不写 `decrypt.completed`。非空或异常批次最多一条 system 事件，detail 只放聚合计数和安全 public key version。`expected_pubkey_ver` 参数存在本身不算 key-version event；若检测转换需要迁移，停止上报。
6. **事务与持久性。** “主动作 + 审计”优先一次 commit；纯平台访问显式保证审计在依赖/请求完成前提交。保留 `write_audit` fail-safe，不让审计 sink 故障拖垮主业务。平台启停测试必须关闭原 Session 后用新 Session 查询。
7. **向后兼容 API。** `AuditLogOut` 新增只读 `category`（目录计算，不加 DB 列）；新增可选 `category`（逗号分隔）与 `include_system`。无新参数时保持旧集合/字段语义/filters/pagination；`include_system=false` 排除 system，`true` 不放宽 category；total/has_more 基于过滤后查询。既有 `operator=system` 原样保留（见「背景与项目现状」的处置决策）。
   注：`test_http_contract.py:126-136` 的 `_snapshot_response_model()` 只记录 response
   model 的**类名**，给 `AuditLogOut` 加字段不会触发路由快照失败——所以该文件对本票
   保持只读是正确的，不要去改它。
8. **聚焦测试。** 每个高价值 writer 有成功写入、tenant、敏感字段缺失证据；关键失败/no-op 断言不写成功日志；导出和媒体原行为继续通过。

**Out of scope（显式非目标）：**
- 不记录会话打开、时间线、搜索请求/搜索词、缩略图、签名 URL、媒体预览、设置读取、健康检查或内部子请求。
- 不改审计页面 UI；RND-336 单独处理。
- 不做 SIEM、webhook、告警、通知、hash chain、WORM、防篡改或合规报表。
- 不新增 Agent 身份/delegation/run schema。
- 不保存 `docs/agent-data-minimization.md` §2 列出的任何字段族。本票**唯一**的例外是
  平台事件 detail 中的稳定 `platform_admin_id`（由 AC-5d 显式授权）。
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
- `backend/app/db/models.py`、`backend/alembic/versions/` — 无 schema 变化；**所有者 RND-337 → RND-339**
- `backend/app/export_approval.py`、`backend/app/routers/export_approval.py`、`backend/app/routers/export_audit.py` — 现有 writer 已复用目录，只读回归
- `backend/app/main.py`、`backend/tests/test_http_contract.py` — 不新增 route；**所有者 RND-337 → RND-339**
- `backend/tests/test_rnd280_rbac_scaffold.py` — 本票不新增 router，不触发闭世界白名单
- `backend/app/web/templates/audit_log.html`、`backend/app/routers/admin_audit_page.py`、`backend/app/web/sidenav.py`、`backend/app/assets/i18n.js` — **所有者 RND-336**
- `backend/app/web/static/styles.css`、`scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats` — 共享/部署文件，无论工作树处于何种状态本票都不得改

> 跨票所有权与并发矩阵的权威来源是 `tasks/WAVE-ownership.md`；本清单与它冲突时以它为准。

若必须改只读文件，先停止，给出调用链、失败测试和最小授权请求，不得“顺手修”。

## 验收标准（Acceptance Criteria）

> **每条子 AC 是一个原子断言，独立 pass/fail。** QA 按子 AC 编号逐条判定，findings
> 也按子 AC 编号定位。不要把多个断言合并进一条，也不要在实现时把子 AC 当作
> 「大致覆盖即可」的提示。

### AC-1 权威动作目录
- **AC-1a**：Given `audit.py` 的目录，When 参数化遍历全部登记 action，Then 每个 action 恰好映射到一个 category。
- **AC-1b**：Given 一个目录外的历史 action 字符串，When 调用查询函数，Then 返回 `system` 且不抛异常。
- **AC-1c**：When `rg 'action\s*=\s*"' backend/app backend/scripts`，Then 除 argparse 的 `action="store_true"` 外，生产 writer 无目录外裸 action 字符串；剩余每一处都在 QA Summary 中逐条解释。
- **AC-1d**：Then `backend/app/db/models.py` 与 `backend/alembic/versions/` 的本票归因 diff 为空。
- **AC-1e**：Then `AuditAction.CONFIG_VIEWED` 常量仍存在，在目录中显式登记为 `configuration`（不走 AC-1b 的降级路径），且全仓无任何 writer 写 `config.viewed`。

### AC-2 认证活动
- **AC-2a**：Given 有效密码凭据，When 登录成功，Then AuditLog 恰增 1 行 `auth.login`。
- **AC-2b**：Given 有效 WeCom 凭据，When 登录成功，Then AuditLog 恰增 1 行 `auth.login`。
- **AC-2c**：When 登出成功，Then AuditLog 恰增 1 行 `auth.logout`。
- **AC-2d**：Given 默认租户已安全解析，When 密码登录失败，Then AuditLog 恰增 1 行 `auth.login_failed`。
- **AC-2e**：Given 存在的账号密码错误 vs 不存在的账号，When 各自登录失败，Then 两者的 HTTP 状态码、响应体、错误文案完全一致（防枚举未被本票削弱）。
- **AC-2f**：Given AC-2d 的事件，Then 其 `detail` 与 `object_id` 递归不含用户提交的 username / email / 密码 / 密码片段 / 源 IP。
- **AC-2g**：Given 无法安全解析租户的登录请求，When 失败，Then AuditLog +0。

### AC-3 账号与密码活动
- **AC-3a**：邀请、重新邀请各 +1 `user.invited`。
- **AC-3b**：邀请接受 +1 `user.invite_accepted`。
- **AC-3c**：启用 +1 `user.enabled`；停用 +1 `user.disabled`。
- **AC-3d**：管理员重置 +1 `user.password_reset_initiated`。
- **AC-3e**：重置完成 +1 `auth.password_reset_completed`；主动改密 +1 `auth.password_changed`。
- **AC-3f**：AC-3a~3e 每条事件与其主动作在**同一次 commit** 中落库（主动作回滚则事件不存在）。
- **AC-3g**：AC-3a~3e 每条事件的 actor、target、tenant 三个字段取值正确。
- **AC-3h**：Given 失败请求 / 跨租户请求 / 自我停用 / 状态无实际变化的 no-op，When 请求返回，Then AuditLog +0。

### AC-4 配置与保留策略
- **AC-4a**：Given 设置项有效值真实变化，When `PUT /api/admin/settings` 成功，Then +1 `config.changed`，`detail` 只含排序稳定的 `changed_keys`。
- **AC-4b**：AC-4a 的 `detail` 递归不含任何值、密文、mask 后的 secret 或 secret 的派生表示（长度/前缀/指纹）。
- **AC-4c**：Given 提交 blank secret（no-op），When 请求成功，Then 该 key 不进入 `changed_keys`，且若无其它变化则 AuditLog +0。
- **AC-4d**：保留天数变更 +1 `retention.config_changed`，含安全的旧/新状态。
- **AC-4e**：首次永久锁定 +1 `retention.config_locked`（与 4d 区分）。
- **AC-4f**：Given 423 拒绝或 no-op，Then AuditLog +0（不写成功事件）。

### AC-5 数据访问与平台
- **AC-5a**：既有 4 个导出 action 与媒体下载的 action / tenant 断言全部不变（纯回归）。
- **AC-5b**：平台租户访问、租户启用/停用改用目录常量，且其 category 为 `security`，**不是** `system`。
- **AC-5c**：Given 平台管理员执行租户启停或访问，Then 该 AuditLog 行 `admin_user_id IS NULL`（不得把 `PlatformAdmin.id` 写进指向租户 `AdminUser` 的外键）。
- **AC-5d**：AC-5c 的 `detail` 含稳定的 `platform_admin_id`，且递归不含 Basic Auth 凭据、密码或 email。
- **AC-5e**：Given 目标租户 `is_active` 已等于目标值，When 再次提交同一状态，Then AuditLog +0。
- **AC-5f**：Given Preflight P-2 提供的启用真实外键的 PostgreSQL，When 租户停用请求完成，Then 该审计行确实落库（不被 `write_audit` 的 fail-safe 静默吞掉）。**P-2 未满足时本条不得标记通过，按 P-2 规则上报。**

### AC-6 worker 信号密度
- **AC-6a**：Given `scanned=0` 且无 repair/reconciliation 且无 anomaly/failure，When 一轮 worker 结束，Then AuditLog +0。
- **AC-6b**：Given 有扫描、或有 repair/reconciliation、或有 anomaly 的单批，Then AuditLog 至多 +1。
- **AC-6c**：AC-6b 事件的 `detail` 只含聚合计数与安全的 public key version。
- **AC-6d**：Given 仅 `expected_pubkey_ver` 参数存在的空跑，Then AuditLog +0（参数存在本身不构成 key-version 事件）。
- **AC-6e**：保留锁脚本空跑 AuditLog +0。

### AC-7 事务与持久性
- **AC-7a**：租户启用事件在关闭原 Session 后可由**新 Session** 查到。
- **AC-7b**：租户停用事件同上。
- **AC-7c**：平台租户访问依赖产生的事件同上。
- **AC-7d**：代码审阅证明不存在「主状态已 commit、审计仍留在未提交 Session」的窗口。
- **AC-7e**：`write_audit` 的 fail-safe 未被移除——审计 sink 故障不会使主业务失败。
- 说明：AC-7a~7c 必须关闭原 Session 后新建 Session 查询；同 Session 查询、`refresh()` 或 identity map 命中**不算证据**。依赖 Preflight P-2。

### AC-8 API 向后兼容与分类过滤
- **AC-8a**：Given 不带任何新参数的旧请求，Then 结果集合、字段语义、既有 filters、pagination 与改动前完全一致。
- **AC-8b**：每个返回项都有 `category`，取值来自 AC-1 的同一目录（不是第二套映射）。
- **AC-8c**：`category` 单值与逗号分隔多值过滤结果正确。
- **AC-8d**：`include_system=false` 排除 `category=system`；`include_system=true` 不放宽任何其它 category 条件。
- **AC-8e**：`category` 与 `include_system` 组合、tenant 隔离、排序均正确。
- **AC-8f**：`total` 与 `has_more` 基于**过滤后**的查询计算，不是过滤前。
- **AC-8g**：非法 `category` 值返回确定性 4xx（不是 500、不是静默忽略）。
- **AC-8h**：既有 `operator=system`（`admin_user_id IS NULL`）语义不变；测试固定它与 `category=system` 的差异——平台人工动作同时满足 `operator=system` 与 `category=security`。

### AC-9 数据最小化
- **AC-9a**：对本票新增的**全部** `detail` 与 API 响应，使用 `docs/agent-data-minimization.md` §5 的哨兵组做递归 key/value 断言。
- **AC-9b**：全部 fixture 只用假数据，不含真实企微 ID / 邮箱 / 手机号。

### AC-10 回归
- **AC-10a**：`make verify` exit 0。
- **AC-10b**：`backend/tests/test_architecture_boundary.py` exit 0。
- **AC-10c**：「验证方式」列出的四组聚焦测试全部 exit 0。
- **AC-10d**：本票归因 diff 不含前端、schema、migration、route、CI/CD、部署文件（他票的在途改动按 Preflight P-1 归因规则排除）。

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
- **AC-5f / AC-7a~7c 依赖 Preflight P-2 的 `DATABASE_URL`。** 若 P-2 未满足，这些用例会按
  全仓既有的 `skipif(not _DB_AVAILABLE)` 约定自动 skip——这**不是**实现缺陷，也**不是**
  你可以自行绕过的东西。按 P-2 规则输出 `BLOCKED_NEEDS_HUMAN` 并在 QA Summary 里
  写明「AC-5f/AC-7a~7c 待 PG 环境」。**禁止**为了让它们不 skip 而修改 `conftest`、
  测试基础设施、CI 配置，或用 SQLite 替代。
- 通过 = 全部子 AC 满足且所有适用命令 exit 0；失败进入最多 2 轮有界修复。

## 依赖（Dependencies）
- 无前置 blocker；Linear 当前 In Progress。
- 本票 PASS 后才解除 RND-336 blocker；不要顺手做前端。

## 完成定义（Definition of Done）
- [ ] Preflight P-1 基线已采集并记录，P-2 结论已记录（满足 / 已 BLOCK）
- [ ] AC-1a ~ AC-10d **每一条子 AC** 都有测试名或 `file:line` 证据（不是每个大项一条）
- [ ] `make verify` 与聚焦测试全绿
- [ ] 新 Session 证据不是 identity map 假阳性
- [ ] `docs/agent-data-minimization.md` §5 哨兵递归断言覆盖全部新 detail
- [ ] 无 schema/migration、route、frontend、CI/CD、deploy 改动
- [ ] 本票归因 diff 只在拥有文件；他票在途改动已按 P-1 规则分离并写进 notes
- [ ] QA Summary 列出最终 action/category 契约、`operator=system` 与 `category=system` 的差异说明、RND-336 默认 query 示例
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
- **Escalation**：Preflight P-2 无法满足、需要 migration、改变鉴权响应、审计故障阻断主动作、修改只读文件，或无法安全审计失败登录时，立即 `BLOCKED_NEEDS_HUMAN`。
- **Escalation**：2 轮仍 FAIL 时附失败命令/测试和最小决策问题，不扩大 scope。

## 开发 agent 执行指引（步骤）
1. 跑 Preflight P-1 与 P-2 并记录结论；不得 reset/revert 任何非本票改动。
2. `rg "write_audit|record_export_audit|AuditLog\\(" backend/app backend/scripts` 形成 writer 清单；以 `audit.py` 为唯一目录。
3. 先写目录/API 过滤测试，再改 `audit.py`/`routers/audit.py`，保住无参数旧行为。
4. 按认证 → 账号/密码 → 设置/保留 → 平台 → worker → 既有字面量逐组实现并跑测试。
5. detail 用明确 allowlist；用 `docs/agent-data-minimization.md` §5 的哨兵递归检查 key/value。
6. 平台测试关闭原 Session 后创建新 Session 查询（依赖 P-2）。
7. 跑验证，输出 QA Summary、catalog、前端 query 示例和归因后的 `git status`；不要 commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不改 CI/CD、部署或 `.gitignore`。
- 不碰生产数据/密钥；数据最小化以 `docs/agent-data-minimization.md` 为准，例外只有 AC-5d 的 `platform_admin_id`。
- 不扩大到 RND-336、SIEM、告警、Agent 活动或防篡改。
- 不新增 route/migration，不改 route-count/RBAC 契约。
- 复用 `write_audit`/`record_export_audit`，最小正确改动优先于框架重写。
- 证据优先：以 exit 0、新 Session 持久性与递归敏感字段断言为证。
