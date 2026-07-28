# RND-202 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-202 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-202-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-202**（企业版 audio_archive 语音通话存档）的验收标准 + 项目现有代码（`message_type_registry.py`、`structured_message_parser.py`、`media_classification.py`、`media_storage.py`、`services/decrypt_worker.py`、`services/media_worker.py`、`services/media_access.py`、`routers/media.py`、`services/timeline_service.py`、`web/static/console/message-renderers.js`）。

---

## 0. 验收依据

### 0.1 工单验收标准（来自 RND-202 描述，逐条必过）
- 官方 payload 有明确 schema 与测试 fixture。
- 通话 metadata 正确持久化与展示（通话双方、开始时间、结束时间、通话时长）。
- 录音进入统一 media pipeline（下载/存储/授权访问）。
- tenant isolation、访问权限、日志脱敏正确。
- 缺少企业版权限时系统安全忽略或明确标记。
- 提供真实环境验证步骤与限制说明。

### 0.2 验收边界（硬约束，来自 RND-202，验收 agent 必须断言）
- 必须明确区分 **「代码与 fixture 已实现」** 与 **「使用真实企业版权限与真实通话数据完成验证」**。
- **没有真实企业版环境时，不得宣称 Production Verified** —— 只能标「已实现（代码+fixture）」并附验证步骤与限制说明。
- 验收报告必须显式标注二者之一（见 §5）。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支（`main`）。若开发 agent 仅在工作树改动未提交，**只验收工作树**（不要求 commit），但结论注明「未提交」。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动**前台**进程后再验（不后台化、不加 `&`）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 解析与 schema
- **C1 官方 schema + fixture**：`backend/tests/fixtures/` 含 `meeting_voice_call_internal.json` 与 `meeting_voice_call_external_customer.json`（通话双方/起止时间/voiceid/sdkfileid/可选 shared_doc），覆盖内部通话与和微信客户通话。`parse_meetingvoicecall_message` 对 fixture 返回含 `caller`/`callee`/`starttime`/`endtime`/`voiceid`/`sdkfileid` 且能推导 `duration_seconds`。
- **C2 通话双方 + 起止 + 时长**：`parse_meetingvoicecall_message` 提取 `caller`/`callee`（双方 userid；外部通话时 callee 为外部标识）、`starttime`、`endtime`，`duration_seconds = endtime - starttime`（缺失时 `None`，不伪造）；场景（内部/微信客户）可被区分。

### 持久化与展示
- **C3 元数据持久化**：audio_archive 消息的结构化字段正确落库（structured_content），`caller`/`callee`/`starttime`/`endtime`/`duration_seconds` 可从 DB 读回。
- **C4 时间线展示**：时间线 API 对 audio_archive 返回 `caller`/`callee`/`starttime`/`endtime`/`duration_seconds`（及 `shared_doc` 若存在）；关键元数据不丢失。

### 媒体 pipeline
- **C5 录音进入统一 pipeline**：audio_archive 的 `sdkfileid` 经与 image/voice/file 相同的 `media_worker` 路径下载并落 `media_files`（`tenant_id`/`sdkfileid`/`storage_ref`/`download_status`）；不另写下载器。
- **C6 授权访问**：已下载录音经 `GET /api/conversations/{cid}/messages/{msgid}/media/access` 返回授权访问（`media_access_url` 非 null 且可解析；signed URL 不进日志/异常）。

### 前端
- **C7 真实播放**：`renderAudioArchiveMessage`（`message-renderers.js:359`）在 `media_access_url` 可用时渲染真实 `<audio controls>` 懒加载播放器，并展示通话双方、起止时间、时长；`shared_doc` 有则给可读链接。
- **C8 安全降级**：`media_access_url` 不可用（无 sdkfileid / 无企业版权限 / 未下载）时渲染明确可操作降级态（类型标签 + 「暂无可播放录音 / 权限未开通」等），关键元数据不丢失；不崩溃、不伪造内容。

### 安全与边界
- **C9 缺企业版权限安全忽略/标记**：构造无 `sdkfileid`（无权限）的 audio_archive 消息 → 时间线/API 不 500、渲染为明确降级态，不暴露敏感 ID/密钥。
- **C10 tenant isolation**：用**另一个租户**的认证调 `media/access` → 仅见本租户录音；`(tenant_id, sdkfileid)` 解析不被越权绕过（沿用 `test_media_access_descriptor.py` / `test_thumbnail_media_access.py` 套路）。
- **C11 日志脱敏**：任何日志/异常**不含** signed URL、sdkfileid、local_path、oss_key、storage_ref（抽查 `app/services/media_access.py` 与下载路径日志；对照 `download_wecom_media_once.py:58` 的脱敏约定）。
- **C12 公共字段不外泄**：时间线音频消息响应体**不含** `sdkfileid`/`corpid`/`media_key`（对照 `timeline_service.py:440` `INTERNAL_STRUCTURED_FIELD_KEYS` 与 `test_rnd_210_msgtype_and_card.py:541`）。

### 回归（不破坏既有能力）
- **C13 SERVABLE 集合未被改坏**：`test_generic_media_serving.py:109`（`SERVABLE_MEDIA_MSGTYPES == SUPPORTED_MIGRATION_MEDIA_TYPES | {"emotion"}`）仍 PASS —— 证明 audio_archive 接入走的是 timeline_service 显式分支，而非改迁移 guard 集合。
- **C14 注册表一致性**：`test_message_type_registry_core.py`（audio_archive 仍为 PARTIAL、别名解析、前端 registry 一致性）全绿；`test_rnd_210_msgtype_and_card.py` 全绿（音频卡片渲染未回退）。
- **C15 路由数不变**：`test_http_contract.py` 路由数断言仍 PASS（RND-202 未新增路由）。
- **C16 前端 JS 合法**：`test_search_page_js_syntax.py`（或等价）仍 PASS。

---

## 3. 测试方法

- **后端**：`cd backend` 后跑
  - `python -m pytest tests/test_rnd_202_audio_archive.py tests/test_message_type_registry_core.py tests/test_media_classification.py tests/test_generic_media_serving.py tests/test_rnd_210_msgtype_and_card.py tests/test_media_access_descriptor.py tests/test_http_contract.py -q`
  - 若 `test_rnd_202_audio_archive.py` 不存在（开发 agent 未交付测试）→ 本 agent **自行补最小验收测试**覆盖 C1–C6/C9–C12，再判定。
- **前端 / 集成**：用项目既有 Playwright 脚本做真实浏览器 smoke，覆盖 C7/C8（必要时编写针对性 Playwright 步骤或最小脚本）。
- **收口**：跑 `make verify` 确认 lint-diff + typecheck + build + 全量 pytest 全绿。

---

## 4. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过 tenant isolation 做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。
- RND-202 无真实企业版环境时，**绝不**在结论中写 Production Verified。

---

## 5. 输出格式（必须结构化）

```
## RND-202 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 是否含 RND-202 实现：是/否 · make verify：通过/失败

| 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|------|--------|------|---------------------------|
| C1   | 官方 schema + fixture | PASS | ... |
| C2   | 通话双方 + 起止 + 时长 | PASS | ... |
| ...  | ...    | FAIL | 复现：<步骤>；期望：<X>；实际：<Y> |
| C12  | 公共字段不外泄 | PASS | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### RND-202 边界确认（硬约束）
- 实现层（C1–C12）：PASS / FAIL
- Production Verified：是 / 否（无真实企业版环境 → 必须为否）
- 真实环境验证步骤与限制说明：齐备 / 缺失

### 边界与已知限制
- <观察到的限制，如 audio_archive 真实播放需企业版权限；amr 浏览器兼容情况等>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺依赖）。
- 标注：已实现（代码+fixture） / 已 Production Verified（仅当有真实企业版环境）。
```

- 若某条无论如何无法复现（如环境导致 C7/C8 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给可复现证据。
- 最终把该报告作为 Linear RND-202 评论贴出（状态保持 In Progress，交还用户 Haisu 决策合并）。
