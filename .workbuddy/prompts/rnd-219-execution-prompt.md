# RND-219 执行提示词（单人端到端：复现 → 实现 → 验证）

> 用途：粘贴给单一开发智能体（hy3 / Claude Code / Codex 皆可），由其端到端跑完 RND-219。
> 工单：`RND-219「提取 Conversation Schemas 与 Listing Service」`，优先级 P3（Linear 实时状态 **Todo**，assignee Haisu Zuo），父脉络 **RND-212**（渐进式重构为模块化单体），并 **阻塞 RND-220**（Timeline Resolution 与 Projection Service）。
> 本次只做「Listing 侧」抽取；Timeline / Media 属 RND-220 范围，本任务**严禁**触碰。

---

## 0. 任务与来源

- **目标**：将 `monitored accounts`、`contacts`、`conversation list` 的 **schemas（Pydantic 响应模型）**、**复杂查询** 与 **aggregation rules** 从 `backend/app/routers/conversations.py` 中抽取出去，使 router 只保留「解析 HTTP 参数 + 调 service + 返回」的薄壳。
- **验收清单（来自 Linear 工单，逐条必过）**：
  - [ ] `ordering`（(last_message_time, conversation_id) 降序 + RND-158 tie-break）与现状等价
  - [ ] `group-wins`（同一 conv_id 同时被 direct-pair key 与真实 roomid 命中时必须为 group，且与消息处理顺序无关）与现状等价
  - [ ] `seat status`（active/history 排名）与现状等价
  - [ ] `display names`（group 用 room 名、direct 用 contact/staff 名、latest_sender 名）与现状等价
  - [ ] `tenant isolation`（tenant_id 只来自 `get_current_user`，绝不接受请求参数）与现状等价
  - [ ] router 只处理 HTTP 参数与返回（不含聚合/查询逻辑）
  - [ ] query count 不得恶化（与改造前持平，不要求优化）
- **非目标（严禁，避免 gold-plate / 越界）**：
  - 不做查询性能优化（只求 query 数持平，不引入新索引、不重写 SQL）。
  - 不改 timeline / media：`get_conversation_messages`（router ~1756）、`get_message_media`/`get_message_media_access`/`get_nested_message_media`/`get_nested_message_media_access`（~2410/2554/2754/2868）及其用到的 `TimelineMessageOut`/`MediaAccessOut`/`NestedMediaAccessOut`/`PaginationOut`/`ConversationMessagesOut`（~1391–1540）**一律不动**。
  - 不新增路由、不新增端点、不引入后台进程、不改前端/视觉。

### 前置依赖（开工前先确认已在 `main`）
1. **RND-212 已合并**（父任务，模块化单体基础）。
2. **`backend/app/conversation_membership.py` 已存在**（共享 seat 检测服务，本任务直接复用其函数对象，见 §1.1）。
3. **RND-226 / RND-210 已在 `main`**（nested-media entity-context、msg-type card 渲染）——本任务的 router 收敛不得破坏这两块已上线的逻辑；若收敛时误删/误改了相关 import 或函数，停下并标注。
4. 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit` / `push`**。

---

## 1. 精确落点（文件 / 函数级）

### 1.1 新建 Listing Service：`backend/app/conversation_listing.py`（扁平单文件，镜像 `conversation_membership.py` 约定）
从 `routers/conversations.py` **整体 MOVED（非 reimplement）** 以下函数，保持函数体逐字等价：

- `get_monitored_accounts` 的实现体（去掉 `@router.get` 装饰器与 `Depends`，改为 `def list_monitored_accounts(db: Session, tenant_id: str) -> list[MonitoredAccountOut]`）。
- `get_contacts` 实现体 → `def list_contacts(db: Session, tenant_id: str) -> list[ContactOut]`。
- `get_conversations` 实现体 → `def list_conversations(db: Session, tenant_id: str, mode: str, staff_id: Optional[str], contact_id: Optional[str]) -> list[ConversationOut]`（保留 `mode` 校验与 400 语义，但 400 由 service 抛 `HTTPException` 或上移到 router——见 §1.3 决策）。
- 复杂查询 / aggregation：
  - `_build_conversation_list`（router ~1177，aggregation rule，含 group-wins 升级与 RND-158 排序契约）
  - `_count_entity_conversations`（~890）
  - `_latest_own_participation_time`（~930）
  - `_fetch_compact_messages_for_entity`（~1047）
  - `_load_recipients_map_compact`（~1139）
  - `_compact_entity_messages`（~772，被 `_count_entity_conversations` 调用）

**复用（不重定义）`app.conversation_membership` 的同一批函数对象**（与当前 router 的 import 来源完全一致，见 router ~100–115）：
`_collect_staff_ids`、`_collect_archive_participant_ids`、`_load_display_names_for_ids`、`_staff_ids_for_participants`、`_derive_conversation_membership`、`_direct_conv_id`、`_entity_seed_ids`。

**关于 `_load_display_names`（router ~233）**：它被 listing 端点使用（~1584、~1651），**也**被 **out-of-scope 的 timeline** 在 ~1881 调用。处理办法（二选一，必须保证 ~1881 仍可用）：
- 方案 A（推荐，最干净）：将 `_load_display_names` MOVE 进 `conversation_listing.py`，并在 router 顶部 `from app.conversation_listing import ... _load_display_names` 作为 **re-export**（沿用 `conversation_membership.py` 的「router 持有服务模块 re-export」惯例，保证 timeline 的 ~1881 调用与任何测试 import 不破）；
- 方案 B：直接将 timeline 的 ~1881 调用改为 `from app.conversation_listing import _load_display_names`，并同步更新相关测试 import。
无论选 A 还是 B，**不得**在 router 与 service 各定义一份 `_load_display_names`（会漂移）。

### 1.2 新建 Schemas：`backend/app/conversation_schemas.py`（扁平单文件）
从 `routers/conversations.py` 移入以下 **三个** Pydantic 模型（逐字等价）：
- `MonitoredAccountOut`（router ~1349）
- `ContactOut`（~1360）
- `ConversationOut`（~1366）

**不**动 `TimelineMessageOut`（~1391）、`MediaAccessOut`（~1459）、`NestedMediaAccessOut`（~1483）、`PaginationOut`（~1517）、`ConversationMessagesOut`（~1522）——这些属 timeline/media，非目标。

### 1.3 Router 收敛为「HTTP 参数 + 返回」薄壳
`routers/conversations.py` 保留三个路由 handler，`@router.get` 路径与 `Query` 参数 **完全不变**：
- `GET /api/monitored-accounts`（`response_model=list[MonitoredAccountOut]`）
- `GET /api/contacts`（`response_model=list[ContactOut]`）
- `GET /api/conversations`（`response_model=list[ConversationOut]`，参数 `mode`/`staff_id`/`contact_id` 不变）

handler 新函数体只做：
1. `_, tenant_id = auth`
2. 调对应 service（如 `list_monitored_accounts(db, tenant_id)` / `list_contacts(...)` / `list_conversations(...)`）
3. `return` service 结果（service 直接返回 schema 实例或可被 `response_model` 序列化的结构）

**`mode` 校验（400）放哪**：推荐保留在 router handler（HTTP 参数校验天然属于 router 职责），service 的 `list_conversations` 接收已校验的 `mode`；若你倾向把 400 校验也下沉到 service，必须确保 `HTTPException` 仍被 FastAPI 正常转换（service 内 `from fastapi import HTTPException`）。两种都可，但**全工程只许一种风格**，且与现有其他 handler 一致。

router 顶部 import 改为：
`from app.conversation_schemas import MonitoredAccountOut, ContactOut, ConversationOut`
`from app.conversation_listing import list_monitored_accounts, list_contacts, list_conversations` （及必要的 re-export，见 §1.1）

删除 router 中已迁出的函数定义与三段 schema 定义；保留 MediaAccessNoStoreMiddleware、timeline/media 全部函数、RND-210 公共字段投影块（~1716–1734）等无关代码。

---

## 2. 执行步骤（严格：复现/基线 → 实现 → 验证）

### 阶段一：复现 / 基线（先锁定现状，禁止先改实现）
- 跑现有等价测试，确认**基线全绿**（记录用例数）：
  - `backend/tests/test_staff_seats.py`（含 `test_full_equiv_direct_group_key_collision` 及其 `_group_first`/`_direct_first` order-independence 对——group-wins 与 RND-158 排序的权威锚点）
  - `backend/tests/test_conversation_display_names.py`
  - `backend/tests/test_conversation_membership_service.py`
  - `backend/tests/test_tenant_isolation.py`
  - `backend/tests/test_http_contract.py`（路由计数基线）
- **query 数基线**：用 query-counter fixture（在 `Session` 上包一层统计 `db.execute` 调用次数，或在 `engine` 上挂 `before_cursor_execute` 事件计数）记录三个端点当前 query 数，把数字写进 Linear 评论作为 baseline。本任务是等价重构，不写 RED 测试——改为「先确认基线全绿 + query 基线」，再动代码。

### 阶段二：实现（最小改动，严守 §1 + 等价铁律）
- 按 §1.2 建 `conversation_schemas.py`；按 §1.1 建 `conversation_listing.py`；按 §1.3 收敛 router。
- **函数对象同一性**：service 与 router 对 membership 逻辑共用 `app.conversation_membership` 的**同一函数对象**（相同 import 来源），不重定义——保证行为永不漂移。
- 确保 router 三个 handler 之外**无任何聚合/查询逻辑残留**（供 §2 阶段三 C7 代码审查）。

### 阶段三：验证（不达标不收工）
- 阶段一等价测试必须**全绿**（test_staff_seats / display_names / membership_service / tenant_isolation）。
- **query 数不得恶化**：三个端点 query 数 ≤ baseline（用 counter fixture 复测）；若超，说明抽移时多引入了查询，必须修回持平。
- **路由数不变**：`test_http_contract.py` 路由计数不变；三路径仍在。
- 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
- 浏览器 smoke（项目既有 Playwright / 手动）：`/admin` 控制台的 monitored-accounts / contacts / conversations 列表——排序、group-wins、seat 状态（active/history）、显示名与改造前一致；确认 RND-159 内联下拉、RND-229 跳转等无关功能未被破坏。

---

## 3. 硬性约束（不可违反）
- **行为等价优先**：MOVED not reimplemented；不重排 SQL、`and_/or_` 组合不变、tenant scoping 不变、排序/分组契约不变。
- **不改 timeline/media**：`get_conversation_messages` 及 media 系列、对应 schema 一律不动（非目标）。
- **不做查询性能优化**（非目标），但保证 query 数不恶化。
- **路由数与路径不变**；`Query` 参数与 400 校验语义不变；不新增端点。
- **tenant_id 永远来自 `get_current_user`**；绝不接受请求传入 tenant。
- **复用 `app.conversation_membership` 既有函数对象**（同 import 来源），不在 service 内重定义 membership 逻辑。
- **`_load_display_names` 不能双定义**：按 §1.1 方案 A 或 B 处理，保证 timeline ~1881 调用与测试 import 不破。
- 不改动前端/视觉令牌；不引入后台进程；假设开发服务器已在运行，命令前台运行。
- 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit` / `push`**（需用户显式授权）。

---

## 4. 收尾动作
- 在 Linear 把 RND-219 状态置 `In Progress`（或保持 Todo 待用户决定）；写一条评论：改动文件/函数级清单（router 收敛了哪些、service/schemas 模块新增了哪些）+ 等价测试全绿证据 + **query baseline 对比**（改造前 vs 改造后数字）+ `make verify` 结论 + 对「非目标（timeline/media/查询优化）未触碰」的重申。
- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 若收敛中发现某 helper 的调用面比预期更广（如还有别处引用已迁出函数），在评论中**如实标注边界**，沿用 re-export 惯例解决，不私自扩展范围。
