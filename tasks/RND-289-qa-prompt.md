[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-289 的 7 条 AC（重点验证复用 timeline_service、跨租户统一 404、契约同步）并产出带证据的 PASS/FAIL 判定。

# RND-289 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-289「A4-3 外部联系人详情时间线」｜风险等级 R1
- **响应含客户 PII（姓名/企业/往来消息）。AC-4/AC-5 从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 详情可用
- 证据：测试断言 `GET /api/admin/external-contacts/{external_userid}` 返回详情 + 时间线。
- 判定：端点可用且结构完整 = PASS。

### AC-2 — 时间线复用 `timeline_service`（**关键，本项目已两次因重复实现返工**）
- 证据：`grep -n "timeline_service\|from app.services.timeline_service import" backend/app/routers/external_contacts.py` **应有命中**。
- **反模式排查（重点）**：审阅详情路由的实现，确认它**不是**自己写了一套「查消息 → 组装投影 → 拼媒体 URL」的逻辑。若本票另写了一套平行的时间线组装 → FAIL（`IMPLEMENTATION_DEFECT`, severity: major）——那会与审阅台的消息渲染漂移，同一条消息两处显示不一致。
- 判定：确实复用 = PASS。平行实现 = FAIL。
- **例外**：若开发 agent 已按 dev prompt 的 Escalation 上报「`timeline_service` 接口无法直接服务按联系人聚合」并给出具体不匹配之处 → 判 `BLOCKED`（这是正确行为，不算失败）。

### AC-3 — 分页生效
- 证据：时间线走后端分页参数；`offset` 超总数返回空列表而非报错（有用例）。
- 反模式：确认**不是**一次性拉全部消息再前端/内存切片（高频联系人可能有数万条）。
- 判定：后端分页 + 边界用例 = PASS。

### AC-4 — 租户隔离
- 证据：跨租户反例测试——以租户 A 鉴权查租户 B 的联系人，断言拿不到数据。
- 代码审阅：`tenant_id` 只来自鉴权上下文（`require_role()` 解包），**不接受请求参数**。
- 判定：有反例测试 + `tenant_id` 不来自外部输入 = PASS。任一缺失 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-5 — 不存在返回 404 且不泄露存在性（**重点**）
- 证据：
  - 查询完全不存在的 `external_userid` → 404。
  - **跨租户查询他人租户中确实存在的 ID → 也必须 404**（不是 403、不是任何暗示"存在但无权"的响应）。须有这条用例。
- 背景：返回 403 会泄露"这个客户 ID 在系统里存在"，属于跨租户信息泄露。
- 判定：两种情况都返回 404 = PASS。**跨租户返回 403 或其他可区分的状态 → FAIL（`SECURITY_VIOLATION`, major）。**

### AC-6 — 展示名实参正确
- 证据：若响应含 `owner_display_name`，`grep -n "resolve_person_display_name" backend/app/routers/external_contacts.py` 有命中，且**逐字核对第二个实参是展示名（来自 `Contact.name`），不是 `tenant_id`**。
- 背景：签名是 `resolve_person_display_name(raw_id, name=None)`，逻辑是「`name` 非空就直接返回」。传 `tenant_id` 会让展示名变成租户 UUID，不报错、看着有值、全是错的（RND-288 首轮的真实教训）。
- 证据补充：有测试断言 `owner_display_name != tenant_id`。
- 判定：实参正确 + 有行为断言 = PASS。

### AC-7 — 契约同步 + 回归（**强制随附改动**）
- 背景：本票新增 1 个路由 → **必须**同步 `test_http_contract.py`（`route_count` 硬编码基线 + expected path 集合 + snapshot 列表）。这是新增路由的强制随附改动，**不是**独立工单（详见 `docs/ticket-autopilot-workflow.md` §3.3）。
- 证据：`make verify` exit 0；`test_http_contract.py` 与 `test_rnd280_rbac_scaffold.py` 均绿；`test_architecture_boundary.py` 通过。
- 判定：
  - 全绿 = PASS。
  - **新增路由但未同步契约测试 → FAIL（`REGRESSION`）**，`recommended_next_state: FIXING`（**不是** `BLOCKED_NEEDS_HUMAN`，也不要建议"另开契约维护票"——那会让 main 红灯）。
  - `route_count` 被写死成具体数字而非"当前基线 +1" → 记 minor finding。

## 本项目专属检查（必查）
1. **未改共享 service**：`git diff --stat -- backend/app/services/timeline_service.py backend/app/conversation_membership.py backend/app/display_names.py` **必须全无输出**（只调用不改）。
2. **未改模型 / 无迁移**：`git diff --stat -- backend/app/db/models.py` 无输出；`backend/alembic/versions/` 无新文件。
3. **未破坏 RND-288 的列表端点**：审阅 `external_contacts.py` 的 diff，确认列表路由逻辑未被改动（本票只**新增**详情路由）。若列表行为被改 → FAIL（`REGRESSION`）。
4. **未越界做前端**：diff 中不得出现 `templates/contacts.html` 或 `admin_contacts_page.py`（RND-330 已交付，详情跳转不在本票范围）。
5. **架构边界**：router 未 import `app.main`；service 层未 import `app.routers.*`。
6. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/external_contacts.py`（仅新增详情路由）、`schemas/external_contact.py`（仅新增详情 schema）、`tests/test_http_contract.py`（仅契约同步）、`tests/test_external_contact_detail.py`（新）、可选 `tests/test_rnd280_rbac_scaffold.py`。
   > 共享工作树可能含他票在途改动（见 §3.4）——先 `git status` 分离归因，只把本票的部分记在本票账上。

## 附加检查（Security）
- 测试中**无**真实客户姓名 / 手机号 / 企业名 / 真实聊天内容 → 否则 FAIL（`SECURITY_VIOLATION`, blocker）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_external_contact_detail.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -n "timeline_service" backend/app/routers/external_contacts.py    # AC-2：必须有命中
grep -n "resolve_person_display_name" backend/app/routers/external_contacts.py   # AC-6
git diff --stat -- backend/app/services/timeline_service.py backend/app/conversation_membership.py backend/app/display_names.py backend/app/db/models.py   # 必须全无输出
git status --porcelain
git log origin/main..HEAD                                              # 必须无输出
```

## 产出
写入 `tasks/RND-289-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：详情端点的确切响应结构、时间线分页参数名（供后续 contact-detail 页面对接）。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **AC-2 若发现另写了一套时间线组装 → 直接 FAIL**，不接受"功能上也能跑"。
- **AC-5 若跨租户查询返回 403 而非 404 → 直接 FAIL**，那是存在性泄露。
