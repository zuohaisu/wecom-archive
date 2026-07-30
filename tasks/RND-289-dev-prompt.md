[Goal check] This work advances 开发（Development） by 交付外部联系人详情端点与消息时间线（复用 timeline_service），收口 A4 epic 最后一块。

# RND-289 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-289 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-289「A4-3 外部联系人详情时间线」｜父 Epic RND-264（A4 外部联系人）
- 优先级：High｜风险等级：**R1**｜milestone：R3
- **本票是 A4 epic 的最后一块**（A4-1 RND-287 ✅ / A4-2 RND-288 ✅ 均已 Done 并上线）

## 背景与现状（已实地核实）

| 前置 | 状态 | 落地证据 |
|---|---|---|
| A4-1 `ExternalContact` 实体 + WeCom 同步 | ✅ Done | `backend/app/db/models.py` 的 `ExternalContact` |
| A4-2 列表/筛选 API | ✅ Done | `backend/app/routers/external_contacts.py`（`GET /api/admin/external-contacts`） |

RND-330 的 contacts 列表页已上线，但**刻意不提供详情跳转**（其 AC-4 明确要求"不悬挂详情链接"，因为详情路由不存在，点了必 404）。本票补齐详情端点。

**可复用的既有资产（先读，本项目已两次因重复实现返工）：**
- **时间线组装**：`backend/app/services/timeline_service.py` 已有成熟实现（消息投影、媒体 URL 拼装、`conversation_id`+`msgid` 处理）。**优先复用，不要另写一套时间线组装逻辑。**
- **会话 ID 推导**：`backend/app/conversation_membership.py:169` 的 `_direct_conv_id(uid_a, uid_b)`（1:1 会话）；group 直接用 `roomid`。**不要重新实现**——该文件有大段 collision/precedence 注释，重写必漏边界。
- **展示名**：`backend/app/display_names.py` 的 `resolve_person_display_name(raw_id, name)` —— **第二参数是展示名（取自 `Contact.name`），不是 `tenant_id`**。RND-288 首轮曾在此传错，导致每行展示名都变成租户 UUID 且不报错。
- **列表端点范式**：`backend/app/routers/external_contacts.py`（RND-288 交付）已有租户隔离、分页、schema 写法，**照它的模式写**，保持一致。

**❗ 本项目高频踩坑：**
- **架构边界硬闸**：router 不得 import `app.main`；service 层不得 import `app.routers.*`。
- **租户隔离铁律**：`tenant_id` 只能来自鉴权上下文（`require_role()` 解包），**绝不**从请求参数取。
- **契约测试同步（强制随附）**：本票新增 1 个路由 → 必须同步 `test_http_contract.py`。见下方所有权清单与 `docs/ticket-autopilot-workflow.md` §3.3。
- 架构冻结 D1：纯后端，无前端。

## 目标（Goal）
让管理员能通过 API 拿到某个外部联系人的完整档案 + 与其往来的消息时间线，为 contact-detail 页面（后续票）备好数据源。

## 范围边界

**In scope：**
1. `GET /api/admin/external-contacts/{external_userid}`：
   - 详情部分：复用 RND-288 已定义的字段（`name`/`company`/`tags`/`owner_wecom_userid`/`owner_display_name`/`last_interaction_at`/`message_count` 等）。
   - 时间线部分：该联系人参与的消息，**分页**，**复用 `timeline_service`** 做投影。
2. `backend/app/schemas/external_contact.py` —— 扩展详情响应 schema（该文件由 RND-288 创建，本票扩展它）。
3. **契约同步**：`test_http_contract.py` 的 `route_count`（读当前真实基线 +1，禁止写死）+ expected path 集合 + snapshot 列表。
4. 测试：`backend/tests/test_external_contact_detail.py`。

**Out of scope（显式非目标）：**
- **不改前端**：`contacts.html` 加详情跳转不在本票范围（RND-330 已交付且明确不悬挂链接；跳转另行处理）。
- 不做导出（C2 / RND-315）。
- **不改 `ExternalContact` 模型、不改 sync 逻辑、不新增迁移**（数据已在库里，本票只读）。
- 不改 RND-288 已交付的列表端点行为。
- 不改 `timeline_service.py` / `conversation_membership.py` / `display_names.py` 的实现（**只调用**）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/routers/external_contacts.py` —— **仅新增详情路由**；不得改动 RND-288 已交付的列表路由逻辑
- `backend/app/schemas/external_contact.py` —— 仅新增详情响应 schema
- `backend/tests/test_http_contract.py` —— **强制随附**：仅 `route_count`（当前基线 +1）+ 追加本票新路由到 expected/snapshot；**不得**动他票条目
- `backend/tests/test_external_contact_detail.py`（新）
- `backend/tests/test_rnd280_rbac_scaffold.py` —— **仅当**本票路由使用 `require_role` 且 `external_contacts.py` 尚未在白名单中时才需要（RND-288 应已加入，先确认再决定是否要改）

**只读、绝不可写：** `timeline_service.py`、`conversation_membership.py`、`display_names.py`、`db/models.py`、`services/external_contact_sync.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 详情可用**：`GET /api/admin/external-contacts/{external_userid}` 返回该联系人详情 + 消息时间线。
- **AC-2 时间线复用 `timeline_service`（关键）**：代码可见 `from app.services.timeline_service import ...` 的 import 与调用；**不是**本票另写的一套消息组装逻辑。若发现平行实现 → 视为失败。
- **AC-3 分页生效**：时间线走后端分页参数（`offset`/`limit` 或等价），**不得**一次性拉全部消息再切片；`offset` 超总数返回空列表而非报错。
- **AC-4 租户隔离**：A 租户不能查 B 租户的联系人（须有跨租户反例测试）；`tenant_id` 只来自鉴权上下文，不接受请求参数。
- **AC-5 不存在返回 404 且不泄露存在性**：查询不存在的 `external_userid` → 404；**跨租户查询他人存在的 ID 也返回 404**（不能返回 403 或任何暗示"这个 ID 存在但你无权"的信息）。
- **AC-6 展示名实参正确**：若响应含 `owner_display_name`，须经 `resolve_person_display_name` 且**第二参数是展示名不是 tenant_id**；有测试断言 `owner_display_name != tenant_id`。
- **AC-7 契约同步 + 回归**：`test_http_contract.py` 已同步（route_count 为当前基线 +1）；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_external_contact_detail.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -n "timeline_service" backend/app/routers/external_contacts.py    # AC-2：必须有命中
git diff --stat -- backend/app/services/timeline_service.py backend/app/conversation_membership.py backend/app/display_names.py backend/app/db/models.py   # 必须全无输出
git status --porcelain    # 共享工作树，先分离归因
```

## 依赖（Dependencies）
RND-287 ✅ / RND-288 ✅ 均 Done。**无剩余前置，可立即开始。**

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿、契约测试已同步
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**说明：详情端点的确切响应结构（供后续 contact-detail 页面对接）、时间线分页参数名**
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1**：另写一套时间线组装 → 与审阅台的消息渲染逻辑漂移，同一条消息在两处显示不一致。**由 AC-2 防守。**
- **风险 2**：404 vs 403 泄露存在性 —— 跨租户查到"存在但无权"会泄露其他租户的客户 ID。**由 AC-5 防守，统一 404。**
- **风险 3**：外部联系人是**真实客户 PII**（姓名/企业/往来消息）。测试必须固定假数据，绝不引入真实数据。
- **风险 4**：时间线不分页 → 高频联系人可能有数万条消息，一次性拉取会拖垮响应。**由 AC-3 防守。**
- 回滚：新增路由 + schema 扩展，`git checkout -- <files>` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress（已置）。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：若 `timeline_service` 的现有接口无法直接服务"按外部联系人聚合消息"（例如它只按 `conversation_id` 组织，而联系人可能跨多个会话）→ `BLOCKED_NEEDS_HUMAN`，说明具体不匹配之处，**不要**为绕开它而另写一套。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`routers/external_contacts.py`（RND-288 范式）、`services/timeline_service.py`（重点：现有接口签名与它需要什么输入）、`conversation_membership.py:169`、`display_names.py`、`models.py` 的 `ExternalContact`。
2. 确认如何从 `external_userid` 找到其参与的消息（可能需 join `ArchiveMessageRecipient` 或按 sender/receiver 匹配——先 grep 确认真实字段名，不要猜）。
3. 在 `external_contacts.py` **新增**详情路由（不动列表路由）。
4. 扩展 schema。
5. **同步契约测试**（route_count 读当前值 +1）。
6. 写测试覆盖 AC-1~AC-6（**AC-4/AC-5 是反例测试，重点**）。
7. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据；测试用固定假数据（客户 PII）。
- 不扩大 Scope：前端跳转、导出、模型改动一律 Out。
- 复用优先：`timeline_service` / `_direct_conv_id` / `resolve_person_display_name` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
