# RND-220 执行提示词（单人端到端：复现 → 实现 → 验证）

> 用途：粘贴给单一开发智能体（hy3 / Claude Code / Codex 皆可），由其端到端跑完 RND-220。
> 工单：`RND-220「提取 Timeline Resolution 与 Projection Service」`，优先级 P3（Linear 实时状态 **In Progress**，assignee Haisu Zuo），父脉络 **RND-212**（渐进式重构为模块化单体），**被 RND-219 阻塞**、并**自身阻塞 RND-221**（统一 Media Access Service 与 Router）。
> 本次只做「Timeline 侧」抽取：conversation resolution + cursor pagination + timeline projection + revoke fold + nested media enrichment orchestration；media access 路由与授权 helper 属 RND-221，本任务**严禁**触碰。

---

## 0. 任务与来源

- **目标（来自 Linear 工单）**：将 conversation resolution、cursor pagination、timeline projection、revoke fold 与 nested media enrichment orchestration 提取为**可脱离 FastAPI 独立测试的 service**。
- **非目标（严禁）**：
  - 不做 DB-level pagination 性能优化；
  - 不改变 response contract（端点的 HTTP 契约、返回字段形状保持不变）。
- **验收标准（逐条必过）**：
  - 全部 timeline、staff collision、pagination、revoke、structured、media、tenant tests 通过；
  - router 仅负责参数校验与 HTTP 转换；
  - 旧实现可作为短期回滚 facade。
- 同一模块化单体重构链中，RND-219（Listing）已完成其抽取；本任务抽取其后的 Timeline 层，RND-221 再抽取 Media Access 层。三者都改 `routers/conversations.py`，**必须顺序执行、禁止并行重叠编辑**。

### 前置依赖（开工前先确认已在 `main`）

1. **RND-212 已合并**（父任务，模块化单体基础）。
2. **RND-219 已合并到 `main`（关键前置）**——本任务依赖 `app/conversation_listing.py`（已含 `_load_display_names` 的 re-export）与 `app/conversation_schemas.py`（含 MonitoredAccountOut/ContactOut/ConversationOut）。若 RND-219 未合入，**立即停止并报告**，不要基于 pre-219 的 router 实现开工。
3. **RND-226 / RND-210 已在 `main`**（nested-media entity-context、msg-type card 渲染）——本任务的 router 收敛不得破坏这两块已上线逻辑；若收敛时误删/误改了相关 import 或函数，停下并标注。
4. 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit` / `push`**。

---

## 1. 精确落点（文件 / 函数级）

### 1.1 新建 Timeline Service：`backend/app/conversation_timeline.py`（扁平单文件，镜像 `conversation_membership.py` / `conversation_listing.py` 约定）

从 `routers/conversations.py` **整体 MOVED（非 reimplement）** 以下函数/类/常量，保持函数体逐字等价：

- **核心入口（NEW 函数名）**：`resolve_timeline_page(db, tenant_id, conversation_id, *, limit, before, mode, staff_id, contact_id, conversation_type) -> ConversationMessagesOut`。
  - 实现体 = 现 `get_conversation_messages` 的整个函数体（router ~1760–2203），去掉 `@router.get` 装饰器与 `Depends`，入参改为显式 `(db, tenant_id, conversation_id, *, limit, before, mode, staff_id, contact_id, conversation_type)`。
  - `tenant_id` 作为显式参数（来自 `get_current_user`，**绝不接受请求传入**）。
  - 内部所有 `raise HTTPException(...)`（400/404）保持不变——`HTTPException` 来自 fastapi，可脱离 `Request`/`Response` 直接单测，符合「可脱离 FastAPI 独立测试」的工单目标。
- **conversation resolution**：`_resolve_entity_context`（~208）→ 放 `conversation_timeline.py`，re-export 到 router（media 路由 RND-221 仍用）。
- **cursor pagination**：`_encode_message_cursor`（~1541）、`_decode_message_cursor`（~1545）。
- **timeline projection**：`INTERNAL_STRUCTURED_FIELD_KEYS`（~1725）、`PUBLIC_STRUCTURED_FIELD_ALLOWLIST`（~1727）、`_project_public_structured_fields`（~1736）。
- **revoke fold**：`_RevocationMaps`（~288）、`_load_revocations_map`（~296）、`_ensure_aware`（~339）、`_datetime_to_epoch_ms`（~345）。
- **nested media enrichment orchestration**：`_enrich_nested_media_fields`（~676）+ 内部 `_walk`（~729）、`_build_nested_media_descriptor`（~588）、`_load_media_files_by_sdkfileid_map`（~569）。
- **timeline 专用辅助**：`_load_media_files_map`（~250）、`_thumbnail_timeline_fields`（~2326）。
- **共享 URL 上下文**：`_entity_context_query_string`（~170）→ 放 `conversation_timeline.py`，re-export 到 router（media 路由 RND-221 仍用）。
- **共享 roomid 判定**：`_is_valid_roomid`（~761）→ **移动到 `app/conversation_membership.py`**（membership helper 与 listing / timeline / media 路由共用，放最底层可避免循环 import），router 与 `conversation_timeline.py` / `conversation_listing.py` 均从 `app.conversation_membership` 导入并 re-export。`conversation_timeline.py` 顶部 `from app.conversation_membership import ... _is_valid_roomid, _fetch_conversation_messages, _load_recipients_map`（及 RND-219 已迁出的 membership 函数对象）。

**`conversation_timeline.py` 需要的 import（从 router 迁移，逐字保留语义）**：
`from fastapi import HTTPException`、`from datetime import datetime, timezone`、`from sqlalchemy.orm import Session`、`from app.db.models import (AdminUser, ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile, MessageRevocation)`、`from app.db.session import get_db`（如需要）、`from app.display_names import resolve_person_display_name, resolve_room_display_name`、`from app.media_classification import classify_media, resolve_downloadable_media_status`、`from app.media_download import NESTED_MEDIA_MSGTYPES, iter_nested_media_refs`、`from app.message_type_registry import describe_message_type`、`from app.revoke_reconciliation import display_status as _revoke_display_status`、`from app.media_storage import (...)`（仅 timeline 用到的导出：见现 router import 块 ~83–98）、`from app.conversation_membership import _is_valid_roomid, _fetch_conversation_messages, _load_recipients_map, ...`、`from app.conversation_listing import _load_display_names`（RND-219 已迁出）。

> 注：若 RND-219 合并后的 `conversation_listing.py` 目前从 router 导入 `_is_valid_roomid`，将其改为 `from app.conversation_membership import _is_valid_roomid`（因 §1.1 已将定义移入 `conversation_membership.py`）。保证单一定义、无循环 import。

### 1.2 Schemas 归位：`backend/app/conversation_schemas.py`（已有，RND-219 创建）

将 timeline 三模型从 router 迁入（逐字等价）：`TimelineMessageOut`（~1391）、`PaginationOut`（~1517）、`ConversationMessagesOut`（~1522）。`MediaAccessOut`（~1459）、`NestedMediaAccessOut`（~1483）**不动**（属 RND-221）。router 顶部 `from app.conversation_schemas import TimelineMessageOut, PaginationOut, ConversationMessagesOut`（并 re-export 给测试）。

### 1.3 Router 收敛为「HTTP 参数 + 返回」薄壳 + 回滚 facade

`routers/conversations.py` 保留 `get_conversation_messages` handler，`@router.get` 路径、`Query` 参数、`response_model=ConversationMessagesOut` **完全不变**：

```python
import os  # 如尚未导入

@router.get("/api/conversations/{conversation_id}/messages", response_model=ConversationMessagesOut)
def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(20, ge=1, le=100, description="Max messages to return"),
    before: Optional[str] = Query(None, description="..."),
    mode: Optional[str] = Query(None, description="..."),
    staff_id: Optional[str] = Query(None, description="..."),
    contact_id: Optional[str] = Query(None, description="..."),
    conversation_type: Optional[str] = Query(None, description="..."),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
):
    _, tenant_id = auth
    if os.environ.get("WEARCHIVE_LEGACY_TIMELINE"):
        return _resolve_timeline_page_legacy(
            db, tenant_id, conversation_id, limit=limit, before=before,
            mode=mode, staff_id=staff_id, contact_id=contact_id,
            conversation_type=conversation_type,
        )
    return resolve_timeline_page(
        db, tenant_id, conversation_id, limit=limit, before=before,
        mode=mode, staff_id=staff_id, contact_id=contact_id,
        conversation_type=conversation_type,
    )
```

- **回滚 facade（短期措施）**：将旧 `get_conversation_messages` 内联实现**逐字**保留为 `conversation_timeline._resolve_timeline_page_legacy(...)`（改造前代码快照，非重实现），并在 handler 中通过环境变量 `WEARCHIVE_LEGACY_TIMELINE` 切换。默认走新 service；线上若发现回归，置 `WEARCHIVE_LEGACY_TIMELAIN=1` + 重启即可**即时回退（无需重新部署）**。该 facade 明确标注 `@deprecated`，待新 service 在 prod 稳定后由后续清理任务移除。legacy 与 new 两条路径都必须通过 §2 的全部 timeline/revoke/pagination/structured/media/tenant 测试。
- **re-export 契约（硬性）**：router 顶部必须 re-export 以下所有被测试或 media 路由引用的符号（沿用 `conversation_membership` 的 re-export 惯例，避免行为漂移与测试破损）：
  `_resolve_entity_context`、`_entity_context_query_string`、`_load_media_files_map`、`_RevocationMaps`、`_load_revocations_map`、`_ensure_aware`、`_datetime_to_epoch_ms`、`_encode_message_cursor`、`_decode_message_cursor`、`_project_public_structured_fields`、`_enrich_nested_media_fields`、`_build_nested_media_descriptor`、`_load_media_files_by_sdkfileid_map`、`_thumbnail_timeline_fields`、`TimelineMessageOut`、`PaginationOut`、`ConversationMessagesOut`（及其配套常量 `INTERNAL_STRUCTURED_FIELD_KEYS` / `PUBLIC_STRUCTURED_FIELD_ALLOWLIST`）。
  原因：以下测试从 `app.routers.conversations` 直接 import 这些符号——`test_rnd_210_msgtype_and_card.py`（`_project_public_structured_fields`）、`test_rnd_226_nested_entity_context.py` 与 `test_nested_media_access.py`（`_build_nested_media_descriptor`、`_enrich_nested_media_fields`）、`test_tenant_media_access.py`（`_load_media_files_map`）。任一缺失即 FAIL。
- 删除 router 中已迁出的函数定义与三模型 schema 定义；保留 `MediaAccessNoStoreMiddleware`、media access 路由（`get_message_media` / `get_message_media_access` / `get_nested_message_media` / `get_nested_message_media_access`）、以及 `_resolve_authorized_media` / `_resolve_authorized_nested_media` / `_resolve_variant_serve_ref` / `_resolve_servable_backend_and_ref` / `_with_variant_thumb` / `_find_nested_media_ref` / `_validate_nested_media_path` 等 media 授权 helper（均属 RND-221，本任务**严禁**动）。

---

## 2. 执行步骤（严格：复现/基线 → 实现 → 验证）

### 阶段一：复现 / 基线（先锁定现状，禁止先改实现）

- 跑现有等价测试，确认**基线全绿**（记录用例数）：
  - `backend/tests/test_revoke_timeline_api.py`（timeline 分页 + revoke 折叠权威锚点）
  - `backend/tests/test_revoke_frontend_render.py`
  - `backend/tests/test_admin_auto_load_older.py`（分页 UI）
  - `backend/tests/test_rnd229_focus_locate.py`
  - `backend/tests/test_rnd_206_qa_fixes.py` / `test_rnd_206_rich_media.py` / `test_rnd_206_top_level_image.py`
  - `backend/tests/test_rnd_207_thumbnail_frontend.py`
  - `backend/tests/test_rnd_210_msgtype_and_card.py`（structured projection + card）
  - `backend/tests/test_nested_media_access.py`（nested media enrichment）
  - `backend/tests/test_rnd_226_nested_entity_context.py`（nested entity context）
  - `backend/tests/test_staff_seats.py`（direct/group 碰撞 —— staff collision 权威锚点）
  - `backend/tests/test_tenant_media_access.py`（timeline serializer batch lookup + 租户隔离）
  - `backend/tests/test_tenant_isolation.py`
  - `backend/tests/test_http_contract.py`（路由计数基线）
- **query 数基线（建议）**：用 query-counter fixture（包 `Session.execute` 或 `engine` `before_cursor_execute` 事件）记录 `GET /api/conversations/{id}/messages` 当前 query 数，写入 Linear 评论作 baseline。等价重构，不写 RED 测试——改为「先确认基线全绿 + query 基线」，再动代码。

### 阶段二：实现（最小改动，严守 §1 + 等价铁律）

- 按 §1.2 迁 schemas；按 §1.1 建 `conversation_timeline.py`（含 `_resolve_timeline_page` 与所有被迁移 helper）；按 §1.3 收敛 router + 加回滚 facade。
- **函数对象同一性**：service 与 router 对 membership 逻辑共用 `app.conversation_membership` 的**同一函数对象**；`_is_valid_roomid` 单一定义于 `conversation_membership.py`。
- 确保 `get_conversation_messages` handler 之外**无任何聚合/投影/分页/revoke 折叠逻辑残留**（供 §2 阶段三 C9 代码审查）。

### 阶段三：验证（不达标不收工）

- 阶段一等价测试必须**全绿**（revoke_timeline / revoke_frontend / auto_load_older / rnd206*/rnd207 / rnd210 / nested_media_access / rnd226 / staff_seats / tenant_media_access / tenant_isolation）。
- **回滚 facade 双路径**：默认（新 `resolve_timeline_page`）与 `WEARCHIVE_LEGACY_TIMELINE=1`（legacy）均通过同一组 timeline/revoke/pagination/structured/media/tenant 测试（至少 smoke 这组合，证明 facade 等价）。
- **query 数不恶化**：改造后 `GET /api/conversations/{id}/messages` query 数 ≤ baseline（用 counter fixture 复测）。
- **路由数/路径不变**：`test_http_contract.py` 路由计数不变；`/api/conversations/{conversation_id}/messages` 仍在，`response_model` 为 `ConversationMessagesOut`。
- 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
- 浏览器 smoke（项目既有 Playwright / 手动）：`/admin` 控制台某会话的 timeline——消息顺序、翻页（load older）、revoke 折叠、图片/表情缩略图、nested media 节点、名片显示名与改造前一致；确认 RND-159 内联下拉、RND-229 跳转等无关功能未被破坏。

---

## 3. 硬性约束（不可违反）

- **行为等价优先**：MOVED not reimplemented；排序/分页契约、direct/group 碰撞 resolution、revoke 折叠、nested media 投影契约、structured 字段剥离（sdkfileid / audio 白名单）全部不变；tenant scoping 不变。
- **不改 media access 路由与授权 helper**（RND-221 范围）——包括 `_resolve_authorized_media`、`_resolve_authorized_nested_media`、四个 media 端点及其 schema `MediaAccessOut`/`NestedMediaAccessOut`。
- **不做 DB-level 分页性能优化**（非目标），但保证 query 数不恶化。
- **路由数/路径/`Query` 参数/`response_model` 不变**；不新增端点。
- **`tenant_id` 永远来自 `get_current_user`**；绝不接受请求传入 tenant；entity context（mode/staff_id/contact_id/conversation_type）绝不削弱租户隔离。
- **re-export 契约**：所有被测试或 media 路由引用的迁移符号必须从 router re-export，禁止双定义/行为漂移。
- 回滚 facade 仅为短期安全措施，legacy 路径不得偏离 new 路径的契约。
- 不改动前端/视觉令牌；不引入后台进程；假设开发服务器已在运行，命令前台运行。
- 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit` / `push`**（需用户显式授权）。

---

## 4. 收尾动作

- 在 Linear 把 RND-220 状态保持 `In Progress`（或保持待用户决定）；写一条评论：改动文件/函数级清单（router 收敛了哪些、service/schemas 模块新增了哪些、`_is_valid_roomid` 移入 `conversation_membership.py`）+ 等价测试全绿证据 + **query baseline 对比**（改造前 vs 改造后数字）+ `make verify` 结论 + 回滚 facade 说明（env 开关 `WEARCHIVE_LEGACY_TIMELINE`、legacy 路径）+ 对「非目标（media access 路由/授权 helper/MediaAccessOut·NestedMediaAccessOut 未触碰）」的重申。
- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 若收敛中发现某 helper 的调用面比预期更广（如还有别处引用已迁出函数），在评论中**如实标注边界**，沿用 re-export 惯例解决，不私自扩展范围。
