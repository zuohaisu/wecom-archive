# 逐页面上线路线图 — 365 企微会话存档（前端可见性）

> 目标：把"线上生产环境里每个可见页面"对齐到 Linear 任务，查清依赖，给出上线顺序。
> 数据来源（两层）：
> 1. **设计系统（页面真相源）**：`design/Crowntime WeCom Archive Design System/` —— 含 `reference/`（自称已上线 6 页）+ `pages/`（v1 待建 16 页）+ 新 `styles.css` 设计系统。
> 2. **生产代码实测**：`backend/app/web/templates/*.html`、后端 router、`review_console.html` side-nav、Linear 项目 `365企微会话存档`（RND）。
> 生成时间：2026-07-29 ｜ 更新：2026-07-29 15:20（对照官方设计系统补全 16 v1 页 + CSS 落地缺口）

---

## 0. 为什么"界面一直没变化"——三层根因

不是你开发慢，是"能看见的东西"被三道墙挡住：

1. **生产环境与 main 脱钩（总闸门）。** 生产路径（`/srv/apps/wecom-archive-365` 等）被冻结，只能经 **RND-237（开源发布包装 / 发布导出）** 导出 + 部署才更新。RND-237 当前 **Todo**，挂开源准备 epic **RND-232** 下。main 天天有提交，但只要没跑发布导出，生产界面就停在旧版本。
2. **导航里 7/11 入口是灰的"即将推出"占位符，多数后端已 Done 却无前端票。** 团队一直在交后端引擎，但没给这些页面建 UI 票 → 永远停在占位态。
3. **设计稿从没接进生产（第三层，本次新确认）。** 你做的 16 个 v1 页面只是 `design/.../pages/` 下的**静态 HTML 模拟稿**，从来没有"设计稿 → 可部署页面"的转化工序；而且设计系统的新 `styles.css` **完全没被任何 backend 模板引用**——生产还在用旧版 `base.css` / `styles.css`。即连"已上线"页面也没用上你这套新设计外观。

结论：要让界面出现变化，需要 **(a) 补建前端 UI 票并合入 main + (b) 跑一次 RND-237 发布导出 + (c) 把设计系统 `styles.css` 落地到生产模板**。三者缺一不可。

---

## 0.5 设计系统：页面真相源（本次新确认）

官方设计系统目录：`design/Crowntime WeCom Archive Design System/`
- `reference/*.html` — 设计系统自述"已上线在跑"的 6 页：`review_console` / `messages` / `search` / `message_detail` / `diagnostics` / `message_detail_404`。**实测确认这 6 页在生产有真实模板 + 路由，属已上线。**
- `pages/*.html` — **v1 待建 16 页**（本路线图的核心对象）：login / forgot-password / settings / dashboard / analytics / users / contacts / contact-detail / search-advanced / audit-log / media / platform / tenant-provisioning / onboarding / onboarding-invite / onboarding-done。
- `styles.css` — 新设计系统（含 light/dark、组件层）。**实测：零个 backend 模板引用它** → 设计外观尚未落地到生产。

> ⚠️ 设计稿与生产是两套独立体系，中间无 build / 转换步骤。这是"界面一直没变"的隐藏主因之一。

---

## 1. 控制台导航 12 页 × 任务 × 依赖（导航视角）

| # | 页面（导航项） | 导航状态 | 后端任务（状态） | 前端任务 | 关键依赖 | 上线波次 |
|---|---|---|---|---|---|---|
| 1 | 对话审阅 `/admin/conversations` | ✅ 已启用 | 核心归档链路；RND-323/321/320 Done，RND-324 **In Progress** | 已有 | — | Wave 0（已上线） |
| 2 | 消息记录 `/admin/messages` | ✅ 已启用 | RND-284 等 | 已有 | — | Wave 0（已上线） |
| 3 | 消息可达性诊断 `/admin/diagnostics/reachability` | ✅ 已启用 | **RND-180 Done** | 已有 | — | Wave 0（已上线） |
| 4 | 同步与任务 `#sync-status` | ✅ 已启用 | `/api/admin/sync-status`、`/api/admin/sync-now` | 已有 | — | Wave 0（已上线） |
| 5 | 员工与坐席 | 🚫 即将推出 | **A3 epic RND-273 Done**；A3-1/2/3 = RND-284/285/286 Done | **缺失 → 需新建**（= v1 `users`） | F0 Done | **Wave 1（后端已就绪）** |
| 6 | 媒体与附件 | 🚫 即将推出 | A6-1 RND-291 **Done**；A6-2 RND-292 Backlog | **缺失 → 需新建**（= v1 `media`） | A6-1 Done | **Wave 1（后端已就绪）** |
| 7 | 外部联系人 | 🚫 即将推出 | A4-1 RND-287 **Done**；A4-2 RND-296 / A4-3 RND-298 Backlog | **缺失 → 需新建**（= v1 `contacts` + `contact-detail`） | A4-1 Done | **Wave 1（后端已就绪）** |
| 8 | 审计日志页（导航无入口） | ❌ 未列入导航 | **A7 epic RND-274 Done**；A7-1/2/3 = RND-293/294/295 Done | **缺失 → 需新建 + 加导航**（= v1 `audit-log`） | A7 Done | **Wave 1（后端已就绪）** |
| 9 | 设置 | 🚫 即将推出 | A8-1/2 Done（RND-302/297）；**RND-244 配置中心 In Progress**（RND-245~256） | RND-251 进行中（仅页面框架；偏好持久化 RND-297 未合） | RND-232/241/242，RND-237 | **Wave 2（进行中）** |
| 10 | 全局搜索 | 🚫 即将推出 | A5-1 RND-290 Backlog；A5-2 RND-299 Backlog | **缺失 → 需新建**（= v1 `search-advanced`，在基础 `search` 上加筛选） | A5 后端未做 | Wave 3（后端先行） |
| 11 | 审阅任务与标记 | 🚫 即将推出 | A9-1 RND-301 Backlog；A9-3 RND-304 Todo | **缺失 → 需新建** | A9 后端未做 | Wave 3（后端先行） |
| 12 | 导出记录 | 🚫 即将推出 | C2 epic RND-271 Backlog；C2-1/2/3 = RND-315/316/317 Backlog | **缺失 → 需新建** | C2 后端未做；依赖 A7 Done | Wave 3（后端先行） |

> 早期 **In Review 冻结票 RND-104/107/108/129/130/175** 被"企业微信企业名称变更"阻挡，多属核心控制台/品牌。你已能看到核心控制台，故该阻塞影响的是品牌改名而非页面可见性，本路线图不重复计入。

---

## 1.5 16 个 v1 设计页 × 生产现状（设计系统视角，本次新补）

对照 `design/Crowntime WeCom Archive Design System/pages/` 与生产 `backend/app/web/templates/`，逐页核实：

| # | v1 设计页 | 生产现状 | 后端状态 | 前端缺口 | 对应 Linear |
|---|---|---|---|---|---|
| 1 | `login` | ✅ 已建（有模板 + `/admin/login`） | Done | 无 | RND-277 等 |
| 2 | `forgot-password` | ✅ 已建（`forgot_password.html` + `reset_password.html`，文件名下划线差异） | Done | 无 | RND-302 相关 |
| 3 | `settings` | 🟡 **部分建**（仅"改密码"卡片，缺"账户 + 偏好"整页） | A8-1/2 Done；偏好 RND-297 未合 | 补整页 + 偏好 UI | RND-302 / RND-297 / RND-244 |
| 4 | `users`（员工与坐席） | 🔧 后端 Done，**无 UI** | A3 Done | 建页面 + 导航去灰 | RND-273/284/285/286 |
| 5 | `contacts`（外部联系人） | 🔧 后端 Done，**无 UI** | A4-1 Done | 建列表页 + 导航去灰 | RND-287 |
| 6 | `media`（媒体与附件） | 🔧 后端 Done，**无 UI** | A6-1 Done | 建网格页 + 导航去灰 | RND-291 |
| 7 | `audit-log`（审计日志） | 🔧 后端 Done，**无 UI 且无导航入口** | A7 Done | 建页面 + 加导航项 | RND-274/293/294/295 |
| 8 | `dashboard`（概览首页） | ❌ 无模板无路由 | 后端未做 | 全缺 | —（Line A，P0） |
| 9 | `analytics`（用量分析） | ❌ 无模板无路由 | 后端未做 | 全缺 | —（Line A，P0） |
| 10 | `contact-detail`（外部联系人详情） | ❌ 无模板 | 依赖 `contacts` UI + 搜索 | 全缺（随 contacts） | A4-2/3 |
| 11 | `search-advanced`（高级搜索） | ❌ 无模板（基础 `search` 已上线） | A5 Backlog | 在基础 search 上加筛选 + 导出 | RND-290/299 |
| 12 | `platform`（平台总控台） | ❌ 无模板 | B1 PlatformAdmin 实体 RND-306 Done，但无 UI；B2 未做 | 全缺 | RND-275 / RND-306 |
| 13 | `tenant-provisioning`（租户开通） | ❌ 无模板 | B2-1 RND-311 **BLOCKED F0** | 全缺 | RND-270/311 |
| 14 | `onboarding`（首次配置向导） | ❌ 无模板 | A9 RND-269；RND-304 **BLOCKED F0** | 全缺 | RND-269/304 |
| 15 | `onboarding-invite` | ❌ 无模板 | 随 A9 | 全缺 | RND-269 |
| 16 | `onboarding-done` | ❌ 无模板 | 随 A9 | 全缺 | RND-269 |

**16 页汇总**：✅ 已建 2（login / forgot-password）｜🟡 部分 1（settings）｜🔧 后端就绪只差 UI 4（users / contacts / media / audit-log）｜❌ 全缺 9（dashboard / analytics / contact-detail / search-advanced / platform / tenant-provisioning / onboarding / onboarding-invite / onboarding-done）。

> 与 §1 导航视角的关系：#4–7、#11 即导航表里的 #5–8、#10；#8–10、#12–16 是导航视角未覆盖的"新页面"（概览首页、用量、平台、租户、向导等），属设计稿定义了但生产从未立项。

---

## 2. 依赖关系（闸门）

```
F0 账号体系重构 (RND-263 Done)
 └─> 所有页面鉴权基础（已就绪）

A7 审计日志 (RND-274 Done: RND-293/294/295)
 ├─> 审计日志页 (#8/#7 in §1.5, UI 缺失)
 ├─> C2-3 导出审计钩子 (RND-317, 阻塞中)
 └─> B1-5 内容访问申请 gate (RND-309, 阻塞中)

RND-244 配置中心 (In Progress: RND-245~256)
 ├─ 依赖 RND-232 开源准备 / RND-241 / RND-242(改名)
 └─> 设置页整页 (#9 / §1.5 #3)

RND-237 发布导出 (Todo, 隶属 RND-232)
 └─ ★ 总闸门①：以上所有页面要"出现在生产"，必须先完成 RND-237 导出 + 部署。

设计系统 styles.css 落地（独立任务，当前未立项）
 └─ ★ 总闸门②：即使页面写出来，生产模板仍引用旧 base.css/styles.css，
        新设计外观（含 dark mode / 白标换肤）不会生效，直到把 styles.css 接进模板。
```

**最关键的单一依赖 = RND-237 + 设计系统 CSS 落地。** RND-237 是 Todo 且耦合在开源准备里；CSS 落地甚至还没立票。意味着：即便 Wave 1 四页 UI 全做完合入 main，只要没跑发布导出、没接 styles.css，你在生产上看到的仍是旧外观旧内容。

---

## 3. 逐页上线路线图（按波次）

### Wave 0 — 已上线（只需收尾）
- 生产已可见页：`review_console` / `messages` / `message_detail`(+404) / `diagnostics` / `search`（基础）/ `login` / `forgot-password`。
- 在途：RND-324（语言切换弹层窄屏溢出修复，In Progress）——合入 + 跑一次导出即可。

### Wave 1 — 后端已就绪，只差前端接线（最快见效，建议优先）
后端逻辑全部 Done，纯"写页面 + 把灰色 `<span>` 换成 `<a href>`"：
- **`users`（员工与坐席）** ← A3 全 Done（RND-273/284/285/286）
- **`media`（媒体与附件）** ← A6-1 Done（RND-291），A6-2 下载端点可拆 fast-follow
- **`contacts` + `contact-detail`（外部联系人）** ← A4-1 Done（RND-287），补 A4-2/3 两个小后端
- **`audit-log`（审计日志页）** ← A7 全 Done（RND-274/293/294/295），当前连导航入口都没有，需新建页面 + 加导航项

> 行动：为这 4 页各建一张前端 UI 票（挂一个 "UI 接线上线" epic 下）。投入产出比最高的"肉眼可见"增量。

### Wave 2 — 进行中（配置中心驱动设置整页 + 设计系统落地）
- **`settings` 整页** ← RND-244 配置中心 epic（RND-245~256，RND-251 是前端框架）。当前只做了"改密码"卡片，需补"账户 + 偏好"。
- **设计系统 `styles.css` 落地**（独立任务，建议本波启动）：把 `pages/shell.js` 的导航配置抽成 Jinja include，替换 `base.css` 引用为 `styles.css`，校验 legacy 语义类（`.badge` 等）不破。

### Wave 3 — 后端先行，再接前端
后端尚未完成，需先补后端：
- **`dashboard`（概览首页）** / **`analytics`（用量分析）** — Line A P0，后端未做。
- **`search-advanced`（高级搜索）** ← A5（RND-290/299 均 Backlog）。
- **`platform`（平台总控台）** ← B1 实体 Done 无 UI；**`tenant-provisioning`** ← B2-1 RND-311 **BLOCKED F0**。
- **`onboarding`(+invite/done)** ← A9 RND-269；RND-304 **BLOCKED F0**。
- **`审阅任务与标记`（导航 #11）/ `导出记录`（导航 #12）** ← A9 / C2 后端均 Backlog。

---

## 4. 还差什么 —— 明确的缺口清单

把上面所有"未做"聚合，当前一共差这几类：

**A. 13 个 v1 页面尚未建成**（16 个里除 login / forgot-password 外全部）：
- A1. **4 个后端已就绪、只差 UI**（最高优先级）：`users` / `contacts`(+detail) / `media` / `audit-log` —— 建 4 张前端票即可开发，不动后端。
- A2. **9 个全缺**（需先有后端或解 F0 阻塞）：`dashboard` / `analytics` / `contact-detail` / `search-advanced` / `platform` / `tenant-provisioning` / `onboarding` / `onboarding-invite` / `onboarding-done`。

**B. 设计系统 CSS 未落地（独立缺口，未立项）**：新 `styles.css` 零引用，生产用旧 `base.css`/`styles.css`。不解决，新页面写出来也是旧外观。

**C. 设置页只做了一半**：仅"改密码"卡片，缺"账户 + 偏好"整页（依赖 RND-244 / RND-297）。

**D. 总闸门 RND-237 发布导出仍是 Todo**：所有页面要"出现在你看到的生产环境"都卡在这。

**E. 两个必须马上拍板的决策**：
1. **建票**：Wave 1 的 4 个页面目前**没有前端任务票**。不建票，永远排不进迭代。
2. **解开通往生产的闸门**：把 RND-237 从"等开源准备全做完"里解耦，建立定期发布导出节奏（如每周一次）。

---

## 5. 推荐执行顺序（一句话版）

> 收尾 RND-324 → **建 Wave 1 四张 UI 票并开发（users/contacts/media/audit-log）** → **启动"设计系统 `styles.css` 落地"独立任务** → **跑一次 RND-237 发布导出 + 部署（你"看到变化"的开关）** → 并行推进 RND-244 设置整页 + Wave 3 后端 → 之后按 Wave 3 补后端再接前端。
