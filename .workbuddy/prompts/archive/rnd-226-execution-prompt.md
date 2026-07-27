# RND-226 执行提示词（hy3 / WorkBuddy 端到端：复现 → 修复 → 验证）

> 用途：粘贴给 WorkBuddy 的 hy3 智能体，由其单人端到端跑完 RND-226。
> 工单原文推荐流：Codex 先复现 → Claude Code 修复 → Codex 独立验收；
> 本提示词等价地把三个阶段交给单一智能体完成。
> 把 Cursor 让给 P1（RND-225），本任务在 WorkBuddy 并行执行，不碰 DB migration / CI / 企业名变更。

---

## 0. 任务与来源
- Linear 工单：**RND-226「修复 Nested Media 未传播会话实体上下文」**，优先级 P2，标签 `type: fix`。
- 目标：确认并修复 nested media URL 与 access resolver 的实体上下文（entity-context）传播，使 nested media 与产生它的 timeline 使用**同一套授权与 conversation resolution 语义**。
- 范围：新增 direct/group 碰撞路由探针 + 回归测试；descriptor 传播必要实体上下文；nested bytes/access 两条路径保持一致；保持 tenant isolation、forbidden fields、signed URL/proxy 行为。
- 非目标（严禁）：不重构整个 media router；不改变 nested media 公开 schema 中禁止暴露的字段；不混入 RND-207 源站/缩略图改动。

## 1. 精确根因（已定位，文件 `backend/app/routers/conversations.py`）
timeline 路由解析会话时会带实体上下文 `mode/staff_id/contact_id/conversation_type`，但 nested media 的访问 URL 与访问 resolver 没有把这段上下文传播下去。在 **direct/group 会话 ID 碰撞**场景下，timeline 能正确解析，而 nested media 访问要么返回 400（歧义），要么解析到错误实体（潜在跨实体授权泄漏）。

具体断点（行号按当前代码）：
1. timeline 路由在 ~1712 用 `mode/entity_id` 调 `_fetch_conversation_messages`；在 ~1751 已算出
   `media_context_qs = _entity_context_query_string(mode, staff_id, contact_id, conversation_type)`。
2. `_enrich_nested_media_fields`（~611）与 `_build_nested_media_descriptor`（~542）**不接收** `media_context_qs` → nested descriptor 的 `access_url`/`thumbnail_access_url` 不带实体上下文。
3. 两个 nested 路由 `get_nested_message_media`（~2564）与 `get_nested_message_media_access`（~2649）**不接受** `mode/staff_id/contact_id/conversation_type` 参数。
4. `_resolve_authorized_nested_media`（~471）调 `_fetch_conversation_messages(db, conversation_id, tenant_id)` 时**不传** `mode/entity_id`，也**不做** `conversation_type` 一致性校验（对照 timeline 路由的 ~1728-1736 mismatch 检查）。

## 2. 执行步骤（严格按顺序）

### 阶段一：复现（先证明问题，禁止先改实现代码）
- 不修改任何实现代码。先在 `backend/tests/` 新增回归测试（建议 `test_rnd_226_nested_entity_context.py`，或并入 `test_nested_media_access.py`），构造 **direct/group 会话 ID 碰撞**探针：
  - 同一 `conversation_id` 在不同实体上下文（如 `mode=staff, staff_id=A, conversation_type=direct` 与 `mode=contact, contact_id=B, conversation_type=group`）解析到不同会话实体/消息集。
  - 断言 A：timeline 带上实体上下文能正确取到消息；该消息 nested media descriptor 的 `access_url` **应包含**实体上下文查询串（先写此断言，预期失败）。
  - 断言 B：携带实体上下文回调 nested access 路由能取到正确对象；不带上下文在碰撞场景下应 400 或拿到**错误**实体（即证明"不带上下文会出错/越权"）。
- 运行该测试，确认**失败（RED）**。若无论如何都无法复现，则给出 `NOT REPRODUCED` 证据（构造了哪些场景、为何没触发），写进 Linear 评论——**绝不允许假装修好**。
- 复现测试必须作为改动的第一笔先存在并失败。

### 阶段二：修复（最小改动，严守范围）
改动面控制在"函数签名 + 透传"，不做整体重构：
1. `_build_nested_media_descriptor(media_type, media_file, conversation_id, msgid, path, media_context_qs="")`：把 `media_context_qs` 追加到 `access_url` 与 `thumbnail_access_url`（仅非空时追加）。
2. `_enrich_nested_media_fields(..., media_context_qs="")`：透传给 `_build_nested_media_descriptor`。
3. timeline 路由（~1751 之后）把已算好的 `media_context_qs` 传给调用 `_enrich_nested_media_fields` 的点。
4. `get_nested_message_media` 与 `get_nested_message_media_access`：新增 `mode/staff_id/contact_id/conversation_type` 四个 `Query` 参数，复用 timeline 路由的校验逻辑（非法 mode/id → 400；`conversation_type` 非 direct/group → 400；`_resolve_entity_context`；`conversation_type` mismatch → 409，参照 ~1728-1736）。
5. `_resolve_authorized_nested_media` 新增 `mode=None, entity_id=None, conversation_type=None`，透传给 `_fetch_conversation_messages(db, conversation_id, tenant_id, mode=mode, entity_id=entity_id)`，并加与 timeline 一致的 `conversation_type` mismatch 校验。
6. 两路由内部构建的 proxy `url`（~2709 附近）也追加同一上下文，保证 bytes 与 access 两条路径一致。

### 阶段三：验证（不达标不收工）
- 阶段一复现测试现在必须**通过（GREEN）**。
- 运行相关既有套件，确认无回归（先 `cd backend` 确认 pytest 配置，可参考 `Makefile`）：
  - `python -m pytest tests/test_nested_media_access.py tests/test_http_contract.py tests/test_tenant_media_access.py tests/test_media_access_cache_control.py tests/test_qiniu_media_serving.py -q`
  - 收工前跑一遍 `make verify`（lint-diff + typecheck + build + 全量 pytest），确认 nested/tenant/cache-control/qiniu 全部通过。
- 按需做 mixed/chatrecord nested image/video 的浏览器 smoke（项目既有 Playwright 脚本），确认前端跟随的 access_url 仍可用。

## 3. 硬性约束（不可违反）
- **租户隔离**：`sdkfileid` 只从服务端本消息 `structured_content` 读取，绝不来自请求；不得出现跨 tenant / 跨 entity 授权放宽。
- **禁止泄露字段**：nested 公开 schema 不得暴露 `sdkfileid`、`MediaFile` 主键、`local_path`、`storage_ref`/`oss_key`、任何存储凭据（保持现状）。
- **不改变** signed URL / proxy 行为、Cache-Control（`no-store` 与 proxy `max-age`）、`_NESTED_MEDIA_STATUS_BY_FILE_STATE` 语义。
- **不混入** RND-207 源站/缩略图改动；**不重构**整个 media router。
- 不引入后台进程；假设开发服务器已在运行；所有命令前台运行。
- 参考 `DEV_AGENT_RULES.md` 的 AI agent 工作流；**不要自行 `git commit`/`push`**（需用户显式授权）。

## 4. 收尾动作
- 在 Linear 把 RND-226 状态 Todo→In Progress（如尚未）；写一条评论：复现方式 + 修复点（函数级）+ 测试结果 + `make verify` 结论；若 NOT REPRODUCED 则如实说明。
- 不要自动合入/提交，保留给用户（Haisu）人工 merge 关卡——本任务与 RND-225 都碰授权语义。
