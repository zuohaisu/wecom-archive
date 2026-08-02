[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-335 的 10 条 AC，重点验证失败登录最小化、空跑零审计、新 Session 持久性和分类过滤兼容性，并产出机器可读判定。

# RND-335 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-335 的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始「验收方法」里的 AC-1，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发下方「产出」规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：读「任务身份」确认工单号，然后直接进入「验收方法」逐条核对。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-335「Backend: Small-business security activity policy and audit signal cleanup」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-335/backend-small-business-security-activity-policy-and-audit-signal
- 风险等级：R2｜类型：代码改动独立验收（只读）
- 依赖：本票 blocks RND-336；只有 PASS 才能建议解除前端 blocker。

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = AC-1 ~ AC-10 每条都有测试名、`file:line` 或命令 exit code，并形成 PASS/FAIL/BLOCKED 与 findings。

## 你的角色与权限
- 只判断 RND-335 是否满足批准范围；可读仓库、查票面、跑只读测试/grep/git。
- 不修改代码、测试或文档，不 commit/push/建分支，不改工单或放松 AC。
- 缺实现或缺测试必须 FAIL，不替开发补做。

## 输入
- `tasks/RND-335-dev-prompt.md` 的 AC-1 ~ AC-10。
- 开发 agent 的 QA Summary、开始时 HEAD/dirty baseline 与当前 diff。
- 重点：`audit.py`、audit API、认证/用户/设置/保留/平台/worker writers 与本票测试。

## 共享工作树归因（先做）

提示词撰写时已有用户改动：`backend/app/web/static/styles.css`、`scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats`，且 `main` ahead 1。

- 先对照开发 baseline；既有改动写 verdict notes，不因此 FAIL，也不得修改/回滚。
- 只有开发期间新增、可归因于 RND-335 的 commit 才违反“agent 不 commit”；不要仅因 `origin/main..HEAD` 有既有 commit 误判。
- 本票归因 diff 碰到拥有清单外文件，才判 `SCOPE_VIOLATION`。

## 验收方法（证据优先）

### AC-1 — 权威动作目录
- 证据：列出 `backend/app/audit.py` 的完整 action/category；测试证明每个 action 恰有一个 category、未知历史 action 归 `system`；全量 `rg` 证明生产 writer 不用目录外裸字符串。
- 判定：目录单一、稳定、可复用且无 migration = PASS；重复映射或新 writer 裸字符串 = FAIL。

### AC-2 — 认证活动与失败登录最小化
- 证据：password/WeCom 登录、登出、失败 password 登录测试；失败登录需证明 tenant 已安全解析、HTTP 响应与未知账号一致、detail/object_id 递归不含提交 username/email/password/IP/凭证。
- 判定：成功事件各一条；失败事件安全且不枚举；无法解析 tenant 时零写入 = PASS。记录提交标识或改变 401 防枚举 = blocker FAIL。

### AC-3 — 账号与密码活动
- 证据：邀请/重新邀请、接受、启停、管理员重置、重置完成、主动改密逐项断言 action/tenant/actor/target 和单次持久化；失败、跨租户、自我停用、相同状态 no-op 不写成功事件。
- 判定：全部有聚焦测试且事务结果正确 = PASS；少任一动作或只 mock、不验证结果 = FAIL。

### AC-4 — 设置与保留策略
- 证据：真实设置变化只记录排序稳定 `changed_keys`；blank secret/相同值不进入事件；无旧/新值、密文或 mask。保留测试覆盖首次创建、变更、永久锁、423 和 no-op。
- 判定：只记录有效变化且安全状态正确 = PASS；secret 泄露或失败请求写成功日志 = blocker FAIL。

### AC-5 — 数据访问与平台活动
- 证据：导出审批/拒绝/消费/执行、媒体下载原测试保持 action/tenant；平台访问/启停用目录常量，状态 no-op 不写。平台事件必须在启用真实外键的生产形状测试中持久化，`admin_user_id` 为 null，安全 detail 只保留稳定 `platform_admin_id`，不能把 `PlatformAdmin.id` 塞进 tenant `AdminUser` 外键。
- 判定：既有行为无回归，平台人工动作未归 system，且不存在被 fail-safe 静默吞掉的外键失败 = PASS。

### AC-6 — worker 信号密度
- 证据：AuditLog before/after；空跑严格 +0；有扫描、repair/reconciliation 或 anomaly 的单批最多 +1；detail 仅聚合字段。仅 `expected_pubkey_ver` 存在的空跑仍 +0；保留锁空跑 +0。
- 判定：空跑零行、有效批次至多一行 = PASS；空跑仍写或逐消息写 = blocker FAIL。

### AC-7 — 持久性与事务语义
- 证据：平台启用、停用、租户访问测试关闭原 Session/请求作用域后，用新 Session 查询；代码不存在先 commit mutation、后留下未提交 audit。
- 判定：新 Session 可读且无持久性窗口 = PASS；同 Session 查询/refresh/identity map 不能作为证据。

### AC-8 — API 向后兼容分类过滤
- 证据：无参数旧请求、category 单/多值、include_system false/true、组合、非法 category、跨租户、分页/total/has_more 测试；item.category 来自同一目录。
- 判定：无参数集合兼容，服务端过滤/计数一致，非法输入 4xx = PASS。

### AC-9 — 数据最小化
- 证据：测试递归遍历全部新 detail key/value，用诱饵 password/token/URL/storage key/search/message/path，证明未进入 AuditLog/API JSON。
- 判定：递归断言 + 代码审阅无敏感字段 = PASS；只检查顶层 key 或靠注释 = `INSUFFICIENT_TEST_COVERAGE`。

### AC-10 — 回归
- 证据：`make verify`、架构硬闸、聚焦测试 exit 0；无前端/schema/migration/route/CI/CD 本票 diff。
- 判定：全绿 = PASS；共享工作树他票失败先隔离归因并写 notes。

## 本项目专属检查（必查）
1. service 不 import `app.routers.*`，routers 不 import `app.main`，`app/main.py` 无业务改动。
2. `backend/app/db/models.py` 与 `backend/alembic/versions/` 无本票 diff。
3. RND-336 的 template/page/sidenav/i18n 无本票归因 diff。
4. 无新增 route；route contract/RBAC scaffold 不应变化。
5. 未把 `write_audit` 全局改成 audit sink 失败即破坏主业务；未经批准则 FAIL。
6. 逐个解释剩余 action 字符串；未解释的生产 writer 裸字符串 = FAIL。

## 附加检查（Scope / Security）
- 消息正文、解密 payload、password/hash、token、secret、signed URL、storage key、搜索文本、路径或真实生产标识进入 fixture/detail/API → `SECURITY_VIOLATION`。
- 未批准引入 DB category/migration/SIEM/webhook/alert/hash-chain → `SCOPE_VIOLATION`。
- agent 新 commit/push/分支/历史改写 → `SECURITY_VIOLATION`；必须先与 baseline 区分。

## 验证命令（只读，可运行）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd335_security_activity.py backend/tests/test_rnd294_audit_hook.py backend/tests/test_rnd295_audit_list.py -q
.venv/bin/python -m pytest backend/tests/test_password_auth.py backend/tests/test_rnd278_password_reset.py backend/tests/test_rnd285_invite_flow.py backend/tests/test_rnd286_user_admin.py backend/tests/test_rnd302_change_password.py -q
.venv/bin/python -m pytest backend/tests/test_rnd249_settings_api.py backend/tests/test_rnd318_retention_config.py backend/tests/test_decrypt_worker_service.py -q
.venv/bin/python -m pytest backend/tests/test_platform_auth_scope.py backend/tests/test_rnd310_tenant_activation.py backend/tests/test_rnd316_export_approval.py backend/tests/test_rnd317_export_audit.py backend/tests/test_media_download_audit.py backend/tests/test_rnd319_retention_lock.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
rg -n "write_audit|record_export_audit|AuditLog\(" backend/app backend/scripts
rg -n "action\s*=\s*['\"]" backend/app backend/scripts
git diff --check
git diff --stat -- backend/app/db/models.py backend/alembic/versions backend/app/web backend/app/assets/i18n.js backend/app/main.py
git status --porcelain
git log origin/main..HEAD
```

## 产出
写入 `tasks/RND-335-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- 全部 AC PASS 且无 blocker/major → `verdict: PASS`，`recommended_next_state: PASS`；notes 明确“RND-336 blocker 可解除”。
- 任一 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`，findings 只描述最小修复。
- 需求歧义、需 migration/鉴权决策、已 2 轮仍 FAIL → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。

## 禁止事项
- 除规定 verdict 外不修改文件；不替开发补实现，不放松 AC。
- 不用同 Session 可见性替代持久性。
- 核心持久性/敏感字段测试若因 `DATABASE_URL` 缺失被 skip，不能 PASS；应 FAIL 或按真实环境缺口 BLOCKED。
