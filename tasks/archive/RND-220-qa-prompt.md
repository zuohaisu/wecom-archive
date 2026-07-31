# RND-220 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-220 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-220-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-220** 的验收标准（全部 timeline / staff collision / pagination / revoke / structured / media / tenant tests 通过；router 仅 HTTP+返回；旧实现可作短期回滚 facade）+ 代码硬约束。

---

## 0. 验收依据

- **Linear RND-220 验收（必须全过）**：
  - timeline、staff collision、pagination、revoke、structured、media、tenant 测试全部通过；
  - router 只负责参数校验与 HTTP 转换；
  - 旧实现可作为短期回滚 facade（环境变量可即时切换回 legacy 路径）。
- **非目标不可被破坏**：media access 路由与授权 helper（`get_message_media` / `get_message_media_access` / `get_nested_message_media` / `get_nested_message_media_access` 及 `_resolve_authorized_media` / `_resolve_authorized_nested_media` 等）与对应 schema（`MediaAccessOut` / `NestedMediaAccessOut`）必须保持改造前行为（RND-221 才动）。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支，且 **RND-212 已在 `main`**、**RND-219 已在 `main`**（本任务依赖 `app/conversation_listing.py` 与 `app/conversation_schemas.py`）、`app/conversation_timeline.py` 已存在。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动 **前台** 进程后再验（不后台化、不加 `&`）。
4. 拿到开发 agent 评论中的 **query baseline 数字**（timeline 端点改造前 query 数）；若缺失，本 agent 自行用 counter fixture 重测 baseline 与改造后对比。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 后端等价（直接打 timeline 端点 `GET /api/conversations/{conversation_id}/messages`）

- **C1 timeline 等价**：返回按 `(msgtime, id)` 升序（oldest first），字段与改造前逐字一致（`sender`/`sender_display_name`/`recipients`/`recipient_display_names`/`msgtype`/`content_text`/`roomid`/`decrypt_status`/`media_type`/`media_status`/`normalized_type`/`category`/`support_status`/`renderer_strategy`/`display_label_key`/`structured_content`/`is_revoked`/`revoked_at`/`revoke_event_msgid`/`revoke_association_status`）。对照 `test_revoke_timeline_api.py`。
- **C2 cursor pagination 等价**：`before=<cursor>` 翻页返回紧邻更旧的一页；`pagination.has_older` / `pagination.next_before` 与改造前一致；RND-158 边界（同 msgtime 多行、cursor 为复合 `msgtime:id`）不丢行、不重排；`page` 在 revoke 折叠前切片、折叠不影响 `has_older`/`next_before`。对照 `test_revoke_timeline_api.py::test_pagination_never_duplicates_or_skips_messages_*`。
- **C3 staff collision 等价（direct/group）**：构造 `conversation_id` 同时被 direct-pair key 与真实 `roomid` 命中的碰撞场景；`conversation_type` 未传时解析逻辑与改造前一致；传入 `conversation_type` 时按「group if any message 有真实 roomid else direct」校验，不匹配返回 400。对照 `test_staff_seats.py` 与 timeline 路由的 conversation_type policy 注释。
- **C4 revoke fold 等价**：已 linked 的 revoke 事件行被折叠进原消息（`is_revoked`/`revoked_at`/`revoke_event_msgid`/`revoke_association_status` 正确）；未 linked 的 revoke 行作为 standalone 露出（`revoke_event_msgid`=`msgid`）。对照 `test_revoke_timeline_api.py` / `test_revoke_frontend_render.py`。
- **C5 structured projection 等价**：`sdkfileid`/`corpid`/`media_key` 等内部键被剥离；`audio_archive`/`audio_doc` 类型仅含白名单字段；`card` 类型带 `contact_name`（来自租户 contacts）。对照 `test_rnd_210_msgtype_and_card.py::test_project_public_structured_fields_*`。
- **C6 nested media enrichment 等价**：mixed/chatrecord 消息的 `structured_content.fields` 中每个 media 节点被替换为完整 descriptor（`status`/`media_type`/`mime_type`/`size_bytes`/`access_url`/`thumbnail_access_url`/`image_width`/`image_height`），层级/兄弟顺序/`path` 不变；函数**从不修改输入**（deep-copy 安全）；`roomid` 派生的 per-message `conversation_type` 正确拼入 access_url。对照 `test_nested_media_access.py`（unit）、`test_rnd_226_nested_entity_context.py`、`test_rnd_206_rich_media.py`。
- **C7 media 端点未破坏（行为）**：真实打 `get_message_media` / `get_message_media_access` / `get_nested_message_media` / `get_nested_message_media_access`（及 nested entity context 透传），返回结构与改造前一致（可对照既有 media 测试套件）。本任务不重构这些，仅确认未误伤。
- **C8 tenant isolation 等价**：用**另一租户**认证调 timeline 端点，断言只能见到本租户数据；entity context（`mode`/`staff_id`/`contact_id`/`conversation_type`）即使被恶意构造也不能越权读到别租户消息；请求里即使塞 `tenant_id` 参数也应被忽略。对照 `test_tenant_isolation.py` / `test_tenant_media_access.py`。

### 架构与契约（代码审查）

- **C9 router 仅 HTTP+返回**：`routers/conversations.py` 中 `get_conversation_messages` handler 函数体只含「解析 auth →（可选 env 切换）→ 调 `resolve_timeline_page` / `_resolve_timeline_page_legacy` → return」；**不得**含排序/分页/`_fetch_conversation_messages`/`_load_revocations_map`/`_enrich_nested_media_fields`/`_project_public_structured_fields` 等聚合/投影/分页逻辑（这些应只在 `conversation_timeline.py`）。
- **C10 回滚 facade 有效**：`WEARCHIVE_LEGACY_TIMELINE=1` 时 handler 走 `_resolve_timeline_page_legacy`，且 legacy 与 new 两条路径跑同一组 C1–C8 等价测试结论一致（至少 smoke）；env 默认（未设）走新 `resolve_timeline_page`。legacy 函数标注 `@deprecated` 且为改造前代码快照、非重实现。
- **C11 非目标未被破坏（代码）**：`git diff` 确认 media access 路由（~2410/2558/2754/2872）、授权 helper（`_resolve_authorized_media` ~2206 / `_resolve_authorized_nested_media` ~471 / `_resolve_variant_serve_ref` ~2350 / `_resolve_servable_backend_and_ref` ~2370 / `_with_variant_thumb` ~2319 / `_find_nested_media_ref` ~443 / `_validate_nested_media_path` ~419）及 `MediaAccessOut`/`NestedMediaAccessOut`（~1459/1483）**无改动**。
- **C12 re-export 契约**：以下符号仍可从 `app.routers.conversations` import（现有测试依赖）：`_project_public_structured_fields`、`_build_nested_media_descriptor`、`_enrich_nested_media_fields`、`_load_media_files_map`、`_resolve_entity_context`、`_entity_context_query_string`、`_load_revocations_map`、`_RevocationMaps`、`_encode_message_cursor`、`_decode_message_cursor`、`_is_valid_roomid`、`TimelineMessageOut`、`PaginationOut`、`ConversationMessagesOut`。逐一确认 import 成功。
- **C13 单一 `_is_valid_roomid`**：定义仅存在于 `app.conversation_membership.py`；`conversation_timeline.py` / `conversation_listing.py` / router 均从同一处 import（无双定义、无循环 import）。

### 回归（不破坏既有能力）

- **C14 query 数不恶化**：用 counter fixture 复测 timeline 端点改造后 query 数 ≤ 改造前 baseline（对照开发评论或本 agent 自测 baseline）。超即 FAIL。
- **C15 等价 + 回归套件全绿**：`cd backend` 后跑
  - `python -m pytest tests/test_revoke_timeline_api.py tests/test_revoke_frontend_render.py tests/test_admin_auto_load_older.py tests/test_rnd229_focus_locate.py tests/test_rnd_206_qa_fixes.py tests/test_rnd_206_rich_media.py tests/test_rnd_206_top_level_image.py tests/test_rnd_207_thumbnail_frontend.py tests/test_rnd_210_msgtype_and_card.py tests/test_nested_media_access.py tests/test_rnd_226_nested_entity_context.py tests/test_staff_seats.py tests/test_tenant_media_access.py tests/test_tenant_isolation.py tests/test_http_contract.py -q`
  - 若开发 agent 未交付 query 基线，本 agent 用 counter fixture 自测改造后 query 数，并标注「baseline 缺失，本次自测为准」。
- **C16 `make verify` 全绿**（lint-diff + typecheck + build + 全量 pytest）。
- **C17 RND-226 / RND-210 / RND-219 未被破坏**：`test_rnd_226_nested_entity_context.py`、`test_rnd_210_msgtype_and_card.py`、`test_rnd_219_*`（若有）、listing 套件（`test_staff_seats.py` / `test_conversation_display_names.py` / `test_conversation_membership_service.py`）仍绿（确认收敛未误伤已上线逻辑）。

---

## 3. 测试方法

- **后端等价 + 回归**：按 C15 跑 pytest 子集；按 C1–C8 手工/脚本打 timeline 端点与 media 端点核对返回。
- **代码审查项（C9/C10/C11/C12/C13）**：直接读 `git diff` 与 `conversation_timeline.py` / `conversation_schemas.py` / `conversation_membership.py` / `routers/conversations.py` 头部 import，逐项核对。
- **回滚 facade（C10）**：分别以默认 env 与 `WEARCHIVE_LEGACY_TIMELINE=1` 启动同一组 timeline 等价断言，确认双路径结论一致。
- **收口**：跑 `make verify` 确认全绿。

---

## 4. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码；只**读**、**断言**与**跑测试**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过租户隔离做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-220 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 是否含 RND-212/RND-219/conversation_timeline：是/否 · make verify：通过/失败
query 基线：timeline=<n>（来源：开发评论/本 agent 自测）

| 编号 | 验收点 | 结果 | 证据（实测/命令/代码位置） |
|------|--------|------|---------------------------|
| C1   | timeline 等价 | PASS | ... |
| C2   | cursor pagination 等价 | PASS | ... |
| C3   | staff collision 等价 | PASS | ... |
| C4   | revoke fold 等价 | PASS | ... |
| C5   | structured projection 等价 | PASS | ... |
| C6   | nested media enrichment 等价 | PASS | ... |
| C7   | media 端点未破坏 | PASS | 真实打 media 端点返回一致 |
| C8   | tenant isolation 等价 | PASS | ... |
| C9   | router 仅 HTTP+返回 | PASS | 代码审查：handler 无聚合/投影/分页逻辑 |
| C10  | 回滚 facade 有效 | PASS | legacy/new 双路径等价 |
| C11  | 非目标未破坏 | PASS | git diff 确认 media 路由/授权 helper/MediaAccessOut·NestedMediaAccessOut 无改动 |
| C12  | re-export 契约 | PASS | 符号均可从 app.routers.conversations import |
| C13  | 单一 _is_valid_roomid | PASS | 定义仅在 conversation_membership |
| C14  | query 数不恶化 | PASS | 改造后 ≤ baseline（<n>） |
| C15  | 等价+回归套件 | PASS | pytest 全绿 |
| C16  | make verify | PASS | lint-diff+typecheck+build+pytest 全绿 |
| C17  | RND-226/210/219 未破坏 | PASS | 相关测试仍绿 |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 非目标（media access 路由/授权 helper/性能优化）确认未触碰 → 视为 PASS 非缺陷。
- 回滚 facade 为短期措施，legacy 路径不偏离 new 路径契约。
- 其他观察到的限制：<…>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺 RND-212 或 RND-219 或 conversation_timeline）。
```

- 若某条无论如何无法复现（如环境导致 C1–C8 打不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-220 评论贴出（状态保持 In Progress，交还用户 Haisu 决策合并）。
