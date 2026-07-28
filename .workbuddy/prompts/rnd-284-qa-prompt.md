# RND-284 QA 验收提示词（A3-1 用户列表/筛选 API）

> 本文件是交给**独立 QA agent** 的验收 brief。开发 agent 完成后，由你**独立**按"验收标准映射 → 硬约束核查 → 回归 → 交付报告"验收。
> 工程纪律（全局）：QA **绝不 `git commit` / `git push`**。仅输出验收结论（PASS / FAIL + 阻塞项），由用户决定是否提交。代码标识符加反引号。

---

## 0. 任务身份

- **Linear**: `RND-284` — `[BLOCKED: F0] A3-1 用户列表/筛选 API`
- **验收对象**: 开发 agent 按 `rnd-284-execution-prompt.md` 实现的 `GET /api/admin/users`
- **范围**: 纯后端列表/筛选 API（角色 / 状态 / 最后活跃 / 近 30 天消息数 + 分页 + 筛选）。不含前端页面。

---

## 1. 验收标准映射（Linear Acceptance Criteria）

| # | 验收项 | 验证方法 |
|---|---|---|
| AC-1 | 列表含**角色** | 响应每项含 `role`，取值 ∈ {owner, admin, compliance, legal, readonlyaudit}；与 `AdminUser.role` 一致 |
| AC-2 | 列表含**状态** | 响应每项含 `status` ∈ {active, disabled}；与 `AdminUser.status` 一致（设计稿"存档状态"列） |
| AC-3 | 列表含**最后活跃** | 响应每项含 `last_active_at`（ISO-8601 或 `null`）；与 `AdminUser.last_active_at` 一致 |
| AC-4 | 列表含**近 30 天消息数** | 响应每项含 `msg_count_30d`（int≥0）；与手工 `SELECT count(*) FROM archive_messages WHERE tenant_id=? AND sender=? AND msgtime >= cutoff_ms` 一致（seed 数据核对） |
| AC-5 | 分页 | 响应信封含 `total` / `page` / `per_page`；`page=1&per_page=20` 返回前 20；越界 `page` 返回空 `items` 不 5xx |
| AC-6 | 筛选 | `role` / `status` / `q`（姓名/账号/部门/邮箱 ILIKE）/`silent_days`（派生静默）筛选生效且结果正确 |
| AC-7 | 字段齐全对齐设计稿 | 响应含 `name`/`wecom_user_id`/`email`/`department`/`role`/`status`/`last_active_at`/`msg_count_30d`（对照 `design/ui-v1/pages/users.html:61-62`） |

---

## 2. 硬约束核查（一票否决）

逐一验证，任一失败即 **FAIL**：

1. **租户隔离 fail-closed**：用租户 A 的会话 cookie 请求，断言响应中**绝不**出现租户 B 的 `admin_users`（构造双租户 seed 数据验证）。确认代码无"接受请求参数 `tenant_id`"路径。
2. **SF-1 数据最小化**：`msg_count_30d` 聚合 SQL 仅 `func.count(id)`，确认未 `SELECT` `content_text` / `decrypted_payload` / `structured_content`（读源码 + 断言响应体无消息内容）。
3. **不改角色逻辑**：确认未修改 `admin_user_role` / `admin_user_status` 枚举、未新增写操作（禁用/邀请）、未改权限判定。本票只读。
4. **无 migration**：`git diff` 不含 Alembic 迁移文件、不含 `models.py` 表结构变更。
5. **路由基线**：`tests/test_http_contract.py:325` `assert route_count == 43`（非 42），注释含 `RND-284`。运行该测试通过。
6. **D1 冻结**：确认未新增前端文件 / React 依赖 / 路由页面；未改 i18n。
7. **零泄露**：响应体与日志不含 `password_hash` / `invite_token` / secret。
8. **分页上限**：`per_page > 100` 返回 422（FastAPI `Query(..., le=100)`）；`q` 超长返回 422。
9. **认证**：未带会话 cookie 请求返回 401 或重定向（依赖 `get_current_user`）。

---

## 3. 测试执行

- 运行 `make test`（或 `pytest backend/tests`）全绿。
- 重点运行：
  - `tests/test_http_contract.py::test_router_count`（路由数 == 43）
  - 新增/既有 admin API 测试中含 `GET /api/admin/users` 的用例（租户隔离、筛选、分页、`msg_count_30d` 一致性）
- 若开发 agent 未附测试，QA **自行补**最小集成测覆盖 AC-1~AC-7 + 硬约束 1/2/5，再判定。

---

## 4. 回归清单

- 既有 admin API 不受影响：`GET /api/admin/sync/*`、`/api/admin/users` 之外路由基线变化仅为 +1。
- `app/services/listing_service.py`（RND-219 的监控账号/联系人/会话列表）未被改动 → `get_monitored_accounts` / `get_contacts` / `get_conversations` 行为不变。
- `auth.py` `get_current_user`（`app/auth.py:319`）未被改动 → 全站鉴权回归通过。
- 全量 `pytest` 无新增失败。

---

## 5. 交付报告格式（输出给用户）

```
RND-284 QA 结论: PASS / FAIL
- 验收项: AC-1~AC-7 全部通过 / 失败项(列出)
- 硬约束: 9 项全部满足 / 违反项(列出+证据)
- 测试: 通过 X / 失败 Y（附失败栈关键行）
- 回归: 全绿 / 回归退化(列出)
- 阻塞项(如有): ...
- 备注: msg_count_30d 口径=sender 匹配；last_active_at 实际填充情况观测
```

> 仅输出结论与证据，**不代用户提交**。若 FAIL，明确列出必须返工的开发项，交用户决定是否退回开发 agent。
