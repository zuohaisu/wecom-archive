# RND-258 计划交付概览 — 语音消息「语音播放失败」根因与修复方案

> 角色：plan agent（建票 → 开发提示词 → 验收提示词；未进入开发阶段，agent 不 commit/push）

## 一、根因（高置信度）

生产环境 `voice`（普通语音）与 `audio_archive`（会议录音）消息点击播放时恒显「语音播放失败」（`voice.playbackError`，`backend/app/assets/i18n.js:96`）。

- WeCom 语音落盘为 **AMR**（字节签名 `#!AMR`，`media_storage.py:891`）或 **SILK**（`#!SILK_V3`，`:893`），媒体服务以 `audio/amr`（`_ALLOWED_VOICE_CONTENT_TYPES`，`:781`）原样对外提供。
- 前端 `swapRichMediaPlaceholder` 的 voice 分支直接 `audio.src = desc.url`（`message-renderers.js:674`），**无任何转码**。
- **AMR / SILK 不被任何主流浏览器的 `<audio>` 元素原生解码** → 触发 `audio.onerror` → `handleRichMediaPlaybackFailure`（`:675`）→ 渲染「语音播放失败」。
- **反证**：图片（JPEG/PNG）与视频（.mp4）通过同一套媒体管线可正常播放，唯独语音必失败 → 问题在**格式不兼容**，而非鉴权/网络/存储。
- 该失败与运行环境无关（AMR 在任何浏览器都播不了），生产被观察到只是因为真实 WeCom 数据只存在于生产。

## 二、修复方案（下载期转码 + 播放变体，复用图片缩略图管线模式）

| 落点 | 文件 / 函数 | 改动 |
|---|---|---|
| 转码工具（新增） | `backend/app/voice_transcode.py` | 镜像 `media_thumbnails.generate_thumbnail`，失败隔离，ffmpeg AMR/SILK → MP3（必要时 WAV） |
| 数据模型 | `MediaFile` + 新 Alembic 迁移 | 新增 `playback_ref` / `playback_status`（`not_applicable`/`pending`/`generated`/`failed`/`unsupported_format`） |
| 下载管线 | `media_download.py` / `thumbnail_pipeline.py` | voice 下载后接转码，落 co-located MP3 变体（镜像缩略图落盘） |
| 访问描述符 | `services/media_access.py` `_resolve_variant_serve_ref`(:308) / `build_access_descriptor`(:445) + `routers/media.py`(:312/:512) | voice 行自动/显式(`variant=play`)返回 MP3 描述符（`audio/mpeg`） |
| 历史回填（新增脚本） | `scripts/backfill_voice_transcode_once.py` | 镜像 `backfill_thumbnails_once.py`，补转码已下载 AMR |
| 配置（镜像） | `settings.py` `VoiceTranscodeSettings` / `get_voice_transcode_settings()`(:105) | 转码开关 |
| 前端（可选） | `message-renderers.js` voice 分支(:669) | `audio.type = desc.content_type` 兜底 |

**不变约束**：不新增路由（route 数基线见 `test_http_contract.py`）；不改 `SERVABLE_MEDIA_MSGTYPES`（迁移 guard，`media_storage.py:1044`）；tenant isolation / 日志脱敏铁律保持。

## 三、前置依赖（务必同步）

- 生产 ECS 与 CI/dev 必须安装 `ffmpeg`（含 `libmp3lame` 编码、`libopencore-amr` 解码 AMR）。本机（plan 环境）**未装 ffmpeg**。
- SILK 多数 ffmpeg 静态构建**不支持**解码 → 优雅降级为 `unsupported_format`、保留原文件可下载（已知限制，非缺陷）。
- 2c2g ECS 成本：转码发生在**下载期 / 回填期**，非每次播放请求，可接受；MP3 64k 控制体积。

## 四、交付物

1. **Linear 工单 RND-258**（Todo, P2，挂 RND-195）— `https://linear.app`（id `3563ad0c-b7c6-41bd-ac44-f74959878d05`）
2. 开发提示词：`.workbuddy/prompts/rnd-258-execution-prompt.md`
3. 验收提示词：`.workbuddy/prompts/rnd-258-qa-prompt.md`

下一步：由用户将两份提示词交独立开发 agent 与 QA agent 执行；agent 绝不 commit/push，验收通过后由用户决定合并。

## 五、验收边界（硬约束）

- 无真实 WeCom 语音样本 + 真实浏览器实测时，**不得**标 Production Verified，仅标「已实现（代码+fixture）」并附验证步骤与限制说明（含 SILK 降级边界）。
