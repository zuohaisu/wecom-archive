# RND-258 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的验收角色），对**已实现的** RND-258 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-258-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-258**（语音消息前端播放失败 / AMR·SILK 浏览器不兼容）的验收标准 + 项目现有代码（`assets/i18n.js`、`web/static/console/message-renderers.js`、`media_storage.py`、`media_download.py`、`media_access.py`、`routers/media.py`、`db/models.py`、`voice_transcode.py`（新增）、`thumbnail_pipeline.py`、`settings.py`）。

---

## 0. 验收依据

### 0.1 工单验收标准（来自 RND-258 描述，逐条必过）
- 根因确认：voice/audio_archive 以 AMR/SILK 落盘，浏览器 `<audio>` 无法原生解码 → 「语音播放失败」。
- 修复：下载期转码为 MP3（必要时 WAV），以「播放变体」对外提供；前端可真实播放。
- 历史已下载 AMR 提供一次性回填转码。
- 转码失败（如 SILK 不支持）优雅降级，不静默假成功、不破坏下载。
- 不破坏图片/视频/文件媒体管线与鉴权边界、日志脱敏。

### 0.2 验收边界（硬约束，验收 agent 必须断言）
- 必须明确区分 **「代码与 fixture 已实现」** 与 **「真实生产样本 + 真实浏览器实测通过」**。
- **没有真实 WeCom 语音样本 + 真实浏览器实测时，不得宣称 Production Verified** —— 标「已实现（代码+fixture）」并附验证步骤与限制说明。
- SILK 在多数 ffmpeg 构建下不支持解码：若实现仅覆盖 AMR→MP3，须如实标注 SILK 为已知限制（降级为原文件可下载），不得算 FAIL（除非工单明确把 SILK 真机解码列为 must-have）。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支（`main`）。若开发 agent 仅在工作树改动未提交，**只验收工作树**（不要求 commit），但结论注明「未提交」。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动**前台**进程后再验（不后台化、不加 `&`）。
4. **ffmpeg 可用性**：`shutil.which("ffmpeg")` 或 `which ffmpeg`。CI 未装时相关转码测试应 skip；**真实浏览器播放验收必须有 ffmpeg + libmp3lame + libopencore-amr**。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 转码工具
- **C1 失败隔离**：`voice_transcode.transcode_voice_to_playable` 在 `ffmpeg` 缺失 / 非法输入 / 超时时返回 `None`，**不抛异常、不崩溃**。对照 `media_thumbnails.generate_thumbnail` 隔离约定。
- **C2 AMR→MP3/WAV**：有 ffmpeg 时，AMR（`#!AMR`）样本 → 返回 MP3（`audio/mpeg`）或 WAV（`audio/wav`）字节（magic 断言）。无 ffmpeg 时该测试 skip（非 FAIL）。
- **C3 SILK 降级**：SILK（`#!SILK_V3`）样本在 ffmpeg 不支持时返回 `None`（实现据此标 `unsupported_format`）；若实现声称支持 SILK 解码，则断言产出可播字节。

### 落盘与字段
- **C4 MediaFile 模式**：`media_files` 表含 `playback_ref` / `playback_status` 列（迁移已落）；非 voice 行默认 `not_applicable`，voice 下载后推进为 `generated`/`unsupported_format`/`failed`。
- **C5 播放变体落盘**：一条 voice 消息下载并转码成功后，`playback_ref` 非空、与原始同 backend / 同 `tenants/{tenant}/` 前缀（`build_voice_playback_storage_ref` 派生），可从存储读回 MP3/WAV 字节。

### 访问描述符（核心修复）
- **C6 描述符返回可播格式**：voice 行 `playback_status="generated"` 时，`GET /api/conversations/{cid}/messages/{msgid}/media/access` 返回 `content_type == "audio/mpeg"`（或 `audio/wav`）且 `url` 指向该 MP3/WAV 对象（local 代理或 qiniu 签名 URL）。**这是「前端不再语音播放失败」的服务端根因修复**。
- **C7 无变体回落**：voice 行无播放变体时描述符回落为 `audio/amr`，且 `GET .../media` 字节路由仍可下载原文件（不破坏「可下载」）。
- **C8 variant=play 显式**：`GET .../media/access?variant=play` 与 `GET .../media?variant=play` 解析到播放变体（`size_bytes=None`，同步 `_with_variant_play` 逻辑）；与缩略图 `variant=thumb` 互不干扰。
- **C9 audio_archive 同样受益**：企业会议录音（`audio_archive`）消息在播放变体生成后同样经 `/media/access` 拿到可播描述符（沿用 RND-202 的时间线接入，不新增路由）。

### 前端
- **C10 真实播放**：用真实浏览器（Chrome/Firefox/Safari 至少一种）打开含 voice/audio_archive 的时间线，点击播放 → 原生 `<audio>` 正常播放，**控制台/UI 不再出现「语音播放失败」**。
- **C11 安全降级**：播放变体缺失（如 SILK 不支持）时，UI 不应静默假成功；至少保持原「可下载」语义或明确降级文案，不 500 / 不崩溃。（若开发做了 2.6 `audio.type` 兜底，断言 voice 分支设了 `audio.type`。）

### 安全与边界
- **C12 tenant isolation**：用**另一租户**认证调 `GET .../media/access?variant=play` → 仅见本租户对象；`object_key_tenant_prefix_matches` 对 `playback_ref` 生效（沿用 `test_thumbnail_media_access.py` / `test_media_access_descriptor.py` 套路）。
- **C13 日志脱敏**：任何日志/异常**不含** signed URL、sdkfileid、local_path、storage_ref、`playback_ref`（抽查 `media_access.py` 与下载/回填空路径日志）。
- **C14 公共字段不外泄**：时间线响应体不含 `sdkfileid`/`corpid`/`media_key`（沿用 `test_rnd_210_msgtype_and_card.py:541` 基线）。

### 回填
- **C15 历史回填**：`backfill_voice_transcode_once.py` 对一条已下载 AMR 的 voice 行生成 `playback_ref` + `generated`；失败行标记 `unsupported_format`/`failed` 并跳过，不中断批次。

### 回归（不破坏既有能力）
- **C16 SERVABLE 集合未被改坏**：`test_generic_media_serving.py:109`（`SERVABLE_MEDIA_MSGTYPES == SUPPORTED_MIGRATION_MEDIA_TYPES | {"emotion"}`）仍 PASS。
- **C17 路由数不变**：`test_http_contract.py` 路由数断言仍 PASS（RND-258 仅扩展既有 `/media`、`/media/access` 的 `variant` 语义，未新增路由）。
- **C18 缩略图管线不受影响**：`test_thumbnail_*` / `test_media_thumbnails.py` 全绿（播放变体复用了缩略图落盘模式，不可反向破坏）。
- **C19 内容类型判定不变**：`test_media_signature_detection.py`（mp3/voice/AMR 识别）全绿。
- **C20 前端 JS 合法**：`test_search_page_js_syntax.py`（或等价）仍 PASS。

---

## 3. 测试方法

- **后端**：`cd backend` 后跑
  - `python -m pytest tests/test_rnd_258_voice_playback.py tests/test_voice_transcode.py tests/test_generic_media_serving.py tests/test_media_signature_detection.py tests/test_http_contract.py tests/test_thumbnail_media_access.py tests/test_media_access_descriptor.py -q`
  - 若 `test_rnd_258_voice_playback.py` / `test_voice_transcode.py` 不存在（开发未交付测试）→ 本 agent **自行补最小验收测试**覆盖 C1–C9/C12–C15，再判定。
- **前端 / 集成**：用项目既有 Playwright 脚本或最小真实浏览器 smoke 覆盖 C10/C11（需 dev server + ffmpeg + 真实/合成 AMR fixture）。无浏览器环境时 C10 标 **NOT REPRODUCIBLE** 而非 FAIL。
- **回填**：在测试租户下跑一次 `backfill_voice_transcode_once.py`（前台），核对 C15。
- **收口**：跑 `make verify` 确认 lint-diff + typecheck + build + 全量 pytest 全绿。

---

## 4. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过 tenant isolation 做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。
- RND-258 无真实生产样本 + 真实浏览器实测时，**绝不**在结论中写 Production Verified。
- ffmpeg 缺失导致的 skip 必须如实标注，不得折算为 PASS。

---

## 5. 输出格式（必须结构化）

```
## RND-258 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 含 RND-258 实现：是/否 · make verify：通过/失败 · ffmpeg：有/无(libmp3lame/libopencore-amr)

| 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|------|--------|------|---------------------------|
| C1   | 转码失败隔离 | PASS | ... |
| C6   | 描述符返回可播格式 | PASS | curl .../media/access → content_type=audio/mpeg |
| ...  | ...    | FAIL | 复现：<步骤>；期望：<X>；实际：<Y> |
| C20  | 前端 JS 合法 | PASS | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### RND-258 边界确认（硬约束）
- 实现层（C1–C20）：PASS / FAIL
- Production Verified：是 / 否（无真实样本 + 真实浏览器实测 → 必须为否）
- SILK 限制标注：齐备 / 缺失
- 真实环境验证步骤与限制说明：齐备 / 缺失

### 边界与已知限制
- <如：SILK 在多数 ffmpeg 构建下不支持，降级为原文件可下载；AMR→MP3 已覆盖>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺依赖：如生产未装 ffmpeg）。
- 标注：已实现（代码+fixture） / 已 Production Verified（仅当有真实样本 + 真实浏览器实测）。
```

- 若某条无论如何无法复现（如环境导致 C10 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给可复现证据。
- 最终把该报告作为 Linear RND-258 评论贴出（状态保持 Todo，交还用户 Haisu 决策合并）。
