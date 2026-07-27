# RND-219 执行提示词（单人端到端：探查现状 → 提取 → 验证行为等价）

> 用途：粘贴给单一开发智能体（Claude Code / Codex / hy3 皆可），由其端到端跑完 RND-219。
> 工单原文口径：RND-219「提取 Conversation Schemas 与 Listing Service」，P2，父任务 RND-212，阻塞 RND-220。
> **本任务是一个行为保持的结构性重构（纯搬迁：不新增功能、不改行为、不改查询性能），不是 bug 修复。**

---

## 0. 任务与来源
- Linear 工单：**RND-219「提取 Conversation Schemas 与 Listing Service」**，优先级 P2。
- 目标：把 **monitored accounts / contacts / conversation list** 三类的 **schemas（Pydantic 响应模型）、复杂查询、aggregation rules** 从 `backend/app/routers/conversations.py` 中抽取为独立的 service + schema 模块，使 router 收敛为「只处理 HTTP 参数校验与返回」。
- 父任务：**RND-212**（渐进式重构为 AI 友好的模块化单体）。
- 直接前置（必须先合并进 main）：**RND-218**（把 web / legacy message routes 移出 `main.py`，并落地 `routers/web.py`、`routers/messages.py`、`schemas/messages.py` 的新目录约定）。
- 本任务阻塞：**RND-220**（提取 Timeline Resolution 与 Projection Service）——RND-219 必须先完成并合并，RND-220 才能开工，避免两者争夺 `conversations.py`。
- 验收标准（原文）：ordering、group-wins、seat status、display names、tenant isolation 与现状等价；router 只处理 HTTP 参数与返回；query count 不得恶化。

## 1. 前置条件（先确认，不满足则停下）
- 确认 **RND-218 已 commit 并 merge 到 `origin/main`**（不是工作区未提交状态）。
  - 当前工作区存在 RND-217 期间的未提交改动：`backend/app/main.py`、`backend/app/web/static/console/*`、`backend/tests/_rnd216_web_shims.py`、`backend/tests/test_http_contract.py` 等。**不要基于这些未提交改动开工，也不要自行 commit 它们。**
- 开工方式：从最新的 `origin/main`（RND-218 合并后）rebase，在干净的 main 上直接实现（参考 `DEV_AGENT_RULES.md`：直接在 main 上改，不建 task branch，除非 Haisu 明确要求）。
- 若 RND-218 尚未合并，停下并在 Linear 评论说明依赖未满足，等待合并后再开工。

## 2. 提取范围（精确，文件 `backend/app/routers/conversations.py`）

### 2.1 路由端点（改为薄封装）
- `get_monitored_accounts`（装饰器 ~1558）
- `get_contacts`（装饰器 ~1641）
- `get_conversations`（装饰器 ~1662）
重构后三者应只做：参数校验（`mode` / `staff_id` / `contact_id` 的 400 逻辑）→ 调 service → 返回 `response_model`。body 中的查询与聚合逻辑全部下沉。

### 2.2 Schemas → 新建 `backend/app/schemas/listing.py`
- `MonitoredAccountOut`（~1349）
- `ContactOut`（~1358，以实际定位为准）
- `ConversationOut`（~1364，以实际定位为准）及其嵌套子模型（`monitored_account_ids` / `display_name` / `room_display_name` / `latest_sender_display_name` 等字段所在模型）
- 沿用 RND-218 已建立的 `schemas/` 导入与 `response_model` 引用方式；更新 `conversations.py` 的 import。

### 2.3 逻辑 → 新建 `backend/app/services/listing_service.py`
把下列「**仅被上述三个 listing 端点使用**」的 helper / aggregation 抽取为服务的纯函数或方法，接收 `db: Session` 与所需参数，返回与现状逐字段一致的数据结构：
- 座位 / 参与者识别：`_collect_staff_ids`、`_collect_archive_participant_ids`（及 RND-132 座位检测的配套内部函数——精确位置请 grep 定位）
- display name：`_load_display_names`（~233）、`_load_display_names_for_ids`（~105）、`_staff_ids_for_participants`
- 座位排序 / 计数：`_latest_own_participation_time`（~930）、`_count_entity_conversations`（~890）
- 会话列表聚合：`_fetch_compact_messages_for_entity`（~1047）、`_load_recipients_map_compact`（~1139）、`_build_conversation_list`（~1177）、`_derive_conversation_membership`（group-wins 逻辑所在，grep 定位）
- 其余被三端点独占的 compact-projection 配套函数（如 `_compact_entity_messages` 等）

### 2.4 耦合红线（必须遵守）
- **只搬「三端点独占」的 helper**。若某 helper 同时被 timeline 路由（消息时间线、`get_*` 消息列表、cursor 分页、revoke fold、nested media enrichment——即 **RND-220 的领域**）使用，**不要搬**：留在 `conversations.py` 或放到一个内部共享模块（如 `app/services/_shared_listing_deps.py`），并在 Linear 评论标注供 RND-220 消费。禁止在 RND-219 里预支 RND-220 的提取，避免与 RND-220 冲突。
- **不要碰**：timeline cursor `_encode_message_cursor` / `_decode_message_cursor`（~1541-1550）、消息时间线路由、media / timeline 任何逻辑（非目标）。
- 保持 `seat_status` 的 active-first / latest_message_time desc 排序、`group-wins` 的 `_derive_conversation_membership` 规则、`display_name` 解析路径、tenant 过滤（所有查询都带 `tenant_id`）**与现状逐字节等价**。

## 3. 非目标（严禁）
- 不做查询性能优化（非目标明确）：纯搬迁，不重写 SQL、不加索引、不引入缓存。
- 不改 timeline / media 行为（那是 RND-220 / RND-221 的范围）。
- 不新增 / 删除对外字段；response contract（字段名、类型、顺序、OpenAPI schema）必须保持一致。
- 不引入新的第三方依赖。
- 不自行 `git commit` / `push`（见 §6）。

## 4. 执行步骤

### 阶段一：探查与锁定现状（先写/确认 characterization test，不改实现）
- 不修改实现代码。先为三个端点的**当前行为**补/确认 characterization 测试（若 `tests/test_staff_seats.py`、`tests/test_conversation_display_names.py`、`tests/test_contact_sync.py`、`tests/test_tenant_isolation.py`、`tests/test_http_contract.py` 已覆盖，则确认它们绿；若有缺口，补充断言）：
  - monitored accounts：`seat_status` 的 active/history 判定与排序、`conversation_count`。
  - contacts：参与人 − 员工 = 联系人的集合与排序。
  - conversations：ordering（last activity desc）、group-wins（`_derive_conversation_membership`）、`monitored_account_ids` / `display_names` / `room_display_name` / `latest_sender_display_name` 字段。
  - tenant isolation：跨租户不可见彼此数据。
  - HTTP contract：路由、status、response body、OpenAPI 与 `test_http_contract.py` 的 route snapshot 必须保持兼容。
- 运行这些测试，确认**全绿（作为行为基线）**。

### 阶段二：提取（机械搬迁，最小改动）
1. 新建 `app/schemas/listing.py`，把 §2.2 的模型移入，更新 `conversations.py` 的 `response_model` 引用与 import。
2. 新建 `app/services/listing_service.py`，把 §2.3 的 helper 迁入为 service 方法 / 函数；保持函数签名语义、查询顺序、tenant 过滤完全不变。
3. 三个端点改为薄封装，调用 service；保留参数校验与 400 分支（放 router 层）。
4. 为 service 新增可脱离 FastAPI 的单元测试（直接喂 `Session` / fixture），覆盖座位识别、联系人集合、会话列表聚合、group-wins、display name、tenant 过滤。
5. 确认 §2.4 耦合红线：shared helper 未误搬。

### 阶段三：验证（行为等价，不达标不收工）
- 阶段一的 characterization 测试 + 新增 service 单测必须**全绿**。
- 运行既有相关套件（先 `cd backend`）：
  - `python -m pytest tests/test_staff_seats.py tests/test_conversation_display_names.py tests/test_contact_sync.py tests/test_tenant_isolation.py tests/test_http_contract.py -q`
  - 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
- **query count 不恶化**：用 SQL echo / query-count 断言确认三端点搬迁后的 DB 查询次数与搬迁前一致（参照既有 query-count 测试手法；若无现成机制，至少在 Linear 评论逐项核对查询序列）。
- 浏览器 smoke（项目既有 Playwright 脚本）：会话列表、联系人、监控账号页在重构后仍正常渲染。

## 5. 硬性约束
- **行为等价优先**：这是重构，不是功能开发。任何输出字段、排序、状态码、tenant 边界的变化都视为回归。
- **租户隔离**：所有查询必须带 `tenant_id`，不得出现跨租户数据泄漏。
- **耦合红线**：只搬三端点独占逻辑；shared helper 留给 RND-220，详见 §2.4。
- 不引入后台进程；假设开发服务器已在运行；命令前台运行。
- 参考 `DEV_AGENT_RULES.md` 的 AI agent 工作流；**不要自行 `git commit`/`push`**（需用户显式授权）。也不要触碰工作区里 RND-217 的未提交改动。

## 6. 收尾动作
- Linear：RND-219 状态保持 `In Progress`；写一条评论：提取了哪些模型 / 函数到哪些新文件、薄封装后的端点、characterization 测试结果、`make verify` 结论、query-count 核对结论、与 RND-220 的耦合边界标注。
- **不要自动合入 / 提交**，保留给用户（Haisu）人工 merge 关卡；且须等独立 QA agent 验收通过、用户明确许可后才可 commit/push。
- 若发现某 helper 实际被 timeline 共享而本任务无法干净搬迁，如实记录在 Linear 评论并标记，不强行搬迁制造与 RND-220 的冲突。
