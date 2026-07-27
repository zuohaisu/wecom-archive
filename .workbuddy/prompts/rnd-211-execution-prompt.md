# RND-211 开发 agent 执行提示词
> 面向开发 agent（单人端到端实现 RND-211）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（��用户 Haisu 本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）
实现同步感知的会话刷新与"立即同步"操作：后端给 `SyncState` 表新增状态字段 + 同步状态 API + 触发异步 Worker 的端点；前端在控制台添加"立即同步"按钮与同步状态反馈，并结合同步版本变化决定是否自动刷新数据。

## 二、实现范围（精确落点，附 文件:行号:函数/类）

### 后端 — 新建文件
- **`backend/app/routers/sync.py`** — 新建，含两个端点（参考 `reachability_audit.py:65` 的 tenant-scoped admin auth 模式）：
  - `GET /api/admin/sync-status` — 返回当前同步状态（idle/syncing/error + lastSeq + startTime + errorMessage）
  - `POST /api/admin/sync-now` — 使用 FastAPI `BackgroundTasks` 触发异步归档 Worker（调用 `run_archive_worker_once.py` 子进程），立即返回 HTTP 202；Worker 运行期间锁住并发
- **`backend/app/schemas/sync.py`** — 新建，Pydantic 模型 `SyncStatusResponse`、`SyncNowResponse`

### 后端 — 修改文件
- **`backend/app/db/models.py:226`** (`SyncState` 类) — 追加字段：
  - `status: Column(Enum('idle','syncing','error'), default='idle', nullable=False)`
  - `started_at: Column(DateTime(timezone=True), nullable=True)`
  - `error_message: Column(Text, nullable=True)`
  - `seq_version: Column(Integer, default=0)` — 每次同步成功递增，供前端判断"版本有变化才重拉"
- **`backend/alembic/versions/`** — 生成新 Alembic migration，建表变更
- **`backend/app/services/sync_worker.py:93`** (`run_sync_once()`) — 同步前后写入 `SyncState.status`（进入时设为 syncing、成功 idle、失败 error）；同步结束无论成败均更新时间与版本号
- **`backend/app/main.py:93-100`** — 挂载 `sync_router`（`prefix="/api/admin"`，与既有 admin 路由一致）
- **`backend/scripts/run_archive_worker_once.py:135`** — 可选：同步完成后调用回调更新状态（当前靠子进程退出码推断，若 worker 被系统 kill 不更新，可接受为边界情况）

### 前端 — 修改文件
- **`backend/app/web/templates/review_console.html:363,381-384`**：
  - 在导航中启用"同步与任务"（`nav.syncTasks`）— 去掉 disabled 属性
  - 在 refresh-bar 旁边或内部新增"立即同步"按钮（`<button class="btn-sync-now">` + 同步状态指示 `<span id="sync-status">`）
- **`backend/app/web/static/console/refresh.js:110`** (`refreshNow()`) — 新增 `syncNow()` 函数：
  - 调用 `POST /api/admin/sync-now`
  - 成功后开始轮询 `GET /api/admin/sync-status`，更新 `updateSyncStatus()`
  - 当 `seq_version` 变化时自动触发一次 `refreshConversationList()` / `refreshTimelineIfSelected()`
- **`backend/app/web/static/console/console-state.js:33`** — 新增 `syncStatus`（对象）、`syncInProgress`（bool）
- **`backend/app/web/static/console/api-client.js:19-58`** — 新增 `fetchSyncStatus()` 函数
- **`backend/app/assets/i18n.js:57,395,733`** — 在 3 个 locale 各新增：
  - `sync.now` = "立即同步" / "立即同步" / "Sync Now"
  - `sync.inProgress` = "同步中..." / "同步中..." / "Syncing..."
  - `sync.lastSync` = "上次同步：{time}" / "上次同步：{time}" / "Last synced: {time}"
  - `sync.noData` = "暂无同步记录" / "暫無同步記錄" / "No sync records"

### 复用已有工具
- 异步 Worker 子进程：直接调用 `backend/scripts/run_archive_worker_once.py`（含 `fcntl.flock` 防并发，已投产）
- 控制台页面路由：`backend/app/web/web.py:39` `GET /admin/conversations`
- 定时同步兜底机制（systemd timer `wecom-archive-worker.timer`）不动

## 三、验收标准（来自 issue 描述）
1. **同步状态真实更新**：成功归档同步后，`GET /api/admin/sync-status` 返回的 `status` 示 idle、`lastSeq` 推进；零消息成功也要推进 `seq_version`
2. **控制台不再周期性重拉**：在无同步版本变化时（`seq_version` 不变），控制台不重复拉取完整的实体/会话/时间线数据（现有 30s 定时刷新可保留，但改为先检查版本再决定是否重拉；或降低轮询频率并依赖 sync-status 事件驱动）
3. **定时同步完成后自动刷新**：定时同步完成并产生新 `seq_version` 后，控制台在合理延迟内（≤ 10s）刷新当前会话列表与聊天时间线；当前会话的滚动位置与媒体播放状态不丢失
4. **"立即同步"发起真实 Worker**："立即同步"按钮调用 `POST /api/admin/sync-now`，触发真实的异步 WeCom 归档 Worker（`run_archive_worker_once.py`），不是仅刷新本地 API 数据
5. **防并发与安全**：重复点击"立即同步"、定时任务、事件触发之间不会造成并行 Worker（`fcntl.flock` 生效）；权限（仅认证用户可触发）、租户隔离、频率限制（两次"立即同步"间至少 30s）可验证
6. **状态反馈**：同步成功（含无新消息）、排队（已有同步运行中）、运行中、失败状态均有清晰且非敏感的用户反馈
7. **回归测试**：补充 Worker/状态 API/前端交互的 pytest 与可选的 Playwright 测试

## 四、硬约束（违反即判失败）
- **不执行 `git commit` / `git push`**（用户本人提交）
- 不改既有 API 的 URL/status/body/OpenAPI 契约（新增端点 OK，不改已有的）
- 不改 `deploy/systemd/wecom-archive-worker.timer` 的定时调度（定时兜底不动）
- 不改租户隔离/认证逻辑（复用 `get_current_user` 即可）
- 不改 i18n 既有 key 名（新增 key 可）
- 不改前端既有自动刷新基础行为（`startAutoRefresh()` 存在即可，可改为版本感知）
- 同步锁复用既有 `fcntl.flock`（不引入 Redis/distributed lock）
- 与相关工作树改动无冲突前提下最小化改动；冲突则停下报告

## 五、收尾（交付物）
向用户交付：
- 改动文件清单（新建 + 修改）
- `git status` + `git diff`
- 自动刷新行为变更说明（30s 定时刷新是否改为版本感知，在什么条件下触发）
- "立即同步"功能的使用说明
- 不 commit 声明（请 Haisu 本人 `git commit`）
