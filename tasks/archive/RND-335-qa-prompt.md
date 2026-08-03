[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-335 的全部子 AC，重点验证失败登录最小化、空跑零审计、新 Session 持久性和分类过滤兼容性，并产出机器可读判定。

# RND-335 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-335 的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接从 Preflight 开始，然后逐条核对子 AC。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发下方「产出」规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-335「Backend: Small-business security activity policy and audit signal cleanup」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-335/backend-small-business-security-activity-policy-and-audit-signal
- 风险等级：R2｜类型：代码改动独立验收（只读）
- 依赖：本票 blocks RND-336；只有 PASS 才能建议解除前端 blocker。

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = AC-1a ~ AC-10d 每条子 AC 都有测试名、`file:line` 或命令 exit code，并形成 PASS/FAIL/BLOCKED 与 findings。

## 你的角色与权限
- 只判断 RND-335 是否满足批准范围；可读仓库、查票面、跑只读测试/grep/git。
- 不修改代码、测试或文档，不 commit/push/建分支，不改工单或放松 AC。
- 缺实现或缺测试必须 FAIL，不替开发补做。

## 输入
- `tasks/archive/RND-335-dev-prompt.md` 的「验收标准」全部子 AC。
- 开发 agent 的 QA Summary、Preflight 记录与当前 diff。
- 重点：`audit.py`、audit API、认证/用户/设置/保留/平台/worker writers 与本票测试。

## Preflight（先做，两项都做完再进 AC-1）

### P-1 现场采集，不要相信任何文档里的快照

本提示词**不记录**工作树状态。自己采集，并与开发 agent 报告的开工 baseline 对照：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
```

- 不在 dev prompt「本工单拥有的文件」清单里的改动 → 写进 verdict notes，
  **不因此 FAIL**，也不得修改/回滚。
- 只有开发期间新增、可归因于 RND-335 的 commit 才违反「agent 不 commit」；
  不要仅因 `origin/main..HEAD` 非空就误判。
- 本票**归因** diff 碰到拥有清单外文件，才判 `SCOPE_VIOLATION`。
- `backend/app/main.py`、`backend/app/db/models.py`、`backend/tests/test_http_contract.py`
  的所有者是 RND-337 → RND-339（见 `tasks/WAVE-ownership.md` §5）。它们有 diff 是
  预期的，**不得**记在 RND-335 账上——这正是 `docs/ticket-autopilot-workflow.md` §3.4
  记录过的 RND-288 误判模式。

### P-2 生产形状数据库（决定 AC-5f / AC-7a~7c 怎么判）

```bash
echo "DATABASE_URL=[${DATABASE_URL}]"
```

本仓的生产形状测试统一用 `skipif(not _DB_AVAILABLE)`（`_DB_AVAILABLE` 即
`DATABASE_URL` 非空，见 `backend/tests/test_rnd293_audit_log.py:31,58`，
32 个测试文件同此模式）。`make test` 不设它，CI 的 PG job 才设。

**按 `docs/agent-test-database.md` 执行。** 你和开发 agent 一样，**可以自建**
一次性测试库来验证 AC-5f / AC-7a~7c，不要因为 `DATABASE_URL` 为空就直接判 BLOCKED。

- 该文件 §2 的三条硬性否决命中任意一条 → 不许用那个库；§5 是绝对禁止清单。
- 凭据在 **`.env` 文件**里，不在环境变量里（§3.1）；**没有 `psql`/`createdb`
  不是 BLOCK 理由**，用 §3.2 的 psycopg2 脚本建库（已实测）。先走完 §3.3 的
  排除表再决定要不要 BLOCK。
- 按 §3 自建空库并 `export DATABASE_URL`，正常跑、正常判。
- 只有本机根本没有可用 PG 实例时，才 → `verdict: BLOCKED`，notes 写
  `PG_ENV_MISSING: AC-5f/AC-7a~7c 未验证`。其余子 AC 照常逐条判定并写进
  evidence——**不要**因为这一项就放弃整份验收。**不得**把 skip 当作 PASS，
  也**不得**判 FAIL（那是环境缺口，不是实现缺陷）。
- 另需核对开发 agent 是否遵守了同一套规则：若它对开发库/共享库执行过写入
  → blocker `SECURITY_VIOLATION`。

## 验收方法（证据优先）

**判定单位是子 AC，不是大项。** `tasks/archive/RND-335-dev-prompt.md` 的「验收标准」已把
10 个大项拆成 AC-1a ~ AC-10d 的原子断言。**本节不重述断言内容**（重述必然与 dev
prompt 漂移）——去读 dev prompt 的原文，本节只规定**每类断言需要什么形态的证据**
和**怎么判**。

evidence 里每一条子 AC 独立成行：`AC-3f | test_rnd335_security_activity.py::test_invite_audit_same_commit | PASS`。

### 通用证据形态

| 断言形态 | 可接受的证据 | 不可接受 |
|---|---|---|
| 「+1 行 `<action>`」 | 请求前后 `AuditLog` 实际计数差 + 该行的 action 取值 | HTTP 200；mock 断言 `write_audit` 被调用 |
| 「+0」 | 请求前后计数差为 0 | 「没看到日志」 |
| 「同一次 commit」 | 主动作回滚后事件不存在的测试 | 代码阅读 |
| 「递归不含 X」 | `docs/agent-data-minimization.md` §5 的递归遍历断言 | 只查顶层 key；注释；人工看过 |
| 「新 Session 可查」 | 显式关闭原 Session → 新建 Session → 查询 | 同 Session 查询 / `refresh()` / identity map 命中 |
| 「语义不变 / 回归」 | 既有测试原样通过，断言未被削弱或删除 | 改了断言让它变绿 |
| 「无归因 diff」 | `git diff --stat -- <path>` + P-1 归因 | `git status` 目测 |

### 逐大项的判定重点

- **AC-1**：目录必须是**唯一**事实源。出现第二套 action→category 映射（尤其前端或
  API 层自建）= FAIL。AC-1c 的 `rg` 剩余项必须**逐条**在 QA Summary 中有解释，
  未解释的生产 writer 裸字符串 = FAIL。AC-1e 检查 `config.viewed` 既登记为
  `configuration` 又确实无 writer——两个条件缺一即 FAIL。
- **AC-2**：AC-2e（防枚举未削弱）与 AC-2f（不记录提交标识）任一不成立 = **blocker FAIL**。
  这两条比「有没有写日志」重要。
- **AC-3**：AC-3f、AC-3h 是重点。只 mock `write_audit` 而不验证 DB 结果 = FAIL。
- **AC-4**：AC-4b 出现 secret 值或其派生表示（长度/前缀/指纹）= **blocker FAIL**。
- **AC-5**：AC-5c 若把 `PlatformAdmin.id` 写进 tenant `AdminUser` 外键 = blocker FAIL。
  AC-5f 依赖 Preflight P-2，按 P-2 规则处理，不得以「同 Session 能查到」冒充。
- **AC-6**：AC-6a 空跑仍写、或逐消息写 = **blocker FAIL**。
- **AC-7**：AC-7e 若为了持久性而移除 `write_audit` 的 fail-safe（让审计故障拖垮主业务）
  且未经批准 = FAIL。正确修法是调用顺序与 commit 边界。
- **AC-8**：AC-8a（无参数兼容）与 AC-8h（`operator=system` 语义不变）是本项的两个硬点。
  若实现把 `operator=system` 改写、废弃或让 `include_system` 顶替它 = FAIL——
  dev prompt 已明确裁决保留。
- **AC-9**：只检查顶层 key 或靠注释 = `INSUFFICIENT_TEST_COVERAGE`。
- **AC-10**：共享工作树里他票的失败先按 P-1 隔离归因再写 notes，不记在本票账上。

## 本项目专属检查（必查）
1. service 不 import `app.routers.*`，routers 不 import `app.main`。
2. `backend/app/db/models.py`、`backend/alembic/versions/`、`backend/app/main.py`、
   `backend/tests/test_http_contract.py` 无**本票归因** diff。
   ⚠️ 这四个文件的所有者是 RND-337 → RND-339，很可能同时有他票的在途改动——
   先按 P-1 归因，**只**判 RND-335 应负责的部分。
3. RND-336 的 template / page router / sidenav / i18n 无本票归因 diff。
4. 无新增 route；`test_rnd280_rbac_scaffold.py` 不应变化（本票不新增 router）。
5. 未把 `write_audit` 全局改成 audit sink 失败即破坏主业务；未经批准则 FAIL。
6. `AuditLogOut` 新增 `category` 字段**不应**触碰 `test_http_contract.py`：
   `_snapshot_response_model()`（`:126-136`）只记录 response model 类名。
   若开发 agent 改了该文件，属越界，判 `SCOPE_VIOLATION`。

## 附加检查（Scope / Security）
- 违反 `docs/agent-data-minimization.md` §2 的任何字段族进入 fixture / detail / API / 日志
  → `SECURITY_VIOLATION`。本票唯一授权的例外是 AC-5d 的 `platform_admin_id`；
  任何未写进 AC 的例外主张（注释、commit message、QA Summary）一律不接受。
- 未批准引入 DB category 列/migration/SIEM/webhook/alert/hash-chain → `SCOPE_VIOLATION`。
- agent 新 commit/push/分支/历史改写 → `SECURITY_VIOLATION`；必须先与 P-1 baseline 区分。

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
git diff --stat -- backend/app/db/models.py backend/alembic/versions backend/app/web backend/app/assets/i18n.js backend/app/main.py backend/tests/test_http_contract.py
git status --porcelain
git log origin/main..HEAD
```

## 产出
写入 `tasks/archive/RND-335-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- **全部子 AC** PASS 且无 blocker/major → `verdict: PASS`，`recommended_next_state: PASS`；notes 明确「RND-336 blocker 可解除」。
- 任一子 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`；findings **按子 AC 编号定位**（如 `AC-3f`），只描述最小修复。
- Preflight P-2 无法满足（**本机根本没有 PG 实例**，不是「`DATABASE_URL` 恰好为空」）
  → `verdict: BLOCKED`，notes 写 `PG_ENV_MISSING`，其余子 AC 判定照常写进 evidence。
- 需求歧义、需 migration/鉴权决策、已 2 轮仍 FAIL → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。

evidence 必须逐子 AC 成行，不得用「AC-3 全部通过」这类聚合表述。

## 禁止事项
- 除规定 verdict 外不修改文件；不替开发补实现，不放松 AC。
- 不用同 Session 可见性替代持久性。
- **不要**因为 `DATABASE_URL` 恰好为空就判 BLOCKED——先按 `docs/agent-test-database.md`
  §3 自建测试库再跑。只有本机确实没有 PG 实例才 BLOCKED。
- **也不要**把 skip 记成 PASS。
- **更不要**为了跑通而对开发库执行写入——那是 blocker `SECURITY_VIOLATION`。
