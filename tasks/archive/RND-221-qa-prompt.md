# RND-221 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-221 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-221-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-221** 的验收标准（media/Qiniu/nested/tenant/cache-control 测试全过；授权先于 provider；top-level/nested 各保持公开 schema；可经 router registration 回切）+ 代码硬约束。

---

## 0. 验收依据

- **Linear RND-221 验收（必须全过）**：
  - 全部 media、Qiniu、nested、tenant、cache-control 测试通过；
  - 授权先于 provider 调用；
  - top-level / nested 保持各自公开 schema；
  - 可通过 router registration 回切旧实现。
- **非目标不可被破坏**：不增媒体格式、不改 signed URL TTL、不改公开 URL/schema/cache behavior；RND-226 nested entity-context 传播、RND-207 origin signed-URL 行为必须保持改造前。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支，且 **RND-218、RND-226 已在 `main`**（前者是链前置、重构了 `main.py`/`conversations.py` 下游；后者是工单硬门，nested entity-context 必须先修复）。若任一未合入，停下并在 Linear 评论说明，不验收。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动 **前台** 进程后再验（不后台化、不加 `&`）。
4. 拿到开发 agent 评论中的 **route-count 数字**（改造后注册路由总数，预期 == 33，post-RND-218）；若缺失，本 agent 自行用 `test_http_contract.py` 复测对比。
5. 确认 **RND-219 是否已合入 `main`**：若已合入，须一并跑其回归（`test_staff_seats.py`、`test_conversation_display_names.py`、`test_conversation_membership_service.py`、`test_tenant_isolation.py`）；若未合入，本任务不依赖它，但 C13 仅核对未被破坏即可。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 行为等价（直接打 4 条 media 端点）

- **C1 授权先于 provider（代码审查 + 测试）**：`app/services/media_access.py` 中 `resolve_authorized_media` 函数体内**不实例化** `get_media_storage_provider`、不构建 URL、不调用任何签名；两个端点共用该单一 resolver，且 `serve_media_bytes` / `build_access_descriptor` 仅在 resolver 返回后才被调用。现有授权测试（404/400/503/502 路径）全绿即佐证。
- **C2 top-level 字节 + descriptor 等价**：`GET .../media`（local + Qiniu 混合）与 `GET .../media/access` 行为、字段、`access_type` 分支（proxy / signed_url）、`Cache-Control` 与改造前一致（对照 `test_media_access_descriptor.py`、`test_generic_media_serving.py`、`test_rnd_206_top_level_image.py`、`test_rnd_206_rich_media.py`、`test_rnd_206_qa_fixes.py`）。
- **C3 nested 字节 + descriptor 等价**：`GET .../nested-media/{p}` 与 `GET .../nested-media/{p}/access` 的路径校验（400）、不解析 ref（404）、存储等价、descriptor 泄漏检查与改造前一致（对照 `test_nested_media_access.py`）。
- **C4 tenant isolation 等价**：用**另一租户**认证调 4 条 media 端点，断言只能见到本租户数据；请求里即使塞 `tenant_id` 参数也应被忽略（`tenant_id` 只来自 `get_current_user`）。Qiniu signed-URL 的 `object_key_tenant_prefix_matches` 校验仍生效（对照 `test_tenant_media_access.py`、`test_qiniu_media_serving.py`）。
- **C5 Cache-Control 等价**：access 路由（含 401 / 错误路径）一律 `no-store`；字节代理路由 `private, max-age=3600`；中间件 `_MEDIA_ACCESS_PATH_RE` 仍按路径覆盖 access，且**不**影响字节代理路由（对照 `test_media_access_cache_control.py` 的 `no-store` 断言与「`/media` 代理不受影响」断言）。
- **C6 错误分类等价**：`MediaObjectNotFound→404` / `MediaStorageUnavailable→503` / `MediaStorageOperationError→502` / 配置错误→500；原始异常文本与 signed URL 绝不返回/记录（代码审查 `serve_media_bytes` + 现有异常路径测试）。
- **C7 Qiniu 端到端等价**：`GET .../media` 经 Qiniu backend 端到端（tenant 前缀校验、local/Qiniu 混合、origin 域名 signed URL）与改造前一致（对照 `test_qiniu_media_serving.py`、`test_qiniu_storage.py`、`test_qiniu_provider_factory.py`、`test_qiniu_https_domain.py`、`test_signed_url_window.py`）。
- **C8 双 schema 独立（代码 + 行为）**：`MediaAccessOut`（含 `media_id`）与 `NestedMediaAccessOut`（**不含** `media_id`，用 `mime_type`/`filename`）为**两个独立模型**，字段名/类型/顺序与改造前完全一致；`git diff` 确认两模型定义未改（或仅迁移 import 来源）；nested 绝未泄漏内部 DB id（对照 `test_nested_media_access.py` 的 descriptor 泄漏断言）。
- **C9 router registration / 路由数不变**：`test_router_count` 总数 == 33（post-RND-218）；4 条 media 路径仍存在且 `response_model` 分别为 `MediaAccessOut` / `NestedMediaAccessOut` / 无；`test_route_snapshot_with_real_model_names` 的 path/methods/`response_model`/`response_class` 完全匹配；`test_routers_are_registered` 路径集合不增不减；`git diff` 确认 `conversations.py` **已无** media 端点定义（无副本）。
- **C10 回切可行（代码审查 + 文档）**：`app/main.py` 本次**唯一**结构变化 = 「新增一行 `app.include_router(media_router)` + `MediaAccessNoStoreMiddleware` import 来源改为 `app.routers.media`」；4 条 media 路由**路径零变化**；开发评论记录了经 revert 合并（或移除该行 + 从 history 恢复 `conversations.py` media 块）即回切旧实现的具体步骤。落判：回切是单点、干净的。
- **C11 RND-226 nested entity-context 保持（代码 + 行为）**：`test_rnd_226_nested_entity_context.py` 全绿；nested 两条路由仍接受并转发 `mode`/`staff_id`/`contact_id`/`conversation_type`；`_entity_context_query_string` / `media_context_qs` 行为逐字不变（仅随端点搬到新 router）。
- **C12 RND-207 行为保持（代码 + 行为）**：`test_rnd_207_thumbnail_frontend.py` 全绿；Qiniu signed-URL 仍来自校验过的 HTTPS origin 域名（无 CDN）；`variant=thumb` 行为不变（对照 `test_thumbnail_media_access.py`、`test_thumbnail_pipeline.py`、`test_media_thumbnails.py`）。

### 回归（不破坏既有能力）

- **C13 既有 media / 关联回归全绿**（直接 `cd backend` 后跑，命令见 §3）。
- **C14 RND-219 回归（若其已合入 `main`）**：`test_staff_seats.py`、`test_conversation_display_names.py`、`test_conversation_membership_service.py`、`test_tenant_isolation.py` 全绿——确认本任务对 `conversations.py` timeline 路由的 import 源改动未破坏 listing/timeline。
- **C15 `make verify` 全绿**（lint-diff + typecheck + build + 全量 pytest）。
- **C16 无新依赖 / 无 migration / 无 CI 变更（代码审查）**：`git diff` 确认未引入第三方依赖、未改 `pyproject.toml`/`requirements*`、未生成/修改 DB migration、未碰 `.github/`/`deploy/` CI 配置。

---

## 3. 测试方法

- **行为等价 + 回归**：`cd backend` 后跑
  ```bash
  python -m pytest tests/test_media_access_descriptor.py tests/test_media_access_cache_control.py tests/test_nested_media_access.py tests/test_tenant_media_access.py tests/test_qiniu_media_serving.py tests/test_qiniu_storage.py tests/test_qiniu_provider_factory.py tests/test_qiniu_https_domain.py tests/test_signed_url_window.py tests/test_generic_media_serving.py tests/test_media_download.py tests/test_media_classification.py tests/test_media_signature_detection.py tests/test_media_thumbnails.py tests/test_thumbnail_pipeline.py tests/test_thumbnail_media_access.py tests/test_staff_seats.py tests/test_http_contract.py tests/test_rnd_226_nested_entity_context.py tests/test_rnd_207_thumbnail_frontend.py tests/test_rnd_206_top_level_image.py tests/test_rnd_206_rich_media.py tests/test_rnd_206_qa_fixes.py -q
  ```
  - 若 RND-219 已合入 `main`，追加：`tests/test_conversation_display_names.py tests/test_conversation_membership_service.py tests/test_tenant_isolation.py`。
  - 若开发 agent 未交付 route-count 数字，本 agent 用 `test_http_contract.py::test_router_count` 自测改造后总数，并标注「baseline 缺失，本次自测为准」。
- **代码审查项（C1/C6/C8/C9/C10/C11/C12/C16）**：直接读 `git diff` 与 `app/services/media_access.py` / `app/routers/media.py` / `app/main.py` / `app/routers/conversations.py` 头部 import，逐项核对。
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
## RND-221 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 是否含 RND-218/RND-226：是/否 · 是否含 RND-219：是/否 · make verify：通过/失败
route 总数：<n>（预期 33，来源：开发评论/本 agent 自测）

| 编号 | 验收点 | 结果 | 证据（实测/命令/代码位置） |
|------|--------|------|---------------------------|
| C1   | 授权先于 provider | PASS | 代码审查：resolve_authorized_media 无 provider 实例化 |
| C2   | top-level 字节+descriptor 等价 | PASS | test_media_access_descriptor 等全绿 |
| C3   | nested 字节+descriptor 等价 | PASS | test_nested_media_access 全绿 |
| C4   | tenant isolation 等价 | PASS | test_tenant_media_access 等 |
| C5   | Cache-Control 等价 | PASS | test_media_access_cache_control 全绿 |
| C6   | 错误分类等价 | PASS | 代码审查 + 异常路径测试 |
| C7   | Qiniu 端到端等价 | PASS | test_qiniu_* 全绿 |
| C8   | 双 schema 独立 | PASS | git diff 确认两模型未改；nested 无 media_id |
| C9   | router registration/路由数不变 | PASS | test_router_count==33；conversations.py 无副本 |
| C10  | 回切可行 | PASS | main.py 仅一处 include_router 变化；路径零变 |
| C11  | RND-226 entity-context 保持 | PASS | test_rnd_226 全绿 |
| C12  | RND-207 行为保持 | PASS | test_rnd_207 等全绿 |
| C13  | 既有 media 回归 | PASS | pytest 全绿 |
| C14  | RND-219 回归（若已合入） | PASS/N/A | ... |
| C15  | make verify 全绿 | PASS | ... |
| C16  | 无新依赖/migration/CI 变更 | PASS | git diff 确认 |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 非目标（增格式 / 改 TTL / 改 URL-schema-cache / RND-226 / RND-207）确认未触碰 → 视为 PASS 非缺陷。
- 其他观察到的限制：<…>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺 RND-218 或 RND-226）。
```

- 若某条无论如何无法复现（如环境导致 C2–C5 打不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-221 评论贴出（状态保持 In Progress / Todo，交还用户 Haisu 决策合并）。
