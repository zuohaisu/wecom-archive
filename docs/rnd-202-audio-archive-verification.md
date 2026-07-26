# RND-202 验证步骤与限制说明：企业版 `audio_archive`（语音通话存档）

> **状态标注（硬约束，见 RND-202 工单描述）**：本轨道当前状态为 **「已实现（代码 + fixture）」**，**未 Production Verified**。
> 本环境没有真实企业版 WeCom 权限、没有真实录音数据，因此本文档只能覆盖到"代码可以正确解析/存储/播放一段已知格式的音频字节"这一层。**没有真实企业版环境时，不得宣称 Production Verified** —— 这条边界来自 RND-202 工单本身，本文档据此如实标注。

---

## 1. 已实现范围（代码 + fixture，本仓库测试已覆盖）

- `app/structured_message_parser.py::parse_meetingvoicecall_message`：解析官方 `meeting_voice_call` 信封（`voiceid` 位于顶层、`endtime`/`sdkfileid`/`demofiledata`/`sharescreendata` 位于 `meeting_voice_call` 子对象 —— 这是 RND-210 QA round 4 已确认的真实信封形状，非猜测）。RND-202 增量：防御性探测 `starttime`（候选字段，非官方确认字段），仅当 `starttime`/`endtime` 均存在且 `endtime > starttime` 时才计算 `duration_seconds` —— 缺失时不臆造。同时修复了一个真实回归：`audio_archive`（该类型的 `raw_type`，非仅别名）此前未被纳入顶层 `voiceid` 合并的 msgtype 判断，导致该拼写下 `voiceid` 会被静默丢弃。
- 通话双方：本身即通用 `sender`/`tolist`（`ArchiveMessage.sender`/`.tolist`，时间线 API 已为每条消息通用暴露 `sender_display_name`/`recipient_display_names`），无需 `audio_archive` 专属字段 —— 未重复实现。
- 媒体管线接入：`app/media_download.py::_SIGNATURE_CATEGORY_BY_MSGTYPE["audio_archive"]="voice"`（录音字节按语音字节签名校验，AMR/SILK/WAV/MP3）+ `app/media_storage.py::MEDIA_TYPE_KEY_CATEGORIES["audio_archive"]="call_recordings"`（独立存储目录，不与普通语音消息混放）。此改动使 `audio_archive` 自动加入 `SUPPORTED_MIGRATION_MEDIA_TYPES`/`SERVABLE_MEDIA_MSGTYPES`/`GENERIC_DOWNLOAD_MSGTYPES`，`scripts/download_wecom_media_once.py`、`timeline_service.py` 的 `media_access_url` 签发、`/media`/`/media/access` 路由**零改动**即可正确处理该类型（复用 RND-199 既有 pipeline，未重新实现下载/存储/鉴权逻辑）。
- 前端播放：`renderAudioArchiveMessage` 在 `media_status==='available' && media_access_url` 时，复用 RND-206 语音消息完全相同的懒加载路径（`richMediaPlaceholder('voice', ...)` → `hydrateRichMedia` → 真实 `<audio controls>`），否则保留既有"暂不支持播放"降级文案；新增开始时间/通话时长/对方（`recipient_display_names`）展示，缺失时均不显示（不臆造）。
- 测试覆盖（`backend/tests/test_rnd_202_audio_archive.py`，19 条，全绿）：
  - 内部通话 + 与微信客户通话两种 fixture 场景的字段抽取正确性。
  - 三种官方拼写（`meeting_voice_call`/`meetingvoicecall`/`audio_archive`）均正确路由并合并顶层 `voiceid`。
  - `starttime` 缺失、`endtime < starttime` 等异常输入下 `duration_seconds` 不被计算，不崩溃。
  - `classify_media` 在有/无 `sdkfileid`（即无企业版权限/未下载）两种情况下的降级行为。
  - `download_one()` 端到端：真实 AMR 字节被接受并按签名而非扩展名分类，非音频字节被拒绝且不留 `.part` 残留，失败诊断信息不泄漏 `sdkfileid`。
  - 前端：Node 真实执行 `renderAudioArchiveMessage`（非源码字符串匹配）验证真实播放器渲染、降级文案、字段缺失时不臆造。

## 2. 未覆盖 / 明确限制

- **未使用真实企业版 WeCom 会话存档权限验证过**：本仓库/开发环境没有企业版语音通话存档权限的 WeCom 测试企业，无法调用真实 `GetChatData`/`GetMediaData` 获取一条真实 `meeting_voice_call` 消息与其录音字节。
- **`starttime` 字段的官方 schema 未确认**：WeCom 官方文档目前只确认 `voiceid`/`endtime`/`sdkfileid`/`demofiledata`/`sharescreendata`。`starttime` 是防御性探测（候选键），如果真实企业版信封根本不携带该字段，前端将始终不显示"开始时间"/"通话时长"——这是已知的、按设计保守的限制，不是 bug。
- **"内部通话 vs 与微信客户通话"的场景区分未在后端做显式分类字段**：两种场景在解析/存储/权限上走**完全相同**的代码路径（都只是 `sender`/`tolist`/`roomid` 的通用消息属性），本仓库没有发现官方 payload 里有可靠的、已确认的"场景类型"字段可用于区分，因此没有新增一个可能猜错的分类字段。如果真实环境的信封确实携带这类字段，需要用真实样本补充。
- **`call_recordings` 存储路径分段、AMR/SILK/WAV/MP3 字节签名假设未经真实录音验证**：假设依据是"录音与普通语音消息共享同一 WeCom `GetMediaData` 取回路径，字节格式应当一致"，但未用真实企业版录音样本验证过这一假设本身。
- **前端"缺权限安全降级"只验证了 `media_status`≠`available` 的通用降级路径**，未验证真实"企业版权限缺失"时 WeCom 服务端实际返回什么错误码/是否影响 `sdkfileid` 的存在性 —— 现有实现假设无权限时后端根本拿不到 `sdkfileid`（`classify_media(has_sdkfileid=False)` 分支），如果真实情况是"拿得到 `sdkfileid` 但 `GetMediaData` 调用本身返回权限错误"，需要用真实环境验证 `download_one` 对应的失败分支（`sdk_error`）行为是否同样安全（当前实现的通用 `sdk_error`/`unsupported_type` 分支应能覆盖，但未用真实权限错误验证过）。

## 3. 真实企业版环境下的验证步骤（供后续在有权限的企业验证时使用）

前提：一个已开通「会话内容存档」且已升级到**企业版**（具备语音通话存档权限）的 WeCom 企业，`WECOM_SDK_LIB_PATH`/`WECOM_CORP_ID`/`WECOM_ARCHIVE_SECRET`/`WECOM_PRIVATE_KEY_PATH`/`WECOM_PUBLIC_KEY_VERSION` 均已配置，且该企业内确有一通已结束的语音/视频通话被存档。

1. **确认消息落库**：运行现有同步 worker（`scripts/sync_wecom_archive_once.py`）后，在 `archive_messages` 表中确认出现一条 `msgtype` 为 `meeting_voice_call`（或 `meetingvoicecall`/`audio_archive`，视该企业 SDK 版本实际吐出的拼写而定）的行。
2. **确认解密与结构化字段**：运行 `scripts/decrypt_wecom_messages_once.py` 后，检查该行 `decrypt_status='success'` 且 `structured_content->'fields'` 含 `voiceid`/`endtime`/`sdkfileid`；记录真实信封里是否出现本文档 §2 提到的 `starttime` 或任何"内部/外部通话"分类字段——如果有，据此更新 `parse_meetingvoicecall_message` 的候选键探测,而不是继续用本文档假设的键名。
3. **确认媒体下载**：运行 `scripts/download_wecom_media_once.py`（默认 `--types` 已包含 `audio_archive`，无需显式指定）后，检查对应 `media_files` 行 `download_status='downloaded'`、`storage_ref` 路径包含 `call_recordings/`，且下载的字节能被 `ffprobe`/播放器正确识别为音频（验证 §2 提到的字节签名假设）。
4. **确认时间线可见与鉴权**：在审阅控制台打开该对话，确认卡片正确显示类型标签/结束时间/（如信封确有该字段）开始时间与时长/对方，且能听到真实录音播放；换一个非该 tenant 的账号访问同一 `/media/access` 链接确认返回 401/403/404（tenant isolation，复用既有 `/media/access` 鉴权，未新增校验逻辑）。
5. **确认日志/URL 不泄漏**：抓取 worker 日志与浏览器网络面板，确认 `sdkfileid`、签名 URL 的密钥参数、私钥内容均未出现在任何日志或前端可见位置（复用既有 RND-199/RND-174 的日志脱敏约定，未新增日志点）。
6. **验证结果回填**：完成以上 6 步后，将本文档 §1 的状态从「已实现（代码 + fixture）」更新为「已 Production Verified」，并在 Linear RND-202 下附上验证环境（企业/日期）与关键截图/日志片段（不得包含私钥/密文/录音内容本身）。

## 4. 非目标（未做，不在本轨道范围）

- 未重新设计认证/租户隔离/会话聚合逻辑。
- 未修改企业微信原始消息语义。
- 未引入新的第三方媒体播放器依赖（复用浏览器原生 `<audio>`）。
- 未修改 RND-199 的下载/存储 pipeline 本身，只是让 `audio_archive` 通过已有的注册表（`MEDIA_TYPE_KEY_CATEGORIES`/`_SIGNATURE_CATEGORY_BY_MSGTYPE`）接入。
