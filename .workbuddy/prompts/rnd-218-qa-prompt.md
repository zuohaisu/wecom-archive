# RND-218 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**验收智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-218 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-218-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-218** 的验收标准 + `tests/test_http_contract.py` 的路由快照契约 + 既有 legacy tests。

---

## 0. 验收依据

- **Linear RND-218 验收标准（必须全过）**：路由、status、redirect、response body、OpenAPI 和 tenant isolation 保持兼容；route snapshot 与 legacy tests 全部通过。
- **非目标不可被破坏**：未删除任何 legacy 页面；查询语义未变；`/api/messages*` 仍是 401 认证。
- **硬契约（来自 `tests/test_http_contract.py`）**：
  - `test_router_count` 断言路由数 **== 33**（移出而非新增，绝不涨到 34）。
  - `test_route_snapshot_with_real_model_names` 逐字段匹配：path / methods(GET) / response_model 名 / response_class。
  - `test_routers_are_registered` 的 path 集合完全一致（不增不减、不重名）。
  - `test_http_contract.py` 中 public routes / headers（Cache-Control）/ 前端 HTML 结构顺序等既有断言不得回归。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支；RND-218 的 3 个目标文件 `app/routers/web.py`、`app/routers/messages.py`、`app/schemas/messages.py` 均存在且被 `main.py` `include_router`。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动**前台**进程后再验（不后台化、不加 `&`）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 结构 / 合约（纯静态 + 单元）
- **C1 路由数不变**：`tests/test_http_contract.py::test_router_count` → 断言 `route_count == 33` PASS；若 34 即 main.py 留了副本 → FAIL。
- **C2 路由快照逐字段相等**：`tests/test_http_contract.py::test_route_snapshot_with_real_model_names` PASS；逐项核对 `/admin/messages`(+`{msgid}`)、`/admin/conversations`、`/admin/search`、`/admin/diagnostics/reachability` 为 `HTMLResponse`；`/api/messages`→`list[MessageOut]`、`/api/messages/{msgid}`→`MessageDetailOut`；methods 全 GET。
- **C3 path 集合一致**：`tests/test_http_contract.py::test_routers_are_registered` PASS（33 个 path 与期望集合完全相等，不增不减）。
- **C4 目标文件就位**：`app/routers/web.py`、`app/routers/messages.py`、`app/schemas/messages.py` 存在；`main.py` 已 `include_router(web_router)` / `include_router(messages_router)`；`main.py` 中不再有被迁走的 7 条路由定义（grep 验证）。
- **C5 main.py 收敛**：`main.py` 仍保留 `app=FastAPI(...)`、中间件、`_RedactOAuthCallbackQueryFilter`、3 个 `/health*`、既有 `include_router`；无被迁走路由的残留定义。

### 行为 / 语义（HTTP 实测，用 TestClient 或真实服务器）
- **C6 HTML 路由认证=302**：对 `/admin/messages`、`/admin/messages/{msgid}`、`/admin/conversations`、`/admin/search`、`/admin/diagnostics/reachability` **不带 cookie** 请求 → 期望 `302`、`location: /admin/login`（与改造前一致）。带合法 session cookie → `200` + HTML body。
- **C7 API 路由认证=401**：对 `/api/messages`、`/api/messages/{msgid}` **不带 cookie** 请求 → `401`（`get_current_user` 原行为，未改成 redirect）。
- **C8 租户隔离**：用**另一租户**的合法 session 调 `/admin/messages` 与 `/api/messages` / `/api/messages/{msgid}`，断言只能见到本租户数据；`/api/messages/{msgid}` 对跨租户 msgid 返回 404 而非泄露（沿用 `tests/test_tenant_isolation.py` 套路）。
- **C9 查询语义不变**：`/admin/messages?sender=&q=&limit=` 与 `/api/messages?sender=&q=&msgtype=&roomid=&limit=` 的过滤/排序/limit 范围与改造前一致；`limit` 越界（`<1` 或 `>100`）仍 422。抽查返回行数与改造前 fixture 一致。
- **C10 响应体兼容**：HTML 页渲染结果（`/admin/messages` 表格、`/admin/messages/{msgid}` 详情、`/admin/conversations`/`/admin/search`/`/admin/diagnostics/reachability` 控制台）结构与文案与改造前一致；JSON 字段名（`msgid/seq/msgtype/sender/roomid/msgtime/content_text/decrypt_status/recipients`）不变。
- **C11 OpenAPI 兼容**：`GET /openapi.json` 含上述 7 条路由且 `response_model` 名与改造前一致（可用 C2 的 snapshot 作为等价证据）；`/docs` 可正常渲染。

### 回归（不破坏既有能力）
- **C12 legacy tests 全绿**（逐一列名，必须全部 PASS）：
  - `tests/test_http_contract.py`
  - `tests/test_auth.py`、`tests/test_password_auth.py`、`tests/test_rnd225_auth_fail_closed.py`（认证闸门）
  - `tests/test_tenant_isolation.py`（租户隔离）
  - `tests/test_admin_timestamp_formatting.py`（消息时间格式）
  - `tests/test_admin_auto_refresh.py`、`tests/test_admin_auto_load_older.py`（控制台行为）
  - `tests/test_reachability_diagnostics_page.py`（诊断页）
  - `tests/test_rnd_206_top_level_image.py`（消息内媒体）
- **C13 全量 `make verify` 绿**：lint-diff + typecheck + build + 全量 pytest 全过；无因本次重构引入的 unused import / 循环 import。

---

## 3. 测试方法

- **契约 / 单元 / 回归**：`cd backend` 后跑
  - `python -m pytest tests/test_http_contract.py tests/test_auth.py tests/test_password_auth.py tests/test_rnd225_auth_fail_closed.py tests/test_tenant_isolation.py tests/test_admin_timestamp_formatting.py tests/test_admin_auto_refresh.py tests/test_admin_auto_load_older.py tests/test_reachability_diagnostics_page.py tests/test_rnd_206_top_level_image.py -q`
  - 若开发 agent 未交付针对本任务的测试（如验证 `require_html_session` 统一依赖的专项用例），本 agent **自行补最小验收测试**覆盖 C6/C7 的 302/401 行为，再判定。
- **行为实测**：用 `fastapi.testclient.TestClient`（复用 `test_http_contract.py` 的 fixture 套路）构造多租户 + 无 cookie 场景，断言 C6–C11。
- **收口**：跑 `make verify` 确认 lint-diff + typecheck + build + 全量 pytest 全绿。

---

## 4. 硬性约束（验收 agent 自身也要守）
- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过租户隔离做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-218 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 目标文件就位：是/否 · make verify：通过/失败

| 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|------|--------|------|---------------------------|
| C1   | 路由数==33 | PASS | pytest ... |
| C2   | 路由快照逐字段相等 | PASS | ... |
| C3   | path 集合一致 | PASS | ... |
| C4   | 目标文件就位 | PASS | ... |
| C5   | main.py 收敛 | PASS | grep 证据 |
| C6   | HTML 路由 302 | PASS | ... |
| C7   | API 路由 401 | PASS | ... |
| C8   | 租户隔离 | PASS | ... |
| C9   | 查询语义不变 | PASS | ... |
| C10  | 响应体兼容 | PASS | ... |
| C11  | OpenAPI 兼容 | PASS | ... |
| C12  | legacy tests 全绿 | PASS | ... |
| C13  | make verify 全绿 | PASS | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 302 用 HTTPException(302, Location) 实现时 redirect body 可能与原 RedirectResponse 有差异（空 body vs JSON detail）—— 若所有 legacy 测试仍 PASS 则视为兼容，否则判 FAIL 并标注。
- 其他观察到的限制：<…>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺目标文件或 make verify 失败）。
```

- 若某条无论如何无法复现（如环境导致 C6–C11 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-218 评论贴出（状态保持 In Progress，交还用户 Haisu 决策合并）。
