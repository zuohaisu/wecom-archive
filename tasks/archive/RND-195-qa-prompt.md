# RND-195 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对 **RND-195 及其 9 个子任务**做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-195-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-195**（父任务）描述 + **9 个子任务**（RND-196/197/198/199/200/201/202/206/210）的验收标准 + 项目现有代码（`message_type_registry.py`、`structured_message_parser.py`、`media_classification.py`、`media_storage.py`、`revoke_reconciliation.py`、`routers/conversations.py`、`routers/media.py`、`main.py`、`web/static/console/message-renderers.js`、`assets/i18n.js`）。

---

## 0. 验收依据

### 0.1 父任务闸门（来自 RND-195 描述，硬约束）
- **所有子任务通过独立 QA 验收后，父任务方可完成。**
- **archive（RND-202）独立推进，不阻塞服务版完整支持。** → 验收结论必须区分两层：
  - **服务版完整支持**：RND-196/197/198/199/200/201/206/210 这 8 个子任务全部 PASS → 即可认定「服务版完整支持」成立；
  - **含 archive 全量**：需 RND-202 也 PASS → 父任务 RND-195 方可标 Done。RND-202 未完时，服务版可独立认定完成，RND-202 作为独立 track 继续。

### 0.2 RND-202 验收边界（来自 RND-202 描述，硬约束）
- 必须明确区分 **「代码与 fixture 已实现」** 与 **「使用真实企业版权限与真实通话数据完成验证」**。
- **没有真实企业版环境时，不得宣称 Production Verified**——只能标「已实现（代码+fixture）」并附验证步骤与限制说明。

### 0.3 子任务验收标准来源（逐条必过，以下为要点提炼）
> 完整原文见各 Linear 子任务描述；本表为可断言的验收点提炼。

- **RND-196（Registry 与支持矩阵）**：所有已知消息类型均在 Registry 有定义；未知类型有稳定 fallback；支持矩阵可由代码/测试验证；新增类型只需注册+解析+展示元数据+测试；Backend 与 Frontend 复用统一类型定义或等价契约。
- **RND-197（基础结构化）**：每种消息均可从解密 payload 正确解析、保存、展示；不展示原始 JSON 代替正式支持；缺失可选字段不导致时间线失败；未知/异常数据有明确 fallback；Backend+Frontend 测试覆盖全部类型。
- **RND-198（互动业务与系统）**：所有类型有明确解析与展示策略；系统事件不误渲染为普通用户消息；缺失字段与未知子类型安全降级；时间/参与人/状态等关键字段准确；Backend+Frontend 测试覆盖全部类型。
- **RND-199（通用媒体下载/存储/访问）**：各媒体类型复用统一 pipeline，无多套重复实现；新媒体下载默认进配置 storage backend；tenant isolation、幂等、失败恢复正确；无媒体实体的消息不进下载流程；Local 与 Qiniu 对 image/video/voice/file 访问均正常；RND-186 已迁移历史 video/voice/file 可经授权 Media Access/Signed URL 获取；Timeline 对各类型提供正确展示/播放/下载或明确 fallback；Backend/Frontend/storage regression 通过。
- **RND-200（mixed & chatrecord）**：mixed/chatrecord 能展示真实子消息而非仅摘要/原始 JSON；已支持子类型复用现有 renderer；未知子类型安全 fallback；嵌套媒体遵循统一 media access；深度异常不导致递归失控/页面崩溃/性能问题；Backend+Frontend 测试覆盖多层嵌套/混合类型/异常数据。
- **RND-201（revoke）**：原消息已存在时 revoke 正确关联并标记；原文本/结构化内容/已下载媒体仍保留；时间线展示原内容并明确显示「已撤回」；revoke 先到、原消息后到可自动补关联；原消息缺失时保留 revoke 事件与待关联信息、不伪造内容；重复 revoke 不重复修改/生成/破坏时间线；撤回不触发本地或七牛媒体文件删除；tenant isolation/权限/审计正确；测试覆盖原消息先到/revoke 先到/跨批次/原消息缺失/重复 revoke/文本撤回/媒体撤回/时间线标识/原内容与媒体仍可访问。
- **RND-202（企业版 audio_archive）**：官方 payload 有明确 schema 与测试 fixture；通话 metadata 正确持久化与展示；录音进入统一 media pipeline；tenant isolation/访问权限/日志脱敏正确；缺少企业版权限时系统安全忽略或明确标记；提供真实环境验证步骤与限制说明。（边界见 §0.2）
- **RND-206（统一查看器与富媒体前端）**：图片可浮层放大 + 上一张/下一张；视频不再显示「不支持视频消息」、可浏览器内播放或具体格式 fallback；chatrecord 可展开看真实子消息；link 以安全可读卡片展示并可打开；emotion 图片/GIF 可正常显示查看；revoke 能据后端数据展示原内容与撤回状态或明确说明原消息不可用；所有 viewer 关闭后保留原会话与滚动状态；加载失败/媒体缺失/权限不足/未知类型均有清晰 fallback；不暴露敏感 URL/密钥/本地路径；中英文及现有 i18n 文案一致；Backend contract/Frontend renderer/Browser-level tests 覆盖核心类型；生产或等价环境完成 image/video/chatrecord/link/emotion/revoke 端到端浏览器验收。
- **RND-210（官方类型错配修复 + 名片渲染）**：`meetingvoicecall` 与 `voipdocshare` 不再归为未知类型；音频通话存档可播放或以明确可操作降级样式展示、关键元数据不丢失；名片不再显示通用不可用占位、至少展示企业名称及联系人名称或标识；联系人数据可用时名片名称与手机端一致、无法从协议还原的头像等字段有明确降级规则；回归测试覆盖上述官方原始类型及名片渲染。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支（`main`）。若开发 agent 仅在工作树改动未提交，**只验收工作树**（不要求 commit），但结论注明「未提交」。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动**前台**进程后再验（不后台化、不加 `&`）。
4. 确认待测范围：本验收默认覆盖 9 个子任务；若用户明确只验收「服务版 8 子任务（不含 RND-202）」，按 §0.1 双层结论处理。

---

## 2. 各子任务验收清单（逐条 PASS / FAIL + 证据）

> 每条给「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §6。

### 2.1 RND-196（Registry）
- **C196-1** Registry 覆盖全部已知类型：`python -c "from app.message_type_registry import MESSAGE_TYPE_DEFINITIONS; print(len(MESSAGE_TYPE_DEFINITIONS))"` 返回非负且覆盖 text/image/video/voice/file/audio_archive/location/link/card/emotion/weapp/markdown/news/docmsg/audio_doc/todo/revoke/mixed/chatrecord/sys/vote/collect/meeting/schedule/redpacket/switch_corp。
- **C196-2** 未知类型稳定 fallback：`resolve("never_heard_of")`、`resolve(None)` 不抛错且 `normalized_type=="unknown"` / `support_status=="unknown"`。
- **C196-3** 支持矩阵可由测试验证：`build_support_matrix()` 返回结构与 `tests/test_message_type_registry_core.py` 断言一致；前端 JS registry 与后端 `build_frontend_registry_entries()` 一致（该测试存在即通过）。
- **C196-4** 新增类型只注册：`git diff` 无在第四处新增 if/elif 的散落分支（Registry 为单一权威源）。

### 2.2 RND-197（基础结构化）
- **C197-1** 解析正确：用 fixtures 跑 `parse_structured_content` 对 location/link/markdown/news/docmsg 等返回非 None 且字段正确；不返回原始 JSON 当正式支持。
- **C197-2** 缺失可选字段不崩：构造缺字段 payload → 时间线/API 不 500。
- **C197-3** 未知/异常 fallback：畸形 payload → 明确 fallback（不崩、不伪造）。
- **C197-4** Backend+Frontend 测试覆盖全部类型（查 `tests/` 对应用例存在且通过）。

### 2.3 RND-198（互动业务与系统）
- **C198-1** 类型覆盖：todo/vote/collect/meeting/schedule/redpacket/switch_corp/sys 均有解析+展示策略（Registry SUPPORTED + STRUCTURED_CARD/SYSTEM_CARD）。
- **C198-2** 系统事件不误渲染为普通用户消息（`sys` 走 SYSTEM_CARD，不在时间线当普通气泡）。
- **C198-3** 安全降级：未知子类型/缺失字段不崩。
- **C198-4** 关键字段准确 + 金额不落字段（redpacket 金额仅 raw 保留，结构化字段无金额）。

### 2.4 RND-199（通用媒体）
- **C199-1** 统一 pipeline：image/video/voice/file 复用同一下载/存储/访问链路（无重复实现；`media_classification` + `media_storage` 单一路径）。
- **C199-2** 无媒体实体不进下载：`not is_text_content` 之外无媒体引用的消息不触发下载。
- **C199-3** Local 与 Qiniu 对 image/video/voice/file 访问均正常（用两类 backend 各跑一遍媒体访问测试）。
- **C199-4** RND-186 已迁移历史 video/voice/file 经授权 Media Access/Signed URL 可获取。
- **C199-5** tenant isolation/幂等/失败恢复正确（跨租户不可见；重复下载幂等）。
- **C199-6** Timeline 对各类型提供正确展示/播放/下载或明确 fallback；Backend/Frontend/storage regression 通过。

### 2.5 RND-200（mixed & chatrecord）
- **C200-1** 真实子消息：mixed/chatrecord 渲染真实子消息（非仅摘要/原始 JSON）。
- **C200-2** 已支持子类型复用 renderer；未知子类型安全 fallback。
- **C200-3** 嵌套媒体遵循统一 media access。
- **C200-4** 深度异常不失控：构造超深/损坏 payload → 不递归崩溃、不页面卡死、性能可控。
- **C200-5** Backend+Frontend 测试覆盖多层嵌套/混合类型/异常数据。

### 2.6 RND-201（revoke，含 RND-231）
- **C201-1** 原消息已存在 → revoke 正确关联并标记 `is_revoked`/`revoked_at`，原文本/结构化内容/已下载媒体仍保留。
- **C201-2** 时间线展示原消息内容并明确「已撤回」标识。
- **C201-3** revoke 先到、原消息后到 → 自动补关联；原消息缺失 → 保留 revoke 事件与待关联信息、不伪造内容。
- **C201-4** 重复 revoke 不重复修改/生成/破坏时间线；撤回不触发本地或七牛媒体文件删除（抽查媒体文件未被删）。
- **C201-5** tenant isolation/权限/审计正确；测试覆盖全部场景（原消息先到/revoke 先到/跨批次/缺失/重复/文本撤回/媒体撤回/时间线标识/原内容与媒体仍可访问）。
- **C201-6（RND-231 清场项）**：历史回填中单条坏消息（SIGSEGV）只记 `sigsegv` outcome、不终止整批；畸形 `encrypt_key`（NUL/超长）被前置拦截不调 SDK；**正常 88 字节 key 必须放行**（不基于长度阈值拒绝）；`decrypt_wecom_messages_once.py`/`backfill_revoke_associations_once.py` 业务循环未改。

### 2.7 RND-202（企业版 audio_archive，独立 track）— 见 §3 边界
- **C202-1** 官方 schema + fixture：`tests/` 含 `meeting_voice_call` 真实载荷 fixture（通话双方/起止时间/时长/文档或屏幕共享数据），覆盖内部通话与和微信客户通话。
- **C202-2** 通话 metadata 正确持久化与展示（双方/起止/时长）。
- **C202-3** 录音进入统一 media pipeline：经与 image/video/voice/file 相同路径下载、存入配置 backend、经授权 signed URL 访问。
- **C202-4** 前端播放或明确降级：`media_access_url` 可用时真实播放器；不可用时明确可操作降级样式、关键元数据不丢失。
- **C202-5** 缺少企业版权限时系统安全忽略或明确标记（不崩、不伪造）。
- **C202-6** tenant isolation/访问权限/日志脱敏正确（URL/密钥不进日志）。
- **C202-7** 提供真实环境验证步骤与限制说明（见 §3 边界断言）。

### 2.8 RND-206（统一查看器与富媒体前端，真实浏览器 Playwright）
- **C206-1** 图片浮层放大 + 上一张/下一张。
- **C206-2** 视频消息不再显示「不支持视频消息」，浏览器内播放或具体格式 fallback。
- **C206-3** chatrecord 可展开看真实子消息（非通用占位/原始 JSON）。
- **C206-4** link 安全可读卡片且可打开。
- **C206-5** emotion 图片/GIF 正常显示查看。
- **C206-6** revoke 据后端数据展示原内容与撤回状态，或明确说明原消息不可用。
- **C206-7** viewer 关闭后保留原会话与滚动状态。
- **C206-8** 加载失败/媒体缺失/权限不足/未知类型均有清晰 fallback；不暴露敏感 URL/密钥/本地路径。
- **C206-9** 中英文及现有 i18n 文案一致（`assets/i18n.js` zh-CN/zh-TW/en 键一致）。
- **C206-10** 端到端浏览器验收覆盖 image/video/chatrecord/link/emotion/revoke。

### 2.9 RND-210（官方类型错配修复 + 名片）
- **C210-1** `meetingvoicecall`/`voipdocshare` 不再归为未知类型（`resolve`/`describe_message_type` 命中 audio_archive/audio_doc 定义）。
- **C210-2** 音频通话存档可播放或明确可操作降级、关键元数据（voiceid/endtime）不丢失。
- **C210-3** 名片不再显示通用不可用占位，至少展示企业名称 + 联系人标识；联系人数据可用时名称与手机端一致；无法还原的头像等字段有明确降级规则。
- **C210-4** 回归测试覆盖上述官方原始类型及名片渲染。

---

## 3. RND-202 特殊边界（验收 agent 必须断言）

- **实现层（C202-1..C202-6）PASS** 仅证明「代码与 fixture 已实现」。
- **Production Verified 层**：只有当**真实企业版权限 + 真实通话数据**完成验证时才可标；**无真实环境 → 强制结论为「已实现（代码+fixture）」，不得写 Production Verified**。
- 验收报告 §6 的 RND-202 行必须显式标注二者之一，并附「真实环境验证步骤与限制说明」是否齐备。

---

## 4. 测试方法

- **Backend**：`cd backend` 后跑
  - `python -m pytest tests/test_message_type_registry_core.py tests/test_search_api.py tests/test_search_page_js_syntax.py tests/test_http_contract.py -q`（基线，应全绿）
  - 针对未覆盖子任务**自行补最小验收测试**（如 `test_rnd_202_audio_archive.py`、`test_rnd_231_isolation.py`、`test_rnd_197_structured.py` 等）再判定；若开发 agent 已交付则直接跑其用例。
- **前端 / 集成**：用项目既有 Playwright 脚本做真实浏览器 smoke，覆盖 C206-*（必要时编写针对性 Playwright 步骤或最小脚本）。
- **收口**：跑 `make verify` 确认 lint-diff + typecheck + build + 全量 pytest 全绿。

---

## 5. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过 tenant isolation 做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。
- RND-202 无真实企业版环境时，**绝不**在结论中写 Production Verified。

---

## 6. 输出格式（必须结构化）

```
## RND-195 验收报告
整体结论（服务版 8 子任务）：PASS / FAIL / BLOCKED
整体结论（含 archive 全量 / 父任务 RND-195）：PASS / FAIL / BLOCKED / 部分（RND-202 独立未完）
环境：分支 <x> · 是否含 RND-202 实现：是/否 · make verify：通过/失败

| 子任务 | 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|--------|------|--------|------|---------------------------|
| RND-196 | C196-1 | 类型覆盖 | PASS | ... |
| ...    | ...  | ...    | FAIL | 复现：<步骤>；期望：<X>；实际：<Y> |
| RND-202 | C202-7 | 验证边界 | 已实现(代码+fixture) / Production Verified | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### RND-202 边界确认
- 实现层：PASS/FAIL
- Production Verified：是/否（无真实企业版环境 → 必须为否）
- 真实环境验证步骤与限制说明：齐备/缺失

### 边界与已知限制
- <观察到的限制，如 audio_archive 真实播放需企业版权限>

### 结论与建议
- 服务版完整支持：可认定完成 / 需返工（列出必须修的项）
- 父任务 RND-195：可标 Done（需 RND-202 也 PASS）/ 维持 Todo（RND-202 独立推进中）
- 阻塞项（如有）：<…>
```

- 若某条无论如何无法复现（如环境导致 C206-* 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给可复现证据。
- 最终把该报告作为 Linear RND-195 评论贴出（状态保持 Todo，交还用户 Haisu 决策合并/完成）。
