# RND-229 / RND-230 搜索增强 · UI 设计规格

> 面向「对话审阅控制台」(`/admin/conversations`) 的搜索体验升级。
> **RND-229**：独立搜索结果页 + 定位回原对话（高亮 + 自动滚动）。
> **RND-230**：结果页多维 Filter（日期 / 用户 / 员工 / 消息类型）。
> 设计原则：**严格衔接现有 UI**，不引入新视觉语言；只做克制、一致、可访问的增量。

---

## 0. 决策记录（2026-07-23 已拍板）

1. **消息类型索引范围（v1 现状可接受，但 UI 选项必须全量）**
   后端 v1 仅索引 `msgtype='text'`（已解密未撤回）属可接受现状；但前端「消息类型」筛选项**必须全量覆盖**企微全部消息类型（文本 / 图片 / 语音 / 视频 / 文件 / 链接卡片 / 系统消息 / 表情 / 聊天记录 / 红包 / 小程序 …），**不**按"已索引项"动态裁剪。
   - 理由：筛选器可**脱离关键词单独使用**（见决策 2），用户期望类型清单完整；非文本类型选中后当前可能返回空，待索引扩展后自动生效（前向兼容）。
   - UI 类型清单来源：静态 `MessageTypeRegistry` 全量类型常量，而非 API 当前返回的类型集合。
2. **关键词可选（纯筛选模式）**：`q`（搜索词）变为**可选**。用户可仅用筛选器（如"只看某员工发的语音"）而不输入任何关键词；后端需在至少存在一个筛选条件时允许 `q` 缺省。
3. **后端 API 契约归属**：本文档第 5 节契约由 **UI Designer 与 技术总监** 共同拍板确定，无需再次打扰需求方（Haisu）。

---

## 1. 设计基础（100% 复用现有令牌）

所有取值直接取自 `backend/app/main.py` 的 `_REVIEW_CONSOLE_HTML`，**不新增品牌色**。

| 令牌 | 值 | 用途 |
|---|---|---|
| `--primary` | `#1890ff` | 主操作、链接、激活态、目标高亮 |
| `--primary-hover` | `#0958d9` | 悬浮态 |
| `--primary-soft` | `#e6f4ff` | 激活背景、筛选 chip 背景 |
| `--topbar-bg` | `#001529` | 顶部栏 |
| `--page-bg` | `#f0f2f5` | 页面背景 |
| `--col-bg` | `#fff` | 卡片/栏背景 |
| `--border` | `#e8e8e8` | 边框 |
| `--header-bg` | `#fafafa` | 次级标题条 |
| `--text` | `#222` | 主文字 |
| `--text-2/3/4` | `#666 / #888 / #bbb` | 次级→最弱文字 |
| `--kw` | `#e00` | 关键词高亮（沿用 `.sr-highlight`） |
| `--err / --err-bg / --err-border` | `#cf1322 / #fff2f0 / #ffccc7` | 错误态 |
| 字体 | `system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif` | 全站统一 |

间距沿用现有 4px 节奏（`.2/.35/.45/.6/.75/1rem`）。圆角统一 `6px`（卡片）、`4px`（输入/按钮）、`999px`（chip/徽标）。

---

## 2. 页面结构

```
┌─ 顶部栏（复用现有：标题 / 刷新 / 搜索框 / 导航 / 语言 / 退出）──┐
│  搜索框：输入后 Enter → 跳转 /admin/search?q=…                 │
├───────────────────────────────────────────────────────────────┤
│ 结果页头部：‹ 返回控制台    “关键词” 或 “筛选结果” 共 N 条消息匹配   排序 ▾   │
├───────────────────────────────────────────────────────────────┤
│ 筛选栏（sticky）：[日期▾][用户▾][员工▾][消息类型▾]  ← 触发器     │
│                   [日期:近7天×][员工:张伟×]  清除全部           │ ← 已生效 chips
├───────────────────────────────────────────────────────────────┤
│ 结果列表（max-width 920px 居中）：                              │
│  ┌ 结果卡片 ──────────────────────────────────────┐           │
│  │ [群聊] 客户群·华东大区   张伟(员工)   07-23 14:02│           │
│  │ 关于昨天的订单，我们已经发起<mark>退款</mark>流程…       │           │
│  │ 员工 张伟 · 客户 李娜 · 文本            查看上下文 ↗ │           │
│  └───────────────────────────────────────────────┘           │
│  （列表项可滚动，支持骨架屏/空/错误态）                         │
└───────────────────────────────────────────────────────────────┘
```

---

## 3. 组件规格

### 3.1 顶部栏搜索框（RND-229 入口变更）
- 沿用现有 `.search-bar` / `.search-bar input`（深色输入框 `#0a1a2e`）。
- **新增 Enter 行为**：在输入框按 Enter（而非等待 300ms 内联下拉）跳转至 `/admin/search?q=<编码关键词>`。
- 内联下拉（现有 RND-159 行为）**保留**——下拉项点击仍是「就地导航」；Enter 才是「进入结果页」。两者互补，互不破坏。
- 输入框右侧加一个极轻的提示 `↵ 结果页`（沿用 `--topbar-border` 描边的小标签），教育用户 Enter 的用途。

### 3.2 结果页头部
- `‹ 返回控制台`：链回 `/admin/conversations`（沿用 `--primary` 链接色）。
- 查询词展示：有关键词时以 `“关键词”`（`--primary` 着色）展示；**无关键词（纯筛选）时**显示 `筛选结果`（`--primary`），表明本次为按条件过滤。右侧始终 `共 N 条消息匹配`（`--text-3`）。
- 排序选择器：默认「时间（新→旧）」，可选「时间（旧→新）」。仅影响展示顺序，不影响筛选。

### 3.3 筛选栏（RND-230）
四个触发器按钮，样式复用 `.mode-tab`/`.filter-btn` 思路：
- **日期**：单选 popover（全部时间 / 近24h / 近7天 / 近30天 / 近90天）。
- **用户**：多选 popover，列出结果中出现的「对话联系人」（contact 侧），顶部带搜索框可过滤选项；支持多选。
- **员工**：多选 popover，列出结果中出现的「监控账号」（staff 侧）。
- **消息类型**：多选 popover，选项来自 `MessageTypeRegistry` 的已索引类型（文本/图片/语音/视频/文件/链接卡片/系统消息/表情/聊天记录…）。

**已生效条件**：每个激活的筛选渲染为可移除 chip（`--primary-soft` 背景 + `--primary` 文字 + `×`），点击 `×` 单项移除；存在任意激活条件时显示「清除全部」。激活的触发器按钮加 `.active`（蓝边 + 浅蓝底），并显示数量角标。

> 多选同时生效：后端以 AND 组合（日期 ∩ 用户 ∩ 员工 ∩ 类型）。

### 3.4 结果卡片（RND-229）
复用现有 `.sr-item` / `.sr-highlight` 的视觉基因，升级为卡片：
- **上下文行**：`[群聊/单聊]` 徽标（沿用 `.badge-group`/`.badge-direct` 配色）+ 会话名 + 发送者（员工名用 `--primary-hover` 加粗，对应 `.tl-staff`）+ 时间（`--text-4`）。
- **摘要**：`content_snippet`，关键词用 `<mark>`（`--kw`）高亮。
- **路由脚注**：`员工 X · 客户 Y · 类型`，帮助用户判断「这条结果属于哪段对话哪个人」——直接回应 RND-229「用户可判断不同结果对应的内容」。
- 悬浮：边框变 `--primary`、轻微阴影、显示「查看上下文 ↗」。
- 整卡可点击 → 触发「回到会话定位」。

### 3.5 状态
- **加载**：骨架屏（`sk-line` 渐变动画），沿用现有 `.loading` 语义。
- **空**：`🔍 没有匹配的聊天内容` + 提示（有筛选时引导放宽/清除，无筛选时说明无关键词命中）。
- **错误**：`⚠️ 搜索失败，请稍后重试`（沿用 `--err-bg`/`--err-border`）。

### 3.6 回到会话定位（RND-229 核心）
点击结果卡 → 在当前控制台（`/admin/conversations`）完成定位，不新开整页：
1. 按 `entity_type` 切换到对应员工/联系人模式，选中目标 `conversation_id`（复用现有 `onEntityClick` / `onConvClick`）。
2. 加载时间线后，以 `msgid`/片段定位目标消息，并将时间线滚动至其可见（`scrollIntoView`，`block:center`，`scroll-margin-top` 留白）。
3. **视觉反馈**：目标气泡附加 `.target-flash` 关键帧（1.6s 进场闪光：蓝色光晕 → 浅蓝底），动画结束后转为持久 `.target-active`（蓝色左边框 + 浅蓝底），让用户持续知道落在哪。
   - 关键帧（`msgFlash`）见 mockup：从 `#fffbe6` 黄闪过渡到 `#e6f4ff` 蓝底，避免纯红闪的刺眼感，同时保持醒目。
4. 顶部提供「← 返回搜索结果」入口（可沿用 URL `?focus=msgid` 状态，便于浏览器后退）。

---

## 4. 交互流程

```
搜索框输入 → [Enter] → /admin/search?q=关键词
                         │
                         ├─ 展示结果页（默认按时间倒序）
                         ├─ 用户加筛选（日期/用户/员工/类型）→ 即时过滤 + chips
                         ├─ 清除单项 / 清除全部 → 恢复
                         └─ 点击结果卡 → 定位回 /admin/conversations
                                          └─ 滚动 + 闪光高亮目标消息
```

---

## 5. 后端 API 契约（RND-230 所需，前端依赖）

> 契约归属：本节由 **UI Designer 与 技术总监** 共同拍板确定（见 §0 决策 3），无需需求方二次确认。

`GET /api/search/messages` 现有参数：`q`、`limit`、`before`（游标）。
**改造点**：`q` 由必填降为**可选**；当至少存在一个筛选参数时，允许 `q` 缺省（支持"纯筛选"模式，见 §0 决策 2）。
**需新增可选筛选参数（租户内 AND 组合，不改权限/隔离逻辑）**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `q` | str | 否（有筛选时可缺省） | 关键词；与筛选条件 AND 组合 |
| `date_from` / `date_to` | int (ms) | 否 | 时间范围；或由 `date_range=1d\|7d\|30d\|90d` 预设推导 |
| `user` | str (重复) | 否 | contact 侧参与者 userid，多选 |
| `staff` | str (重复) | 否 | 员工/监控账号 userid，多选 |
| `msgtype` | str (重复) | 否 | 消息类型，多选（全量类型可选，见 §0 决策 1） |

返回结构沿用现有 `MessageSearchResult`（已含 `conversation_id/type/name`、`sender_display_name`、`content_snippet`、`msgtime`、`entity_id/type`、`match_position`）。

**索引范围说明（已拍板）**：v1 后端仅索引 `msgtype='text'`（已解密未撤回）。`msgtype` 筛选仍**全量开放**类型选项，选中非 `text` 类型时当前可能返回空结果——属已知限制，待索引扩展后自动生效，UI 不做裁剪（见 §0 决策 1）。`q` 缺省 + 非 `text` 类型筛选组合时，后端按"该类型在索引内"自然返回（text 命中，其他为空）。

分页：沿用现有游标（`msgtime:id`）分页，新增筛选参数原样透传即可，不影响现有消息展示与分页加载（RND-229 非目标明确排除）。

---

## 6. i18n Key 清单（沿用 `I18N.t('search.*')` 命名空间）

```
search.resultsTitle        “关键词” 结果
search.resultCount         {n} 条消息匹配
search.backToConsole       返回控制台
search.sort                排序
search.filter.date         日期
search.filter.user         用户
search.filter.staff        员工
search.filter.msgtype      消息类型
search.filter.dateAll      全部时间
search.filter.date1d       近 24 小时
search.filter.date7d       近 7 天
search.filter.date30d      近 30 天
search.filter.date90d      近 90 天
search.filter.clear        清除
search.filter.clearAll     清除全部
search.filter.active       已生效条件
search.empty.title         没有匹配的聊天内容
search.empty.hint          试试调整或清除筛选条件
search.error               搜索失败，请稍后重试
search.viewContext         查看上下文
search.enterHint           按 Enter 查看全部结果
search.jumpBack            ← 返回搜索结果
search.targetLocated       已定位到目标消息
```

---

## 7. 无障碍（WCAG AA）

- 颜色对比：正文 `#222` on `#fff` ≈ 15:1；次级 `#888` on `#fff` ≈ 3.5:1（仅用于非必要辅助文字，关键文字保持 ≥4.5:1）。蓝 `#1890ff` 仅作非文字语义（边框/背景），不单独承载含义。
- 键盘：筛选 popover 内选项可用 Tab/方向键 + 空格切换；chip 的 `×` 为真实 `<button>` 带 `aria-label`；结果卡可聚焦（`tabindex=0`）并响应 Enter。
- 焦点指示：沿用现有 `:focus` 蓝色描边；popover 打开时焦点落入首个选项，Esc 关闭并归还触发器。
- 动效：目标闪光动画时长 ≤1.6s，且 `prefers-reduced-motion` 下降级为静态高亮（仅 `.target-active` 蓝边蓝底，无闪烁）。
- 触摸目标：筛选按钮/芯片最小高度 ≥32px，满足 44px 命中区附近（桌面场景可放宽，移动端需 ≥44px）。

---

## 8. 与现有功能衔接点（明确「不动」的部分）

- **权限 / 租户隔离**：所有筛选在 `tenant_id` 作用域内执行，复用 `get_current_user` 与现有会话解析，**不新增任何租户/权限分支**。
- **消息展示与分页**：结果页与控制台时间线各自独立；点击结果回控制台后，沿用现有 `loadTimeline` / 游标分页逻辑，不改造。
- **内联搜索下拉（RND-159）保留**：仅新增 Enter→结果页入口，下拉的「就地导航」行为完全不变。
- **视觉语言零新增**：全部颜色/字体/圆角/间距取自现有令牌，新增 CSS 类名（`results-*`/`filter-*`/`chip`/`target-*`）仅为布局与状态所需，不引入新设计决策。

---

## 9. 实现落点建议

| 产物 | 文件 | 说明 |
|---|---|---|
| 新页面 HTML | `backend/app/main.py` 新增常量 `_SEARCH_PAGE_HTML`（仿 `_REVIEW_CONSOLE_HTML`） | 路由 `GET /admin/search` 返回，未登录则 302 到 `/admin/login` |
| 顶部栏 Enter | 同文件搜索框 JS 增加 `keydown`/form `submit` | Enter → `location.href='/admin/search?q='+encodeURIComponent(v)` |
| 筛选 API | `backend/app/routers/search.py` `search_messages` | 增加 `date_*`/`user`/`staff`/`msgtype` 参数与查询拼接 |
| 定位高亮 | 控制台 JS 新增 `focusMessage(msgid)` | 复用 `onEntityClick`/`onConvClick` + 滚动 + `.target-flash` |
| 设计令牌/CSS | 直接复用现有 `:root` 与类 | 仅追加第 3 节列出的新类 |

> 交互原型见同目录 `search-results-mockup.html`（可直接用浏览器打开评审；右下角「演示控制」可切换 加载中/空/错误/回到会话 状态，仅评审用，可删除）。
