# RND-258 执行提示词（单人端到端：调研 → 复现 → 实现 → 验证）

> 用途：粘贴给单一开发智能体（hy3 / Claude Code / Codex 皆可），由其端到端跑完 RND-258。
> 工单：`RND-258「语音消息前端播放失败：AMR/SILK 浏览器不兼容导致『语音播放失败』」`，优先级 **P2**，状态 **Todo**（建票时 Linear 默认落 Backlog，已尝试置 Todo），assignee Haisu Zuo，父脉络 **RND-195**（支持全部消息类型）。
> 权威来源：Linear RND-258 描述 + 现有代码（已逐项确认，见 §1）；`DEV_AGENT_RULES.md`。
> 设计/架构权威：`docs/ARCHITECTURE.md`、`docs/ai/known-pitfalls.md`、`DEV_AGENT_RULES.md`。
> 相邻参考：RND-202 执行/QA 提示词（audio_archive 播放闭环，但当时**未做转码**、未 Production Verified，AMR 不兼容问题即由此遗留）。

---

## 0. 任务与验收边界

- **目标**：让 `voice`（普通语音）与 `audio_archive`（会议录音）消息在前端**真实可播放**。根因是 WeCom 语音落盘为 **AMR / SILK**，浏览器 `<audio>` 元素无法原生解码，触发 `onerror` → 「语音播放失败」。修复策略：**下载期转码为浏览器可播的 MP3（必要时 WAV）**，并以「播放变体」对外提供，复用现有图片缩略图管线模式。
- **验收边界（硬约束）**：
  - 必须严格区分**「代码与 fixture 已实现」**与**「真实生产样本在真实浏览器验证通过」**。没有真实 WeCom 语音样本 + 真实浏览器实测时，不得宣称 Production Verified，只能标「已实现（代码+fixture）」并附验证步骤与限制说明。
  - **不要改 `SERVABLE_MEDIA_MSGTYPES`**（迁移 guard，见 §1 与 §4），音频接入走描述符/管线显式分支。
- **范围聚焦**：voice / audio_archive 的播放转码与访问描述符；历史已下载 AMR 的一次性回填转码；前端 `audio.type` 兜底（可选）。不重新设计认证/租户隔离/会话聚合，不引入新的第三方播放器依赖。

---

## 1. 当前已落地（开工前确认，禁止重写）

> 以下证据已通过代码检索确认存在且在工作树；若某条缺失 → **停下并报告 BLOCKED**，不要自己补做别的子任务的活。

- **错误文案**：`backend/app/assets/i18n.js:96` 定义 `voice.playbackError = "语音播放失败"`。
- **前端播放挂载**：`backend/app/web/static/console/message-renderers.js`：
  - `swapRichMediaPlaceholder` 的 `voice` 分支（:669-676）创建 `<audio controls preload="metadata" src=desc.url>`，**无任何转码**；`audio.onerror` → `handleRichMediaPlaybackFailure('voice', ...)`（:675）→ `buildErrorBox(kind='voice', 'error')`（:629）→ 渲染 `voice.playbackError`。
  - `renderVoicePreview`（:785）、`renderAudioArchiveMessage` 的 `richMediaPlaceholder('voice', m.media_access_url, ...)`（:390）均走同一 hydration 契约（`loadRichMedia` → `fetchDescriptorWithRecovery` → `/media/access` 描述符）。
  - 注意：`desc` 描述符含 `content_type` 字段（见 §2.4），但 `swapRichMediaPlaceholder` 当前**未**给 `<audio>` 设 `type` —— 这是可选兜底增强点。
- **语音格式落盘**：`backend/app/media_storage.py`：
  - `_ALLOWED_VOICE_CONTENT_TYPES`（:781-786）= `{".amr":"audio/amr", ".silk":"audio/silk", ".wav":"audio/wav", ".mp3":"audio/mpeg"}`。
  - `detect_media_signature_from_bytes`（:891-898）：AMR 签名 `#!AMR`、SILK 签名 `#!SILK_V3`、WAV `RIFF/WAVE`、MP3 `ID3` 均识别为 `voice` 类别。`.mp3` 已被 `detect_media_content_type_for_ref`（:923）映射为 `audio/mpeg` 合法 `voice` 类型 —— **新增 MP3 变体无需改内容类型判定**。
- **下载分类**：`backend/app/media_download.py:72,76` 的 `_SIGNATURE_CATEGORY_BY_MSGTYPE`：`voice→voice`、`audio_archive→voice`（企业会议录音走同一 voice 字节签名类别）。
- **缩略图管线（镜像模板，不要改写）**：
  - `backend/app/media_thumbnails.py:119` `generate_thumbnail(data) -> Optional[ThumbnailResult]`：纯函数、失败隔离（任何异常返回 `None`），是转码工具的**直接模板**。
  - `backend/app/thumbnail_pipeline.py`（:48,160）演示「生成 → 落同一 backend / 同一 `tenants/{tenant}/` 前缀」的完整流程，是播放变体落盘流程的**直接模板**。
  - `backend/app/media_storage.py:300` `build_thumbnail_storage_ref(original_ref, tenant_id, output_ext)`：与原始 co-located 的变体对象键构造，是 `build_voice_playback_storage_ref` 的**直接模板**。
  - `backend/app/settings.py:99-106` `ThumbnailSettings` / `get_thumbnail_settings()`：开关配置模式，是 `VoiceTranscodeSettings` / `get_voice_transcode_settings()` 的**直接模板**。
- **访问描述符（可变点）**：`backend/app/services/media_access.py`：
  - `_resolve_variant_serve_ref(media_file, variant, original_ref)`（:308-325）：当前仅处理 `variant="thumb"`（读 `thumbnail_ref`/`thumbnail_status`）。**扩展点**：增加 `voice` 播放变体解析（读 `playback_ref`/`playback_status`）。
  - `build_access_descriptor(...)`（:445）：根据 `serve_ref` 与 `effective_backend` 产出 `{storage_backend, access_type, url, expires_at, content_type, size_bytes}`；`content_type = detect_media_content_type_for_ref(serve_ref)`。`variant="thumb"` 时 `size_bytes=None`（:314-315 的调用处）。**此处是让 voice 描述符返回 MP3 的核心**。
- **路由变体解析（集成点）**：`backend/app/routers/media.py`：
  - `get_message_media_access`（:203）与 `get_nested_message_media_access`（:423）：各调用 `_resolve_variant_serve_ref(media_file, variant, effective_ref)`（:312 / :512），再 `build_access_descriptor(serve_ref=serve_ref, ...)`。需让 `variant="play"` 或 voice 行自动选播放变体。
  - 字节路由 `get_message_media`/`get_nested_message_media`（:193-196 / :413-416）同样走 `_resolve_variant_serve_ref` → `serve_media_bytes`。
- **时间线接入**：`backend/app/services/timeline_service.py`：
  - 顶层 `voice` 已在 `SERVABLE_MEDIA_MSGTYPES` 内，构造 `media_access_url`（:886-892）；`audio_archive` 由 RND-202 在两处端点加了显式分支（:818/:855、:1100/:1124）。**前端拿到的 `media_access_url` 已指向 `/media/access`**，修复后该描述符自动返回 MP3 —— **时间线侧原则上无需改动即可生效**（见 §2.5）。

> 开发 agent 第一步：用 `git log --oneline -5` 与 `git status` 确认当前在 `main` 且工作树干净（无未提交改动），再逐条核对上述文件确实存在、行号仍可对应。

---

## 2. 精确落点（文件 / 函数级）

### 2.1 转码工具 · 新增 `backend/app/voice_transcode.py`（镜像 `media_thumbnails.py`）
- `transcode_voice_to_playable(data: bytes, *, prefer_mp3: bool = True) -> Optional[VoiceTranscodeResult]`：
  - 纯函数、失败隔离：**任何异常 / ffmpeg 不存在 / 非零退出 / 超时（建议 30s）都必须返回 `None`**，绝不要让调用方（下载 worker）崩溃。
  - 通过 `subprocess` 调 `ffmpeg`，从 **stdin** 读原始字节、向 **stdout** 写转码后字节（`-i pipe:0 -f <outfmt> -`）：
    - 解码侧交给 ffmpeg 自动探测（`#!AMR`/`#!SILK_V3`/`RIFF` 等）；AMR 走 `libopencore_amrnb`（NB）/ `libopencore_amrwb`（WB）。
    - 编码侧：优先 `libmp3lame -b:a 64k`（MP3，`audio/mpeg`）；若 MP3 编码器不可用，回退 `pcm_s16le`（WAV，`audio/wav`，浏览器亦可播）。
  - 返回 `(bytes, ext)`（`ext` 为 `.mp3` 或 `.wav`），供调用方落盘。
- **SILK 处理**：多数 ffmpeg 静态构建**不直接解码** WeCom SILK_v3；若 ffmpeg 报错退出，转码函数应返回 `None`，由调用方标记为 `playback_status="unsupported_format"`（优雅降级，见 2.3），**不要**为 SILK 伪造编解码。
- 失败隔离参考 `media_thumbnails.generate_thumbnail` 的 `try/except` 结构。

### 2.2 MediaFile 模式 + 迁移 · `backend/app/db/models.py` + 新增 Alembic 迁移
- `MediaFile` 新增两列（可空）：
  - `playback_ref: Optional[str]`（co-located 于原对象的 MP3/WAV 变体对象键）。
  - `playback_status: str`，取值枚举：`"not_applicable"`（非 voice，默认）/ `"pending"`（voice 已下载待转码）/ `"generated"` / `"failed"` / `"unsupported_format"`。
  - 非 voice 行的 `playback_status` 默认 `"not_applicable"`；voice/audio_archive 行初始 `"pending"`（下载落库后由 2.3 推进）。
- 新增 Alembic migration（在 `backend/alembic/versions/` 现行 head 之后），仅 `add_column`，无数据迁移；downgrade 对应 `drop_column`。

### 2.3 下载管线集成 · `backend/app/media_download.py` 与/或 `backend/app/thumbnail_pipeline.py`（镜像缩略图流程）
- 在 voice/audio_archive 文件**已下载落库且字节签名已校验**之后，调用 `transcode_voice_to_playable`：
  - 成功 → `build_voice_playback_storage_ref(original_ref, tenant_id, ext)`（见 2.8）算 `playback_ref`，用与原对象**相同 backend**（local 或 qiniu_kodo）落盘（参考 `thumbnail_pipeline.py` 的写回流程），置 `playback_status="generated"`、`playback_ref=<...>`。
  - ffmpeg 缺失 / 转码异常 / SILK 不支持 → `playback_status="unsupported_format"`（或 `"failed"`，二选一并在测试中固定语义），**保留原 AMR 可下载**，不阻断下载主流程。
- **不要另写下载器**；复用既有 `media_worker` 下载路径（RND-202 §1 已确认 audio_archive 顶层 sdkfileid 已被候选命中）。
- 开关由 `get_voice_transcode_settings().enabled` 控制（关闭时 voice 行 `playback_status` 保持 `"not_applicable"`，行为回退为现状）。

### 2.4 访问描述符 · `backend/app/services/media_access.py` + `backend/app/routers/media.py`
- 扩展 `_resolve_variant_serve_ref`（:308）：当 `variant == "play"` **或**（无 variant 且 `media_file.file_type == "voice"` 且 `playback_status == "generated"` 且 `playback_ref` 存在）时，返回 `(playback_ref, is_playback=True)`；否则维持原 `effective_ref`。
- 在 `routers/media.py` 的 `get_message_media_access`（:312）/ `get_nested_message_media_access`（:512）调用处：当 `is_playback` 为真，`size_bytes=None`（与缩略图同形），`proxy_url` 追加 `?variant=play`（镜像 `_with_variant_thumb`，新增 `_with_variant_play`）。
- `build_access_descriptor` 已按 `serve_ref` 重算 `content_type` → MP3 变体自动得到 `audio/mpeg`；**qiniu 分支的 `object_key_tenant_prefix_matches(serve_ref, tenant_id)` 校验对 `playback_ref` 同样适用**（同租户前缀），无需改动签名逻辑。
- 字节路由（`get_message_media`/:193）同样受益：variant=play 时 `serve_media_bytes(effective_backend, playback_ref)` 直接回源 MP3/WAV。
- **关键**：当 `playback_status` 为 `unsupported_format`/`failed`（未生成）时，描述符回落为原 AMR（现状），保证「可下载」不回退 —— 真正的「可播放」提升来自成功生成的变体。

### 2.5 时间线（最小 / 可选）
- 原则上**无需改动**：前端 `media_access_url` 已指向 `/media/access`，修复后该描述符自动返回 MP3。
- 可选增强：在 `timeline_service.py` 的 voice / audio_archive 媒体字段里额外暴露 `playback_access_url`（镜像 `thumbnail_access_url` 模式，`:515`），便于前端区分「有可播变体 / 仅原文件」。若做，须同时更新 `TimelineMessageOut` / `NestedMediaAccessOut` schema 与对应测试。

### 2.6 前端兜底（可选，低风险）
- `message-renderers.js` `swapRichMediaPlaceholder` 的 `voice` 分支（:669）增加 `audio.type = desc.content_type || "audio/mpeg";`（部分浏览器依据 type 选择解码器）。不改变既有的 `onerror` 回退逻辑。

### 2.7 历史回填 · 新增 `backend/scripts/backfill_voice_transcode_once.py`（镜像 `migrate_local_media_to_qiniu.py`）
- 一次性脚本：扫描 `media_files` 中 `file_type="voice"`、`download_status="downloaded"`、`playback_status in (NULL, "not_applicable", "pending")` 的行，逐条读原对象字节 → 转码 → 落 `playback_ref` → 置 `playback_status`。失败行标记 `unsupported_format`/`failed` 并跳过，不中断批次。
- 复用既有存储 provider 抽象与租户前缀约定；日志脱敏（见 §4）。

### 2.8 变体对象键与配置
- `backend/app/media_storage.py` 新增 `build_voice_playback_storage_ref(original_ref, tenant_id, output_ext)`（镜像 `build_thumbnail_storage_ref`，:300）：基于原 `storage_ref` 派生 `..._play.<ext>`，保持 `tenants/{tenant}/` 前缀与同目录 co-location。
- `backend/app/settings.py` 新增 `VoiceTranscodeSettings`（字段 `voice_transcode_enabled` 等，镜像 `ThumbnailSettings`，:99）与 `get_voice_transcode_settings()`（:105）。

### 2.9 测试（先 RED 后 GREEN）
- `backend/tests/test_voice_transcode.py`：ffmpeg 缺失时 `transcode_voice_to_playable` 返回 `None`；存在时 AMR 样本 → MP3 字节（`ID3` 头或 ffmpeg 输出 magic），SILK 样本 → `None`（断言 `unsupported_format` 语义）。**ffmpeg 不可用必须用 `pytest.mark.skipif(shutil.which("ffmpeg") is None, ...)` 优雅跳过**，不硬失败（与现有可选依赖测试一致）。
- `backend/tests/test_rnd_258_voice_playback.py`：
  - T1（RED→GREEN）：voice 行 `playback_status="generated"` + `playback_ref` 存在时，`GET /media/access` 返回的 `content_type == "audio/mpeg"` 且 `url` 指向 MP3/WAV 对象（local 代理或 qiniu 签名 URL）。
  - T2：voice 行无播放变体时描述符回落原 AMR（`content_type == "audio/amr"`），`/media` 字节路由仍可下。
  - T3（租户隔离基线）：跨租户 user 调 `/media/access?variant=play` → 仅见本租户对象；`object_key_tenant_prefix_matches` 对 `playback_ref` 生效。
  - T4：回填脚本对一条已下载 AMR 行生成 `playback_ref` + `generated`。
- 回归基线（见 §3 收口）重点盯：`test_generic_media_serving.py:109`（SERVABLE 集合未变）、`test_media_signature_detection.py`（mp3/voice 识别不变）、`test_http_contract.py`（**路由数不变**）、`test_thumbnail_*` 不受影响。

### 2.10 部署 / CI 前置（务必同步）
- 生产 ECS 与 CI/dev 必须安装 `ffmpeg`，且含：`libmp3lame`（编码 MP3）、`libopencore-amrnb`/`libopencore_amrwb`（解码 AMR）。SILK 支持需实测确认（多数构建不支持 → 走 `unsupported_format` 降级）。
- 在 GitHub Actions 测试 job 与 `deploy/`（生产部署脚本）中加 `apt-get install -y ffmpeg`（或等价），并验证编码器可用；CI 未装前，2.9 的 ffmpeg 相关测试按 skip 处理。
- 2c2g ECS 成本：转码发生在**下载期（worker）/回填期**，非每次播放请求 → 可接受；MP3 64k 控制体积。

---

## 3. 执行步骤（严格：复现(RED) → 实现 → 验证(GREEN)）

### 阶段一：确认已落地（先证明不缺，禁止先改实现）
- 逐条核对 §1 的文件/函数证据确实存在且在工作树。`git status` 应为干净（在 `main`）。
- 跑回归基线（先 `cd backend`）：
  - `python -m pytest tests/test_message_type_registry_core.py tests/test_media_classification.py tests/test_generic_media_serving.py tests/test_media_signature_detection.py tests/test_http_contract.py -q`
  - 任一失败 → **停下报告 BLOCKED**，不随手改无关代码。

### 阶段二：复现 / 红灯（先证明缺口，禁止先改实现）
- 新增 `test_rnd_258_voice_playback.py` 断言「尚未实现」行为，预期 **RED**：
  - T1：voice 行 `playback_status="generated"` 时 `/media/access` `content_type` 仍为 `audio/amr`（当前无播放变体 → RED）。
  - T2（可选）：前端 `swapRichMediaPlaceholder` 的 voice 分支未设 `audio.type`（当前缺失 → RED，若做 2.6）。
- 运行确认 RED。**绝不假装修好**。

### 阶段三：实现（最小改动，严守 §2 + §4）
- 2.1 新增 `voice_transcode.py`；2.2 `MediaFile` 列 + 迁移；2.3 下载管线接转码；2.4 描述符/路由接播放变体；2.5（可选）时间线；2.6（可选）前端 `audio.type`；2.7 回填脚本；2.8 对象键 + 配置；2.9 测试。
- 全程遵守硬约束（§4）。

### 阶段四：验证（不达标不收工）
- 阶段二 T1/T2 必须转 **GREEN**。
- 运行（先 `cd backend`）：
  - `python -m pytest tests/test_rnd_258_voice_playback.py tests/test_voice_transcode.py tests/test_generic_media_serving.py tests/test_media_signature_detection.py tests/test_http_contract.py tests/test_thumbnail_media_access.py -q`
  - 重点盯：`test_generic_media_serving.py:109`（SERVABLE 集合未变）、`test_http_contract.py`（路由数未增）、`test_thumbnail_*`（缩略图管线不被破坏）、日志脱敏回归。
  - 前端 JS 语法合法：`test_search_page_js_syntax.py` 或等价。
  - 真实浏览器 smoke（若有 dev server + 真实 AMR fixture）：voice 卡片点击 → 浏览器原生播放，不再「语音播放失败」。
  - 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。

---

## 4. 硬性约束（不可违反）

- **工作直接在 `main` 上**；不创建 task 分支（Haisu 显式要求才破例）。
- **绝不自行 `git commit` / `push`**（需用户显式授权）。
- **最小正确改动**：已 Done 的语音接入（RND-206/RND-202）禁止重复实现，只在 RND-258 自身范围内增量；镜像缩略图管线模式，**不另写下载器/路由**。
- **验收边界**：无真实 WeCom 语音样本 + 真实浏览器实测，**绝不**标 Production Verified；密钥/signed URL/本地路径**不进日志**；跨租户不可见。
- **不要改 `SERVABLE_MEDIA_MSGTYPES`**（`media_storage.py:1044`，迁移 guard 锁定）；voice 播放接入走描述符/管线显式分支。
- **tenant isolation 铁律**：`tenant_id` 永远来自 `get_current_user`；`playback_ref` 同 `tenants/{tenant}/` 前缀；`object_key_tenant_prefix_matches` 对播放变体同样校验。
- **不新增路由**（route 数基线见 `test_http_contract.py`）；仅扩展既有 `/media` 与 `/media/access` 的 `variant` 语义（`variant="play"`）。
- **ffmpeg 失败隔离**：转码异常/超时/缺失必须返回 `None` 并由调用方标记 `unsupported_format`/`failed`，**绝不**让下载 worker 崩溃或伪造编解码。
- **日志脱敏**：任何日志/异常**不含** signed URL、sdkfileid、local_path、storage_ref、`playback_ref`（对照 `download_wecom_media_once.py:58` 脱敏约定）。
- 不引入新的第三方媒体播放器依赖（浏览器原生 `<audio>` + 服务端 MP3 即可）。
- 参考 `DEV_AGENT_RULES.md` 与 `docs/ai/known-pitfalls.md`。

---

## 5. 收尾动作

- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 在 Linear 把 RND-258 状态保持 **Todo**（或 In Progress 视推进），写一条评论：改动文件/函数级清单 + 红灯→绿灯证据 + `make verify` 结论 + **明确标注「已实现（代码+fixture）」或「已 Production Verified（仅当有真实样本 + 真实浏览器实测）」** + 真实环境验证步骤与限制说明（含 SILK 降级边界）。
- 若实现中发现需要比预期更大的改动或存在未覆盖的官方格式/场景（如 SILK 真机解码），在评论中**如实标注边界**，不私自扩展范围。
