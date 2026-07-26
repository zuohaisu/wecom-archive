# RND-210 执行提示词（单人端到端：复现 → 修复 → 验证）

> 用途：粘贴给单一智能体（hy3 / DeepSeek V4 / Claude Code 皆可），由其单人端到端跑完 RND-210。
> 工单原文：`RND-210「修复会话存档官方消息类型错配，并实现名片内容一致性渲染」`，P2，`type: fix`，父任务 RND-195，关联 RND-197（基础结构化消息解析）、RND-202（音频通话存档）。
> **时序硬约束（务必先读）**：RND-226 与 RND-210 都改 `backend/app/routers/conversations.py` 的重叠区（~1777–1822，即 `classify_media`/`describe_message_type` 每消息调用 + nested-media 检查）。本任务**必须在 RND-226 已合并进 main 之后，从最新 main 拉分支开工**。严禁在 RND-226 的 pre-merge 分支上启动本任务，否则必冲突。

---

## 0. 任务与来源
- Linear 工单：**RND-210**。
- 目标（两条主线）：
  1. **类型错配修复**：真实官方载荷 `meetingvoicecall`（音频通话存档）与 `voip_doc_share` / `voipdocshare`（音频共享文档）当前被项目以 `audio_archive` / `audio_doc` 做精确匹配，导致官方载荷落入 **UNKNOWN 回退**——媒体与关键元数据无法完整处理、前端显示未知类型占位。
  2. **名片（card）内容一致性渲染**：`card` 当前只显示通用"不可用"占位，未展示企业名称（corpname）与联系人标识；需在联系人数据可用时显示联系人名称，且协议未提供的字段（如头像、历史快照）不得伪造为手机端原样内容。
- 范围：注册官方 msgtype 别名 + 提取字段 + 前端名片渲染 + 回归测试。
- 非目标（严禁，避免 gold-plate）：
  - **不重命名** `audio_archive` / `audio_doc` 的 `raw_type` / `normalized_type`（下游 i18n 键 `messageType.audioArchive`、`messageType.audioDoc`、前端 renderer 派发、`classify_media` 分支都依赖这两个名字；用 **alias** 而非改名）。
  - **不实现音频播放**（完整 playback 是 RND-202 范围）。本任务只要求：提取并保留关键元数据（voiceid/endtime 等），并保证降级样式"明确且可操作"——不丢元数据即可。
  - 不触碰 DB 迁移（纯代码 + i18n，无 schema 变更 → 无 alembic migration）。
  - 不混入 RND-226 的 nested-media 实体上下文改动。

## 1. 精确根因（已定位，文件级）

### 1.1 类型错配
- `backend/app/message_type_registry.py`：`_DEFINITIONS` 中 `audio_archive`（~217）与 `audio_doc`（~354）的 `raw_type` 就是这两个内部名，**没有** `meetingvoicecall` / `voip_doc_share` 别名。
- `resolve(msgtype)`（~572）只按 `raw_type` + `aliases` 查表；收到官方 `meetingvoicecall` / `voip_doc_share` 时命中 `FALLBACK_DEFINITION`（`support_status=UNKNOWN`）。
- `backend/app/media_classification.py` 的 `classify_media`（~69）：`definition.support_status == UNKNOWN` → 返回 `("unknown","unknown","unregistered_msgtype")`（~81-84）。于是媒体状态/可访问性全部退化，前端走未知类型占位。
- 修复后（加 alias）这两个官方名会解析到现有 `audio_archive` / `audio_doc` 定义，`classify_media` 自动落入 byte-bearing PARTIAL 分支（~111-127），媒体可访问性恢复——**无需改 `classify_media` 本身**。

### 1.2 字段提取缺失
- `backend/app/structured_message_parser.py`：`audio_archive` / `audio_doc` 与 `card` 当前是 `RAW_PASSTHROUGH`（`parse_structured_content` ~1060-1065 的特判分支），`fields=None`——只保留 raw，不提取任何字段。
- 工单要求 `meetingvoicecall` 提取主媒体 + 关键元数据（voiceid、结束时间、sdkfileid，以及存在时的文档/共享屏幕数据）；`voip_doc_share` 提取文档元数据；`card` 提取 corpname + 联系人标识。
- 模块 docstring 明确警告：此前因本仓库无 card 真实字段佐证，RND-197 故意选 RAW_PASSTHROUGH **避免臆造字段**。本任务现在要求提取 corpname + 联系人标识——落地前**必须先对照企业微信官方文档/既有 fixtures 确认字段名**，只提取协议实际提供的字段，绝不臆造 avatar/快照。

### 1.3 前端名片渲染缺失
- `backend/app/main.py`：`STRUCTURED_CARD_RENDERERS` 映射（~1289-1305）中 `card: renderStructuredFallback`（~1295）；`renderStructuredFallback`（~1026）对 `card` 只输出 `card.generic.unavailable` 的"不可用"占位（仅 `audio_doc` 有特殊文案 `card.audioDoc.playbackUnavailable`）。
- `renderStructuredCard`（~1307）按 `m.normalized_type` 派发；`card` 的 `normalized_type` 为 `"card"`，需新增 `renderCardMessage` 并替换映射中的 `renderStructuredFallback`。

## 2. 执行步骤（严格按顺序：复现 → 修复 → 验证）

### 阶段一：复现（先证明问题，不改实现）
- **不修改实现代码**。在 `backend/tests/` 新增回归测试（建议 `test_rnd_210_msgtype_and_card.py`），断言当前"错误行为"，预期失败（RED）：
  - 断言 A：`resolve("meetingvoicecall").support_status` 当前应为 `UNKNOWN`（先写此断言，预期失败——证明官方类型未被识别）。
  - 断言 B：`classify_media("meetingvoicecall", True)` 当前应为 `("unknown","unknown","unregistered_msgtype")`（预期失败）。
  - 断言 C：构造一条 `card` 消息（payload 含 `corpname` 与联系人标识字段），其 `structured_content.fields` 当前为 `None`（预期当前就是 None，说明未提取）——此条应**通过**（用来锁定现状，修复后应变为非 None）。
  - 断言 D（前端契约）：`STRUCTURED_CARD_RENDERERS["card"]` 当前为 `renderStructuredFallback`（锁定现状）。
- 运行该测试，确认 A/B 失败（RED）、C/D 通过（现状正确）。若 A/B 无论如何都通过（即官方类型已被识别），则给出 `NOT REPRODUCED` 证据（构造了哪些 payload、命中了哪个定义），写进 Linear 评论——**绝不允许假装修好**。

### 阶段二：修复（最小改动，严守范围）

**改动一 · 注册官方别名（`app/message_type_registry.py`）**
- `audio_archive` 定义加 `aliases=("meetingvoicecall",)`。
- `audio_doc` 定义加 `aliases=("voip_doc_share", "voipdocshare")`（工单同时出现两种拼写，两个都加）。
- `_build_registry()`（~545）会在重复 key 时启动即报错——确认新别名不与现有 key 冲突即可。
- 保持 `raw_type` / `normalized_type` 不变；`card` 定义（~247-266）的 `parser_strategy` 由 `RAW_PASSTHROUGH` 改为 `STRUCTURED_FIELDS`（见改动二）。

**改动二 · 提取字段（`app/structured_message_parser.py`）**
- 新增 `parse_meetingvoicecall_message(payload)`：提取 `voiceid`、`endtime`（秒级时间戳，`_safe_int`）、`sdkfileid`（主媒体引用）、以及存在时的文档/共享屏幕元数据（按官方文档字段名，**仅提取协议确实提供的**；不确定时多候选防御式查找，参照 `parse_news_message` 的 image 字段查找风格）。返回 `fields` dict。
- 新增 `parse_voip_doc_share_message(payload)`：提取文档元数据（标题/链接/文档 id 等，依官方文档；禁止臆造）。
- 新增 `parse_card_message(payload)`：提取 `corpname`、联系人标识（如 `userid` / `name` 字段，**严格按官方名片 payload 字段名**）。**绝不提取或伪造头像/历史快照**——这些字段若协议未提供，保持缺失并走降级。
- 将三者在 `_STRUCTURED_FIELD_PARSERS`（~667-681）注册：`meetingvoicecall`、`voip_doc_share`、`card`（注意 `voipdocshare` 不带下划线这个别名走 `resolve()` 即可命中定义，无需单独注册 parser——`parse_structured_content` 用规范 `msgtype` 派发，见下）。
- 修改 `parse_structured_content`（~1014）：把 `audio_doc` 与 `card` 从 `RAW_PASSTHROUGH` 特判分支（~1060-1065）移出，使其与 `meetingvoicecall`/`voip_doc_share` 一起走 `STRUCTURED_FIELDS` 派发（`_STRUCTURED_FIELD_PARSERS.get(msgtype)`）。`audio_archive` 的 `msgtype` 在真实载荷里是 `meetingvoicecall`，靠 alias 解析后 `msgtype` 仍按调用方传入值派发——确认 `parse_structured_content` 的 `msgtype` 参数用的是原始入参（它确实是），所以 `_STRUCTURED_FIELD_PARSERS` 必须以**原始官方名** `meetingvoicecall` / `voip_doc_share` 为键注册（alias 只影响 `resolve()`，不影响 parser dict 的键；调用链：`decrypt` 层用原始 `msgtype` 调 `parse_structured_content`）。
- 所有 parser 保持"纯函数、永不抛异常、坏数据降级为缺字段 + `parse_warnings`"（与现有 `parse_*` 一致）。

**改动三 · 前端名片渲染（`app/main.py` + `app/assets/i18n.js`）**
- 新增 `renderCardMessage(m)`（参照 `renderSwitchCorpCard` ~1251 的"展示 corp_name"模式）：读取 `m.structured_content.fields` 的 `corpname` 与 `contact_name`/`contact_userid`；展示企业名称 + 联系人名称（或标识）；缺字段时降级为可读占位（**不伪造**头像/快照，明确标注"无头像/协议未提供"）。
- 在 `STRUCTURED_CARD_RENDERERS`（~1289-1305）把 `card: renderStructuredFallback` 改为 `card: renderCardMessage`。
- 在 `app/assets/i18n.js` 三语（zh-CN/zh-TW/en）新增键：`messageType.businessCard`（或复用 `card` 标签）、`card.businessCard.corpName`、`card.businessCard.contactName`、`card.businessCard.noAvatar`（降级文案）等。参照现有 `card.audioDoc.playbackUnavailable` / `card.switchCorp.switchedTo` 的键风格与三语位置。
- `renderStructuredFallback`（~1026）保留给 `audio_doc` 的"暂不支持播放"文案与真正的兜底（解析失败的结构化类型）。

**改动四 · （可选，bounded）联系人名称回填**
- 工单："在联系人数据可用时显示联系人名称"。若名片联系人名称只能从 **contacts 表**查得（而非 payload 自带），则在 `conversations.py` 的 structured_content 富化段（~1818-1840 区域，与 RND-226 重叠区——这正是必须 post-226 开工的原因）做一次性、按请求 bounded 的联系人查表回填，gated behind 联系人可用性，缺则降级。
- **决策点**：先确认手机端"联系人名称"能否仅由 payload 字段得到；若必须查 DB，保持查表 bounded（不要 N+1，按请求去重），且不改变结构化字段的隐私边界。若评估成本超本任务范围，**回退为仅展示 payload 提供的 corpname + 联系人标识**，并在 Linear 评论说明 DB 回填需单独评估——切勿 gold-plate。

### 阶段三：验证（不达标不收工）
- 阶段一复现测试（A/B）现在必须**通过（GREEN）**；C/D 断言随修复更新为新期望（fields 非 None、renderer 为 `renderCardMessage`）。
- 新增/扩展回归测试，覆盖工单验收点：
  - `meetingvoicecall` / `voip_doc_share` 经 `resolve()` 不再 UNKNOWN；`classify_media(..., True)` 落入音频 PARTIAL 桶（参照 `tests/test_media_classification.py` 现有写法）。
  - 解析器单测：构造官方样例 payload，断言提取的 `voiceid`/`endtime`/文档字段、card 的 `corpname`+联系人标识（参照 `tests/test_rnd_198_parser.py` 模板）。
  - 前端渲染单测：断言 `renderCardMessage` 对含 corpname 的 fields 输出企业名称与联系人名；缺字段时降级文案（参照 `tests/test_rnd_198_frontend.py`）。
  - 未知类型回退仍正确（别名不应污染 `FALLBACK_DEFINITION`）。
- 运行既有套件确认无回归（先 `cd backend`）：
  - `python -m pytest tests/test_message_type_registry.py tests/test_message_type_registry_core.py tests/test_structured_message_parser.py tests/test_media_classification.py tests/test_decrypt_structured_content.py tests/test_unsupported_message_labels.py -q`
  - **务必跑 `tests/test_message_type_registry_core.py`**：它校验 registry 与前端 JS registry 的一致性，新增别名/条目可能触发该断言。
  - 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
- 浏览器 smoke（项目既有 Playwright 脚本）：确认名片消息现在显示企业名称+联系人，音频类型不再显示未知类型占位。

## 3. 硬性约束（不可违反）
- **不臆造字段**：card 的 avatar / 历史快照、音频的未提供字段，协议未给就保持缺失 + 明确降级，绝不伪造为手机端原样。
- **别名而非改名**：`audio_archive`/`audio_doc` 的 `raw_type`/`normalized_type` 不动，i18n 键 `messageType.audioArchive`/`messageType.audioDoc` 与前端 renderer 派发保持有效。
- **隐私边界**：card parser 不提取 corpid 等敏感标识进入 `fields`（参照 `parse_switch_corp_message` 只取 `corp_name`）；联系人 DB 回填保持 bounded、不扩展序列化面。
- **不改 `classify_media` 语义**（靠 alias 让官方名命中既有音频分支即可）。
- **不改 signed URL / proxy / Cache-Control / nested-media descriptor 行为**（那是 RND-226 的范围）。
- **时序**：从 RND-226 合并后的最新 main 拉分支；若发现 226 尚未合并，**停下等合并**，不要并行改 `conversations.py` 重叠区。
- 不引入后台进程；假设开发服务器已在运行；命令前台运行。
- 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit`/`push`**（需用户显式授权）。

## 4. 收尾动作
- Linear：RND-210 状态保持 `In Progress`（已置）；写一条评论：复现方式 + 修复点（文件/函数级，含新增别名、parser、前端 renderer、i18n 键）+ 测试结果 + `make verify` 结论；若 `NOT REPRODUCED` 或联系人回填被回退，如实说明决策。
- 不要自动合入/提交，保留给用户（Haisu）人工 merge 关卡。
- 若评估发现音频"可播放"需要超出本任务（触及 RND-202 playback），在评论中显式标注边界，不私自扩展。
