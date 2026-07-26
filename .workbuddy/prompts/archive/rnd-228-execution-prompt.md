# RND-228 执行提示词（端到端：基线测试 → 重构/优化 → 验证）

> 用途：粘贴给实现智能体（Claude Code 主实现 / Codex QA），单人端到端跑完 RND-228。
> 分支：`zuohaisu/rnd-228-optimize-search-scalability-and-shared-staff-resolution`（已由 Haisu 显式要求创建，off `main`）。
> 性质：P2 可扩展性 + 可维护性重构，**不改变对外 API 行为与返回结构**。不阻塞 RND-159 发布。

---

## 0. 任务与来源
- Linear 工单：**RND-228「Optimize search scalability and shared staff resolution」**，优先级 Medium(P2)，related：RND-159。
- 目标：把 RND-159 落地的搜索接口做**可扩展性收敛**与**共享逻辑去重**，消除大租户下的无界查询/内存放大，并让 staff 判定与显示名解析复用统一的单一真源。
- 范围：仅 `backend/app/routers/search.py` 与其对 `backend/app/conversation_membership.py` 既有 helper 的复用；配套回归测试。
- 非目标（严禁）：不改 `/api/search/contacts` 与 `/api/search/messages` 的对外契约（字段、排序语义、分页语义、租户隔离）；不动 `conversation_membership.py` 的既有查询语义；不做跨 issue 的大重构。

## 1. 精确改动点（已定位，均在 `backend/app/routers/search.py`）

### 1.1 可扩展性：`search_contacts` 无界 `.all()` + Python 端排序切片（~L204-262）
现状：对 `Contact`（L204-214）与 `AdminUser`（L216-226）两张表分别 `.all()` 拉全部匹配行，然后在 Python 里 merge/dedup（L229-244）、`sorted(...)[:limit]`（L247-253）。
风险：大租户匹配量大时，DB 与内存开销随匹配总数线性膨胀，`limit` 只在最后才生效。
要求：在进入 Python merge/sort **之前**做 SQL 级有界化。
- 每个来源查询加 SQL 级排序 + `LIMIT`：用 `case()` 表达"名字命中优先"（name ILIKE pattern → 0，否则 1），再按 name/wecom_userid 次序，`.limit(limit)`（或有界上限 `min(limit, _MAX_LIMIT)`）。
- 两来源各取有界 `limit` 行后，再做既有 merge/dedup（AdminUser 名称优先补空）与最终 `sorted(...)[:limit]`。
- 结果集内存上界从"匹配总数"降到"≤ 2×limit"。
- **可接受的语义权衡**（工单已隐含）：极端 dedup 下最终不足 `limit` 时不再回表补齐——这是有界策略的既定取舍，需在测试与 Linear 评论中显式声明。

### 1.2 共享逻辑去重：`_build_staff_ids` 重复实现（~L117-149）
现状：`_build_staff_ids` 的 docstring 自述是 "Simplified version of conversation_membership._collect_staff_ids"，逻辑与 `conversation_membership._collect_staff_ids`（L47-67，含 `_collect_archive_participant_ids`）**等价**。
要求：删除 `_build_staff_ids`，改为 `from app.conversation_membership import _collect_staff_ids`，在 `search_messages`（L336）调用处替换为 `_collect_staff_ids(db, tenant_id)`。行为必须逐行等价（两者都：sender/recipient 全租户 distinct 扫描 → prefix `staff_` ∪ (AdminUser ∩ participants)）。

### 1.3 共享逻辑去重：`_build_display_name_map` 重复实现（~L105-114）
现状：`_build_display_name_map` 与 `conversation_membership._load_display_names_for_ids`（L110-131）**逐字节等价**（同一 `{wecom_userid: name}` scoped 查询）。
要求：删除 `_build_display_name_map`，改用 `_load_display_names_for_ids`（从 `app.conversation_membership` 引入），替换 `search_messages`（L350）调用处。

### 1.4 Cleanup：移除未使用导入（L16）
`from sqlalchemy import and_, case, func, or_, text, union`
- 实际使用：`and_`（L314）、`or_`（多处）。
- 未使用：`func`、`text`（注意 L82 的 `text` 是函数形参，非该导入）、`union`——**删除**。
- `case`：若 1.1 采用 `case()` 做 SQL 排序则**保留并变为已用**；若最终未用则一并删除。以实现落点为准，收工时 `search.py` 不得残留任何未使用导入。

## 2. 执行步骤（严格按顺序）

### 阶段一：基线（先锁住"行为不变"）
- 先跑既有套件确认绿：
  `cd backend && python -m pytest tests/test_search_api.py tests/test_conversation_membership_service.py tests/test_conversation_display_names.py -q`
- 阅读 `tests/test_search_api.py`，确认现有对 contacts 排序（name 命中优先）、messages 分页游标、entity 导航、租户隔离的断言，作为"重构不得破坏"的护栏。
- 新增可扩展性回归（建议并入 `test_search_api.py` 或新建 `test_rnd_228_search_scalability.py`）：
  - 构造某租户下大量（如 50+）name/userid 命中的 Contact + AdminUser，`limit=20`，断言：返回恰好 20 条、name 命中排在 userid 命中之前、去重按 wecom_userid、AdminUser 名称优先。
  - （可选但推荐）用 SQLAlchemy event / query 计数或对 `.all()` 的 mock，断言进入 Python 合并前每个来源取回行数 ≤ limit（证明有界化真实生效，而非"先全取再切"）。

### 阶段二：实现（最小改动，严守范围）
按 1.1 → 1.2 → 1.3 → 1.4 顺序改：
1. `search_contacts`：两来源查询加 `case()` 排序 + `.limit(...)`；保留既有 merge/dedup 与最终 `sorted(...)[:limit]`；`match_field` 判定逻辑不变。
2. 删 `_build_staff_ids`，引入并改用 `_collect_staff_ids`。
3. 删 `_build_display_name_map`，引入并改用 `_load_display_names_for_ids`。
4. 清理未使用导入。
- 全程不改两个路由的响应模型、字段名、排序/分页对外语义、租户隔离（`tenant_id` 仍只来自 session，绝不来自请求参数）。

### 阶段三：验证（不达标不收工）
- 阶段一新增回归**通过（GREEN）**；既有 search / membership / display-name 套件**全绿无回归**。
- 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest），确认无 lint/type/未用导入告警。
- 手动确认：`search_contacts`/`search_messages` 返回结构与 RND-159 一致（可用现有 Playwright/前端 smoke 校验搜索面板行为不变）。

## 3. 硬性约束（不可违反）
- **行为等价**：staff 判定集合、显示名映射、contacts 排序、messages 分页/实体导航，重构前后必须一致（有界化导致的极端 dedup 不足 limit 除外，且需声明）。
- **租户隔离**：所有查询保持 `tenant_id == session tenant`，`tenant_id` 绝不来自请求参数。
- **单一真源**：staff/显示名逻辑只保留 `conversation_membership` 一处；`search.py` 不得再持有平行实现。
- **不动** `conversation_membership.py` 的既有查询语义（仅复用，不改写）。
- 不引入新依赖、不引入后台进程；假设开发服务器已运行；命令前台跑。
- 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit`/`push`**（需 Haisu 显式授权）。本任务按 Haisu 要求在专用分支开发。

## 4. 收尾动作
- 在 Linear 将 RND-228 保持/置为 In Progress；写一条评论：改动点（函数级）+ 有界化策略与语义权衡声明 + 测试结果 + `make verify` 结论。
- QA Summary（按 `DEV_AGENT_RULES.md` §QA）：Files changed / Acceptance criteria / Commands run / Risks / 无 secrets / 仅预期文件变更。
- 保留 merge 关卡给 Haisu 人工处理，不自动合入 main。

## 5. 验收清单（Acceptance Criteria）
- [ ] `search_contacts` 在进入 Python merge 前对每个来源做 SQL 级有界查询（`case()` 排序 + `LIMIT`），内存上界 ≤ 2×limit。
- [ ] `_build_staff_ids` 删除，改用 `_collect_staff_ids`，行为等价。
- [ ] `_build_display_name_map` 删除，改用 `_load_display_names_for_ids`，行为等价。
- [ ] `search.py` 无任何未使用导入。
- [ ] 新增可扩展性回归测试通过；既有 search/membership/display-name 套件全绿。
- [ ] `make verify` 通过。
- [ ] 对外 API 契约（字段/排序/分页/租户隔离）零变更。
