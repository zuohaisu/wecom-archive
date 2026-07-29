[Goal check] This work advances 开发（Development） by 将 .lang-menu 改为左对齐并实测三档视口下菜单左边框不溢出，使语言切换弹层在窄屏可用。

# RND-324 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-324「语言切换弹层在窄屏下向左溢出浏览器可视范围」｜Linear team `Builder`
- 优先级：Medium｜风险等级：**R1**｜标签：`area: frontend`, `type: fix`
- 状态：**已 In Progress**

## ⚠️ 排序约束（最重要，先看）
**本票必须先于 RND-326 完成并合入。**

- 本票改 `backend/app/web/templates/review_console.html` **第 69 行** 的 `.lang-menu` 样式。
- 语言切换的 **markup 位于第 375–377 行**，而它在 `<nav class="side-nav">`（第 ~350 行）到 `</nav>`（第 382 行）**内部**。
- RND-326 会把这整段 `<nav>` 换成 `__SIDENAV__` token 并由 `render_sidenav()` 重新生成 —— **会连带重写语言切换 markup**。
- 若 RND-326 先落地或两票并行，本票的修复会被**静默覆盖**，且没有测试能发现（这是纯视觉问题）。

> 因此：本票尽快完成 → 交 QA → Haisu 合入；RND-326 开工时必须以合入后的 `review_console.html` 为基线，并在其 `render_sidenav()` 输出中**保留本票的左对齐修复**。

## 背景与根因（Linear 工单已完成根因分析，此处复述关键结论）
现状 CSS（`review_console.html:69`）：

```css
.lang-menu{position:absolute;top:auto;right:0;bottom:100%;...;min-width:7rem;...;z-index:50}
```

- 父容器 `.lang-switch{position:relative}`（第 65 行），宽度 ≈ 按钮宽（~60px）。
- `right:0` 让菜单**右对齐**到 `.lang-switch` 右边缘；`min-width:7rem`（112px）比容器宽约 52px → 菜单**向左溢出**约 52px。
- 窄屏（`@media (max-width:1300px)`）下 `.side-nav` 缩到 170px，`.side-nav-user` 的 `padding:12px 16px` 让按钮贴近侧栏左侧 → 菜单左边缘落到**视口左边界之外**，被裁剪，选项点不到。
- `toggleLangMenu()`（`backend/app/web/static/console/console-entry.js`）只切 `display`，**不做定位** —— 位置完全由 CSS 决定。

## 目标（Goal）
让语言切换弹层在任意视口宽度下**完全落在浏览器可视范围内**，三个选项均可见可点击。

## 范围边界

**In scope：**
- 仅修改 `review_console.html` 的 `.lang-menu` 样式块，改为**左对齐**（用户已拍板方案 B）：
  ```css
  .lang-menu{position:absolute;top:auto;bottom:100%;left:0;right:auto;background:#fff;border:1px solid #e8e8e8;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.18);min-width:7rem;overflow:hidden;z-index:50}
  ```

**Out of scope（显式非目标，均为用户明确要求）：**
- **不改 JS 定位逻辑**（`toggleLangMenu()` / `console-entry.js`）—— 最小修改原则。
- **不改 `renderLangMenu()` 渲染逻辑**。
- **不改 i18n 词表**、不改 locale 持久化行为（`selectLocale()` / `applyLocale()` 行为必须不变）。
- **不删 `min-width:7rem`**（会让菜单过窄）、**不改 `z-index:50`**（会打乱遮挡层级）。
- **不改其他弹层**（`scope-popover` / `search-results` / `new-msg-indicator` 等），即便它们有同类风险 —— 另开票。
- 不碰 `<nav class="side-nav">` 的 markup（那是 RND-326 的范围）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/review_console.html` —— **仅限第 69 行附近的 `.lang-menu` 样式块**

> 这是一次一行级 CSS 修改。若你发现需要改 JS、改 markup、或改其他文件才能达成 —— **停止**，`BLOCKED_NEEDS_HUMAN`，说明原因。方案 A（transform 右对齐）与方案 C（向下展开）已被用户否决，不要改用。

## 验收标准（Acceptance Criteria）
- **AC-1 菜单左边框在视口内（硬验收点，用户明确要求）**：任意支持视口下，`.lang-menu` 左边框距浏览器视口左边界 **≥ 0**，不溢出。
- **AC-2 窄屏可用**：视口 **1280px**（<1300px 断点，侧栏 170px）下点击「语言」，弹层完全在视口内，3 个选项（中文 / 繁中 / English）均可见可点击。
- **AC-3 断点之上位置合理**：视口 **1366px** 下菜单左对齐到按钮左边缘，位置合理。
- **AC-4 宽屏不回退**：视口 **1920px** 下弹层位置正常（原本即正常，不得变坏）。
- **AC-5 功能不回退**：切换选项后菜单关闭，`selectLocale` 调用正确，UI 文案刷新；locale 持久化与 `applyLocale()` 无回退。
- **AC-6 布局不冲突**：侧栏 **170px 与 212px** 两种宽度均回归一次；不与 `.col-conv` / `.side-nav` 现有布局冲突；不引入新滚动条；**不遮挡左下角「退出登录」按钮**。
- **AC-7 回归**：`make verify` 全绿。

## 验证方式（Verification）
- 类型：**automated（回归）+ manual visual（定位）**
- 说明：本票核心是视觉定位问题，`make verify` **不能**证明 AC-1~AC-4。必须实测截图。

```bash
make verify                       # AC-7 回归闸
# 本地起服务后，在三档视口宽度实测并截图：
#   1280px / 1366px / 1920px
# 每档确认：菜单左边框距视口左边界 ≥ 0；3 个选项可见可点击
```
交付需附 **3 张截图**（1280 / 1366 / 1920），并特别标注左边框位置。

## 依赖（Dependencies）
无前置阻塞。**本票阻塞 RND-326**（见上方排序约束）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足
- [ ] 三档视口截图已附（1280 / 1366 / 1920）
- [ ] `make verify` 全绿
- [ ] `git status` 只显示 `review_console.html` 一个文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：改成 `left:0` 后，若 `.lang-switch` 未来被移到侧栏右侧，会重新溢出（右侧方向）。当前布局下不会，记录即可。
- 风险：**本票修复可能被 RND-326 覆盖** —— 见排序约束，务必先合入本票。
- 回滚：单一 CSS 声明改动，`git checkout -- backend/app/web/templates/review_console.html`。

## 人工点位
- **Trigger**：已 In Progress。
- **Gate**：Haisu 审阅截图后批准 commit。
- **Escalation**：若左对齐后在某档视口仍溢出（例如未来侧栏更窄）→ `BLOCKED_NEEDS_HUMAN`，附截图，**不要自行改用已被否决的方案 A/C**。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`review_console.html` 第 65–69 行（`.lang-switch` / `.btn-lang` / `.lang-menu`）与第 372–382 行（markup）。
2. 按方案 B 改 `.lang-menu`：`right:0` → `left:0; right:auto`，其余声明保持不变。
3. 本地起服务，在 1280 / 1366 / 1920 三档视口实测并截图，确认左边框 ≥ 0。
4. 侧栏 170px / 212px 两种宽度各回归一次，确认不遮挡「退出登录」。
5. 跑 `make verify`。
6. 输出 QA Summary + 截图 + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- **严禁 git commit / push**（用户硬规则，一律本人提交）。
- 不建分支、不改 git 历史、不改 CI/CD、`.gitignore`、部署配置。
- 架构冻结 D1：SSR + 原生 JS，不引 React；改动仅限 `.lang-menu` 样式块。
- 实现后必须交独立 QA agent 验收。
- 最小正确改动优先。
