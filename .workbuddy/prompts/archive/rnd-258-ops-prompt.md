# RND-258 运维准备 / 部署后验收提示词（ops runbook）

> 用途：粘贴给**运维 agent**（或 Haisu 本人在生产终端手动执行），在 RND-258 开发+QA 通过、代码合入 `main` 并部署后，完成「让生产真正可用 + 闭环 Production Verified」的服务器侧动作。
> 关联交付物：`.workbuddy/prompts/rnd-258-execution-prompt.md`（开发）、`.workbuddy/prompts/rnd-258-qa-prompt.md`（QA）。
> 权威依据：本仓库实际部署脚本 `scripts/deploy_server.sh`（已含 `_ensure_ffmpeg` 与 `alembic upgrade head`）、`.github/workflows/deploy.yml`（CI 已装 ffmpeg）、`deploy/systemd/wecom-archive-media-download.service`、后端 `backfill_voice_transcode_once.py`（由开发 agent 新增，部署后应存在）、`DEV_AGENT_RULES.md`。

---

## 0. 背景与范围修正（重要）

- RND-258 已开发 + QA 通过，结论是「**已实现（代码+fixture）**」，但非「Production Verified」——因为缺真实 WeCom 语音样本 + 真实浏览器实测。
- **关键修正（比开发/QA 提示词更准确）**：ffmpeg 与 Alembic 迁移**已在部署自动化中**，运维**不必手动安装 ffmpeg、不必手动迁库**：
  - 生产 ECS：`scripts/deploy_server.sh` 的 `_ensure_ffmpeg()`（step 2/9）负责装 ffmpeg；step 5/9 跑 `alembic upgrade head`。
  - CI：`.github/workflows/deploy.yml:42-43` 的测试 job 已 `apt-get install -yqq ... ffmpeg`（所以 `test_voice_transcode.py` 在 CI 真实跑，不是 skip）。
- 因此本提示词的运维工作 = **验证编解码器 + 确认迁移 + 回填历史 AMR + 真机 Production Verified 闭环**。范围聚焦，不重新部署、不手动装包（除非验证发现缺失）。

---

## 1. 前置（权限 / 环境）

1. 可 SSH 到生产 ECS；运行时用户（如 `wecomarchive`）已配 passwordless sudo for `systemctl` 与 `apt-get`（见 `deploy_server.sh:29`）。
2. RND-258 代码已合入 `main`，且**已触发部署**（`deploy_server.sh` 或 GitHub Actions 部署完成，含迁移与重启）。
3. 回填脚本 `backend/scripts/backfill_voice_transcode_once.py` 已由开发 agent 落地并随部署上线。
4. **本提示词不要求 agent 自己执行部署 / commit / push**；动作仅限生产服务器上的验证与回填。是否执行由 Haisu 决定。

---

## 2. 执行步骤（部署完成后，顺序执行）

### 2.1 验证生产 ffmpeg 编解码器（核心，决定转码能否工作）
- SSH 登录生产，确认 ffmpeg 在运行时用户 PATH 下可见：
  - `sudo -u wecomarchive which ffmpeg`（应返回 `/usr/bin/ffmpeg`）。
  - `ffmpeg -version`（确认安装成功）。
- 确认**编码器**存在（MP3 输出所需）：
  - `ffmpeg -hide_banner -encoders 2>/dev/null | grep -iE "libmp3lame"` → 应有输出（`audio/mpeg` 编码能力）。
- 确认**解码器**存在（AMR 输入所需）：
  - `ffmpeg -hide_banner -decoders 2>/dev/null | grep -iE "libopencore_amrnb|libopencore_amrwb"` → 应有输出（NB/WB AMR 解码能力）。
- **SILK 检查（已知限制）**：
  - `ffmpeg -hide_banner -decoders 2>/dev/null | grep -i silk` → 多半**无输出**（apt 版 ffmpeg 通常不含 SILK 解码器）。即企业 SILK 语音将降级为「原文件可下载」，不静默假成功——这是工单已记录的边界，非失败。

### 2.2 确认迁移已应用（prod DB）
- 部署后 `media_files` 表应已存在 `playback_ref` / `playback_status` 两列（`deploy_server.sh` step 5/9 已 `alembic upgrade head`）。
- 抽样核对（直连 DB 或 `python -m alembic heads` + 比对）：确认新增列存在且默认 `not_applicable`/`pending` 语义正常。
- 若部署异常未触发迁移：手动 `cd backend && alembic upgrade head`（仅前向，绝不 downgrade）。

### 2.3 确保转码代码在新进程生效
- 转码发生在**下载管线**（`wecom-archive-media-download` worker / `wecom-archive-worker`）。若这些是长驻进程，部署后需重启以加载新代码：
  - `sudo systemctl restart wecom-archive-media-download.service wecom-archive-worker.service`
  - （如由 timer 触发的一次性服务，则下一次 timer 触发即加载新代码，无需手动重启；用 `systemctl status wecom-archive-media-download.timer` 确认启用。）
- 主 API 服务 `wecom-archive-365.service` 已由 `deploy_server.sh` 重启（`deploy_server.sh:22`）。

### 2.4 运行历史回填（prod，关键动作）
- 部署且迁移完成后，执行一次性回填（**前台运行，不要加 `&`**）：
  - `cd backend && python scripts/backfill_voice_transcode_once.py`
- 观察日志：已下载（`download_status=downloaded`）的 voice 行应逐条生成 `playback_ref` + `playback_status="generated"`；SILK/异常行标记 `unsupported_format`/`failed` 并跳过，**批次不中断**。
- 抽样核对：随机挑 2–3 条 `generated` 行，确认 `playback_ref` 指向的对象可读回 MP3/WAV 字节（与原始同 backend / 同 `tenants/{tenant}/` 前缀）。
- 记录统计：generated / unsupported_format / failed / skipped 各多少条。
- **日志脱敏**：任何日志/异常不得含 signed URL、sdkfileid、local_path、storage_ref、`playback_ref`（对照 `download_wecom_media_once.py:58` 脱敏约定）。

### 2.5 验证下载管线实时转码（新消息）
- 部署后新到达的 voice / audio_archive 消息被 media-download worker 拉取时，应自动转码生成播放变体。
- 监控 worker 日志（`journalctl -u wecom-archive-media-download.service -f` 或等价）一段时间，确认有 voice 下载且 `playback_status` 推进为 `generated`；或人工触发一次媒体拉取核对。

### 2.6 真机 Production Verified（prod 浏览器，闭环）
- 用真实浏览器（Chrome / Firefox / Safari 至少一种）登录生产控制台，打开含**真实 WeCom** voice / audio_archive 的会话。
- 点击播放：原生 `<audio>` 正常播放，**不再出现「语音播放失败」**。
- 对一条 SILK 消息验证降级：UI 不应 500 / 崩溃，至少保留「可下载」语义或明确降级文案。
- 此步通过 + 2.4/2.5 通过，方可在 RND-258 结论中写 **Production Verified**（在此之前仅「已实现」）。

---

## 3. 回滚 / 失败处理

- **回填中途报错**：脚本按行跳过、不破坏；检查日志脱敏；记录失败行供后续分析。
- **ffmpeg 缺失（极端：apt 源异常）**：`deploy_server.sh` step 2/9 已失败拦截部署——需先修复 apt 源再重新部署，不要带病上线。
- **迁移失败**：`deploy_server.sh` 已有回滚（不 downgrade、回滚代码至 PREV_SHA），按脚本提示处理，必要时人工介入。
- **转码全失败**：若生产 ffmpeg 缺 `libmp3lame`/`libopencore-amr`（极少见），所有 voice 行 `playback_status=failed`，前端回落为「原 AMR 可下载」，**不影响现有图片/视频/文件管线**——但语音播放功能整体未修复，需先解决 ffmpeg 编解码器再回填。

---

## 4. 已知限制（必须写入 RND-258 评论）

- **SILK 不支持解码**：apt 版 ffmpeg 通常不含 SILK 解码器 → 企业 SILK 语音降级为「原文件可下载」，不静默假成功。此边界已在工单与开发提示词记录。
- **回填仅覆盖已下载行**：`download_status=downloaded` 的 voice 行被回填；未下载消息由下载管线实时转码，无需回填。
- **2c2g ECS 成本**：转码发生在下载期（worker）/回填期，非每次播放请求，MP3 64k 控制体积，可接受。

---

## 5. 收尾动作

- **不自行部署 / commit / push**；仅执行上述运维动作并把结果回贴 RND-258 评论。
- 在 RND-258 评论标注：
  - ffmpeg 编解码器核验结果（libmp3lame / libopencore-amr 有/无；SILK 无）；
  - 迁移核对结果（两列已应用 / 否）；
  - 回填统计（generated / unsupported_format / failed / skipped）；
  - **Production Verified：是 / 否**（仅当 2.6 真机通过 + 2.4/2.5 通过才可为「是」）；
  - SILK 限制说明。
- 状态保持交还 Haisu 决策（Todo → 视情况 Done）。
