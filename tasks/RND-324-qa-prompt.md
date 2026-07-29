[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-324 的 7 条 AC（含三档视口实测）并产出带证据的 PASS/FAIL 判定。

# RND-324 验收提示词（Acceptance / QA Prompt）

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-324「语言切换弹层在窄屏下向左溢出浏览器可视范围」｜风险等级 R1
- 类型：**视觉定位缺陷** —— `make verify` **无法**证明本票主要 AC，必须做视口实测。

## ⚠️ 本票的特殊性：自动化闸证明不了它
本票是纯 CSS 定位问题。`make verify` 全绿**不等于**修复成立 —— 菜单仍可能溢出视口。
因此：**没有三档视口的实测证据（截图或等价的几何测量），一律判 FAIL（`INSUFFICIENT_TEST_COVERAGE`）**，不接受「代码看起来对」。

## 你的角色与权限
- 可以：读所有文件、跑只读命令、本地起服务做视口实测、截图。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 菜单左边框在视口内（**硬验收点，用户明确要求**）
- 证据：三档视口下测量 `.lang-menu` 左边框相对浏览器视口左边界的距离，**必须 ≥ 0**。
  可用截图目测 + DevTools 几何值（如 `getBoundingClientRect().left >= 0`）。
- 判定：三档均 ≥ 0 = PASS。任一档为负 → FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）。

### AC-2 — 窄屏可用（视口 1280px，侧栏 170px）
- 证据：截图显示弹层完全在视口内，中文 / 繁中 / English **三个选项均可见可点击**（不只是可见）。
- 判定：三项可见可点 = PASS。

### AC-3 — 断点之上位置合理（视口 1366px）
- 证据：截图显示菜单左对齐到按钮左边缘，位置合理无溢出。
- 判定：无溢出且对齐正确 = PASS。

### AC-4 — 宽屏不回退（视口 1920px）
- 证据：截图显示弹层位置正常（此档原本即正常，重点是**没变坏**）。
- 判定：无回退 = PASS。

### AC-5 — 功能不回退
- 证据：切换选项后菜单关闭；`selectLocale` 被正确调用；UI 文案刷新；locale 持久化与 `applyLocale()` 行为不变。
- 代码审阅：确认 `console-entry.js` 的 `toggleLangMenu()` / `renderLangMenu()` / `selectLocale()` **未被修改**（本票明确不改 JS）。
- 判定：功能正常 + JS 未改 = PASS。**若 JS 被改 → FAIL（`SCOPE_VIOLATION`）。**

### AC-6 — 布局不冲突
- 证据：侧栏 **170px 与 212px** 两种宽度各回归一次；确认不与 `.col-conv` / `.side-nav` 冲突、**未引入新滚动条**、**未遮挡左下角「退出登录」按钮**。
- 判定：两种宽度均无冲突 = PASS。遮挡「退出登录」= FAIL。

### AC-7 — 回归
- 证据：`make verify` exit 0。
- 判定：exit 0 = PASS。

## 本项目专属检查（必查）
1. **改动必须极小**：本票是一次一行级 CSS 修改。`git diff --stat` 应只显示 `backend/app/web/templates/review_console.html`，且改动集中在 `.lang-menu` 样式块。**大范围重写 = FAIL（`SCOPE_VIOLATION`）。**
2. **禁止项未被触碰**（用户明确要求）：
   - `min-width:7rem` **仍在**（未被删除）
   - `z-index:50` **未被改动**
   - JS 定位逻辑、`renderLangMenu()`、i18n 词表、locale 持久化 **均未改**
   - 其他弹层（`scope-popover` / `search-results` / `new-msg-indicator`）**未被顺手改**
   任一被违反 → FAIL（`SCOPE_VIOLATION`）。
3. **未采用被否决的方案**：用户已否决方案 A（transform 右对齐）与方案 C（向下展开），只接受方案 B（`left:0; right:auto`）。若实现改用 A 或 C → FAIL（`SCOPE_VIOLATION`），即便视觉效果达标。
4. **未侵入 RND-326 范围**：本票**不得**改 `<nav class="side-nav">` 的 markup（第 ~350–382 行）。若 markup 被改 → FAIL（`SCOPE_VIOLATION`），会与 RND-326 冲突。
5. **架构冻结 D1**：无 React / Vue / 构建步骤引入。

## 附加检查（Security）
- 无凭证 / 真实用户数据 / 真实域名进入代码或截图（截图注意脱敏：**不得包含真实聊天内容、真实客户昵称**）→ 否则 FAIL（`SECURITY_VIOLATION`）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
git diff --stat                                                    # 应只有 review_console.html
git diff -- backend/app/web/templates/review_console.html          # 人工核对改动仅限 .lang-menu
grep -n 'min-width:7rem' backend/app/web/templates/review_console.html   # 必须仍存在
grep -n 'z-index:50' backend/app/web/templates/review_console.html       # 必须仍存在
git diff --stat -- backend/app/web/static/console/console-entry.js       # 必须无输出（JS 未改）
git log origin/main..HEAD                                          # 必须无输出
```

## 视口实测清单（必做，无此证据一律 FAIL）
| 视口宽度 | 侧栏宽度 | 需确认 |
|---|---|---|
| 1280px | 170px | 左边框 ≥ 0；三选项可见可点 |
| 1366px | 212px | 左对齐正确；无溢出 |
| 1920px | 212px | 无回退 |

另需在 170px / 212px 两种侧栏宽度下确认未遮挡「退出登录」。

## 产出
写入 `tasks/RND-324-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`commands` 中除命令外，须在 `notes` 记录三档视口的实测结果（左边框像素值或截图路径）。

## 禁止事项
- 不改任何文件、不补做修复、不放松 AC。
- **无三档视口实测证据 → 直接 FAIL**，不接受「CSS 改对了所以应该没问题」。
- 不接受只测 1920px（该档原本就正常，证明不了任何东西）。
