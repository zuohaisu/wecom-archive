# RND-297 QA 验收提示词（A8-2 外观/语言偏好持久化）

> 本文件是交给**独立 QA agent** 的验收 brief。开发 agent 完成后，由你**独立**按"验收标准映射 → 硬约束核查 → 回归 → 交付报告"验收。
> 工程纪律（全局）：QA **绝不 `git commit` / `git push`**。仅输出验收结论（PASS / FAIL + 阻塞项），由用户决定是否提交。代码标识符加反引号。

---

## 0. 任务身份

- **Linear**: `RND-297` — `[BLOCKED: F0] A8-2 外观/语言偏好持久化`
- **验收对象**: 开发 agent 按 `rnd-297-execution-prompt.md` 实现的 theme/locale 持久化 + 端点 + `/api/auth/me` 扩展
- **范围**: 后端持久化（theme+locale）+ 读/写端点 + `/api/auth/me` 返回；前端"登录后应用"薄胶水（若实现）

---

## 1. 验收标准映射（Linear Acceptance Criteria）

| # | 验收项 | 验证方法 |
|---|---|---|
| AC-1 | 偏好**持久化** | `PUT /api/auth/me/preferences` 后，DB `admin_users.ui_theme`/`ui_locale` 落库；重启/重新登录 `GET /api/auth/me` 仍返回该值（用 seed 用户验证） |
| AC-2 | **登录后应用** | 登录后客户端应用服务端 theme/locale（验证 §5 胶水：切 dark/en → 刷新/其他设备登录仍应用；若 dev agent 未实现胶水，记录为"API 已就绪、apply 待 RND-268 前端跟随"并降级为非阻塞备注） |
| AC-3 | 读端点返回偏好 | `GET /api/auth/me` 响应对认证用户含 `theme` / `locale`（合法枚举值或默认 `light`/`zh-CN`） |
| AC-4 | 写端点可用 | `PUT /api/auth/me/preferences` 接受 `{theme?,locale?}` partial update，返回更新后 `{theme,locale}` |
| AC-5 | 取值约束 | 非法 `theme`（如 `system`）/ `locale`（如 `fr`）→ 422；合法 `light`/`dark` + `zh-CN`/`zh-TW`/`en` 通过 |
| AC-6 | 默认与回退 | 未设置时 `GET /api/auth/me` 返回 `theme="light"`、`locale="zh-CN"`（与 `i18n.js` 默认及设计稿一致） |

---

## 2. 硬约束核查（一票否决）

逐一验证，任一失败即 **FAIL**：

1. **仅本人偏好（fail-closed）**：写端点解析自会话（`_resolve_session_user`），**无** `tenant_id`/`user_id` 请求参数。源码确认无法越权改他人偏好。
2. **不改角色/登录语义**：确认未改 `role`/`status`/鉴权流程；`/api/auth/me` 既有字段（`authenticated`/`wecom_user_id`/`display_name`/`tenant_id`/`id`/`role`）行为不变。
3. **Non-goals 未越界**：确认**未**实现 logo 上传/白标（属 `RND-259`）、**未**实现 density 持久化（设计稿 `settings.html:117`）。本票仅 theme+locale。
4. **迁移纪律**：`backend/alembic/versions/0020_*.py` 存在，`down_revision="0019"`；`alembic upgrade head` 在本机跑通；`git diff` 无手工 `ALTER TABLE`、无改 `0019`。确认新列带 server_default（`light`/`zh-CN`）。
5. **路由基线**：`tests/test_http_contract.py:325` 当前 `route_count` 应等于「执行时基线 +1」（本票新增 1 个写路由），且注释含 `RND-297`。**勿断言固定数字 46/47**——因其他路由票可能先合并使基线漂移；以执行时实际基线 +1 为准。运行该测试通过。
6. **D1 冻结**：确认未引入 React/新框架；前端仅极薄原生 JS（§5）。
7. **零泄露**：`GET /api/auth/me` 与 `PUT` 响应不含 `password_hash` / `invite_token` / session secret / cookie 值。
8. **迁移可回滚（建议）**：`alembic downgrade` 至 `0019` 成功（或至少 `downgrade()` 语法正确、能删除两列）。

---

## 3. 测试执行

- 运行 `make test`（或 `pytest backend/tests`）全绿。
- 重点运行：
  - `tests/test_http_contract.py::test_router_count`（路由数 == 47）
  - 偏好 API 测试（`tests/test_preferences_api.py` 或并入 auth 测试）：未登录 401 / 写读往返 / 非法值 422 / partial update / DB 落库 / 隔离（结构上无越权入参）
- 若开发 agent 未附测试，QA **自行补**最小集成测覆盖 AC-1~AC-6 + 硬约束 1/4/5，再判定。

---

## 4. 回归清单

- 既有 `/api/auth/me`（`auth.py:944`）行为不变（仅增字段），全站登录/会话回归通过（复用 `get_current_user`/`require_html_session` 的路由不受影响）。
- `AdminUser` 模型加列不影响既有查询（含 `auth.py` upsert、`conversations.py`、`search.py` 等引用 `AdminUser` 处）—— 全量 `pytest` 无退化。
- Alembic 链完整：`alembic history` 连续到 `0020`，无断链。
- `app/schemas/auth.py` 为新增文件，不改动既有 schema。

---

## 5. 交付报告格式（输出给用户）

```
RND-297 QA 结论: PASS / FAIL
- 验收项: AC-1~AC-6 全部通过 / 失败项(列出)
- 硬约束: 8 项全部满足 / 违反项(列出+证据)
- 测试: 通过 X / 失败 Y（附失败栈关键行）
- 回归: 全绿 / 回归退化(列出)
- 阻塞项(如有): ...
- 备注: §5 前端 apply 胶水是否实现；density/logo 已确认留作跟随(RND-259/RND-268)
```

> 仅输出结论与证据，**不代用户提交**。若 FAIL，明确列出必须返工的开发项，交用户决定是否退回开发 agent。
