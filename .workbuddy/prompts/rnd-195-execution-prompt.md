# RND-195 执行提示词（父任务：支持企业微信会话存档全部消息类型）

> 用途：粘贴给开发智能体（hy3 / Claude Code / Codex 皆可），由其驱动 RND-195 收口。
> 工单：`RND-195「支持企业微信会话存档全部消息类型」`，优先级 P2，状态 **Todo**，assignee Haisu Zuo，父脉络无（顶层）。
> 权威来源：Linear 工单 RND-195 描述 + 其 9 个子任务（RND-196/197/198/199/200/201/202/206/210）+ 项目现有代码（`backend/app/message_type_registry.py`、`structured_message_parser.py`、`media_classification.py`、`media_storage.py`、`revoke_reconciliation.py`、`routers/conversations.py`、`routers/media.py`、`main.py` 的 `_REVIEW_CONSOLE_HTML` / `_SEARCH_PAGE_HTML`、`web/static/console/message-renderers.js`、`assets/i18n.js`）。
> 设计/架构权威：`docs/ARCHITECTURE.md`、`docs/ai/current-status.md`、`docs/ai/known-pitfalls.md`、`DEV_AGENT_RULES.md`。

---

## 0. 任务与来源

- **父目标**：让「企业微信会话存档」覆盖官方全部消息类型——从解密 payload 解析、后端存储/访问，到前端时间线渲染与消息查看器，形成「注册 → 解析 → 渲染 → 验收」的完整闭环。
- **父任务完成闸门（来自 RND-195 描述，硬约束）**：**所有子任务通过独立 QA 验收后，父任务方可完成。**
- **独立推进规则（同样来自 RND-195 描述，硬约束）**：**archive（即 RND-202 企业版语音通话存档）独立推进，不阻塞服务版完整支持。** 即服务端「全部消息类型可用」可先于 RND-202 被认定完成；RND-202 作为独立 track 单独交付，不拖住其它 8 个子任务。
- **RND-202 验收边界（来自 RND-202 描述，硬约束）**：必须明确区分「代码与 fixture 已实现」和「使用真实企业版权限与真实通话数据完成验证」。**没有真实企业版环境时，不得宣称 Production Verified。**

### 0.1 子任务实时状态（2026-07-25 取自 Linear）

| 子任务 | 标题 | Linear 状态 | 说明 |
|--------|------|-------------|------|
| RND-196 | 建立消息类型 Registry 与支持矩阵 | **Done** | 已合入 `main` |
| RND-197 | 基础结构化消息解析与展示 | **Done** | 已合入 `main` |
| RND-198 | 互动业务与系统消息 | **Done** | 已合入 `main` |
| RND-199 | 通用媒体下载/存储/访问 | **Done** | 已合入 `main` |
| RND-200 | mixed 与 chatrecord 复合消息 | **Done** | 已合入 `main`（PARTIAL：已解析，复合查看器仍为 RND-206 范畴） |
| RND-201 | revoke 撤回关联与原内容留存 | **Done** | 已合入 `main`；其下有 2 个 child |
| ├─ RND-208 | 调查 DecryptData SIGSEGV 崩溃（exit 139） | **Done** | 根因已确认（SDK 缺陷） |
| └─ RND-231 | DecryptData SIGSEGV 防御性兜底 | **Todo（P0）** | **清场项：本执行提示词轨道一** |
| RND-202 | 企业版 audio_archive 语音通话存档 | **Backlog（P4）** | **唯一未开工子任务；本执行提示词轨道二（独立，不阻塞服务版）** |
| RND-206 | 统一消息查看器与富媒体前端 | **Done** | 已合入 `main` |
| RND-210 | 官方消息类型错配修复 + 名片渲染 | **Done** | 已合入 `main` |

**结论**：服务版完整支持所需的 8 个子任务（196/197/198/199/200/201/206/210）均已 Done 并合入 `main`。剩余真实可执行项：
1. **RND-231（P0）** —— 必须清场，否则 RND-201 不能算 QA 完整通过。
2. **RND-202（P4，独立 track）** —— 企业版语音通话存档的真实录音下载/播放；按父规则**不阻塞**服务版完成。

---

## 1. 当前已落地代码（开工前确认，禁止重复实现）

> 以下证据用于「确认已合入、不再重写」。若某条证据缺失 → **停下并报告 BLOCKED**，不要自己补做该子任务的活。

- **RND-196（Registry）**：`backend/app/message_type_registry.py` 含 `_DEFINITIONS`（~160）、`MessageCategory` / `MessageSupportStatus` / `MediaCapability` / `ParserStrategy` / `RendererStrategy` 枚举、`resolve()` / `describe_message_type()` / `build_support_matrix()`（~659）/ `build_frontend_registry_entries()`（~681）/ `build_filterable_type_options()`（~807）。`tests/test_message_type_registry_core.py` 存在且与前端 JS registry 做一致性校验。
- **RND-197/210（结构化解析 + 名片/官方类型）**：`structured_message_parser.py` 含 `parse_card_message`（~667）、`parse_voip_doc_share_message`（~753）、`parse_structured_content`（~1163）；`meeting_voice_call`/`meetingvoicecall` 与 `audio_archive` 已 alias 到 `parse_meetingvoicecall_message`（~823），不再落入 UNKNOWN 回退。`db/models.py`（~298）已含结构化字段列。
- **RND-198（互动/系统）**：Registry 中 `todo`/`vote`/`collect`/`meeting`/`schedule`/`redpacket`/`switch_corp` 标记为 SUPPORTED + STRUCTURED_CARD；`sys` 为 SYSTEM_CARD。`parse_structured_content` 覆盖了这些子类型；红包金额不落字段（安全）。
- **RND-199（媒体 pipeline）**：`media_classification.py`（~107–116 区分 image/video/voice/file/audio_archive/audio_doc 分层）、`media_storage.py`（含 Local/Qiniu Provider，~822 注明 audio_archive 暂无 byte-signature）、`routers/media.py`（`/media/access` + signed URL）。`conversations.py` 的 `SERVABLE_MEDIA_MSGTYPES` 负责把可服务媒体的 `media_access_url` 接到时间线。
- **RND-200（复合消息）**：`parse_mixed_message`（~1123）、`parse_chatrecord_message`（~1140）已实现递归解析；Registry 中 `mixed`/`chatrecord` 为 PARTIAL + COMPOSITE_VIEW + `is_composite=True`。注意：`media_type` 对这两类由 `unsupported` 变为 `structured`（API 可见副作用，前端按 `renderer_strategy`/`normalized_type` 分发，不受影响）。
- **RND-201（revoke）**：`revoke_reconciliation.py` 已存在；`conversations.py` 在时间线响应里过滤 `revoke` 行、对原消息标 `is_revoked`/`revoked_at`；Registry 中 `revoke` 为 CONTROL + SUPPORTED，渲染策略刻意留 UNSUPPORTED_PLACEHOLDER（行级不可达）。
- **RND-206（查看器/富媒体）**：`main.py` 的 `_REVIEW_CONSOLE_HTML` 含 `renderMessageBody` + `MEDIA_PREVIEW_KINDS`（image/video/voice/file emotion 真实 `<video>`/`<audio>` 懒加载播放、图片浮层上一张/下一张、chatrecord 展开子消息、link 安全卡片、emotion 预览）；`message-renderers.js` 配套。`_SEARCH_PAGE_HTML` 的 `focusMessage` 跳转回对话高亮（RND-229）已就位。
- **RND-210（名片/音频占位）**：`renderAudioArchiveMessage` 已渲染类型标签 + 结束时间 + 「不可播放」状态（真实播放属 RND-202）；card 已展示企业名 + 联系人标识。

> 开发 agent 第一步：用 `git log --oneline -5` 与 `git status` 确认当前在 `main` 且工作树干净（无未提交改动），再逐条核对上述文件确实存在。

---

## 2. 轨道一：RND-231（P0 防御性兜底，清场项）

> RND-201 已 Done，但其 child **RND-231（P0）仍 Todo**——这是「历史 revoke 回填时单条坏消息导致整批 DecryptData SIGSEGV（exit 139）终止」的运维风险。根因已由 RND-208 确认为 `libWeWorkFinanceSdkC.so v20240606` 的 SDK 内部缺陷（不在我方可控范围，不能换 SDK）。本轨道是**防御性兜底**，非根因修复。

### 2.1 范围（来自 RND-231 描述）
- 把每条历史消息的 `DecryptData` 调用放进**独立子进程/隔离层**（新增 `decrypt_message_isolated` 或等价外包隔离层）；`scripts/decrypt_wecom_messages_once.py` 与 `scripts/backfill_revoke_associations_once.py` **零改动**即受益。
- 结果分类：`success` / `sdk_decrypt_failed`（记录返回码）/ `sigsegv`（退出码 139）/ `other`——各自打 outcome 后继续下一条；**一条坏样本崩了只记 `sigsegv` outcome，不终止整批回填**。
- 畸形输入前置校验：`encryptmsg`/`encryptkey` 含 NUL / 超长 / 字符集异常 → 前置拦截，不调用 SDK。
- ⚠️ **重要纠正（来自 RND-231）**：**不要基于「密钥长度 < 32 字节」拒绝**——`encrypt_key` 长度 88 是 SDK 内部协议的正常格式，生产 3,538 条 ver=4 正是用该 88 字节格式解密成功。基于长度阈值拒绝会把正常数据全部拒掉，制造新故障。校验**只针对真正畸形输入**，正常的 88 字节 key 必须放行。
- 对 `sigsegv` 计数，使该情形可观测；现有 pending/failed 历史消息可用该机制安全重跑。

### 2.2 落点（文件/函数级）
- 新增隔离封装层（建议 `backend/app/services/decrypt_isolation.py` 或在 `decrypt_worker.py` 内新增 `decrypt_message_isolated`），用子进程 + 超时包裹 SDK `DecryptData`。
- `scripts/decrypt_wecom_messages_once.py`、`scripts/backfill_revoke_associations_once.py`：仅改为调用新隔离封装；**不改动其业务循环**。
- 结果/outcome 落库或落日志（复用现有 `decrypt_status`/outcome 字段语义），`sigsegv` 计数可观测。

### 2.3 非目标（严禁）
- 不替换 SDK、不在生产长期开启不受控 core dump、不修改 RND-201 已上线的 schema/实时 revoke 处理/前端行为、不立即执行历史 backfill。

---

## 3. 轨道二：RND-202（企业版 audio_archive，独立不阻塞服务版）

> 唯一未开工子任务。按父规则**独立推进，不阻塞服务版完整支持**——开发 agent 应把它作为独立 track 实现，但**不要**因为它未完而卡住 RND-195 服务版闸门的验收（见 §6）。

### 3.1 现状（已有什么，不要重写）
- Registry：`audio_archive` 已注册为 PARTIAL + `media_capability=SINGLE` + STRUCTURED_CARD，`aliases=("meeting_voice_call","meetingvoicecall")`（`message_type_registry.py` ~233）。
- 解析：`parse_meetingvoicecall_message` 已提取 `voiceid`/`endtime`/`sdkfileid`，及存在时的文档/共享屏幕数据（`structured_message_parser.py` ~699、~823）；`sdkfileid` 已由 `decrypt_wecom_messages_once` 在 `not is_text_content` 下独立拉取，媒体访问保留。
- 时间线：`timeline_service.py`（~443）SERVABLE 字段 = `{voiceid, endtime, shared_doc}`。
- 前端：`renderAudioArchiveMessage` 当前只渲染类型标签 + 结束时间 + 「不可播放」占位（真实播放属本轨道）。

### 3.2 待实现（本轨道增量）
1. **官方 schema + fixture**：在 `tests/` 新增 `meeting_voice_call` 真实载荷 fixture（通话双方 userid、开始/结束时间、时长、及存在时的 doc/屏幕共享数据），覆盖内部通话与「和微信客户通话」两种场景。
2. **通话 metadata 持久化与展示**：扩展结构化字段，正确保存通话双方、开始时间、结束时间、通话时长；`audio_archive` 元数据在时间线/查看器正确呈现。
3. **录音进入统一 media pipeline**：把 `sdkfileid` 接到 RND-199 的统一下载/存储/访问链路（`media_classification` + `media_storage` + `/media/access` signed URL）。当前 `media_storage.py`（~822）注明 audio_archive「无 byte-signature」且为 PARTIAL——本轨道将其提升为有真实字节：经与 image/video/voice/file 相同的 SDK `getmedia` 路径下载、存入配置的 storage backend（Local/Qiniu）、经授权 signed URL 提供访问。
4. **前端播放**：`renderAudioArchiveMessage` 在 `media_access_url` 可用时升级为真实播放器（镜像 RND-206 的 voice `<audio>` 懒加载逻辑），不可用时有明确且可操作的降级样式；关键元数据（双方、时长）不丢失。
5. **场景区分**：明确内部通话 vs 与微信客户通话场景的展示/权限差异。
6. **缺权限安全降级**：缺少企业版权限时，系统**安全忽略或明确标记**（不崩溃、不伪造内容、不暴露敏感 ID/密钥）；`classify_media` 当前对 audio_archive 报 PARTIAL，确保无企业版权限路径不破坏时间线。
7. **tenant isolation、访问权限、日志脱敏**：录音 URL/密钥不进日志；跨租户不可见。
8. **真实环境验证步骤与限制说明**：提供「如何在真实企业版权限 + 真实通话数据下验证」的步骤文档，并写明限制。

### 3.3 验收边界（硬约束，来自 RND-202）
- 必须区分 **「代码 + fixture 已实现」** 与 **「使用真实企业版权限与真实通话数据完成验证」**。
- **没有真实企业版环境时，不得宣称 Production Verified**——只能标「已实现（代码+fixture）」并附验证步骤与限制说明。

### 3.4 非目标（严禁）
- 不重新设计认证/租户隔离/会话聚合；不修改企业微信原始消息语义；不引入新的第三方媒体播放器依赖，除非浏览器原生能力无法满足且有明确评估；不修改 RND-199 的下载/存储 pipeline 本身（只接入）。

---

## 4. 执行步骤（复现 RED → 实现 → 验证 GREEN）

### 阶段一：确认已落地（先证明不缺，禁止先改实现）
- 逐条核对 §1 的文件/函数证据确实存在且在工作树。`git status` 应为干净（在 `main`）。
- 跑现有回归基线（先 `cd backend`）：
  - `python -m pytest tests/test_message_type_registry_core.py tests/test_search_api.py tests/test_search_page_js_syntax.py -q`
  - 若任一失败 → **停下报告 BLOCKED**，不要顺手改无关代码。

### 阶段二：轨道一 RND-231（P0 清场）
- 按 §2 实现隔离封装层 + 结果分类（success/sdk_decrypt_failed/sigsegv/other）+ 畸形输入前置校验（**放行正常 88 字节 key**）。
- 让两个 `scripts/*.py` 切换调用新封装，**零业务改动**。
- 新增/扩展测试：用一条已知坏样本（SIGSEGV）跑隔离层 → 断言只记 `sigsegv` outcome、整批继续；用成功样本对照；畸形 `encrypt_key`（NUL/超长）被前置拦截、不调 SDK；正常 88 字节 key 放行。
- 在隔离环境（非生产）验证，不开启不受控 core dump。

### 阶段三：轨道二 RND-202（独立 track）
- 按 §3.2 增量实现，复用 RND-199 media pipeline 与 Registry 现有 `audio_archive` 条目（**不要新建 msgtype 或改名**）。
- 新增 `meeting_voice_call` fixture + 元数据持久化/展示测试 + 录音下载/存储/访问测试 + 前端播放/降级测试。
- 明确标注本轨道「已实现（代码+fixture）」或「已 Production Verified（仅当有真实企业版环境）」——**无真实环境绝不标 Production Verified**。

### 阶段四：验证（不达标不收工）
- 轨道一：`python -m pytest tests/ -k "decrypt or sigsegv or isolation" -q` 全绿；确认 `decrypt_wecom_messages_once.py` / `backfill_revoke_associations_once.py` 业务循环未改。
- 轨道二：`python -m pytest tests/test_rnd_202_audio_archive.py -q`（新建）全绿；前端 JS 语法合法（`test_search_page_js_syntax.py` 或等价）。
- 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。

---

## 5. 硬性约束（不可违反）
- **工作直接在 `main` 上**；不创建 task 分支（Haisu 显式要求才破例）。
- **绝不自行 `git commit` / `push`**（需用户显式授权）。
- **最小正确改动**：8 个 Done 子任务禁止重复实现，只确认/修复其自身 QA 缺口；新增范围严格限定 RND-231 与 RND-202。
- **RND-202 不阻塞服务版**：即便 RND-202 未完，服务版 8 子任务仍可走独立 QA 闸门（见 §6）。
- **RND-202 边界**：无真实企业版环境不得标 Production Verified；密钥/URL 不进日志；跨租户不可见。
- **RND-231 关键纠正**：不基于密钥长度拒绝；88 字节 key 是正常格式，必须放行。
- **不暴露敏感 URL/密钥/本地路径**；tenant isolation 铁律（`tenant_id` 永远来自 `get_current_user`）。
- 不新增路由（route 数基线见 `test_http_contract.py`）；不引入后台常驻进程（假设开发服务器已在运行，命令前台运行）。
- 参考 `DEV_AGENT_RULES.md` 与 `docs/ai/known-pitfalls.md`。

---

## 6. 收尾动作
- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 在 Linear：
  - RND-231：状态置 **Done**（清场完成），评论附隔离层落点 + sigsegv 继续证据 + 88 字节 key 放行证据 + `make verify` 结论。
  - RND-202：状态保持 **In Progress / Backlog** 视实际推进；评论附「已实现（代码+fixture）」或「已 Production Verified」的明确标注与限制说明；若未做则写明「独立 track，未阻塞服务版」。
  - RND-195：状态**保持 Todo**（父闸门要求所有子任务通过独立 QA 后才可完成）；评论附子任务状态表 + 「服务版 8 子任务已具备独立 QA 条件、RND-202 独立推进」的说明，交还用户决策。
- 若实现中发现需要比预期更大的改动或存在未覆盖的官方类型，在评论中**如实标注边界**，不私自扩展范围。
