# RND-172 QA / 验收 agent 提示词
> 面向独立测试 / QA agent。只读言、不改实现、不 commit / push。
> 验收对象：开发 agent 按 `rnd-172-execution-prompt.md` 产出的改动（新增 `app/media_event_dispatch.py` + `MediaFile.download_attempts` + 迁移 + `settings.py` 配置 + `sync.py` / `wecom_events.py` 触发接线 + 测试）。

## 一、验收目标
确认「企微回调 / 归档同步完成后，新图片自动进入轻量下载流程」已落地，且满足 RND-172 六条验收标准与非目标（仅 image、不碰同步/解密语义、无重型队列）。重点核查：事件触发确实发生、近期优先、批量/并发/重试可配置、预览可用、失败不无限重试/不阻塞、有资源占用记录；并守住架构护栏与安全脱敏。

## 二、逐条验收清单（PASS/FAIL，附证据）

### A. 自动进入下载流程（验收标准 1）
- [ ] A1 解密后的新图片（无 `MediaFile` 行 / `pending`）在 `trigger_recent_image_download` 接线后、经一次 sweep 自动变 `downloaded` —— 证据：`test_rnd_172_event_media_download.py::T1` GREEN；抽查 DB 行 `download_status` 转变。
- [ ] A2 web 同步路径：`sync_now` 触发归档 worker 后，本进程 dispatcher 确实 sweep 近期图片 —— 证据：集成测试 / 手动 `sync-now` 后近期图片变 `downloaded`（限真实环境或 fixture）。
- [ ] A3 回调路径：`wecom_callback_post` 校验通过后 best-effort 触发，且触发失败不导致回调 4xx/5xx —— 证据：`caplog` 或测试断言回调仍返回 `"ok"`。
- [ ] A4 配置关闭（`event_media_download_enabled` 为空/非 `true`）时，行为回退为现状（仅定时），不自动下载、不报错 —— 证据：T1 关闭态 RED + 配置开启态 GREEN 对照。

### B. 队列优先处理新近图片（验收标准 2）
- [ ] B1 单次 sweep 中，新图片先于旧 failed 图片被下载 —— 证据：`test_...::T2` 断言新图先 `downloaded`。
- [ ] B2 优先级由 `select_candidates(..., newest_first=True, since_ms=...)` 实现，非自造排序 —— 证据：Read `media_event_dispatch.py` 调用签名含 `newest_first=True` 与 `since_ms`。

### C. 批量 / 并发 / 重试上限可配置（验收标准 3）
- [ ] C1 存在 `EventMediaDownloadSettings`（`settings.py`）且字段可读：`batch_limit` / `recent_window_hours` / `retry_count` / `backoff_seconds` / `sweep_interval_seconds` / `enabled` —— 证据：Read `settings.py` + `get_event_media_download_settings()`。
- [ ] C2 单次 sweep 处理数受 `batch_limit` 上限约束（不超量） —— 证据：测试/抽查 `MediaDownloadSummary.downloaded <= batch_limit` 或构造超额候选验证截断。
- [ ] C3 「并发上限」= 复用既有 `fcntl.flock`（`MEDIA_DOWNLOAD_LOCK_PATH`），与定时 cron / 归档 worker 不并发；锁被占时 sweep 安全 no-op —— 证据：Read `media_event_dispatch.py` 复用同款锁；并发场景测试或代码审查确认 `LOCK_NB` 非阻塞。
- [ ] C4 `retry_count` 生效：失败 key 重试次数达上限后停止重试 —— 证据：`test_...::T3` 断言 `download_attempts >= retry_count` 后不再重试。

### D. 下载成功后后台可预览（验收标准 4）
- [ ] D1 自动下载成功的图片，其 `media_files` 行 `download_status='downloaded'` 且 `storage_ref` 非空 —— 证据：DB 抽查。
- [ ] D2 既有的图片预览路径（RND-144 / RND-147 的 `/media/access` 描述符 + 前端 viewer）对事件下载的图片同样可用，未因本次改动退化 —— 证据：运行 `test_generic_media_serving.py` / `test_nested_media_access.py` 相关用例全绿；或手动核对描述符契约未变（`SERVABLE_MEDIA_MSGTYPES` 未动）。

### E. 失败不无限重试 / 不阻塞队列（验收标准 5 + 非目标）
- [ ] E1 持续失败的 key 达到 `retry_count` 上限后留 `failed`，不再重试 —— 证据：`test_...::T3`。
- [ ] E2 旧 failed 任务不阻塞同批新图片下载 —— 证据：`T3` 中同批新图仍 `downloaded`。
- [ ] E3 退避：达上限前的两次失败之间有退避间隔（间隔 < `backoff` 时不立即重试） —— 证据：`test_...::T4`。
- [ ] E4 触发 fire-and-forget、非阻塞：sync / 回调响应不因媒体下载慢/失败而变慢或报错 —— 证据：代码审查 `trigger_recent_image_download` 不 await SDK；`sync_now` / `wecom_callback_post` 仍按时返回。

### F. 资源占用 / 生产验证记录（验收标准 6）
- [ ] F1 每次 sweep 输出结构化资源日志，含 `targeted` / `downloaded` / `failed` / `failed_reasons`（��合）/ `bytes_downloaded` / `duration_ms` / `peak_rss_kb`（可选） —— 证据：`test_...::T5` 断言日志字段存在。
- [ ] F2 资源日志**不含** `sdkfileid` / `local_path` / `storage_ref` / `oss_key` / signed URL / 媒体字节 —— 证据：T5 检索日志无敏感字面量。

### G. 非目标守住
- [ ] G1 仅 `image` 被事件下载；voice / video / file / emotion 未被事件层纳入（`msgtypes={"image"}`） —— 证据：Read `media_event_dispatch.py` 仅传 `{"image"}`；语音等仍走原有定时/管线，未被破坏。
- [ ] G2 未改变消息同步与解密语义（`sync_worker` / `decrypt_worker` 行为不变） —— 证据：`git diff` 不触及 `services/sync_worker.py` / `services/decrypt_worker.py` 语义；`test_sync_*` / `test_*decrypt*` 全绿。
- [ ] G3 未引入重型队列基础设施（无 Redis / RabbitMQ / 新 DB 队列表） —— 证据：代码审查 `media_event_dispatch.py` 仅用进程内队列；无新依赖。

### H. 架构 / 安全 / 全局契约
- [ ] H1 `test_architecture_boundary.py` 全绿（新模块已加入 `_FLAT_SERVICE_MODULES`） —— 证据：pytest 输出。
- [ ] H2 `test_http_contract.py` 全绿（未新增路由） —— 证据：pytest 输出。
- [ ] H3 `main.py` 未新增业务路由 / 直接 DB 查询；router 未反向 import `app.main` —— 证据：`git diff app/main.py` + 审查。
- [ ] H4 `tenant_id` 仅来自 `get_current_user` / `TenantWecomConfig` 解析，无调用方输入 —— 证据：代码审查触发接线处。
- [ ] H5 未改 `backend/scripts/`（B 层纪律），除非 Haisu 显式许可并有记录 —— 证据：`git diff --stat` 不含 `backend/scripts/`。
- [ ] H6 未自行 `git commit` / `push` —— 证据：`git status` 显示待提交、无远程推送；或询问用户确认。

## 三、回归套件（必须全绿）
（先 `cd backend`）
- `python -m pytest tests/test_rnd_172_event_media_download.py tests/test_media_download.py tests/test_media_worker_service.py tests/test_download_wecom_media_once.py tests/test_generic_media_serving.py tests/test_nested_media_access.py tests/test_architecture_boundary.py tests/test_http_contract.py -q`
- 重点守：`test_http_contract.py`（路由数不变）、`test_architecture_boundary.py`（护栏）、脱敏回归（`test_download_wecom_media_once.py`）、图片预览契约（`SERVABLE_MEDIA_MSGTYPES` 未动）。
- 收工前开发 agent 应已跑 `make verify` 全绿；QA 至少复跑上述子集确认。

## 四、智能路由判定（每轮必给）
- 实现偏差（如未接线触发、未用 `newest_first`、重试上限未生效、泄露敏感字段）→ 反馈开发 agent 修正，附错误 + 失败点 + 期望；不自行改实现。
- 仅验证命令（如 pytest 参数 / `caplog` 用法）写错 → 可自行修正验证命令（须标注）。
- 全部通过 → 报告 SUCCESS，附 RED→GREEN 证据与资源日志样例。

最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式
```
RND-172 验收结论：PASS / FAIL
自动进入下载流程：A1-A4 证据 ___（PASS/FAIL）
近期优先：B1-B2 ___
可配置：C1-C4 ___
预览可用：D1-D2 ___
失败不阻塞：E1-E4 ___
资源记录：F1-F2 ___
非目标：G1-G3 ___
契约：H1-H6 ___
回归：<pytest 结果摘要>
遗留：___
```
