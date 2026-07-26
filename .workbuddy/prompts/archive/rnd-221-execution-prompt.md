# RND-221 执行提示词（开发 / 实现）

> 用途：粘贴给实现智能体（按 `DEV_AGENT_RULES.md` 的 Claude Code 实现角色），由其独立完成 RND-221。
> 验收由独立 QA 智能体按 `.workbuddy/prompts/rnd-221-qa-prompt.md` 复验。
> 本任务是**行为保持的结构性重构**：把 top-level / nested media 的「授权、provider resolution、descriptor、bytes response、error taxonomy」统一为单一 service，并把 4 条 media 端点从 `conversations.py` 抽到新 router。**不新增功能、不改对外行为。**

---

## 0. 任务与来源

- **Linear 工单**：RND-221「提取统一 Media Access Service 与 Router」，优先级 P2，父任务 RND-212，负责人 Haisu Zuo。
- **在重构链中的位置**：父任务 **RND-212**（渐进式重构为 AI 友好的模块化单体）；**阻塞 RND-223**（引入 App Factory 与分域 Typed Settings）——RND-221 必须先于 RND-223 合入。链：`RND-218 → {RND-219, RND-220, RND-221} → RND-222 → RND-223 → RND-224`。
- **目标（工单原文）**：统一 top-level/nested media 的**授权、provider resolution、descriptor、bytes response 与 error taxonomy**，并将媒体 endpoint 从 conversation router 中提取。
- **前置条件（工单原文）**：RND-207 必须先稳定完成；nested entity-context Bug 单独修复后再开始。
- **非目标（工单原文）**：不增加媒体格式，不改 signed URL TTL，不改变公开 URL / schema / cache behavior。
- **验收标准（工单原文）**：全部 media、Qiniu、nested、tenant、cache-control 测试通过；授权先于 provider 调用；top-level / nested 保持各自公开 schema；可通过 router registration 回切旧实现。

### 0.1 现状架构（精确，文件 `backend/app/routers/conversations.py`）

4 条 media 端点全部在 `conversations.py`（行号为当前 `main` post-RND-216/217；实施时以函数体边界为准，不要按行号硬搬）：

| # | 函数 | 装饰器 / 行 | 路由 |
|---|-------|------|------|
| 1 | `get_message_media` | `~2410` | `GET /api/conversations/{conversation_id}/messages/{msgid}/media` |
| 2 | `get_message_media_access` | `~2558` | `GET /api/conversations/{conversation_id}/messages/{msgid}/media/access` |
| 3 | `get_nested_message_media` | `~2755` | `GET /api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}` |
| 4 | `get_nested_message_media_access` | `~2872` | `GET /api/conversations/{conversation_id}/messages/{msgid}/nested-media/{item_path}/access` |

关键支撑逻辑（全部在 `conversations.py`，均带「统一」注释）：

- 授权：`_resolve_authorized_media`（2206，top-level，按 `archive_message_id` 查 `MediaFile`）；`_resolve_authorized_nested_media`（471，nested，按 `sdkfileid` 查 + `_validate_nested_media_path` / `_find_nested_media_ref`）。两者授权序列完全一致（membership → msgtype gate → 行级查找 → tenant 二次校验），且**都在任何 storage provider / 签名调用之前完成**。
- Descriptor 模型：`MediaAccessOut`（1459，含内部 `media_id`）；`NestedMediaAccessOut`（1483，**故意**不含 `media_id`，用 `mime_type`/`filename` 命名——RND-200 安全修复，nested 绝不暴露内部 DB id）。
- nested 节点 descriptor：`_build_nested_media_descriptor`（588，被 timeline 路由 `get_conversation_messages` 调用，构建每节点 `access_url`）。
- 字节响应：top-level（2517–2551）与 nested（2837–2865）逐字节相同——local 走 `FileResponse`，Qiniu 走 `Response` + 统一异常→HTTP 映射（`MediaObjectNotFound→404` / `MediaStorageUnavailable→503` / `MediaStorageOperationError→502`）。
- 错误信息分类唯一真源在 `app/media_storage.py:52–69`（`MediaObjectNotFound` / `MediaStorageUnavailable` / `MediaStorageConfigurationError` / `MediaStorageOperationError`）；router 只做翻译，绝不返回/记录原始异常文本或 signed URL。
- tenant 派生：全部来自 `get_current_user` 会话（`tenant_id` 只来自 session，绝不接受请求参数）。
- Cache-Control：字节代理路由 `private, max-age=3600`（`_MEDIA_PROXY_CACHE_MAX_AGE=3600`，2316）；access 路由 `no-store`——由 `MediaAccessNoStoreMiddleware`（149）+ 路由体 `response.headers["Cache-Control"]="no-store"` 双重保证，且中间件 regex `_MEDIA_ACCESS_PATH_RE`（144）按**路径**匹配，与定义它的 router 无关。
- 注册：`app/main.py:21–68`——`MediaAccessNoStoreMiddleware` 从 `app.routers.conversations` import 并在 63 行 `add_middleware`；`include_router(conversations_router)` 在 65 行。

> 行号仅作切片指引；实施时以函数体边界为准（handler 签名、装饰器、`return` 整体搬移），不要按行号硬编码。

---

## 0.5 前置条件（硬门，先确认，不满足则停下）

> 任何一条不满足，**停下并在 Linear 评论说明依赖未满足，等待合并后再开工**，不要自行补做前置任务。

1. **RND-226（nested entity-context Bug）必须先 merge 到 `origin/main`。** 工单原文明确要求「nested entity-context Bug 单独修复后再开始」。当前 RND-226 **仍是 Open**（未合入）。nested 两条路由的 `mode`/`staff_id`/`contact_id`/`conversation_type` 参数、`_entity_context_query_string`、`_enrich_nested_media_fields`、`media_context_qs`、`test_rnd_226_nested_entity_context.py` 都是 **RND-226 的领地**。RND-221 启动前必须确认 RND-226 已合入 `main`，且本任务对这部分**逐字保留、绝不改动**。
2. **RND-218 必须先 merge 到 `origin/main`**。它是整条链 `{219,220,221}` 的前置，且重构了 `main.py` 的 router 注册面与 `conversations.py` 下游结构（新增 `routers/web.py`、`routers/messages.py`、`schemas/`、`app/services/`）。
3. **当前工作树存在 RND-217 的未提交改动**（`backend/app/main.py`、`backend/app/web/static/console/*`、`backend/tests/test_http_contract.py` 等）。**不要基于这些未提交改动开工，也不要自行 commit 它们。** 先请 Haisu 把 RND-217（及其后 RND-218、RND-226）经验证改动 commit/push 到 `main`，确认 `git status` 干净后再开始。
4. **开工方式**：从最新的 `origin/main`（RND-218 + RND-226 合并后）rebase，在干净的 main 上直接实现（参考 `DEV_AGENT_RULES.md`：直接在 main 上改，不建 task branch，除非 Haisu 明确要求）。
5. **与 RND-219 的耦合红线**：RND-219（提取 Conversation Schemas 与 Listing Service）同样从 `conversations.py` 抽取，**两者都会改 `conversations.py`**。本任务与 RND-219 **绝不可由两个引擎并发执行**（会产生文本冲突）。顺序由 Haisu 决定，但**必须串行**：若 RND-219 已先合入 `main`，本任务 rebase 到该 `main` 后开工；若本任务先合入，则 RND-219 后开工。无论哪种顺序，开工时 `conversations.py` 必须是「已含另一端抽取、工作树干净」的状态。

---

## 1. 精确范围（必须移出 / 必须保留 / 目标文件）

### 1.1 目标文件（本任务新建）

- `backend/app/routers/media.py` —— 新 `router = APIRouter()`，承载 4 条 media 端点（薄封装，只做参数校验 → 调 service → 返回）。同时迁入 `MediaAccessNoStoreMiddleware` 与 `_MEDIA_ACCESS_PATH_RE`。
- `backend/app/services/media_access.py` —— 统一 `MediaAccessService` / 纯函数：`resolve_authorized_media`、`serve_media_bytes`、`build_access_descriptor`，以及被 media 端点独占或需与 timeline 共享的 helper（见 §2.4）。

> 目录约定对齐 RND-219：service 落 `app/services/`，schema 保持原位（见 §2.3）。`app/services/` 若 RND-219 已先行则已存在，否则本任务创建它（仅放纯逻辑，绝不 import `app.main` 或任何 router 模块）。

### 1.2 必须从 `conversations.py` 移出的内容

- 4 条端点定义：`get_message_media` / `get_message_media_access` / `get_nested_message_media` / `get_nested_message_media_access`（含装饰器、Query 参数、`response_model`）。
- 2 个授权 resolver：`_resolve_authorized_media`（2206）、`_resolve_authorized_nested_media`（471）——统一为一个（见 §2.1）。
- 字节响应块（2517–2551、2837–2865）——统一为一个 `serve_media_bytes`（见 §2.2）。
- descriptor 分支块（2673–2751、2965–3045）——统一为一个 `build_access_descriptor`（见 §2.3）。
- `MediaAccessNoStoreMiddleware` 类（149–161）+ `_MEDIA_ACCESS_PATH_RE`（144–146）——迁入 `routers/media.py`。
- nested 路径语法 helper：`_validate_nested_media_path`（419）、`_find_nested_media_ref`（443）、`_NESTED_MEDIA_PATH_RE` / `_MAX_PATH_SEGMENTS` / `_MAX_PATH_LENGTH`（391–392）、`_NESTED_MEDIA_STATUS_BY_FILE_STATE`（411）。
- nested 节点 descriptor：`_build_nested_media_descriptor`（588）——见 §2.4（与 timeline 共享，需改 timeline 路由 import）。
- 变体/缩略图 helper：`_resolve_variant_serve_ref`（2350）、`_with_variant_thumb`（2319）、`_thumbnail_timeline_fields`（2326）——若仅被 media 端点使用则迁入 service；若也被 timeline 使用则迁入 service 并更新 timeline 路由 import（逻辑不变）。

### 1.3 必须保留在 `conversations.py`（显式不在本期范围）

- timeline 路由 `get_conversation_messages` 自身逻辑——**只改它对上述被迁出 helper 的 import 来源**，绝不改其查询/聚合/投影逻辑（那是 RND-220 的领域）。
- `MediaAccessOut` / `NestedMediaAccessOut` 模型定义（1459–1514）——**留在 `conversations.py` 或迁入 `app/schemas/media.py` 均可，但字段名/类型/顺序一字不改**（见 §2.3 与 §5）。若迁入 schema 模块，更新两处 import 即可。
- 所有非 media 端点、membership helper（`_fetch_conversation_messages` 等）、listing helper（RND-219 领地）。

### 1.4 注册 / 回切（满足「router registration 回切旧实现」）

- 在 `app/main.py` 顶部（既有 `from app.routers.conversations import MediaAccessNoStoreMiddleware` 处）改为从 `app.routers.media` import `MediaAccessNoStoreMiddleware` 与 `router as media_router`；在 `include_router(conversations_router)` 之后追加 `app.include_router(media_router)`。
- 从 `conversations.py` **彻底删除**被迁走的 4 条端点 + 2 个 resolver + 字节/descriptor 块 + middleware 类 + 上述 helper + 仅被它们使用的 import。**严禁在 `conversations.py` 留任何副本**——否则 `test_router_count` / `test_routers_are_registered` 会因重复注册而失败。
- **回切路径**：因为路由**路径完全不变**、且全部 media 逻辑现已收敛到 `app/routers/media.py` + `app/services/media_access.py`，回切 = `git revert` 本次合并（或：移除 `include_router(media_router)` 一行 + 从 history 恢复 `conversations.py` 的 media 块 + 还原 `main.py` 的 middleware import）。**关键不变量**：`main.py` 本次唯一的结构变化就是「新增一行 `include_router(media_router)` + middleware import 改来源」；路由路径零变化，故回切是干净的、单点的。在 Linear 评论中记录该回切步骤。

---

## 2. 统一逻辑（4 类重复 → 1 个 service）

### 2.1 统一授权 resolver

合并 `_resolve_authorized_media`（2206）与 `_resolve_authorized_nested_media`（471）为**单一**函数：

```python
def resolve_authorized_media(
    db: Session,
    conversation_id: str,
    msgid: str,
    tenant_id: str,
    *,
    item_path: Optional[str] = None,        # None → top-level；非 None → nested
    mode: Optional[str] = None,
    entity_id: Optional[str] = None,
    conversation_type: Optional[str] = None,
) -> "MediaAuthResult":   # 含 msg, media_file, ref(仅 nested)
```

- `item_path is None` → top-level 路径：按 `(tenant_id, archive_message_id)` 查 `MediaFile`，**不做**路径校验、不解析 `sdkfileid`。
- `item_path is not None` → nested 路径：先 `_validate_nested_media_path`（400）、`_find_nested_media_ref`（404）、再按 `(tenant_id, sdkfileid)` 查 `MediaFile`。
- **授权序列（两种路径完全一致，逐字保留）**：`_fetch_conversation_messages(db, conversation_id, tenant_id, mode=, entity_id=)` → 消息不存在 404 → `conversation_type` 不匹配 400 → `msgtype` 不在 `SERVABLE_MEDIA_MSGTYPES` / `NESTED_MEDIA_MSGTYPES` 404 → `MediaFile.tenant_id == tenant_id` 二次校验 → `download_status != "downloaded"` 404。
- 该序列**必须在任何 storage provider 实例化、URL 构建、签名调用之前完成**（工单验收「授权先于 provider 调用」）。两个端点现在共用这一个函数，故安全修复自动同时覆盖。
- `tenant_id` 一律来自 session（`get_current_user`），绝不接受请求参数。`mode`/`entity_id` 原样转发进 `_fetch_conversation_messages`（与 timeline 路由一致，不削弱 tenant 隔离）。
- Qiniu signed-URL 分支的 `object_key_tenant_prefix_matches(serve_ref, tenant_id)` 校验保留在 descriptor 构建阶段（见 §2.3），不在授权 resolver 内。

### 2.2 统一字节响应

合并两处逐字节相同的字节响应块为单一：

```python
def serve_media_bytes(
    provider: MediaStorageProvider,
    serve_ref,
    effective_backend: Optional[str],
) -> Response:
    # local → FileResponse(path, media_type=detect_media_content_type_for_ref)
    # Qiniu → provider.read_bytes(serve_ref) → Response(content=..., media_type=...)
    # 异常映射：MediaObjectNotFound→404 / MediaStorageUnavailable→503 / MediaStorageOperationError→502
    # 统一设置 Cache-Control: private, max-age=_MEDIA_PROXY_CACHE_MAX_AGE
```

两个 `get_*_media` 端点调用它；`provider` 来自 `get_media_storage_provider(effective_backend)`（`effective_backend` 来自 `MediaFile.storage_backend` 列，绝不用部署级默认——RND-174 的混合存储安全约束）。

### 2.3 统一 descriptor 构建（保留两个独立模型）

合并两处逐字节相同的 local/Qiniu 分支（含 `object_key_tenant_prefix_matches` 校验、signed-URL TTL）为单一 `build_access_descriptor(...)`，返回一个公共 dict；两个端点各自用**独立的** Pydantic 模型包装：

- top-level：`response_model=MediaAccessOut`（**保留 `media_id` 字段**）。
- nested：`response_model=NestedMediaAccessOut`（**绝不加 `media_id`**；保留 `mime_type`/`filename` 命名）。

> **红线**：`MediaAccessOut` 与 `NestedMediaAccessOut` 必须保持为**两个独立模型**，字段名/类型/顺序与改造前完全一致。`NestedMediaAccessOut` 不得暴露任何内部 DB id（RND-200 安全修复）。两个模型都从同一个 `build_access_descriptor` dict 取值，仅是「是否带上 media_id」。

### 2.4 共享 helper 迁移规则（防与 RND-220 冲突）

- 对 §1.2 每个待迁出 helper，**先用 grep 查全部调用方**：
  - 仅被 4 条 media 端点使用 → 迁入 `app/services/media_access.py`。
  - 同时被 timeline 路由（`get_conversation_messages`）使用（如 `_build_nested_media_descriptor`、`_resolve_variant_serve_ref` 等）→ 迁入 `app/services/media_access.py`，并**只改 timeline 路由的 import 来源**，逻辑一字不改（这是给 RND-220 的单一真源，不要预支 RND-220 的提取）。
- `MediaAccessOut` / `NestedMediaAccessOut` 见 §1.3：可留 `conversations.py` 或迁入 `app/schemas/media.py`，字段不变。
- 错误信息分类**保持** `app/media_storage.py` 为唯一真源；service 只 `import` 并转成 `HTTPException`（消息脱敏，绝不记录/返回原始异常文本或 signed URL——沿用 `redact_signed_url_for_log` 约定）。

---

## 3. 非目标（严禁）

- 不增加任何媒体格式 / 新增 msgtype 处理。
- 不改 signed URL TTL（`compute_signed_url_deadline` 窗口不变）。
- 不改公开 URL / schema / cache behavior：
  - 路由路径**一字不改**。
  - `MediaAccessOut` / `NestedMediaAccessOut` 字段名/类型/顺序**一字不改**。
  - Cache-Control：`private, max-age=3600`（字节代理）、`no-store`（access，含 401/错误路径）**不变**。
  - top-level 与 nested 保持**各自**公开 schema（两模型不合并，nested 不暴露 `media_id`）。
- **不碰 RND-226 的 nested entity-context 传播**：nested 路由的 `mode`/`staff_id`/`contact_id`/`conversation_type` 参数、`_entity_context_query_string`、`_enrich_nested_media_fields`、`media_context_qs`——逐字保留，本任务只负责把它们「原样随端点搬到新 router」，不改语义。
- 不改 RND-207 的 origin signed-URL 行为（无 Qiniu CDN；signed URL 来自校验过的 HTTPS origin 域名）。
- 不引入 DB migration / schema 变更；不碰 CI、企业微信企业名变更、WeCom 回调逻辑。
- 不引入新第三方依赖。
- 不自行 `git commit` / `push` / 开 PR（见 §6）。

---

## 4. 执行步骤（严格按顺序）

### 阶段一：探查与锁定现状（先写/确认 characterization，不改实现）

1. 不修改实现代码。先确认下列**当前行为**测试全绿（作为行为基线）：
   - `tests/test_media_access_descriptor.py`、`tests/test_media_access_cache_control.py`、`tests/test_nested_media_access.py`、`tests/test_tenant_media_access.py`、`tests/test_qiniu_media_serving.py`、`tests/test_qiniu_storage.py`、`tests/test_qiniu_provider_factory.py`、`tests/test_qiniu_https_domain.py`、`tests/test_signed_url_window.py`、`tests/test_generic_media_serving.py`、`tests/test_media_download.py`、`tests/test_media_classification.py`、`tests/test_media_signature_detection.py`、`tests/test_media_thumbnails.py`、`tests/test_thumbnail_pipeline.py`、`tests/test_thumbnail_media_access.py`、`tests/test_staff_seats.py`、`tests/test_http_contract.py`、`tests/test_rnd_226_nested_entity_context.py`、`tests/test_rnd_207_thumbnail_frontend.py`、`tests/test_rnd_206_top_level_image.py`、`tests/test_rnd_206_rich_media.py`、`tests/test_rnd_206_qa_fixes.py`。
2. 运行确认**全绿**，记录（若有缺口，先补 characterization 断言，不实现）。

### 阶段二：建支撑模块（零行为变化）

3. 新建 `backend/app/services/media_access.py`，放入统一函数 `resolve_authorized_media` / `serve_media_bytes` / `build_access_descriptor` + 按 §2.4 规则迁出的 helper。**逻辑逐字保留**，仅做「同模块内函数调用」的机械搬迁。
4. （可选）若把 `MediaAccessOut`/`NestedMediaAccessOut` 迁入 `app/schemas/media.py`，同步更新 `conversations.py` 与新 router 的 import。

### 阶段三：建 `routers/media.py` + 收敛 `conversations.py`

5. 新建 `backend/app/routers/media.py`：迁入 `MediaAccessNoStoreMiddleware` + `_MEDIA_ACCESS_PATH_RE`，定义 `router = APIRouter()`，把 4 条端点改为**薄封装**（参数校验 → `resolve_authorized_media` → `serve_media_bytes` / `build_access_descriptor` → 返回 `response_model`）。`mode`/`staff_id`/`contact_id`/`conversation_type`/`variant` 的 400 分支、tenant 派生、no-store header 全部**逐字保留**于对应端点。
6. 从 `conversations.py` **彻底删除** §1.2 列出的全部内容（4 端点 + 2 resolver + 字节/descriptor 块 + middleware 类 + helper + 仅被它们使用的 import）。确认 `conversations.py` 不再含任何 media 端点定义。
7. 若 §2.4 有 helper 被 timeline 路由共用，更新 `get_conversation_messages` 的 import 来源（**逻辑不变**）。

### 阶段四：注册 + 自测（不达标不收工）

8. 在 `app/main.py` 改 `MediaAccessNoStoreMiddleware` import 来源为 `app.routers.media`，并 `app.include_router(media_router)`（在 `conversations_router` 之后）。确认 `main.py` 仅此一处结构变化。
9. 先跑最关键契约测试：
   ```bash
   cd backend && python -m pytest tests/test_http_contract.py -q
   ```
   必须全绿，尤其 `test_router_count`（断言总数 == 33，post-RND-218；移动 ≠ 新增，变 34 即证明 `conversations.py` 留了副本）、`test_route_snapshot_with_real_model_names`（4 条 media 路由的 path / methods / `response_model` / `response_class` 完全一致）、`test_routers_are_registered`（路径集合不增不减）。
10. 跑本期相关回归套件：
    ```bash
    python -m pytest tests/test_media_access_descriptor.py tests/test_media_access_cache_control.py tests/test_nested_media_access.py tests/test_tenant_media_access.py tests/test_qiniu_media_serving.py tests/test_qiniu_storage.py tests/test_qiniu_provider_factory.py tests/test_qiniu_https_domain.py tests/test_signed_url_window.py tests/test_generic_media_serving.py tests/test_media_download.py tests/test_media_classification.py tests/test_media_signature_detection.py tests/test_media_thumbnails.py tests/test_thumbnail_pipeline.py tests/test_thumbnail_media_access.py tests/test_staff_seats.py tests/test_rnd_226_nested_entity_context.py tests/test_rnd_207_thumbnail_frontend.py tests/test_rnd_206_top_level_image.py tests/test_rnd_206_rich_media.py tests/test_rnd_206_qa_fixes.py -q
    ```
11. 收口跑 `make verify`（lint-diff + typecheck + build + 全量 pytest），确认全绿、无 unused import / 循环 import 告警。

---

## 5. 兼容性契约（不可违反，验收据此判定）

- **路由总数 == 33**（post-RND-218，`tests/test_http_contract.py` `test_router_count`）。移出 ≠ 新增；若变化即证明 `conversations.py` 留了副本或重复注册 → 立即修。
- **路由快照逐字段相等**（`test_route_snapshot_with_real_model_names`）：4 条 media 路由的 path / methods(GET) / `response_model`（`MediaAccessOut` / `NestedMediaAccessOut` / 无）/ `response_class` 与改造前完全一致；路径集合与 `test_routers_are_registered` 期望完全一致（不增不减、不重名）。
- **授权先于 provider**：两个端点共用单一 `resolve_authorized_media`，且其中**不实例化任何 provider / 不构建 URL / 不签名**——仅 finished 授权后才进入 `serve_media_bytes` / `build_access_descriptor`。
- **租户隔离不变**：`tenant_id` 一律来自 `get_current_user` 会话；所有 `MediaFile` 查询带 `tenant_id`；Qiniu signed-URL 前 `object_key_tenant_prefix_matches` 校验保留；跨租户不可见彼此数据。
- **错误分类不变**：`MediaObjectNotFound→404` / `MediaStorageUnavailable→503` / `MediaStorageOperationError→502` / 配置错误→500；原始异常文本与 signed URL 绝不返回/记录。
- **Cache-Control 不变**：字节代理 `private, max-age=3600`；access 路由 `no-store`（含 401/错误路径，由 `MediaAccessNoStoreMiddleware` 按路径保证）。中间件 regex 因路径不变而继续生效。
- **双 schema 独立**：`MediaAccessOut` 含 `media_id`；`NestedMediaAccessOut` 不含 `media_id`、用 `mime_type`/`filename`；字段名/类型/顺序不变。
- **RND-226 传播不变**：nested 路由的 `mode`/`staff_id`/`contact_id`/`conversation_type` 参数、`_entity_context_query_string`、`media_context_qs` 行为与改造前逐字一致（仅随端点搬到新 router）。

---

## 6. 硬性约束（实现 agent 自身也要守）

- **行为等价优先**：这是重构，不是功能开发。任何输出字段、状态码、tenant 边界、缓存头的变化都视为回归。
- **授权先于 provider**：硬验收点，单一 resolver 强制保证。
- **双 schema 不可合并**：nested 绝不暴露内部 DB id（RND-200 安全修复）。
- **耦合红线**：只搬 media 端点独占或需与 timeline 共享的 helper（按 §2.4 grep 规则）；timeline 路由逻辑不改；不预支 RND-220 提取；不与 RND-219 并发改 `conversations.py`。
- 不引入后台进程；假设开发服务器已在运行；命令前台运行。
- 循环依赖防控：新建 `routers/media.py` / `services/media_access.py` / `schemas/media.py` **不得 import `app.main` 或任何 router 模块**；service 只依赖 `app.media_storage` / `app.media_classification` / `app.db.*` / `app.auth` 等纯模块。
- 参考 `DEV_AGENT_RULES.md` 的 AI agent 工作流；**不要自行 `git commit`/`push`**（需用户显式授权）。也不要触碰工作区里 RND-217 的未提交改动。
- **必须满足 §0.5 前置硬门**（RND-226、RND-218 已 commit 到 `main`、工作树干净、与 RND-219 串行）才可开工。

---

## 7. 收尾动作

- 在 Linear 把 RND-221 状态 Todo → In Progress（如尚未）。
- 写一条评论：迁移了哪些端点到 `routers/media.py`、统一了哪些逻辑到 `services/media_access.py`、是否迁出了 `_build_nested_media_descriptor` 等共享 helper（及 timeline 路由 import 变更）、`main.py` 的注册变化、`make verify` 结论、路由数是否保持 33、`MediaAccessNoStoreMiddleware` 去向、以及**「router registration 回切旧实现」的具体步骤**。
- 不要自动合入/提交；交还用户（Haisu）决策是否 merge（本任务为 RND-212 链一环，且阻塞 RND-223）。
- 若发现某 helper 实际被 timeline 共用而本任务无法干净搬迁，如实记录在 Linear 评论并标记，不强行搬迁制造与 RND-220 的冲突。
