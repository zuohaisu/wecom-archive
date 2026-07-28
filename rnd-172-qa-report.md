RND-172 验收结论：PASS

自动进入下载流程：A1-A4 证据 ___（PASS）
- A1: test_rnd_172_event_media_download.py::test_event_sweep_downloads_new_decrypted_image_and_logs_safely GREEN ✓
  - DB 行 download_status 从 pending→downloaded，download_attempts=1
- A2: routers/sync.py#L93 trigger_recent_image_download(tenant_id, triggered_by="sync") ✓
  - _trigger_via_dispatcher 函数在 sync worker 完成后调用
- A3: routers/wecom_events.py#L219 trigger_recent_image_download(tenant_id, triggered_by="callback") + try/except 包裹 ✓
  - 触发失败不阻塞回调响应（pass 处理异常）
- A4: settings.py#L120 event_media_download_enabled=""（空值关闭）✓
  - media_event_dispatch.py#L47 检查 strip().lower() == "true" 才开启
  - 测试通过：配置关闭态 RED（无操作）、开启态 GREEN（执行下载）

近期优先：B1-B2 ___（PASS）
- B1: test_rnd_172_event_media_download.py::test_event_sweep_prioritizes_new_image_and_backoff_does_not_block_it GREEN ✓
  - 新图先 downloaded，旧 failed 图暂不重试
- B2: media_event_dispatch.py#L142-145 select_candidates(..., newest_first=True, since_ms=...) ✓
  - since_ms = int(time.time() * 1000) - window_hours * 3600 * 1000

批量 / 并发 / 重试上限可配置：C1-C4 ___（PASS）
- C1: settings.py#L117-129 EventMediaDownloadSettings 所有字段可读 ✓
  - enabled, batch_limit=20, recent_window_hours=24, retry_count=3, backoff_seconds=30, sweep_interval_seconds=15
- C2: media_event_dispatch.py#L135 limit = _positive(settings.event_media_download_batch_limit, 20) ✓
  - select_candidates(limit=limit) 约束单次处理数
- C3: media_event_dispatch.py#L57-72 _lock() 复用 fcntl.flock(MEDIA_DOWNLOAD_LOCK_PATH) ✓
  - LOCK_NB 非阻塞，锁被占时直接返回 no-op
- C4: media_event_dispatch.py#L104 if media_file.download_attempts >= retry_count: continue ✓
  - test_event_sweep...prioritizes 验证达上限后不再重试

预览可用：D1-D2 ___（PASS）
- D1: MediaFile.download_status='downloaded' + storage_ref 非空 ✓
  - test_event_sweep...safely 断言 (summary.downloaded, row.download_status, row.download_attempts) == (1, "downloaded", 1)
- D2: test_generic_media_serving.py (153 passed) + test_nested_media_access.py 全绿 ✓
  - SERVABLE_MEDIA_MSGTYPES 未动（仅 {"image"}），既有预览路径兼容

失败不无限重试 / 不阻塞队列：E1-E4 ___（PASS）
- E1: media_event_dispatch.py#L104 download_attempts >= retry_count 后留 failed，不再重试 ✓
- E2: test_event_sweep...prioritizes 中同批新图仍 downloaded，旧失败不影响 ✓
- E3: media_event_dispatch.py#L110-112 backoff 退避逻辑 ✓
  - delay = backoff * min(2 ** max(attempts-1, 0), 8)
  - test 断言 31 秒后第二次重试才生效
- E4: trigger_recent_image_download fire-and-forget ✓
  - wecom_events.py#L218-221 try/except pass，不 await SDK

资源记录：F1-F2 ___（PASS）
- F1: media_event_dispatch.py#L188-193 logger.info 输出结构化日志 ✓
  - tenant_tag, triggered_by, targeted, downloaded, failed, failed_reasons, bytes_downloaded, duration_ms, peak_rss_kb
- F2: test_event_sweep...safely#L44 assert "image-safe-id" not in record ✓
  - 不含 sdkfileid/local_path/storage_ref/oss_key/signed URL（只记录 tenant hash）

非目标守住：G1-G3 ___（PASS）
- G1: media_event_dispatch.py#L143 select_candidates(session, tenant_id, {"image"}, ...) ✓
  - 仅 image 类型，voice/video/file/emotion 不受影响
- G2: git diff 未触及 services/sync_worker.py / services/decrypt_worker.py 语义 ✓
  - trigger 接线在 router 层，服务层行为不变
- G3: 无 Redis/RabbitMQ/新 DB 队列表 ✓
  - 仅用进程内 threading.Condition + set 做去重

架构 / 安全 / 全局契约：H1-H6 ___（PASS）
- H1: test_architecture_boundary.py (22 passed) 全绿，media_event_dispatch.py 已加入 _FLAT_SERVICE_MODULES ✓
- H2: test_http_contract.py (62 passed) 全绿，路由数不变（无新增业务路由）✓
- H3: main.py 未新增业务路由/DB 查询；router 无反向 import app.main ✓
  - git grep "from app\.main import" in routers → 0 matches
- H4: tenant_id 仅来自 _active_tenant_for_corp(_get_corp_id()) ✓
  - sync.py#L123/L146 auth 解包获取；wecom_events.py#L216 从 corp_id 解析
- H5: backend/scripts/ 无改动 ✓
  - git diff --stat backend/scripts/ → empty
- H6: 未 git commit/push ✓
  - git status 显示修改文件待提交，无推送

回归套件（必须全绿）：
✓ test_rnd_172_event_media_download.py (2/2 passed)
✓ test_architecture_boundary.py (22/22 passed)  
✓ test_http_contract.py (62/62 passed)
✓ test_media_download.py (54/54 passed)
✓ test_generic_media_serving.py + test_nested_media_access.py (153/153 passed)

遗留：___（无）

---

## 关键实现要点核查

### 1. 事件触发接线
- `routers/sync.py:_trigger_via_dispatcher()` - sync worker 完成后 best-effort 触发
- `routers/wecom_events:wecom_callback_post()` - 解密回调校验通过后触发（try/except 隔离失败）

### 2. 优先级排序
- `select_candidates(..., newest_first=True, since_ms=...)` 确保近期图片优先
- 窗口可配：event_media_download_recent_window_hours=24h

### 3. 重试与退避
- download_attempts 列（MediaFile model）持久化重试计数
- 指数退避：backoff * min(2^(attempts-1), 8)，默认 30s 起始
- 达 retry_count=3 上限后留 failed 状态，等人工介入

### 4. 资源日志脱敏
- tenant_id → SHA256[:12] 哈希标签
- 严禁记录 sdkfileid/local_path/storage_ref/oss_key/signed URL/媒体字节
- caplog 断言敏感字符串不存在

### 5. 架构边界
- Flat module（无子 package），加入 _FLAT_SERVICE_MODULES
- 纯 side-effect 触发器，不引入新 HTTP 路由
- 依赖既有锁机制（MEDIA_DOWNLOAD_LOCK_PATH）避免并发冲突
