# RND-262 验收提示词 — Epic · A1 概览首页 / Dashboard

> **对应开发提示词**：`RND-262-dev-prompt.md`
> **类型**：feature / Epic（概览聚合面板）
> **子票**：RND-281（UsageService）、RND-282（Dashboard API）
> **依赖**：F0（作用域，已满足）；A7/RND-274（AuditLog，recent activity 依赖，未合并 → 降级）
> **Linear**：RND-262

---

## 0. 验收前置

- 确认 `make verify`（lint-diff / typecheck / build / test）全绿。
- 确认 `tasks/RND-262-dev-prompt.md` 硬约束全部满足（见 §5）。
- 确认 `test_http_contract.py:325` 已同步为 `+2`（页面 + API 路由），注释 RND-262。

---

## 1. 验收清单（映射 Linear Acceptance）

| # | 验收项（来自工单） | 验证方式 | 通过标准 |
|---|---|---|---|
| AC1 | 仪表盘渲染**真实聚合**（非写死/假数据） | 种子租户数据后 `GET /api/admin/dashboard/usage?range=30`，比对直接 SQL 聚合值 | `archive_count`/`storage_bytes`/`monitored_employees`/`sync_health` 与 DB 聚合一致 |
| AC2 | **14/30/90 天分段切换改变查询区间** | 前端切 14/30/90；或直接测 API `range=14` vs `range=90` | `archive_count` 随窗口变化；非法 `range` 被拒（400/夹取） |
| AC3 | 被监控员工数 = 租户内去重 `sender` | 比对 `COUNT(DISTINCT sender)` | 值与响应 `monitored_employees` 一致；口径记录在设计说明 |
| AC4 | 同步健康读 `SyncState` | 构造 status=error / syncing | 响应 `sync_health.status` 正确反映；含最近更新时间 |
| AC5 | 最近活动（A7 降级） | AuditLog 缺失时 | `recent_activity == []` 且页面显示「暂无审计数据」占位，API 200 不崩 |
| AC6 | 页面 SSR 可达 | `GET /admin/dashboard` | 未登录 302 `/admin/login`；登录后 200，HTML 含 `data-i18n="dashboard.title"` 与 `.segmented` 控件 |
| AC7 | 不展示消息内容（Non-goal） | 检查 API JSON + 页面 HTML | 响应中无 `content_text`/`decrypted_payload`/消息正文 |

---

## 2. 功能验收步骤（建议顺序）

1. **代码评审**：
   - `app/services/usage.py` 5 个聚合函数均为只读 `func.count/sum/distinct`，强制 `tenant_id` 过滤。
   - `app/routers/dashboard.py` 经 `main.py` `include_router(..., prefix="/api/admin")` 注册；`web.py` 新增 `GET /admin/dashboard`。
   - 无模型/migration 变更；`AuditLog` 未自建。
2. **静态核查**：
   - `test_http_contract.py:325` = 当前基线 + 2，注释 RND-262。
   - `i18n.js` 三语块均含 `dashboard.*`。
   - 前端为 SSR + 原生 JS，无 React / 图表库引用。
3. **自动化测试**：`pytest backend/tests/test_rnd262_dashboard.py -v` 全绿（P1-P7）。
4. **手动 E2E**：
   - 登录 → `/admin/dashboard` 渲染 5 张卡片；概览统计、归档量分段、同步健康、最近活动（占位）、常用入口。
   - 切 14/30/90 → 归档量数字与柱状变化；URL/API `range` 生效。
   - 租户 B 登录看不到租户 A 的数据（隔离）。

---

## 3. 安全 / 隔离专项（不可妥协）

- **租户隔离**：`usage.*` 每个查询带 `tenant_id` 过滤；`tenant_id` 仅来自 `get_current_user`/`require_html_session`，不接收用户参数。跨租户数据零泄露（AC2/P2 覆盖）。
- **只读**：本功能全为 SELECT 聚合；无 INSERT/UPDATE/DELETE；不建表、不改模型。
- **零内容泄露**：响应只含统计量与状态（count/bytes/员工数/同步状态/活动时间），绝不返回消息正文或解密内容。
- **fail-closed**：`range` 非法 / tenant 缺失 / DB 异常 → 合理错误，不 500 泄露堆栈、不返回跨租户数据。
- **A7 降级安全**：AuditLog 缺失时 `recent_activity` 为 `[]`，前端占位；不抛异常、不 500。

---

## 4. 回归测试清单（必须全绿）

- `backend/tests/test_http_contract.py`（route_count 同步）
- `backend/tests/test_i18n_foundation.py`（既有 login 页断言不被破坏）
- `backend/tests/test_auth.py` / `test_password_auth.py`（F0 契约）
- `make verify` 全流程
- 既有页面（`/admin/conversations`、`/admin/search`）路由不受影响

---

## 5. 硬约束合规核对（任一不满足即打回）

- [ ] agent 未执行 git commit/push（交付物仅为两份提示词 + 代码改动，提交由用户本人）
- [ ] `UsageService` 在 `app/services/usage.py`（service 层，无需改 `_FLAT_SERVICE_MODULES`）；dashboard 路由在 `app/routers/dashboard.py` 并由 `main.py` 注册；无 `main.py` 直挂业务路由
- [ ] `test_http_contract.py:325` 已同步（当前基线 +2）并注释 RND-262
- [ ] 前端 SSR + 原生 JS，无 React/SPA/图表库
- [ ] 全聚合强制 `tenant_id` 过滤，来源仅 session
- [ ] 仅只读聚合，无写入/建表/改模型；AuditLog 未自建（A7 职责）
- [ ] 聚合走 `func.count/sum/distinct`，不加载全表；窗口用 `msgtime`（毫秒 epoch）
- [ ] 响应无消息内容；`range` 非法 fail-closed
- [ ] i18n 三语补齐；代码标识符加反引号

---

## 6. 验收结论模板

- **结论**：PASS / FAIL
- **AC1-AC7**：逐条 PASS/FAIL + 证据（API 响应 vs 直查 SQL / 页面 HTML 片段 / 手动步骤）
- **安全专项**：租户隔离 / 只读 / 零内容泄露 / A7 降级 全部 ✅/❌
- **回归**：相关测试全绿 ✅/❌
- **硬约束**：10 项全部 ✅/❌
- **遗留风险**：A7（AuditLog）未合并 → 最近活动为占位，合并后自动填充；被监控员工数口径说明
- **建议**：是否可交用户 review/合并
