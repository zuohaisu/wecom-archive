# RND-202 执行提示词（单人端到端：调研 → 复现 → 实现 → 验证）

> 用途：粘贴给单一开发智能体（hy3 / Claude Code / Codex 皆可），由其端到端跑完 RND-202。
> 工单：`RND-202「调研并支持企业版 audio_archive 语音通话存档」`，优先级 **P4**，状态 **In Progress**，assignee Haisu Zuo，父脉络 **RND-195**（支持全部消息类型）。
> 权威来源：Linear RND-202 + RND-195 描述；现有代码（已逐项确认，见 §1）；`DEV_AGENT_RULES.md`。
> 设计/架构权威：`docs/ARCHITECTURE.md`、`docs/ai/current-status.md`、`docs/ai/known-pitfalls.md`、`DEV_AGENT_RULES.md`。

---

## 0. 任务与验收边界

- **目标**：让企业版语音通话存档（`msgtype` = `audio_archive` / `meeting_voice_call` / `meetingvoicecall`）具备「录音下载 → 存储 → 授权访问 → 前端播放」的完整闭环，并正确展示通话元数据；明确区分**内部通话**与**和微信客户通话**两种场景。
- **验收边界（硬约束，来自 RND-202 描述）**：
  - 必须严格区分 **「代码与 fixture 已实现」** 与 **「使用真实企业版权限 + 真实通话数据完成验证」**。
  - **没有真实企业版环境时，不得宣称 Production Verified** —— 只能标「已实现（代码+fixture）」，并附「真实环境验证步骤与限制说明」。
- **父任务关系**：RND-202 是 RND-195 的**独立 track**，**不阻塞**服务版 8 子任务的验收闸门（即便本任务未完，服务版也可独立认定完成）。
- **本任务范围聚焦**：录音真实可下载/可播放 + 元数据正确持久化与展示 + 缺权限安全降级 + tenant isolation/日志脱敏。不重新设计认证/租户隔离/会话聚合，不修改企业微信原始消息语义。

---

## 1. 当前已落地（开工前确认，禁止重写）

> 以下证据已通过代码检索确认存在且在工作树；若某条缺失 → **停下并报告 BLOCKED**，不要自己补做别的子任务的活。

- **Registry**：`backend/app/message_type_registry.py:233` 已定义 `audio_archive`（`raw_type="audio_archive"`，`normalized_type="audio_archive"`，`aliases=("meeting_voice_call","meetingvoicecall")`，`PARTIAL` + `media_capability=SINGLE` + `category=MEDIA`）。`test_message_type_registry_core.py:697` 断言其为 PARTIAL 且**故意排除在 `SUPPORTED_MIGRATION_MEDIA_TYPES` 之外**（历史迁移不覆盖，仅新录音）。
- **解析**：`backend/app/structured_message_parser.py:696` `parse_meetingvoicecall_message` 已提取 `voiceid` / `endtime` / `sdkfileid` 及可选 `shared_doc` / `demofiledata` / `sharescreendata`；别名映射在 `:823-825`。**关键缺口**：当前**未提取通话双方（`caller`/`callee`）与开始时间（`starttime`）**，因此无法展示「通话双方」与「通话时长」——这是本任务的真实增量。
- **媒体分类**：`backend/app/media_classification.py:69` `classify_media("audio_archive", has_sdkfileid=True)` 已返回 byte-bearing（`status="not_downloaded"`），即已被当作可下载媒体。**无需改动分类逻辑本身**（除非要调整缺 sdkfileid 时的文案）。
- **下载触发**：`backend/app/services/decrypt_worker.py:171-208` 已为非文本内容（audio_archive 属此类，`get_parser_strategy != TEXT_CONTENT`）持久化顶层 `sdkfileid`；`backend/app/services/media_worker.py:157-183` 通过 `get_or_reset_media_file(session, tenant_id, sdkfileid, ...)` 实际下载并落 `media_files`。下载候选选择基于 `ArchiveMessage.sdkfileid.isnot(None)`（顶层），audio_archive 带顶层 sdkfileid → **应已被候选命中**（开发 agent 第一步须实测确认是否真的下载成功并落库）。
- **存储**：`backend/app/media_storage.py:822` 注明 audio_archive「无 byte-signature」——下载/存储路径需对该类型正确落字节并产出可被浏览器播放的内容类型（amr/mp3 等，按 SDK 实际返回），不要为「签名」伪造。
- **访问**：`backend/app/routers/media.py:200` `GET /api/conversations/{conversation_id}/messages/{msgid}/media/access` 已存在；`backend/app/services/media_access.py` 按 `(tenant_id, sdkfileid)` 解析（tenant 永远来自 `get_current_user`），signed URL 本身**永不进日志/异常**（`:269`、`:397`）。**无需新路由**。
- **时间线公共字段白名单**：`backend/app/services/timeline_service.py:445` `audio_archive` 白名单 = `frozenset({"voiceid","endtime","shared_doc"})`。`sdkfileid` 经 `_project_public_structured_fields`（由 `INTERNAL_STRUCTURED_FIELD_KEYS` 于 `:440` 保护）**绝不外泄**（回归见 `test_rnd_210_msgtype_and_card.py:541`）。
- **时间线媒体接入**：`timeline_service.py` 在 `:695` 调 `classify_media`，在 `:818/:855`（及 `:1100/:1124` 第二处端点）为 `msg.msgtype in SERVABLE_MEDIA_MSGTYPES` 构造 `media_access_url`。`SERVABLE_MEDIA_MSGTYPES`（`media_storage.py:1044`）= `SUPPORTED_MIGRATION_MEDIA_TYPES | {"emotion"}`，**不含 audio_archive**，且被 `test_generic_media_serving.py:109` 锁定 → **不可通过改该集合来「接入」**，否则破坏迁移 guard。
- **前端**：`backend/app/web/static/console/message-renderers.js:359` `renderAudioArchiveMessage` 当前仅渲染类型标签 + 结束时间 + 「不可播放」占位（真实播放属本任务）。

> 开发 agent 第一步：用 `git log --oneline -5` 与 `git status` 确认当前在 `main` 且工作树干净（无未提交改动），再逐条核对上述文件确实存在、行号仍可对应。

---

## 2. 精确落点（文件 / 函数级）

### 2.1 解析扩展 · `backend/app/structured_message_parser.py` `parse_meetingvoicecall_message`（~696）
- 增加提取：`caller`、`callee`（通话双方 userid；和微信客户通话时 `callee` 可能是外部标识）、`starttime`（epoch 秒，若有）。
- 由 `starttime` / `endtime` 推导 `duration_seconds`（`endtime >= starttime` 时），缺失任一时置 `None`，**不伪造**。
- 场景区分：内部通话 vs 和微信客户通话——用 `caller`/`callee` 是否含外部标识或 payload 中 `type`/`calltype` 字段做 defensive 探测，**不臆造**场景字段。
- 保持：`sdkfileid` 仅留在内部 media 引用（走 `media_refs`/下载路径），绝不作为公共字段外泄；`shared_doc`/`demofiledata`/`sharescreendata` 保持原行为。

### 2.2 Registry · `backend/app/message_type_registry.py`（~233）
- **复用**现有 `audio_archive` 定义（含别名），**不要新建 msgtype 或改名**。
- 仅当录音可真实播放、且确认不破坏一致性测试时，才考虑把 `support_status` 从 `PARTIAL` 提升；任何改动须同步 `build_support_matrix` / 前端 registry 一致性（`test_message_type_registry_core.py`）并跑绿。

### 2.3 媒体接入 · 下载 / 存储 / 访问（复用 RND-199 pipeline，只接入不重做）
- 实测确认 `decrypt_worker`（~171-208）已为 audio_archive 持久化 `sdkfileid`，且 `media_worker`（~157-183）**实际完成下载并写入 `media_files`**（`tenant_id`/`sdkfileid`/`storage_ref`/`download_status`）。若发现未下载（任何 gate 跳过），最小修复让该类型进入既有下载候选人集合，**不要另写一套下载器**。
- `media_storage.py:822`「无 byte-signature」：确保下载/存储对 audio_archive 正确落字节并产出可播放内容类型（amr/mp3，按 SDK 实际返回）。不为「签名」伪造。
- 访问：复用 `routers/media.py:200` 的 `/messages/{msgid}/media/access`（tenant 由 `get_current_user` 提供，按 `(tenant_id, sdkfileid)` 解析，signed URL 不进日志）。**不新增路由**。

### 2.4 时间线接入 · `backend/app/services/timeline_service.py`（~445, ~695, ~818/:855, ~1100/:1124）
- 扩充 audio_archive 公共字段白名单：加入 `starttime`、`caller`、`callee`、`duration_seconds`（保持 `sdkfileid` 不在白名单）。
- 在**两处**媒体访问解析点（~818/:855 与 ~1100/:1124）为 `audio_archive` 增加**显式分支**：当 `msg` 含 `sdkfileid` 且 `classify_media` 判定为下载态（`not_downloaded`/已 `available`）时，构造与 image/voice 同形的 `media_access_url`。**不要**借改 `SERVABLE_MEDIA_MSGTYPES`（media_storage.py:1044）达成，否则破坏 `test_generic_media_serving.py:109`。

### 2.5 前端播放 · `backend/app/web/static/console/message-renderers.js` `renderAudioArchiveMessage`（~359）
- `media_access_url` 可用时升级为真实 `<audio controls>` 懒加载播放（镜像 voice 渲染逻辑），并展示：通话双方、开始/结束时间、通话时长；`shared_doc` 如有则给可读链接。
- 不可用时（无 sdkfileid / 无企业版权限 / 未下载）给**明确且可操作降级**：类型标签 + 「暂无可播放录音 / 企业版权限未开通」等文案，关键元数据（双方、时长）不丢失。
- 不引入新的第三方播放器依赖；复用现有视觉令牌与渲染风格（见 `web/static/console/` 既有约定）。

### 2.6 Fixture 与文档
- 新增 `backend/tests/fixtures/meeting_voice_call_internal.json`（内部通话）与 `meeting_voice_call_external_customer.json`（和微信客户通话），覆盖 `caller`/`callee`/`starttime`/`endtime`/`voiceid`/`sdkfileid` 及可选 `shared_doc`。
- 在开发与验收注释中给出「真实企业版权限 + 真实通话数据」验证步骤与限制说明（无真实环境时明确写为限制说明，不标 Production Verified）。

---

## 3. 执行步骤（严格：复现(RED) → 实现 → 验证(GREEN)）

### 阶段一：确认已落地（先证明不缺，禁止先改实现）
- 逐条核对 §1 的文件/函数证据确实存在且在工作树。`git status` 应为干净（在 `main`）。
- 跑现有回归基线（先 `cd backend`）：
  - `python -m pytest tests/test_message_type_registry_core.py tests/test_media_classification.py tests/test_generic_media_serving.py tests/test_rnd_210_msgtype_and_card.py -q`
  - 若任一失败 → **停下报告 BLOCKED**，不要顺手改无关代码。
- 实测确认 audio_archive 录音是否已被 `media_worker` 下载落库（用一条已知 sdkfileid 的 audio_archive 消息验证 `media_files` 行 `download_status`）。

### 阶段二：复现 / 红灯（先证明缺口，禁止先改实现）
- 在 `backend/tests/` 新增 `test_rnd_202_audio_archive.py`，断言「尚未实现」的行为，预期**失败（RED）**：
  - T1：用 `meeting_voice_call_internal.json` fixture 调 `parse_meetingvoicecall_message` → 断言返回含 `caller`/`callee`/`starttime`/`duration_seconds`（当前仅 `voiceid`/`endtime`/`sdkfileid` → RED）。
  - T2：audio_archive 消息带已下载 `media_files` 行时，时间线响应 `media_access_url` 非 null（当前因不在 `SERVABLE_MEDIA_MSGTYPES` → null → RED）。
  - T3：时间线公共字段含 `caller`/`callee`/`starttime`/`duration_seconds` 且**不含** `sdkfileid`（泄漏基线）。
  - T4：缺 sdkfileid（无企业版权限）的 audio_archive 消息 → 时间线/API 不 500、渲染为明确降级态。
  - T5（租户隔离基线）：跨租户 user 调 `media/access` → 仅见本租户录音。
- 运行确认 T1–T4 RED、T5 通过。若某项无论如何都通过 → 写 `NOT REPRODUCED` 证据进 Linear 评论，**绝不假装修好**。

### 阶段三：实现（最小改动，严守 §2 + 硬约束）
- 按 2.1 扩展 `parse_meetingvoicecall_message`（caller/callee/starttime/duration_seconds + 场景区分）。
- 按 2.3 确认/修复下载落库（仅在确有 gate 跳过时最小修复）。
- 按 2.4 在 timeline_service 两处显式接入 audio_archive 媒体访问 + 扩充公共字段白名单（不碰 `SERVABLE_MEDIA_MSGTYPES`）。
- 按 2.5 升级 `renderAudioArchiveMessage` 为真实播放器 + 降级态。
- 按 2.6 新增 fixture + 在注释/Linear 评论给出真实环境验证步骤与限制说明。

### 阶段四：验证（不达标不收工）
- 阶段二 T1–T4 必须转 **GREEN**；T5 保持通过。
- 新增/扩展测试覆盖工单验收点：fixture schema、元数据持久化/展示、录音进统一 pipeline、前端播放/降级、缺权限安全忽略、tenant isolation/日志脱敏。
- 运行既有套件确认无回归（先 `cd backend`）：
  - `python -m pytest tests/test_rnd_202_audio_archive.py tests/test_message_type_registry_core.py tests/test_media_classification.py tests/test_generic_media_serving.py tests/test_rnd_210_msgtype_and_card.py tests/test_http_contract.py -q`
  - 重点盯 `test_generic_media_serving.py:109`（SERVABLE 集合未被改坏）与 `test_rnd_210_msgtype_and_card.py:541`（sdkfileid 不泄漏）。
  - 前端 JS 语法合法：`test_search_page_js_syntax.py` 或等价。
  - 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。

---

## 4. 硬性约束（不可违反）

- **工作直接在 `main` 上**；不创建 task 分支（Haisu 显式要求才破例）。
- **绝不自行 `git commit` / `push`**（需用户显式授权）。
- **最小正确改动**：已 Done 的 RND-195 子任务（196–201/206/210）禁止重复实现，只在 RND-202 自身范围内增量；Registry 的 `audio_archive` 条目复用、**不新建/不改名**。
- **验收边界**：无真实企业版环境**绝不**标 Production Verified；密钥/signed URL/本地路径**不进日志**；跨租户不可见。
- **不要改 `SERVABLE_MEDIA_MSGTYPES`**（media_storage.py:1044，迁移 guard 锁定）；audio_archive 接入走 timeline_service 显式分支。
- **tenant isolation 铁律**：`tenant_id` 永远来自 `get_current_user`；媒体访问按 `(tenant_id, sdkfileid)` 解析；公共字段白名单永远排除 `sdkfileid`/`corpid`/`media_key`。
- **不新增路由**（route 数基线见 `test_http_contract.py`）；不引入后台常驻进程（假设开发服务器已在运行，命令前台运行）。
- 不引入新的第三方媒体播放器依赖，除非浏览器原生能力无法满足且有明确评估。
- 参考 `DEV_AGENT_RULES.md` 与 `docs/ai/known-pitfalls.md`。

---

## 5. 收尾动作

- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 在 Linear 把 RND-202 状态保持 **In Progress**（或 Backlog 视推进），写一条评论：改动文件/函数级清单 + 红灯→绿灯证据 + `make verify` 结论 + **明确标注「已实现（代码+fixture）」或「已 Production Verified（仅当有真实企业版环境）」** + 真实环境验证步骤与限制说明。
- 若实现中发现需要比预期更大的改动或存在未覆盖的官方字段/场景，在评论中**如实标注边界**，不私自扩展范围。
