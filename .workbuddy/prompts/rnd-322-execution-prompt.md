# RND-322 开发 agent 执行提示词 —— 登录后左下角语言按钮下拉不可见（侧栏 overflow 裁剪）

> 面向开发 agent（单人端到端修复 RND-322）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
> 关联 Linear issue：**RND-322**（状态应为 Todo）。

## 一、任务（一句话）

修复审阅控制台左下角「语言」按钮点击后下拉菜单不可见的问题，使语言选择下拉在任意视口高度下都**完整可见、可点击、即时切换文案并持久化**，且零回归。

## 二、现象与背景

- 登录进入审阅控制台（`/admin/conversations`）后，点击左下角「语言」按钮（`#btn-lang-toggle`，位于左侧导航底部 `.side-nav-user` 区域），**没有任何下拉出现**，用户感知为「按钮失效」。
- 经预先排查（见第五节已排除项），JS 绑定与 i18n 全局均正常，菜单 `display` 实际会被置为 `block`——问题极可能是**视觉被裁剪**。
- 这是一个纯前端（CSS + 少量 JS 行为）bug，不应触碰后端、i18n 语义或业务逻辑。

## 三、精确落点（已扫描，2026-07-28 基线）

### 关键文件
1. **`backend/app/web/templates/review_console.html`**
   - L20 `.side-nav{...overflow:hidden}` —— 侧栏裁掉溢出内容（**首要嫌疑**）。
   - L65-71 `.lang-switch` / `.btn-lang` / `.lang-menu` 样式；L68 `.lang-menu{position:absolute;top:135%;right:0;...z-index:50}` —— 向下展开约 90px，超侧栏底部边界。
   - L374-377 结构：`#lang-switch` > `#btn-lang-toggle`（onclick=`toggleLangMenu()`） + `#lang-menu`（`display:none`）。
   - L344 `__I18N_SCRIPT__` 注入 i18n.js（早于所有 console 脚本）。
   - L468-475 脚本加载顺序：console-state.js → api-client.js → conversation-list.js → timeline.js → message-renderers.js → media-viewer.js → refresh.js → **console-entry.js**（最后）。
2. **`backend/app/web/static/console/console-entry.js`**
   - L7-17 `renderLangMenu()`：读取 `I18N.availableLocales()` 生成选项，`onclick="selectLocale(...)"`，使用 `esc()`。
   - L18-24 `toggleLangMenu()`：切换 `#lang-menu` 的 `display`。
   - L25-30 `selectLocale(code)`：`I18N.setLocale` + 关闭菜单 + `applyLocale()`。
   - L31-90 `applyLocale()`：切换 locale 后刷新**静态 `data-i18n` + 全部动态渲染内容**（实体/会话/时间线表头、搜索框占位、scope 标签、详情面板、focus banner 等）—— 修复后必须保证这些仍被正确刷新。
   - L91-95 document click 监听：点击落在 `#lang-switch` 内时不关菜单（逻辑**正确，勿动**）。
3. **`backend/app/web/static/console/console-state.js`**
   - L44 `function esc(s){...}` —— `esc` 定义处（确认：`renderLangMenu` 不会因 `esc` 未定义而抛错）。
4. **`backend/app/assets/i18n.js`**（i18n 单一真源）
   - `LocaleRegistry`：`zh-CN` / `zh-TW` / `en` 三语；`STORAGE_KEY="wecom_admin_locale"`；`setLocale` 写 `localStorage` 并广播 `changeListeners`。**修改下拉可见性时勿改此文件语义**。
5. **`backend/app/i18n_assets.py`**
   - 将 i18n.js 以 `<script>` 标签嵌入各 admin 页面（login/console/diagnostics/search）。仅说明，本票不改。

## 四、阶段一：基线（RED，先复现再动手）

1. 启动本地服务并登录，进入审阅控制台。
2. 打开 DevTools：
   - 点击「语言」按钮，观察 Elements 面板中 `#lang-menu` 的 `display` 是否在点击后置为 `block`（预期：是）。
   - 检查 `#lang-menu` 的 bounding box：其底部是否超出 `.side-nav` / 视口底缘，且被 `overflow:hidden` 裁剪（预期：是 → 印证根因）。
   - 记录：当前视口高度下，菜单可见部分高度 vs 实际高度。
3. `make verify`（或既有 `backend/tests/` 中 console 相关测试）跑一遍，记录**全绿基线**，作为回归参照。

> 若复现结果指向**非 CSS 裁剪**（例如 `toggleLangMenu` 实际未被调用或抛错），立即停下，在 issue 评论中回报真实根因，不要按本 brief 的 CSS 方案硬改。

## 五、阶段二：实现（GREEN，最小变更）

目标：让下拉在任意视口高度下完整可见、可交互。从以下方案选**最小且稳健**的一个（不要大面积重写侧栏布局）：

- **方案 A（推荐，首选）**：将 `.lang-menu` 改为**向上展开**——`bottom:100%; right:0; top:auto`。因为按钮在侧栏最底部，向上展开必然落在可视区内，彻底规避底部裁剪。
- **方案 B**：保持向下展开，但给 `.lang-switch` 或 `.side-nav-user` 设 `overflow:visible`，并确认不会破坏侧栏 `.side-nav-scroll` 的滚动（侧栏滚动来自 `.side-nav-scroll` 自身的 `overflow-y:auto`，局部放开 `.side-nav-user` 一般不会影响）。
- **方案 C**：将菜单渲染到更高层级 / portal。代价最大，除非 A/B 不可行否则不选。

实现约束：
- 只改 `review_console.html` 的 CSS（及必要时极小的 JS 行为）——**不要改动** `toggleLangMenu` / `renderLangMenu` / `applyLocale` 的核心逻辑、document click 监听、i18n 注册表。
- 修复后手动确认：点击按钮→下拉完整可见；点选项→全站文案即时切换（含动态内容，对照 `console-entry.js:31-90`）；点外部→关闭；刷新→locale 从 `localStorage` 恢复。

## 六、阶段三：验证

1. 浏览器手测（阶段二末列的四项）全部通过。
2. **新增/加固 E2E 测试**：在 `backend/tests/` 中参照既有 console E2E 模式（如 `test_archive_console_v2.py`、`test_archive_console_v2_qa_fixes.py` 的 Playwright 用法）新增一个用例：
   - 登录 → 点 `#btn-lang-toggle` → 断言 `#lang-menu` **可见且未被裁剪**（例如 `is_visible()` 为 true，且 bounding box 底部 ≤ 视口高度）。
   - 点某个非当前语言选项 → 断言 `<html lang>` 改变、某已知 `data-i18n` 文案已切换、动态内容（如搜索框占位）也切换。
   - 重新加载页面 → 断言 locale 从 `localStorage` 恢复（`<html lang>` 与上次选择一致）。
   - 点下拉外部 → 断言 `#lang-menu` 收起。
   - 选择器优先用语义/`data-testid`，避免脆弱的 CSS class 链；如现有模板无 `data-testid`，可最小补充（属本票合理范围）。
3. `make verify` 全绿（含新测试）。
4. `git diff --stat` 确认改动仅限：`review_console.html`（CSS 为主，必要时加 data-testid）+ 新增/修改的测试文件。

## 七、硬约束（违反即判失败）

- 不改动后端 `.py` 业务逻辑、i18n 注册表语义、locale 持久化逻辑。
- 不破坏：外部点击关闭、locale 切换后静态+动态内容整体刷新、刷新后从 `localStorage` 恢复。
- 不做大面积布局重构或引入新依赖（框架/库）。
- 不扩大范围（只修语言下拉可见性，不顺便改其他 UI）。
- 不执行 git commit / push。

## 八、收尾（交付物）

向用户交付：
- 根因确认结论（RED 阶段实测证据：菜单 display 状态、bounding box 是否被裁剪）。
- 选用方案（A/B/C）及变更文件清单（`git diff --stat`）。
- 新增 E2E 测试路径与运行结果。
- `make verify` 日志（全绿）。
- 未提交声明。
