# RND-219 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-219 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-219-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-219** 的验收标准（ordering / group-wins / seat status / display names / tenant isolation 等价；router 仅 HTTP+返回；query count 不恶化）+ 代码硬约束。

---

## 0. 验收依据

- **Linear RND-219 验收（必须全过）**：
  - ordering、group-wins、seat status、display names、tenant isolation 与现状等价；
  - router 只处理 HTTP 参数与返回（不含聚合/查询逻辑）；
  - query count 不得恶化。
- **非目标不可被破坏**：timeline / media 端点与对应 schema 必须保持改造前行为（RND-220 才动）。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支，且 **RND-212 已在 `main`**、`backend/app/conversation_membership.py` 存在。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动 **前台** 进程后再验（不后台化、不加 `&`）。
4. 拿到开发 agent 评论中的 **query baseline 数字**（三端点改造前 query 数）；若缺失，本 agent 自行用 counter fixture 重测 baseline 与改造后对比。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 后端等价（直接打三个 Listing 端点）
- **C1 ordering 等价**：`GET /api/conversations?mode=staff&staff_id=<X>` 返回按 `(last_message_time, conversation_id)` 降序；构造多条同 `msgtime` 消息，断言 tie-break 由 `id` 决定（RND-158 契约，与 `test_staff_seats.py` 一致）。
- **C2 group-wins 等价**：构造同一 `conversation_id` 同时被 direct-pair key（`_direct_conv_id`）与真实 `roomid` 命中的消息集，断言结果 `conversation_type == "group"`，且**与消息处理顺序无关**（对应 `test_staff_seats.py::test_full_equiv_direct_group_key_collision` 及其 `_group_first`/`_direct_first` 对）。
- **C3 seat status 等价**：`GET /api/monitored-accounts` 中 `latest_message_time` 最大者为 `seat_status="active"`、其余 `history`；整体排序 active-first 再 `latest_message_time` 降序；`is_active_archive_seat` 与 `seat_status` 一致。
- **C4 display names 等价**：group 行 `display_name == resolve_room_display_name(...)`；direct 行取 contact/staff 显示名；`latest_sender_display_name` 正确（对照 `test_conversation_display_names.py`）。
- **C5 tenant isolation 等价**：用**另一租户**认证调三端点，断言只能见到本租户数据；请求里即使塞 `tenant_id` 参数也应被忽略（`tenant_id` 只来自 `get_current_user`）。
- **C6 query count 不恶化**：用 query-counter fixture 复测三端点改造后 query 数 ≤ 改造前 baseline（对照开发评论数字或本 agent 自测 baseline）。超即 FAIL。
- **C7 router 仅 HTTP+返回（代码审查）**：`routers/conversations.py` 中三个 handler 函数体只含「解析 auth/Query → 调 service → return」；**不得**含 `_build_conversation_list`、`_load_display_names`、SQL `db.query(...)` 等聚合/查询逻辑（这些应只在 `conversation_listing.py`）。
- **C8 非目标未被破坏（代码 + 行为）**：
  - `git diff` 确认 `get_conversation_messages`（~1756）、media 系列（~2410/2554/2754/2868）及 `TimelineMessageOut`/`MediaAccessOut`/`NestedMediaAccessOut`/`PaginationOut`/`ConversationMessagesOut`（~1391–1540）**无改动**；
  - 真实打 `GET /api/conversations/{id}/messages` 与 media 端点，返回结构与改造前一致（可对照既有 media/timeline 测试）。
- **C9 路由数与路径不变**：`test_http_contract.py` 路由计数不变；三路径 `/api/monitored-accounts`、`/api/contacts`、`/api/conversations` 仍存在且 `response_model` 分别为 `MonitoredAccountOut`/`ContactOut`/`ConversationOut`（现 import 自 `app.conversation_schemas`）。
- **C10 复用 membership 函数对象（代码审查）**：`conversation_listing.py` 与 router 对 membership 逻辑（`_collect_staff_ids` 等）的 import 来源**均为 `app.conversation_membership`**，无在 service/router 内重定义；`_load_display_names` 在 router 与 service 间**无双定义**（re-export 或单一定义，见执行提示词 §1.1）。

### 回归（不破坏既有能力）
- **C11 `test_staff_seats.py` 全绿**（seat status + ordering + group-wins 权威锚点）。
- **C12 `test_conversation_display_names.py` 全绿**。
- **C13 `test_conversation_membership_service.py` 全绿**。
- **C14 `test_tenant_isolation.py` 全绿**。
- **C15 `test_http_contract.py` 全绿**（路由数不变）。
- **C16 `make verify` 全绿**（lint-diff + typecheck + build + 全量 pytest）。
- **C17 RND-226 / RND-210 未被破坏**：`test_rnd_226_nested_entity_context.py`、`test_rnd_210_msgtype_and_card.py` 仍绿（确认收敛未误伤已上线逻辑）。

---

## 3. 测试方法

- **后端等价 + 回归**：`cd backend` 后跑
  - `python -m pytest tests/test_staff_seats.py tests/test_conversation_display_names.py tests/test_conversation_membership_service.py tests/test_tenant_isolation.py tests/test_http_contract.py tests/test_rnd_226_nested_entity_context.py tests/test_rnd_210_msgtype_and_card.py -q`
  - 若开发 agent 未交付 query 基线，本 agent 用 counter fixture（包 `Session.execute` 或 `engine` `before_cursor_execute` 事件）自测三端点改造后 query 数，并标注「baseline 缺失，本次自测为准」。
- **代码审查项（C7/C8/C10）**：直接读 `git diff` 与 `conversation_listing.py` / `conversation_schemas.py` / `routers/conversations.py` 头部 import，逐项核对。
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
## RND-219 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 是否含 RND-212/conversation_membership：是/否 · make verify：通过/失败
query 基线：monitored-accounts=<n> contacts=<n> conversations=<n>（来源：开发评论/本 agent 自测）

| 编号 | 验收点 | 结果 | 证据（实测/命令/代码位置） |
|------|--------|------|---------------------------|
| C1   | ordering 等价 | PASS | ... |
| C2   | group-wins 等价 | PASS | ... |
| C3   | seat status 等价 | PASS | ... |
| C4   | display names 等价 | PASS | ... |
| C5   | tenant isolation 等价 | PASS | ... |
| C6   | query count 不恶化 | PASS | 改造后 ≤ baseline（<n>/<n>/<n>） |
| C7   | router 仅 HTTP+返回 | PASS | 代码审查：handler 无聚合/查询逻辑 |
| C8   | 非目标未破坏 | PASS | git diff 确认 timeline/media 无改动 |
| C9   | 路由数/路径不变 | PASS | test_http_contract 通过 |
| C10  | 复用 membership 对象 | PASS | import 来源均为 conversation_membership |
| C11–C17 | 回归套件 | PASS | pytest 全绿 / make verify 全绿 |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 非目标（timeline/media/查询优化）确认未触碰 → 视为 PASS 非缺陷。
- 其他观察到的限制：<…>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺 RND-212 或 conversation_membership）。
```

- 若某条无论如何无法复现（如环境导致 C1–C5 打不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-219 评论贴出（状态保持 In Progress / Todo，交还用户 Haisu 决策合并）。
