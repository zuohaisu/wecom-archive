# RND-262 执行提示词 — Epic · A1 概览首页 / Dashboard

> **类型**：feature / Epic（概览首页聚合面板）
> **父 Epic**：无（自身为 A1 Epic，parent=RND-263 F0 方向）
> **子票**：RND-281（A1-1 UsageService 聚合模块，`[BLOCKED: F0]`）、RND-282（A1-2 Dashboard 聚合统计 API，`[BLOCKED: A1-1, A7]`）
> **优先级**：High (2) / **状态**：In Progress
> **标签**：epic
> **Linear**：RND-262

本提示词是 A1 Dashboard 的**权威实现 brief**，覆盖子票 RND-281 + RND-282 的全部工作，并明确分解顺序与依赖闸门。开发 agent 按本文件落地，子票 ID 仅用于 Linear 进度跟踪。

---

## 0. 现状核查结论（已验证）

- **完全未实现**：`grep -rniE "dashboard|usageservice"` 在 `backend/app` 零命中；`app/web/templates/` 无 `dashboard.html`；`app/routers/` 无 dashboard 路由；`app/services/usage.py` 不存在。
- 视觉契约已存在：`design/ui-v1/pages/dashboard.html`（10.7KB，设计稿，仅参考、未接线）。
- 数据模型可用（无需 migration）：
  - `ArchiveMessage`（`models.py:305`）：聚合主表。`tenant_id`、`sender`(String64，企微 userid，已索引)、`msgtime`(BigInteger，**毫秒 epoch**，见 `routers/search.py:284` `ms epoch`)、`msgtype`、`roomid`。
  - `MediaFile`（`models.py:461`）：`tenant_id`、`file_size`(BigInteger nullable, L538)、`download_status`。
  - `SyncState`（`models.py:272`）：`tenant_id`、`corp_id`、`status`(idle/syncing/error)、`last_seq`、`error_message`、`updated_at`。
  - `AdminUser`（`models.py:132`）：`wecom_user_id`、`tenant_id`（可选源）。
- **硬闸门**：
  - **F0 作用域**：所有查询必须按 `get_current_user` / `require_html_session` 返回的 `tenant_id` 过滤（与现有 `web.py` 页面一致）。F0-2 未合并不影响本票（本票只做**只读聚合**，不改写、不需 per-user 鉴权逻辑）。
  - **A7 / RND-274（AuditLog）**：`AuditLog` 模型**当前不存在**（`models.py` 无该类）。「最近活动」卡片依赖它 → **A7 未合并前，最近活动卡片必须降级**（见 §3.4），不得自行建 AuditLog 表（那是 A7 职责）。

---

## 1. 任务概述（来自 Linear 工单）

管理员一眼看到**归档规模 / 健康度 / 最近活动**。指标：
1. **归档规模**：`archive_messages` 在所选窗口内的消息数。
2. **存储用量**：`SUM(media_files.file_size)` + DB 估算（archive_messages 体量）。
3. **被监控员工数**：租户内 `archive_messages.sender` 去重计数（企微 userid）。
4. **同步健康**：读 `SyncState`（status / 最近更新 / 错误）。
5. **最近活动**：近期 `AuditLog`（**依赖 A7**）。
- **建议**：抽 `UsageService` 聚合模块，**供 A2（用量统计）复用**（A2 是 RND-266）。
- **Non-goals**：不展示消息内容；不做实时推送（依赖 RND-211 轮询刷新）。
- **Acceptance**：仪表盘渲染真实聚合；**14/30/90 天分段切换改变查询区间**。

---

## 2. 实现方案（分解顺序）

### 阶段 1 — A1-1 UsageService 聚合模块（= RND-281，可现在做）
新建 `backend/app/services/usage.py`（service 层；`_LAYER_PACKAGES` 已含 `app.services`，**无需改 `_FLAT_SERVICE_MODULES`**）。

提供纯函数（均接收 `db: Session, tenant_id: str, days: int`）：
```python
def archive_count(db, tenant_id, days) -> int:
    # COUNT(*) FROM archive_messages WHERE tenant_id=? AND msgtime >= now_ms - days*86400_000
def storage_bytes(db, tenant_id) -> int:
    # COALESCE(SUM(file_size),0) FROM media_files WHERE tenant_id=?
    # + DB 估算（archive_messages 行数 * 经验字节系数，注明为估算）
def monitored_employee_count(db, tenant_id) -> int:
    # COUNT(DISTINCT sender) FROM archive_messages WHERE tenant_id=? AND sender IS NOT NULL
def sync_health(db, tenant_id) -> dict:
    # 读 SyncState（同 tenant 最新一行）：status / updated_at / error_message / last_seq
def recent_activity(db, tenant_id, limit=10) -> list[dict] | None:
    # 若 AuditLog 模型可用 → 查近期审计行；否则返回 None（交由前端降级）
```
- `msgtime` 为**毫秒 epoch**（对齐 `search.py:284`）。`now_ms = int(time.time()*1000)`。
- 全部走聚合 SQL（`func.count` / `func.sum` / `func.distinct`），**禁止加载全表行**。
- 租户过滤不可省略（fail-closed：tenant_id 只来自 session，不接收用户参数）。
- 复用既有 DB session（`app.db.session.get_db`）。

### 阶段 2 — A1-2 Dashboard 聚合统计 API（= RND-282，可现在做前 4 项）
新建 `backend/app/routers/dashboard.py`：
```python
router = APIRouter()
@router.get("/api/admin/dashboard/usage")
def dashboard_usage(range: int = Query(30, alias="range", description="14|30|90"),
                   ctx: tuple = Depends(get_current_user)):
    # ctx = (AdminUser, tenant_id)；days = range
    return {
        "range_days": range,
        "archive_count": usage.archive_count(db, tenant_id, range),
        "storage_bytes": usage.storage_bytes(db, tenant_id),
        "monitored_employees": usage.monitored_employee_count(db, tenant_id),
        "sync_health": usage.sync_health(db, tenant_id),
        "recent_activity": usage.recent_activity(db, tenant_id) or [],  # A7 前为 []
        "generated_at": now_ms,
    }
```
- 在 `app/main.py` 注册：`app.include_router(dashboard_router, prefix="/api/admin")`（仿 `sync_router` L99）。
- `range` 仅接受 14/30/90（其它 → 400 或夹取到最近档，二选一并在测试覆盖）。
- **最近活动**：A7 未合并时 `recent_activity` 返回 `[]`，前端显示「暂无审计数据」占位；A7 合并后自动填充（UsageService 已做可用检测）。

### 阶段 3 — Dashboard 页面（SSR，D1 冻结；归属本 epic，与 API 同批）
- 新建 `backend/app/web/templates/dashboard.html`：复制 `review_console.html` 的 shell（design-system class、i18n bootstrap、侧栏），按 `design/ui-v1/pages/dashboard.html` 的卡片结构实现：
  1. **概览统计行**：存储占用 / 监控员工数 / 同步健康度（3 个 `.stat`）。
  2. **近 14/30/90 天归档量**：`.segmented` 三段切换（默认 30），切换时 `fetch('/api/admin/dashboard/usage?range=N')` 重渲染数字 + 简易柱状（纯 CSS/SVG，不引图表库）。
  3. **同步健康**：读 `sync_health`，状态徽标 + 最近更新时间。
  4. **最近活动**：`recent_activity[]`（A7 前占位）。
  5. **常用入口**：会话审阅 / 搜索 / 可达性诊断 链接（复用 `review_console.html` 侧栏）。
- 在 `app/routers/web.py` 加 `GET /admin/dashboard`（仿 `admin_conversations` L39）：`require_html_session` → 未登录 302 `/admin/login` → `render_template("dashboard", i18n_script=I18N_SCRIPT_TAG)`。
- **纯原生 JS**（D1）：分段切换的 fetch + DOM 更新写在页面内 `<script>`，**禁止 React/SPA**。

---

## 3. 依赖与设计决策

- **架构落点**：
  - `app/services/usage.py`（新增，service 层）
  - `app/routers/dashboard.py`（新增，router 层，需在 `main.py` 注册）
  - `app/web/templates/dashboard.html`（新增模板）
  - `app/routers/web.py:admin_dashboard`（新增页面路由）
  - `app/assets/i18n.js`（新增 `dashboard.*` 三语 key）
  - `backend/tests/test_http_contract.py:325`（路由基线 `==38` → +2 = `==40`；注释 RND-262）
- **租户作用域**：所有聚合 SQL 强制 `tenant_id` 过滤；`tenant_id` 仅来自 `get_current_user` / `require_html_session`。
- **被监控员工数口径**：默认 = `archive_messages.sender` 去重（`sender IS NOT NULL`）。若产品意图是「已同步通讯录员工」，改用 `contacts` 表（`models.py:729 Contact`，需确认其 `tenant_id` 与员工标识字段）→ 在实现说明里记录选用口径及理由。
- **存储估算**：媒体文件 `SUM(file_size)` 为真实值；DB 体量为估算（标注 `estimated=true`），不写精确行字节。
- **msgtime 单位**：毫秒 epoch，窗口下界 `now_ms - days*86_400_000`，复用 `search.py` 既有约定。
- **A7 降级**：`recent_activity` 在 AuditLog 缺失时返回 `[]`；前端不因此崩；A7 合并后无需改前端即可填充。

---

## 4. RED → GREEN 实施步骤

1. 建 `app/services/usage.py`，实现 5 个聚合函数（租户过滤、聚合 SQL、毫秒窗口）。
2. 建 `app/routers/dashboard.py` + `main.py` 注册（`prefix="/api/admin"`）。
3. `web.py` 加 `GET /admin/dashboard` 页面路由；建 `dashboard.html` 模板（SSR + 原生 JS 分段切换）。
4. `i18n.js` 三语块加 `dashboard.*`（概览标题、存储占用、监控员工数、同步健康度、近N天归档量、最近活动、暂无审计数据、常用入口等）。
5. `test_http_contract.py:325` 改 `== 40`，注释 RND-262（+2 路由：页面 + API）。
6. 新测试 `backend/tests/test_rnd262_dashboard.py`（见 §6）。
7. 跑 `make verify`（lint-diff / typecheck / build / test）全绿。

---

## 5. i18n 硬要求

- 新增 `dashboard.*` key（建议集，至少 zh/en，项目惯例三语全加）：
  `dashboard.title` / `dashboard.storageUsage` / `dashboard.monitoredEmployees` / `dashboard.syncHealth` / `dashboard.archiveVolume` / `dashboard.recentActivity` / `dashboard.noAuditData` / `dashboard.quickLinks` / `dashboard.days14` / `dashboard.days30` / `dashboard.days90` / `dashboard.syncStatus.idle|syncing|error`。
- 三语块全部补齐：`app/assets/i18n.js` 的 zh-CN / zh-TW / en（对齐 `login.*` 既有结构）。

---

## 6. 测试要求（`test_rnd262_dashboard.py`）

- **P1（API 聚合）**：用 TestClient + 租户 seed 数据，断言 `/api/admin/dashboard/usage?range=30` 返回真实 `archive_count`/`storage_bytes`/`monitored_employees`/`sync_health`，值与直接 SQL 一致。
- **P2（租户隔离）**：tenant A 的数据不泄露到 tenant B 的响应（`tenant_id` 强制过滤）。
- **P3（分段切换）**：`range=14` vs `range=90` 的 `archive_count` 不同（窗口生效）；非法 `range` 被拒（400 或夹取）。
- **P4（同步健康）**：构造 `SyncState` status=error → 响应 `sync_health.status=="error"`。
- **P5（A7 降级）**：AuditLog 缺失时 `recent_activity == []` 且 API 200（不崩）。
- **P6（页面）**：`GET /admin/dashboard` 未登录 302 `/admin/login`；登录后 200 且 HTML 含 `data-i18n="dashboard.title"` 与 `.segmented`（仿 `test_i18n_foundation.py` 对 login 页的断言）。
- **P7（回归）**：`test_http_contract.py` route_count==40；现有 `web`/`auth`/`search` 测试不受影响。

---

## 7. 硬约束（违反即打回）

1. **绝不 commit/push**：agent 不执行 git 提交；交付=本执行提示词 + QA 提示词，用户本人决定提交。
2. **架构边界**：`UsageService` 放 `app/services/usage.py`（service 层，无需改 allowlist）；dashboard 路由走 `app/routers/dashboard.py` 并在 `main.py` 注册；**禁止在 `main.py` 直接 `@app.get` 业务路由**。
3. **route-count 基线**：`test_http_contract.py:325` 改 `== 40` 并注释 RND-262。
4. **D1 冻结**：前端 SSR + 原生 JS，**禁止 React/SPA / 图表库**；复用 design-system class 与 `base.css`。
5. **租户作用域**：所有聚合强制 `tenant_id` 过滤，来源仅 `get_current_user`/`require_html_session`。
6. **只读聚合**：本票只做 SELECT 聚合，**不写入、不建表、不改模型**（AuditLog 属 A7，禁止自建）。
7. **性能**：聚合走 `func.count/sum/distinct`，不加载全表；窗口用 `msgtime` 索引（毫秒 epoch）。
8. **fail-closed**：`range` 非法、tenant 缺失、DB 异常 → 合理错误（不返回跨租户数据、不 500 泄露）。
9. **零泄露**：不返回消息内容（`content_text`/`decrypted_payload` 永不进响应）；仅统计量与状态。
10. **i18n 三语** + **代码标识符加反引号**。

---

## 8. 交付报告格式

- 实现摘要（usage.py 函数清单、2 新路由、dashboard.html 卡片、i18n key 清单）；
- 依赖状态：F0 作用域已满足；**A7（AuditLog）未合并 → 最近活动降级为占位**（明确标注）；
- `make verify` 结果；
- `test_http_contract.py` 基线是否同步 `== 40`；
- 回归测试结果；
- 遗留/风险：A7 合并后最近活动自动填充；被监控员工数口径选择及理由。
