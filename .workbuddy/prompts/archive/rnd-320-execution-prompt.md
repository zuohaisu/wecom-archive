# RND-320 开发 agent 执行提示词 —— 聚合聊天记录折叠态不渲染媒体（对齐企微客户端）

> 面向开发 agent（单人端到端实现 RND-320）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

把审阅控制台里「聚合聊天记录（chatrecord）」消息的**折叠态**改为企微客户端行为：折叠时 N 条全部只显示「发送人 + [消息种类] + 时间 + 文本摘要」，**不渲染任何 `<img>/<video>/<audio>` 媒体、不发任何媒体网络请求**；点击「展开全部」后才真正渲染各子项并 hydrate 媒体。嵌套 chatrecord 卡片（弹层入口）与 mixed 内联展示两条路径**保持不变**。

## 二、精确落点 / 根因（已定位，附 文件:行号:函数）

### 根因 A — 折叠态直接把子项完整渲染（含媒体占位）
- `backend/app/web/static/console/message-renderers.js:954` `CHATRECORD_PREVIEW_COUNT=2`
- `:955-980` `renderChatrecordMessage()`：前 `previewCount` 条用 `renderCompositeNode(items[i],1)` **完整渲染**（媒体占位 `data-rnd206-access-url` 随之生成），其余塞进 `#cr-rows-{msgid}`（`style="display:none"`）但同样**完整渲染**。
- `backend/app/web/static/console/timeline.js:271` `renderTimeline()` 末尾调用 `hydrateRichMedia(body)` → `hydrateRichMedia`（`:588`）对 `body` 内**所有** `[data-rnd206-access-url]` 发起请求，**不论是否隐藏**。因此即便后 N 条 `display:none`，其媒体请求仍会发出（性能验收点）。

### 根因 B — 折叠态没有「只显示种类、不渲染媒体」的行渲染器
- 当前没有 placeholder-row 渲染路径；`.chatrecord-row*` 样式（`backend/app/web/templates/review_console.html:265-275`）是**死代码**（已核实：`renderChatrecordMessage` 输出 `.chatrecord-card` / `.v-chatrecord-node` / `.composite-node`，从不引用 `.chatrecord-row`）——正好启用。
- [消息种类] 标签无法用 `resolvePlaceholder()` 稳定取得（见第三节「陷阱」）。

### 根因 C — 刷新/重建后状态
- 当前 `renderChatrecordMessage` 每次调用都从 `previewCount` 重新渲染，折叠态是「真折叠」；新实现须保证默认就是折叠态，且**不引入全局展开态变量**（否则刷新会「闪一下展开」）。

## 三、实现（GREEN，最小变更）

### 路 A — 折叠态只渲染占位行（核心）
1. 删除/归零 `CHATRECORD_PREVIEW_COUNT`（改为 `0`，或直接移除「前 N 条完整渲染」分支）。
2. 新增 `renderChatrecordRowPlaceholder(item)`（与 `renderChatrecordMessage` 同文件）：
   - `sender = item.sender_name || item.sender`
   - `time = fmtTime(item.timestamp)`
   - `[kind]` = `chatrecordChildKindLabel(item)`（见路 B）
   - 文本摘要：仅当 `item.type==='text'` 时取 `item.text`（截断，如前 60 字）；媒体类型不显示文本。
   - **绝不**输出 `<img>/<video>/<audio>`，**绝不**带 `data-rnd206-access-url`。
   - 用已启用的 `.chatrecord-row*` 类（`.chatrecord-row` / `-meta` / `-sender` / `-time` / `-kind` / `-text`）包裹，对齐现有样式。
3. 改写 `renderChatrecordMessage(m)`：
   - 始终输出 `.chatrecord-card` 外壳（标题 `chatrecord.title`、计数 `chatrecord.itemsSuffix`、`viewInViewer` 链接 `data-node` —— **这些保持不动**，否则回归测试失败）。
   - `.chatrecord-card-rows` 内**全部 N 条**渲染为 `renderChatrecordRowPlaceholder` 占位行（默认折叠态）。
   - 在 `.chatrecord-card-rows` 容器上加 `id="cr-rows-{msgid}"` 且**默认可见**，并加 `data-items='<JSON.stringify({fields:fields,children:items})>'`（沿用现有 `data-node` 做法）以支撑懒展开。
   - 始终渲染 toggle 按钮（标签逻辑见路 B），不再依赖 `hasMore`。
4. 改写 `toggleChatrecordRows(msgid, btn)`：
   - 从 `#cr-rows-{msgid}` 读 `data-items` → `JSON.parse` 得 `items`。
   - 展开：容器 `innerHTML = items.map(i => '<div class="v-chatrecord-node">'+renderCompositeNode(i,1)+'</div>').join('')`，随后 `hydrateRichMedia(container)`（**局部** hydrate，只对已展开子树）。按钮文案 → `I18N.t('chatrecord.collapse')`。
   - 收起：容器 `innerHTML` 还原为 N 条 `renderChatrecordRowPlaceholder`（无媒体）。按钮文案 → `I18N.t('chatrecord.expandAll')`。
   - **不要**用全局变量持久化展开态；每次 `renderTimeline` 重建都应是折叠态（满足「刷新不闪展开」）。

### 路 B — [消息种类] 标签（复用 MessageTypeRegistry，不要再造一套）
```js
function chatrecordChildKindLabel(item){
  var t = item && item.type;
  if(t === 'text') return null;            // 文本走摘要，不显示括号种类
  var entry = MessageTypeRegistry.resolve(t);   // 注意：用 resolve，不是 resolvePlaceholder
  if(entry && entry.placeholderKey) return I18N.t(entry.placeholderKey);
  // 结构化类型（link/location/card/mixed/chatrecord）复用注册表自有的 messageType.* 翻译键
  if(t && MessageTypeRegistry.resolve(t)) return I18N.t('messageType.'+t);
  return I18N.t('placeholder.unsupported');
}
```
**陷阱（已核实，必读）**：
- `resolvePlaceholder()` 只匹配 `category==='placeholder'` 的条目；`image/voice/video/file/emotion` 经 RND-206 升级后是 `category:'media'`，所以**必须用 `resolve().placeholderKey`，不能用 `resolvePlaceholder()`** 取媒体标签。
- 当前 i18n **只有** `placeholder.video/voice/file/link/emotion`，**缺 `placeholder.image`**。须补齐：在 `backend/app/assets/i18n.js` 的 **三个** locale 块（zh-CN ≈ L118-336 / zh-TW ≈ L489-696 / en ≈ L851-1060）各加一行 `"placeholder.image": "图片" / "圖片" / "Image"`。
- `_KNOWN_PLACEHOLDER_I18N_KEYS`（`backend/app/routers/web.py:23-24`）是**正则扫描 i18n.js 自动生成**的，加了 `placeholder.image` 后自动纳入，**无需手改 web.py**。补完后 `resolve('image').placeholderKey` 即返回 `placeholder.image`。
- 验收要求 zh / en 两套（现有 zh-TW 也一并同步，保持三语一致）。

### 路 C — 保持不变的路径（勿动）
- `renderChatrecordCard()`（`:863`，嵌套 chatrecord 弹层入口按钮）→ 保持。
- `renderMixedMessage()`（`:929`，mixed 内联）→ 保持。
- `renderCompositeNode()`（`:872`，展开态单行渲染）→ 复用，不改语义。
- `hydrateRichMedia()`（`:588`）→ 不改；只在展开时**对局部子树**调用。

## 四、阶段一：复现 + 测量（RED）
1. 起服务：`cd backend && make dev`（或 uvicorn），浏览器打开任一含 chatrecord 消息的会话。
2. 复现：找到一条含 ≥3 条（含图片/语音）子项的 chatrecord。当前折叠态前 2 条直接显示缩略图/播放器；点开 DevTools → Network，滚动到该消息，**记录发出的媒体/thumbnail 请求数（记为 RED_N）**。
3. 死代码证据：`grep -n "chatrecord-row" backend/app/web/static/console/message-renderers.js` → 预期 0 命中。

## 五、阶段三：验证（GREEN + 回归）
1. 功能：重复阶段一，折叠态下 DevTools Network 对该 chatrecord **0 媒体请求**；点「展开全部」后才出现缩略图/播放器请求（GREEN 仅在展开时有请求）。
2. 截图自测：折叠态每条 = 发送人 + [图片]/[语音]/… + 时间；文本条显示摘要。
3. 回归：`make verify` 全绿。重点套件：
   - `tests/test_rnd_206_rich_media.py`（含 `test_chatrecord_renders_summary_card_with_title_and_count` —— 必须仍含 `chatrecord-card` 与计数）
   - `tests/test_rnd_206_qa_fixes.py`（含 `test_chatrecord_viewer_mount_hydrates_nested_media` —— 测的是弹层 `openChatrecordViewer`，不受内联折叠改动影响）
   - `tests/test_message_type_registry_core.py`、`tests/test_i18n_foundation.py`
   - RND-204 / RND-201 增量刷新相关：`tests/test_rnd_204_incremental_refresh.py`
4. i18n：新增 key 至少 zh/en 两套；`test_i18n_foundation.py::test_registry_contains_zh_cn_zh_tw_and_en` 仍绿。

## 六、硬约束（违反即判失败）
- 不渲染媒体即「零媒体 DOM + 零媒体请求」；折叠态不得含任何 `<img>/<video>/<audio>` / `data-rnd206-access-url`。
- 不改 URL / API / 业务语义 / 租户隔离 / i18n 既有 key 含义；只**新增** `placeholder.image` 等 key。
- 不动 `renderChatrecordCard` / `renderMixedMessage` / `renderCompositeNode` 语义；嵌套与 mixed 行为零变化。
- 不引入新依赖；不改后端 Python（纯前端 JS + i18n 资源）。
- 不执行 git commit / push。

## 七、收尾（交付物）
向用户交付：RED/GREEN 媒体请求数对比、改动文件清单（`git diff --stat`：预期仅 `message-renderers.js`、`review_console.html`（若加样式）、`i18n.js`）、`make verify` 日志、未提交声明。

---

## 附：本票三个开放问题的建议默认值（来自原 ticket，已定为推荐项，勿再阻塞）
1. [消息种类] 文案：**复用** `MessageTypeRegistry` 的 `placeholderKey` / `messageType.*` i18n key（不新增独立短 key 集合）。
2. 折叠行视觉：**启用并沿用** 仓库现有 `.chatrecord-row*` 样式（比企微极简单行更多留白）。
3. 展开/收起切换：**不加过渡动画**，保持 `display` 即时切换。
