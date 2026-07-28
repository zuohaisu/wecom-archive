# RND-172 执行提示词（单人端到端：调研 → 复现 → 实现 → 验证）

> 用途：粘贴给单一开发智能体（hy3 / Claude Code / Codex 皆可），由其端到端跑完 RND-172。
> 工单：`RND-172「自动下载新图片媒体」`，优先级 **P4（低）**，状态 **In Progress**，assignee Haisu Zuo。
> 权威来源：Linear RND-172 描述 + 现有代码（已逐项确认，见 §1）；`DEV_AGENT_RULES.md` + `docs/AGENTS.md`。
> 相邻参考：RND-258 / RND-202 / RND-147 执行提示词（媒体管线复用模式）；`rnd-260-*` 提示词（格式与硬约束模板）。

---

## 0. 任务与验收边界

- **目标**：在「企微回调」或「归档同步」完成后，**自动**把新图片消息接入轻量媒体下载流程，优先处理近期图片，并控制资源占用与失败重试——从而缩短新图片从入库到可预览的延迟。本任务合并原 RND-169 的队列控制要求。
- **现状基线（开工前已确认，见 §1）**：系统**仅**有「定时下载」能力（`scripts/download_wecom_media_once.py`，由 systemd timer / cron 驱动）。**不存在**任何事件驱动触发——回调 POST 不触发、sync 不触发、decrypt 不触发、无任何 `queue`/`enqueue` 机制。因此 RND-172 的缺口是「事件触发层」，不是下载能力本身（下载能力已具备且必须复用）。
- **验收边界（硬约束）**：
  - 必须复用既有 `media_download` / `media_worker` 管线；**不另写下载器、不新开 SDK 路径、不新增存储抽象、不新增路由**（路由数基线见 `test_http_contract.py`）。
  - **不引入重型队列基础设施**（Redis / RabbitMQ / DB 队列表）。「队列」= 既有的 `media_files.download_status='pending'` 行 + 「近期窗口 newest-first 扫描」；事件层只需一个**进程内、轻量、可合并**的触发信号。
  - `backend/scripts/` 属 B 层生产路径（RND-242 / RND-237 纪律），**默认不要改**；所有新逻辑放 `app/`（见 §2 与 §4）。
  - 失败隔离、脱敏、`tenant_id` 来源、fcntl 锁语义一律沿用既有约定。

---

## 1. 当前已落地（开工前确认，禁止重写）

> 以下证据已通过代码检索确认存在且在工作树；若某条缺失 → **停下并报告 BLOCKED**，不要自己补做别的子任务的活。

- **唯一下载入口（定时）**：`backend/scripts/download_wecom_media_once.py` 是 SOLE 媒体下载 CLI（L17 文档明确）。已具备 `--limit`（L254）、`--retry`（L255）、`--since-hours`（L265）、`--newest-first`（L266）；非阻塞 `fcntl.flock` 锁（`_acquire_lock` L145，`_DEFAULT_LOCK_PATH` L113）；脱敏日志约定（L58-68）。
- **候选查询（即「优先级/近期」语义）**：`backend/app/media_download.py`：
  - `build_candidate_query`（L115）要求 `decrypt_status == 'success'`、`msgtype.in_(msgtypes)`、`sdkfileid` 非空；支持 `since_ms`（L166，近期窗口）与 `newest_first`（L168-169，近期优先）。**RND-172 要的「队列优先处理新近图片」已由该查询的 `newest_first` + `since_ms` 提供。**
  - `select_candidates`（L258）返回 `(actionable, stale_repairs, total_eligible)`，按 `limit` / `since_ms` / `newest_first` 裁剪——事件层直接复用。
  - `get_or_reset_media_file`（L516）创建 / 重置 `pending` 的 `MediaFile` 行——这就是「待下载队列」条目，事件层不另造队列。
  - `download_one`（L570）单条下载 + 字节签名门 + `.part` 安全落盘，必须复用，不可重写。
  - `GENERIC_DOWNLOAD_MSGTYPES`（L88）含 `"image"`，事件层只扫 `{"image"}`。
- **下载执行 / 持久化**：`backend/app/services/media_worker.py`：
  - `download_media_candidates`（L143）跑全部候选并逐条持久化——事件层 sweeps 直接复用。
  - `_persist_download_outcome`（L75）落库：成功置 `download_status='downloaded'`，失败置 `'failed'`（L117-124）。
  - `MediaDownloadSummary`（L61）汇总下载/失败计数与原因。
- **状态机缺件**：`backend/app/db/models.py` 的 `MediaFile`（L416）仅有 `download_status`（pending/downloaded/failed），**没有 `download_attempts` 列**——RND-172 的「失败重试次数上限 + 退避」需要它来跨进程/重启持久化重试计数（见 §2.2）。
- **事件完成点（天然 hook）**：
  - `backend/scripts/run_archive_worker_once.py` 在单个 `fcntl.flock` 下先跑 sync 再跑 decrypt（L144-145）。decrypt 完成后新图片即 `decrypt_status='success'`，成为可下载候选。此处是「归档同步/解密完成」最精确的触发点。
  - `backend/app/services/decrypt_worker.py`：`run_decrypt_once`（L420）解密一批后 `session.commit()`（L593）返回。
  - `backend/app/routers/sync.py`：`sync_now`（L129）经 `BackgroundTasks.add_task(_run_archive_worker, ...)`（L182）触发归档 worker——web 侧事件触发 hook。
  - `backend/app/routers/wecom_events.py`：POST 仅做签名校验后返回 `"ok"`（L167-195），明确注释「no worker trigger in RND-105」，RND-107 才会在有效 POST 后触发归档 worker。回调事件触发点在此。
- **架构护栏**：`backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（L70）= 允许的反向依赖白名单；新增的扁平领域模块必须登记于此（见 §2.3）。`main` 组合根纯度规则见 `DEV_AGENT_RULES.md`（无新业务路由、无直接 DB 查询）。

> 开发 agent 第一步：用 `git log --oneline -3` 与 `git status` 确认当前在 `main` 且工作树干净，再逐条核对上述文件确实存在、行号仍可对应（行号可能随后续提交偏移，以函数名为准）。

---

## 2. 精确落点（文件 / 函数级）

### 2.1 配置 · `backend/app/settings.py`（镜像 `ThumbnailSettings` L99 / `VoiceTranscodeSettings` L109）
新增 `EventMediaDownloadSettings`（`BaseSettings`）与 `get_event_media_download_settings()`，字段（默认**关闭**，fail-closed）：
- `event_media_download_enabled: str = ""`（仅当显式 `"true"` 才启用；缺省/空 = 关闭，回退为现状定时下载）。
- `event_media_download_batch_limit: str = "20"`（单次 sweep 最多处理的近期图片数）。
- `event_media_download_recent_window_hours: str = "24"`（sweep 的 `since_hours` 近期窗口）。
- `event_media_download_retry_count: str = "3"`（单 key 最大重试次数，超过则停止重试、留 `failed`）。
- `event_media_download_backoff_seconds: str = "30"`（退避基数，实际等待 = `backoff * min(2**(attempts-1), 8)`，封顶）。
- `event_media_download_sweep_interval_seconds: str = "15"`（进程内触发信号合并间隔，避免每条消息一次 sweep）。

### 2.2 数据模型 + 迁移 · `backend/app/db/models.py` + 新增 Alembic 迁移
- `MediaFile`（L416）新增一列（不可空，默认 0）：`download_attempts: int`（记录该 sdkfileid 已被尝试下载的次数，用于重试上限与退避判定）。
- 新增 Alembic migration（置于现行 head 之后），仅 `add_column`；downgrade 对应 `drop_column`。迁移不得动其他列。

### 2.3 事件分发器（核心新增）· 新增 `backend/app/media_event_dispatch.py`
- **登记**：把 `"app.media_event_dispatch"` 加入 `backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（L70）——否则护栏测试会失败。
- 提供（保持纯函数 / 可测试，失败隔离）：
  - `run_recent_image_sweep(tenant_id: str) -> MediaDownloadSummary`（**同步核心**，测试可直接调用）：
    1. 取配置 `get_event_media_download_settings()`；若未启用直接返回空 summary（fail-closed）。
    2. 复用既有 `download_wecom_media_once.py` 的同款非阻塞 `fcntl.flock`（`MEDIA_DOWNLOAD_LOCK_PATH`，锁文件默认 `/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock`）——锁被占则安全 no-op 返回（不阻塞、不报错）。这保证与定时 cron、归档 worker 永不对同一 key 并发。
    3. 加载 SDK（`wecom_sdk.load_sdk` / `configure_sdk` / `new_sdk` / `init_sdk`），复用 `download_wecom_media_once.py:_run` 的 SDK 生命周期；失败按脚本既有方式退出/返回。
    4. 调 `select_candidates(tenant_id, {"image"}, retry=True, limit=batch_limit, since_ms=recent_window_ms, newest_first=True)` —— 近期优先、按窗口裁剪、且带上 `failed` 行重试（`retry=True`）。
    5. 调 `download_media_candidates(...)` 执行并持久化（逐条走 `_persist_download_outcome` 语义）；每个候选落库前把 `download_attempts += 1`（成功或失败都计）。
    6. **重试上限 / 退避 / 不阻塞**：sweep 选 `failed` 行时，仅当 `download_attempts < retry_count` 且距上次尝试已过去 `backoff` 才纳入；达到上限的 key 留 `failed`、不再纳入，因此**旧失败任务永不阻塞新图片**。退避等待通过 `since` 时间判定实现（无需 sleep 阻塞 sweep）。
    7. 释放 SDK（`destroy_sdk`），发**结构化资源占用日志**（见 §2.6），返回 `MediaDownloadSummary`。
  - `trigger_recent_image_download(tenant_id: str) -> None`（**事件入口**，fire-and-forget、非阻塞）：
    - 懒启动单例后台 daemon 线程（首次触发时启动，幂等；**无需改 `main.py`**）。
    - 把 `(tenant_id)` 合并进进程内有界队列（去重、按 `sweep_interval_seconds` 合并），线程到点调用 `run_recent_image_sweep`。
    - 若配置未启用，直接返回（fail-closed）。
  - 提供 `shutdown()`（测试用，停止线程）。
- **严禁**：新下载器、新 SDK 封装、新存储抽象、任何 DB 队列表、任何外部消息中间件。

### 2.4 触发接线（全部在 `app/`，不碰 `backend/scripts`）
- **web 同步触发（主路径）**：`backend/app/routers/sync.py` 的 `sync_now`（L129），在 `background_tasks.add_task(_run_archive_worker, tenant_id, config.corp_id)`（L182）之后，增加 `background_tasks.add_task(_trigger_via_dispatcher, tenant_id)`（新增私有包装调 `trigger_recent_image_download(tenant_id)`）。归档 worker 跑完 sync+decrypt 后，本进程的 dispatcher 线程随即 sweep 近期图片——实现「归档同步完成后自动下载」。
  - `tenant_id` 必须来自 `get_current_user`（L132 已注入），**绝不**接受调用方输入。
- **回调触发（次路径）**：`backend/app/routers/wecom_events.py` 的 `wecom_callback_post`（L167），在签名校验通过后（L189 之后、返回 `"ok"` 前），best-effort 调 `trigger_recent_image_download(<解析出的 tenant_id>)`。注意：`wecom_events.py` 当前**不持有** tenant 解析（仅校验签名）；触发时从 `corp_id` 经 `TenantWecomConfig` 解析 active tenant（镜像 `download_wecom_media_once.py:_require_tenant_id` 的查法，但只读、不 sys.exit）。若解析失败则静默跳过（回调处理不得因媒体下载失败而报错）。此改动**不**替代 RND-107 的归档 worker 触发，仅额外接入图片下载。
- **不改动** `run_archive_worker_once.py` / `decrypt_wecom_messages_once.py` / 任何 `backend/scripts/*`（B 层纪律，见 §4）。

### 2.5 配置样例 · `.env.example`（仅占位，不提交真实值）
在 `.env.example` 追加与 `EventMediaDownloadSettings` 对应的占位注释行（`event_media_download_enabled`、`event_media_download_batch_limit` 等），值为空或示例。不写真实密钥。

### 2.6 资源占用 / 生产验证记录
- 每次 `run_recent_image_sweep` 必须输出一条**结构化日志**（JSON 或固定字段），含：`tenant_id`（可哈希/脱敏）、`triggered_by`（sync/callback）、`targeted`（本次扫描候选数）、`downloaded`、`failed`、`failed_reasons`（聚合计数，绝不含 sdkfileid/path）、`bytes_downloaded`、`duration_ms`、`peak_rss_kb`（可选，`resource.getrusage`）。这是 RND-172 验收项「有资源占用和生产验证记录」的落地。
- 日志**严禁**含：`sdkfileid`、`local_path`、`storage_ref`、`oss_key`、signed URL、媒体字节（对照 `download_wecom_media_once.py:58-68`）。

### 2.7 测试（先 RED 后 GREEN）
新增 `backend/tests/test_rnd_172_event_media_download.py`：
- T1（RED→GREEN，缺口证明）：构造一条 `decrypt_status='success'`、`msgtype='image'`、`sdkfileid` 非空、且无 `MediaFile` 行的 `ArchiveMessage`；在「事件分发未接线」状态下，断言短时间内（无定时 cron）该图片**不会**自动变 `downloaded`（RED）。实现后，经 `trigger_recent_image_download(tenant_id)` + 跑一次 `run_recent_image_sweep`，断言变 `downloaded`（GREEN）。
- T2（近期优先）：插入一张「旧 failed 图片」+ 一张「新 pending 图片」，单次 sweep 断言**先下载新图**（验证 `newest_first`）。
- T3（重试上限 / 不阻塞）：用 fake SDK 让某 sdkfileid 持续失败；连续 sweep 至 `download_attempts >= retry_count` 后，断言该 key 不再被重试（留 `failed`），且同批新图片仍可正常下载（旧失败不阻塞新图）。
- T4（退避）：达到上限前的两次失败之间，断言第二次未在当前 sweep 立即重试（间隔 < backoff 时不纳入）。
- T5（资源日志）：sweep 后断言日志含 `downloaded`/`failed`/`duration_ms` 字段且**不含**任何 `sdkfileid`/`local_path`/`storage_ref`/`oss_key` 字面量（用 `caplog` 检索）。
- T6（架构护栏）：`test_architecture_boundary.py` 全绿（新模块已登记）。
- T7（路由数不变）：`test_http_contract.py` 全绿（未新增路由）。
- 安全脱敏回归：复用 `test_download_wecom_media_once.py` / `test_media_worker_service.py` 现有断言不被破坏。

---

## 3. 执行步骤（严格：复现(RED) → 实现 → 验证(GREEN)）

### 阶段一：确认已落地（先证明不缺，禁止先改实现）
- 逐条核对 §1 的文件/函数证据确实存在且在工作树。`git status` 应为干净（在 `main`）。
- 跑回归基线（先 `cd backend`）：
  - `python -m pytest tests/test_media_download.py tests/test_media_worker_service.py tests/test_download_wecom_media_once.py tests/test_architecture_boundary.py tests/test_http_contract.py -q`
  - 任一失败 → **停下报告 BLOCKED**，不随手改无关代码。

### 阶段二：复现 / 红灯（先证明缺口，禁止先改实现）
- 新增 `test_rnd_172_event_media_download.py`，T1 断言「解密后的新图片不会自动下载」（当前无触发 → RED）。运行确认 RED。**绝不假装修好。**

### 阶段三：实现（最小改动，严守 §2 + §4）
- 2.1 配置；2.2 `MediaFile.download_attempts` + 迁移；2.3 `media_event_dispatch.py`（登记护栏）；2.4 `sync.py` + `wecom_events.py` 触发；2.5 `.env.example` 占位；2.6 资源日志；2.7 测试。
- 全程遵守硬约束（§4）。**优先选「不碰 `backend/scripts`」的 in-app 设计**。

### 阶段四：验证（不达标不收工）
- 阶段二 T1 必须转 **GREEN**；T2–T7 全绿。
- 运行（先 `cd backend`）：
  - `python -m pytest tests/test_rnd_172_event_media_download.py tests/test_media_download.py tests/test_media_worker_service.py tests/test_download_wecom_media_once.py tests/test_architecture_boundary.py tests/test_http_contract.py -q`
  - 重点盯：`test_http_contract.py`（路由数未增）、`test_architecture_boundary.py`（新模块已登记）、脱敏回归。
  - 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。

---

## 4. 硬性约束（不可违反）

- **工作直接在 `main` 上**；不创建 task 分支（Haisu 显式要求才破例）。
- **绝不自行 `git commit` / `git push`**（需用户显式授权）。
- **最小正确改动**：下载能力已具备，只补「事件触发层」；复用 `select_candidates` / `download_media_candidates` / `get_or_reset_media_file` / `download_one` / 存储 provider / fcntl 锁。**不另写下载器、不新开 SDK 路径、不新增存储抽象、不新增路由。**
- **B 层纪律**：默认**不修改 `backend/scripts/`**（RND-242 / RND-237 发布导出处理）。所有新逻辑放 `app/`。若 Haisu 明确许可改脚本（例如把媒体下载作为 `run_archive_worker_once.py` 的第三步行），那是更低风险的备选——但默认走 in-app 设计；拿不准就停下问 Haisu。
- **不引入重型队列基础设施**：进程内轻量合并队列即可；无 Redis / RabbitMQ / DB 队列表。
- **Fail-closed**：`event_media_download_enabled` 默认关闭；开启需显式 `"true"`。
- **并发 / 资源（2 核 2GB）**：单次 sweep 顺序执行、按 `batch_limit` 有界；触发信号按 `sweep_interval_seconds` 合并，绝不「每条消息一次 sweep」；fcntl 锁保证与定时 cron / 归档 worker 不并发。
- **租户隔离铁律**：`tenant_id` 永远来自 `get_current_user` 或 `TenantWecomConfig` 解析（corp_id 来自配置/签名），**绝不接受调用方输入**。
- **不阻塞核心**：事件触发 fire-and-forget、非阻塞；任何 SDK/存储失败都隔离在 sweep 内，不影响 sync / 回调响应。
- **日志脱敏**：任何日志/异常**不含** `sdkfileid`、`local_path`、`storage_ref`、`oss_key`、signed URL、媒体字节（对照 `download_wecom_media_once.py:58-68`）。
- **架构护栏**：新扁平模块必须加入 `_FLAT_SERVICE_MODULES`；`main.py` 不新增业务路由 / 直接 DB 查询；router 不反向 import `app.main`。
- 参考 `DEV_AGENT_RULES.md`、`docs/AGENTS.md`、`docs/ai/known-pitfalls.md`。

---

## 5. 收尾动作

- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 在 Linear 把 RND-172 状态保持 **In Progress**（或视推进置），写一条评论：改动文件/函数级清单 + 红灯→绿灯证据 + `make verify` 结论 + **明确标注「已实现（代码+fixture）」或「已 Production Verified（仅当有真实企微图片样本 + 真实环境实测）」** + 真实环境验证步骤与限制说明（含退避/重试上限边界、2 核 2GB 资源占用记录位置）。
- 若实现中发现需要比预期更大的改动或存在未覆盖场景，在评论中**如实标注边界**，不私自扩展范围。
