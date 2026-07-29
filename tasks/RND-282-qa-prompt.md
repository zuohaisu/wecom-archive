# RND-282 QA 验收提示词 —— A1-2 Dashboard 聚合统计 API

> 面向测试 agent（独立验收 RND-282 的开发交付）。本文件即你的完整 brief。
> 全程不执行 git commit / push。仅验收、出报告，交用户决策是否提交。

## 一、验收对象

RND-282（A1-2 Dashboard 聚合统计 API）的开发交付。核心交付：
- `GET /api/admin/dashboard?range=14|30|90` 端点（租户隔离聚合）。
- **复用 A1-1 `app.services.usageservice`** 概览原语；本票新增 `app/services/dashboard_service.py`（窗口专属聚合）+ `app/schemas/dashboard.py` + `app/routers/dashboard.py` + `app/main.py` 接线 + `tests/test_dashboard_api.py`。

**验收必须基于开发提示词 `RND-282-dev-prompt.md` 的逐条要求 + Linear RND-282 的 Acceptance Criteria + 项目架构边界 / 租户隔离 / SF-1 数据最小化纪律。**

## 二、验收依据（必读）

1. `RND-282-dev-prompt.md`（开发 brief，含精确落点与硬约束）。
2. Linear RND-282 描述与 Acceptance Criteria：*渲染真实聚合；区间切换改变查询区间。*
3. `backend/tests/test_architecture_boundary.py`（依赖方向 + 组合根纯净）。
4. `backend/tests/test_tenant_isolation.py`、`test_reachability_audit.py`（租户隔离范式）。
5. SF-1 数据最小化：绝不返回消息内容体（`content_text` 等）。
6. 关联票 RND-281（A1-1，`app.services.usageservice`）、RND-283 硬约束（不重复实现 A1-1 职责）。

## 三、验收矩阵（逐条，PASS / FAIL + 证据）

| # | 验收点 | 验证方式 | 判定 |
|---|--------|----------|------|
| AC1 | **A1-1 前置已落实（否则开发应已 STOP）** | `python -c "import app.services.usageservice"` 必须成功；若失败则开发交付须是 BLOCKED 报告、且**未**创建任何 `usage.py`/`usageservice.py` 模块 | 成功或合规 STOP=PASS |
| AC2 | **渲染真实聚合** | seed 已知消息数 / 媒体字节 / 员工 / sync 行，断言 `total_messages`、`storage_bytes`、`staff_count`、`sync_healthy`、`days` 与手算一致（概览值来自 `usageservice`） | 一致=PASS |
| AC3 | **区间切换改变查询区间** | `?range=14` vs `?range=90`，断言 `total_messages`、`daily_series` 长度 / 和不同 | 不同=PASS |
| AC4 | **租户隔离** | seed tenantA + tenantB 数据，用 tenantA 的 `get_current_user` override，断言响应不含 tenantB 数据 | 不含=PASS |
| AC5 | **无消息内容泄露** | 解析响应 JSON，grep 不应出现 `content_text` / 任何消息正文片段；代码层面断言聚合查询未 SELECT 消息体 | 无=PASS |
| AC6 | **A7 缺失优雅降级** | 确认 `app/db/models.py` 无 `AuditLog`；`GET` → 200 且 `recent_activity == []`，**绝不 500** | 空列表=PASS |
| AC7 | **同步健康不编造** | 响应仅含 `sync_status` + `sync_healthy`；**不得**出现设计稿 5 行组件条（会话拉取 / 媒体下载 / 解密 worker / 通讯录同步 / 冷存储） | 未编造=PASS |
| AC8 | **非法 range 归一化** | `?range=7` → 200 且 `range_days == 30`；`?range=120` → 200 且 `range_days == 30` | 归一化=PASS |
| AC9 | **daily_series 自洽** | 长度 == `range_days`；Σ(text+media) 跨天 == `total_messages`；无消息日补 0 | 自洽=PASS |
| AC10 | **未认证拒绝** | override `get_db` 使会话缺失 → 断言 401 | 401=PASS |
| AC11 | **storage 稳定** | `range=14` 与 `range=90` 的 `storage_bytes` 相同（累计非窗口） | 相同=PASS |
| AC12 | **无 schema 变更** | `alembic check` 绿；`git diff` 无新 migration 文件；`grep -rn "class AuditLog\|audit_logs" app/db/models.py` 无（A7 未越权） | 绿 + 无=PASS |
| AC13 | **架构边界** | `app/services/dashboard_service.py` 不 import `routers`/`main`；`app/routers/dashboard.py` 不 import `app.main`；`app/main.py` 仅 `include_router` | 符合=PASS |
| AC14 | **不重复实现 A1-1** | `grep -rn "app/services/usage.py"` 不应存在；开发未自创 `UsageService` 模块；概览原语来自 `app.services.usageservice` | 未重复=PASS |
| AC15 | **不 commit** | 工作树有改动但无新 commit（`git status` 显示未提交） | 未提交=PASS |

## 四、安全 / 契约核查

- **鉴权**：端点依赖 `get_current_user`；未认证 → 401（AC10）。
- **租户作用域**：每个聚合查询必须 `filter(tenant_id == auth[1])`，响应不得出现其他租户数据（AC4）。
- **日期精度**：`msgtime` 为 epoch 毫秒；`daily_series[].date` 为 `YYYY-MM-DD`（北京时间）；`generated_at` 为 ISO8601。
- **响应契约**：`response_model=DashboardOut` 字段与开发提示词第二节 Schema 完全一致（新增字段须有理由，否则判 FAIL）。
- **范围守门**：RND-282 不得触碰 `get_current_user` / WeCom OAuth / 登录路径 / B 层路径（`.env.example`、`backend/scripts`、deploy.yml、systemd）；不得新建 A1-1 或 AuditLog 模块（AC1 / AC12 / AC14）。

## 五、回归套件（必须全绿）

```bash
cd backend
python -m pytest tests/test_dashboard_api.py -q     # RND-282 新测试
python -m pytest tests/test_architecture_boundary.py -q   # 边界不被破坏
python -m pytest tests/test_tenant_isolation.py -q        # 租户隔离不被破坏
python -m pytest tests/test_http_contract.py -q           # 契约不被破坏（若新路由影响）
make verify                                          # 全绿
alembic check                                        # 绿
```

## 六、RED → GREEN 与智能路由

- **若 `app.services.usageservice` 不存在**：
  - 开发交付若是「BLOCKED 报告」且**未**创建任何 `usage.py`/`usageservice.py` 模块 → AC1 PASS（合规停止）。
  - 开发交付若是实际实现了 endpoints 或自建了 `usageservice.py`/`usage.py` → 判 FAIL（违反「不重复实现 A1-1」硬约束，且与 RND-283 决策冲突）。
- **若 `app.services.usageservice` 已存在**（RND-281 已合并）：
  - 断言开发**复用**了它（grep 导入），未重新实现概览原语。
  - 断言概览值（`storage_bytes`/`staff_count`/`sync_health`）确实来自该模块（T3 用已知种子校验透传）。
- **若 A7 已落地**（`AuditLog` 存在）但开发未接 `recent_activity` → 视为可选增强，不强制判 FAIL（本票范围默认 A7 缺失）；可建议补一条「AuditLog 存在时 recent_activity 非空」的用例。
- **若开发 agent 误建了 `audit_logs` migration 或 `AuditLog` 模型** → 判 FAIL（越权建 A7，违反 AC12）。
- **若响应出现消息正文 / 5 行虚构同步组件 / 跨租户数据** → 判 FAIL（AC5 / AC7 / AC4）。

## 七、收尾（交付物）

向用户交付验收报告：
- 总体结论：**PASS / FAIL（含阻塞项）**。
- 逐条验收矩阵结果（AC1–AC15）与关键证据（断言片段 / `git diff --stat` / `make verify` 日志 / `alembic check` 输出 / `python -c "import app.services.usageservice"` 结果）。
- 是否发现范围越权（建 A7 / 自创 A1-1 / 碰 B 层 / 改鉴权）。
- 若 A1-1 未就绪，确认开发是否合规 STOP（而非越权实现）。
- 未提交声明（确认开发 agent 未 commit）。
