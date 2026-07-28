# Planner 建票提示词 — 聚合聊天记录折叠态不渲染媒体（对齐企微客户端）

**目标读者**：Planner agent
**任务**：根据本提示词在 Linear 上建一张工单（按要求填写字段并写好验收口径），**不进入开发**。开发与 QA 由用户后续分发。

---

## 1. 背景与产品动机

用户在审阅台（review_console）查看"聚合聊天记录"消息（`normalized_type=chatrecord`，企微常见场景：多人合并转发 / 群聊记录导出）时，发现**折叠态**会直接把前几条的图片/语音完整渲染出来——这与企微官方客户端的行为不一致：

- **企微客户端**（参考截图1）：折叠时每一条只显示「发送人 + `[消息种类]`（如 `[图片]`/`[语音]`/`[链接]`）+ 文本片段摘要」，**不渲染媒体**。
- **当前实现**（参考截图2 + 代码）：折叠态把 `CHATRECORD_PREVIEW_COUNT=2`（前 2 条）直接完整渲染，后 N 条折叠在 `#cr-rows-{msgid}` 容器里，导致滚动到这条消息时图片/语音会立刻被 hydrate，浪费带宽且视觉上与企微不一致。

**期望行为**：折叠时**全部 5 条**（或任意 N 条）都只显示「发送人 + `[消息种类]` + 时间 + 文本摘要」；点击「展开全部」后才把媒体完整渲染（含图片懒加载/语音播放器等）。

## 2. 关键事实（Pl 建票前请先核对一遍）

- **现有渲染入口**：`backend/app/web/static/console/message-renderers.js:955-980` `renderChatrecordMessage`（顶层 chatrecord 消息，**就是 bug 所在**）。
- **折叠切换函数**：`message-renderers.js:981-987` `toggleChatrecordRows`，通过 `display:none/block` 切换 `#cr-rows-{msgid}` 容器。
- **预览常量**：`message-renderers.js:954` `var CHATRECORD_PREVIEW_COUNT=2;` —— 这是当前"前 2 条直接渲染"行为的根因。
- **媒体 hydrate 总入口**：`backend/app/web/static/console/timeline.js:271` `hydrateRichMedia(body)`，整页只调用一次。折叠再展开后要保证**未 hydrate 的子节点能被再次 hydrate**。
- **媒体占位渲染器**：`message-renderers.js` `richMediaPlaceholder()`（约 739-826 行） + `swapRichMediaPlaceholder` 链。
- **占位样式**：`backend/app/web/templates/review_console.html:198-201` `.media-placeholder` 系列。
- **已存在但疑似未启用的折叠行样式**：`review_console.html:266-275` `.chatrecord-row / .chatrecord-row-avatar / .chatrecord-row-sender / .chatrecord-row-time / .chatrecord-row-kind / .chatrecord-row-text / .chatrecord-row-nested` —— 看起来就是为这种"折叠行"准备的样式，**请在工单里标注让 dev 核实是否可直接复用**，但不要假定未使用就是死代码（可能在其他路径用了）。
- **类型→占位文案**：`MessageTypeRegistry.resolvePlaceholder(normalized_type).placeholderKey`（`message-renderers.js:37`），已有 `timeline.imageMessage / .voiceMessage / .fileMessage / .linkMessage / .videoMessage` 等 key 可直接拿来做 `[消息种类]` 显示。
- **i18n key**：`backend/app/i18n.js`（具体 key 在 `chatrecord.*` 命名空间下）。新增/复用 key 一律在工单里列清楚。
- **必须保留的现有行为**（回归点）：
  1. 嵌套在另一个 composite 里的 chatrecord/mixed 节点仍走 `renderChatrecordCard`（弹层入口），**不要改这条路径**（`message-renderers.js:863-871`，RND-206 QA fix #2 明确要求一条 dispatch 路径）。
  2. `renderMixedMessage`（`message-renderers.js:929-939`）是 mixed 类型内联展示，不折叠，**不要改**。
  3. 「在弹层中查看完整记录」按钮（`renderChatrecordMessage` 最后那个 `.chatrecord-viewer-link`）继续保留。
  4. 自动刷新（`timeline.js:mergeMessagesByMsgid`）后折叠态必须保持一致——即刷新进来默认就是新折叠态。
- **数据模型/接口**：无后端改动，纯前端 JS/HTML/CSS。
- **测试现状**：`backend/tests/test_rnd_206_*.py` 等是相邻 RND-206 测试，本工单若需要新测试文件，请放在 `backend/tests/test_chatrecord_collapsed_placeholder.py` 之类的命名（dev 决定）。

## 3. 建一张工单即可

**不要**拆成多张。这是个聚焦的 UI 行为修复 + 一组回归测试，独立可发。如果实际复杂度需要拆，请在工单里加 todo（Linear sub-issue）而不是直接建多张主票。

### 3.1 Linear 字段

- **Team**: Builder (RND)
- **Project**: `365企微会话存档` (`cfe726ec-810c-4848-8455-9b3607bf1fc1`)
- **Title**（建议）：`RND-XXX 聚合聊天记录折叠态不渲染媒体（对齐企微）`
- **Type**: Bug
- **Priority**: Medium（功能可用但视觉/性能不合预期；非阻塞 P0；非 P3 装饰性）
- **Status**: Todo（按用户规则，新 issue 默认 Backlog；如需立刻派发，状态显式设为 `Todo`）
- **Labels**: 建议 `frontend` / `ui` / `console` 至少一个
- **Description**：见 §3.2

### 3.2 工单 Description 模板（请按此结构填）

```
## 现象
在审阅台（review_console）打开任意一个 chatrecord 类型的消息（"群聊的聊天记录"），折叠态直接把前 2 条的图片/语音完整渲染。
期望：折叠时全部 N 条都只显示"发送人 + [消息种类] + 时间 + 文本摘要"，不渲染媒体；点击"展开全部"后才完整渲染。

## 截图/参考
- 企微客户端行为（期望）：@image#1
- 当前网页端行为（修复目标）：@image#2
- 关键代码落点：
  - `backend/app/web/static/console/message-renderers.js:954` `CHATRECORD_PREVIEW_COUNT=2`
  - `backend/app/web/static/console/message-renderers.js:955-980` `renderChatrecordMessage`
  - `backend/app/web/static/console/message-renderers.js:981-987` `toggleChatrecordRows`
  - `backend/app/web/static/console/timeline.js:271` `hydrateRichMedia(body)`

## 期望行为
1. 折叠态：5 条（任意 N 条）都只渲染"轻量占位行"——发送人头像/名字 + `[消息种类]`（用 `MessageTypeRegistry.resolvePlaceholder().placeholderKey` 的 i18n 文案，如 `[图片]` / `[语音]` / `[文件]` / `[链接]`） + 时间 + 可选的文本片段摘要。**不发起任何 `<img>`/`<video>`/`<audio>` 媒体请求**。
2. 展开态：点击"展开全部"后才真正调用 `renderCompositeNode` 完整渲染各子项，并触发 `hydrateRichMedia`（或等价的局部 hydrate）以正确显示图片/语音播放器。
3. 标题栏的「N 条记录」+「展开全部/收起」按钮行为保持一致。
4. 「在弹层中查看完整记录」按钮保持。
5. 嵌套 chatrecord 卡片（弹层入口）和 mixed 内联展示两条路径**不改动**。
6. 自动刷新 / 切回该会话 / 时间线重建后，折叠态必须默认就是新折叠态（不能因为刷新而"展开"）。

## 技术方向建议（不强制，供 dev 参考）
- 移除 `CHATRECORD_PREVIEW_COUNT` 概念（或保留但改为 0），让所有 N 条默认进折叠容器。
- 新增一个 `renderChatrecordRowPlaceholder(item)` 函数，对每个 item 只输出 `sender + [kind] + time + text excerpt`（不要 hydrate 媒体）。
- 切换时如果要走 lazy 渲染：折叠态只输出 placeholder + 原始 item 的 JSON 缓存（`data-node='...'`，与现有 `chatrecord-card` 的做法保持一致），点击"展开"时才把 JSON 反序列化 + 走 `renderCompositeNode` + 调用局部 hydrate。
- 复用现有 `.chatrecord-row*` 样式（请核实是否当前未被使用，若是死代码则正好启用）；如需新增样式，集中放在 `review_console.html` 的 chatrecord 样式段附近。
- 复用现有 `MessageTypeRegistry.placeholderKey` 来生成 `[消息种类]` 文案，**不要再造一套**。

## 验收口径（QA 必查项）
- [ ] 折叠态下，5 条 chatrecord 每条都不出现 `<img>`/`<video>`/`<audio>` 标签，且 DevTools Network 面板对每条媒体不发出请求（包括 thumbnail）。
- [ ] 折叠态下，5 条每条都显示发送人 + `[消息种类]` + 时间；纯文本类型显示文本片段摘要。
- [ ] 点击「展开全部」后，5 条全部完整渲染；图片正确显示缩略图或原图，语音出现播放器。
- [ ] 切换「展开/收起」按钮文案正确（`chatrecord.expandAll` / `chatrecord.collapse`）。
- [ ] 「在弹层中查看完整记录」按钮仍可点击进入弹层 Viewer。
- [ ] 嵌套 chatrecord 卡片（弹层入口）和 mixed 内联展示行为不变（手动跑相邻 RND-206 测试或类似用例）。
- [ ] 自动刷新该会话后，新加载的 chatrecord 默认就是折叠态（不会因为 hydrate 时序问题而"闪一下展开"）。
- [ ] 现有 RND-206 / RND-204 / RND-201 等相邻 ticket 的回归测试全绿。
- [ ] i18n：新增/复用的 key 在 `i18n.js` 中至少有 zh / en 两套；占位 `[消息种类]` 文案在英文版合理翻译（不要直译成 `[Image]` 这种仍可，但优先用 i18n 系统已有翻译）。
- [ ] 性能：含 5 条图片的 chatrecord，滚动到该消息时不再触发 N 个 thumbnail 预加载请求。

## 范围外（明确不做）
- 不改后端接口 / 数据模型。
- 不动嵌套 chatrecord 卡片（`renderChatrecordCard`）和 mixed（`renderMixedMessage`）。
- 不做"按图片/语音类型自适应占位 UI"这类视觉重做——只做"折叠时不渲染媒体 + 显示种类"。
- 不引入新依赖。

## 工程纪律（dev/QA 必读）
- 严禁 git commit / push（按用户硬规则，一律用户本人提交）。
- 实现后必须交独立 QA agent 验收。
- 不要改 `DEV_AGENT_RULES.md` / `docs/AGENTS.md` / 架构边界测试。
- 如需新增测试文件，命名 `backend/tests/test_chatrecord_collapsed_*.py`（按现有约定）。
- 验收时建议在 `localhost` 起服务后真实截图，**不要只贴 console log 就算过**。
```

### 3.3 不要建什么

- **不要**建"重做 chatrecord 视觉"/"统一聊天记录卡片设计"等大票——本工单只修这一个 bug。
- **不要**建"性能优化：折叠时不加载媒体"独立票——这就是本工单的范围。
- **不要**和 F0 账号体系 / 设计稿 15 页那些 epic 混在一起——这是独立 Bug 票。

### 3.4 待用户拍板（写在工单 description 末尾，但**不阻塞**建票）

1. `[消息种类]` 文案是直接复用 `MessageTypeRegistry` 的 i18n key（推荐），还是新增显式更短的 key（如 `chatrecord.kindImage`）？
2. 折叠行的视觉是否沿用企微的极简单行样式（发送人+种类+摘要一行），还是给到更多留白（与本仓库 `.chatrecord-row*` 样式对齐）？建议后者——可复用现有样式、视觉更一致。
3. 切换"展开/收起"时，是否要加过渡动画？建议不加，保持现有 `display:none/block` 即时切换即可。

## 4. 产出要求

- 在 Linear 建好工单后，**给用户回复**：
  1. 工单 ID（如 `RND-XXX`）
  2. 工单 URL
  3. 三句话以内的"我建了一张什么票/为什么这个优先级/谁被 block"
- 不要在本提示词覆盖范围外做任何事（如修改代码、改其它工单状态、写新文档）。

---

## 附：用户原始请求（保留上下文）

> 有一个展示层的 bug：
> @image#1:mac_1785224918349.jpg 我截图里是企业微信的聚合聊天记录的折叠后的模样
> @image#2:mac_1785224970084.png 另一张截图是会话存档网页端，聚合聊天记录折叠起来的样子
> 我想要做成和企业微信一样，在折叠时不显示图片，只显示[消息种类]，展开之后再显示图片
> 请你建个票跟进这个UI修复
