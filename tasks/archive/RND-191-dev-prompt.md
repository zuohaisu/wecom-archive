# RND-191 开发 agent 执行提示词

> 面向开发 agent（单人端到端实现 RND-191）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户本人操作）。只改工作树，交用户 Review。
> 父任务编排见 `rnd-160-execution-plan.md` / `rnd-160-execution-prompt.md`；验收见 `rnd-191-qa-prompt.md`。

---

## 一、任务（一句话）

基于 RND-190 基线，优化关键 API 与数据库查询（员工/联系人/会话列表、消息时间线、搜索），让首屏更快返回、降低 2GB 内存下 DB 的 CPU/内存峰值，**不改变任何对外契约与租户隔离语义**。

---

## 二、精确根因 / 落点（已定位，附文件:行号，实现前请逐条 `Read` 确认）

### A. 消息时间线全量加载后在 Python 切片（主因，高杠杆）
- `backend/app/services/timeline_service.py:582` `get_conversation_messages` 调 `_fetch_conversation_messages` 取**该会话全部** `ArchiveMessage` ORM 行（`content_text`/`decrypted_payload`/`structured_content` 全列）。
- `:633` 对全量 `sorted(...)`；`:636`/`:640` 在 Python 里按 `before` 游标切片取 `limit`。每次翻页都重算整段。
- 根因在 `backend/app/conversation_membership.py` 的 `_fetch_group_room_messages` / `_fetch_direct_pair_messages` 用 `.all()` 不带 LIMIT。

### B. `get_conversation_detail` 为两个标量物化全表
- `backend/app/routers/conversations.py:497` 取全会话；`:502-504` 遍历算 `participants`；`:524-525` 遍历算 `decrypted_percent`。
- 已有关联先例：**RND-240 已把这条改成聚合 SQL**（见 `rnd-240-execution-prompt.md`）。确认 RND-240 是否已合入；若已合入则 B 已解决，仅验证未回归。

### C. 被监控账号列表 N+1（per-seat 循环）
- `backend/app/services/listing_service.py:561-589` 每个 seat 调 `_latest_own_participation_time`（≈2 查询/seat）+ `_count_entity_conversations`（经 `_compact_entity_messages` ≈5–6 查询/seat）。seat 多时 ≈7×S 查询。
- `_compact_entity_messages` 内部 `:68-155` 反复扫描全租户 room 消息。

### D. 冗余的全租户 `distinct()` 扫描
- `backend/app/conversation_membership.py:25-67` `_collect_staff_ids` 与 `:25-43` `_collect_archive_participant_ids` 各跑两次 tenant-wide `distinct()`。
- `list_contacts` 同时调两者（`listing_service.py:617-618`），`list_monitored_accounts` 调一个（`:554`），`search_messages` 调一/两次（`search.py:342`/`:396`）。

### E. 无作用域的全 Contact 加载
- `backend/app/conversation_membership.py:110-127` `_load_display_names` 加载**每一行** Contact；被时间线（`:612`）、`list_monitored_accounts`（`:558`）、`list_contacts`（`:620`）调用。
- 已有作用域版本 `_load_display_names_for_ids` 可直接复用。

### F. `search_messages` 用 `ILIKE` 命中不了 FTS 索引（高杠杆）
- `backend/app/routers/search.py:325` `content_text.ilike(...)`；唯一文本索引是 `to_tsvector('simple',…)` GIN（`models.py:269-273`）→ ILIKE 退化为全租户顺序扫描。
- `Contact`/`AdminUser` 名字 ILIKE（`search.py:206-233`）同样无索引。

### G. 缺失复合索引（中–高杠杆）
- `models.py:283-323` 仅有单列索引；缺 `(tenant_id, msgtime, id)`（分页 `search.py:381`、时间线）、`(tenant_id, roomid)`、`(tenant_id, decrypt_status, is_revoked)`、`ArchiveMessageRecipient(tenant_id, receiver_userid)`（`models.py:329-348`）。

### H. 2GB 下的连接池
- `backend/app/db/session.py:17` `pool_size=2, max_overflow=2`（仅 4 连接）。N+1 倍数上来后并发请求会排队。**先修 N+1 再谈调池**；可在修复后评估 `pool_pre_ping=True`、保持 ≤5。

### I. 未分页/超大响应体
- `backend/app/routers/messages.py:35`/`:129` 返最多 100 条原始消息含 `content_text`；`list_contacts`/监控账号列表数量无上限、整数组序列化。

> 注意：上述行号来自只读静态分析（RND-190 期）。实现前请 `Read` 对应文件确认；若代码已演进，以实际为准，但优化目标与约束不变。

---

## 三、阶段一：复现 + 测量（RED，必须可追溯到 RND-190）

1. 按 `Makefile` / `README` 起后端。
2. 用 RND-190 的基线场景 + 一个**大会话**（>1000 条）或**多 seat**租户复现：
   - 测 `GET /api/conversations`（会话列表）、员工/联系人列表、消息时间线翻页、`/api/search` 的 P50/P95 与响应体大小。
   - 用 `EXPLAIN (ANALYZE, BUFFERS)` 抓 `search_messages` ILIKE、`list_monitored_accounts` 的慢查询计划。
3. 把 RED 数字写进实现说明（交付时附），并标注每项对应 RND-190 基线里的哪条瓶颈。

---

## 四、阶段二：实现（GREEN，按杠杆排序，最小变更）

**最高杠杆先做**：A、B（若未做）、C、F。

1. **A — 时间线游标分页下推 SQL**：在 `conversation_membership.py` 的取消息函数加 `LIMIT`/游标（`msgtime, id < cursor`），仅 `SELECT` 需要的列；`timeline_service.py:582-640` 改为消费已分页结果，去掉 Python 全量 `sorted`+切片。
2. **B — `get_conversation_detail` 聚合**：若 RND-240 未合入，按 `rnd-240-execution-prompt.md` 路 B 抽 `_aggregate_conversation_stats`（`COUNT`/`SUM(decrypt_status)`/`DISTINCT sender`）取代全量遍历；返回结构与字段逐字段等价。
3. **C — 监控账号列表聚合**：把 per-seat 的参与时间/会话计数合并为一条分组 SQL（`max(msgtime) per seat`、`count(DISTINCT conv_id)` 窗口），消除 ≈7×S 查询。
4. **F — 搜索索引**：新增 `pg_trgm` GIN 索引（或在 `to_tsvector` 上改用 `websearch_to_tsquery`/`plainto_tsquery`）让关键字搜索命中索引；名字搜索同理。迁移放 `alembic/`，命名规范见现有迁移。
5. **G — 复合索引**：按 §G 补 `(tenant_id, msgtime, id)` 等；先在测试库 `EXPLAIN` 验证被采用再上生产。
6. **D/E — 去冗余**：请求内缓存 seat 集合（一次 `distinct`）；`_load_display_names` 改用 `_load_display_names_for_ids` 按参与者 id 作用域。
7. **I — 响应体**：列表端点投影摘要列、限制/分页，避免整 `content_text`；`list_contacts` 加分页。
8. **H — 连接池**：仅在所有 N+1 修复、且压测显示排队后，再考虑 `pool_pre_ping`/微调；**不得为掩盖 N+1 而盲目加池**。

约束：
- 不改消息同步/解密/媒体语义；不靠放宽租户过滤或访问控制换性能。
- 无 RND-190 基线证据**不**引入缓存/消息队列/新依赖（issue 非目标）。
- 任何 DB 索引/迁移必须可回滚（`alembic downgrade` 或等价），且经 `deploy_server.sh` 的迁移校验 + 健康门。

---

## 五、阶段三：验证（GREEN + 回归 + make verify）

1. **功能验证**：回阶段一场景重测——列表/时间线/搜索 P95 应显著低于 RED；响应体缩小；`EXPLAIN` 显示索引命中、无全租户扫描。
2. **回归**（必须全绿）：
   - `make verify`
   - 重点：`test_search_*`、`test_http_contract`（路由数不变）、租户隔离测试、`test_rnd_228_*`、`test_rnd240_*`（若 B 复用其路径）。
   - 确认 `list_monitored_accounts` / `list_contacts` / 时间线 / 搜索的排序、分页、租户隔离、认证行为不变。
3. 若测试因本次改动失败，**先修实现**，不擅自改测试（除非测试断言了旧的慢路径，须在 PR 说明并获用户确认）。

---

## 六、硬约束（违反即判失败）

- ❌ 不改 URL / HTTP status / response body 结构 / OpenAPI / 租户隔离 / i18n。
- ❌ 不改同步/解密/媒体语义；不靠放宽租户过滤换性能。
- ❌ 无 RND-190 基线证据不引入缓存/队列/新依赖；不加无 `EXPLAIN` 验证的索引。
- ❌ 不执行 git commit / push。
- ✅ 与 RND-216/220/240 等工作树改动**无冲突**前提下最小化改动；冲突则停下报告，不强行覆盖。

---

## 七、收尾（交付物）

向用户交付：
1. RED 基线数字（列表/时间线/搜索 P95、响应体、慢查询计划）+ 对应 RND-190 瓶颈条目。
2. GREEN 数字（同上）+ 前后对比。
3. 改动文件清单（预期：`backend/app/services/timeline_service.py`、`backend/app/conversation_membership.py`、`backend/app/services/listing_service.py`、`backend/app/routers/conversations.py`、`backend/app/routers/search.py`、`backend/app/db/models.py`(索引)、`alembic/` 迁移、`backend/app/db/session.py`(若调池)）。
4. `make verify` 通过日志。
5. 注明：未提交，待用户 Review 后自行 commit。
