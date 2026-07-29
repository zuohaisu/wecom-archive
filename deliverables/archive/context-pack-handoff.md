# Context Pack — 365 企微会话存档 · 前端可见性（供推理 agent 接手）

> 用途：把"为什么生产界面一直没变化 + 页面/任务/依赖全貌"浓缩成自包含背景，交给另一个 agent 做排期/优先级/技术推理。
> 数据来源：已核实（生产模板 `backend/app/web/templates/`、控制台 side-nav、Linear 项目 `365企微会话存档`、设计系统目录）。生成 2026-07-29。

---

## 1. 项目是什么
- 产品：**365 企微会话存档**（对外品牌：康冠时代 企业微信会话存档 / Crowntime WeCom Archive），AI-native 企业微信会话归档系统。
- 项目管理：Linear 项目 `365企微会话存档`（team Builder / RND）。主项目 ID `cfe726ec-810c-4848-8455-9b3607bf1fc1`。
- 技术栈：后端 FastAPI（`uvicorn app.main:app`，本地端口 8035）；前端 **SSR + 原生 JS（项目硬规则 D1：禁 React，禁 SPA 路由假设）**。模板在 `backend/app/web/templates/`，控制台 JS 在 `backend/app/web/static/console/`。
- 用户当前观察视角：**线上生产环境**（不是本地）。

## 2. 核心问题
用户开发了很久，但**生产界面一直没变化**。已核实三层根因（都不是"开发慢"）：

1. **生产冻结**：生产路径（`/srv/apps/wecom-archive-365` 等）只能经 **RND-237 发布导出**（当前 Todo，挂开源准备 epic RND-232）更新。main 天天提交，生产不跟着动。
2. **导航占位**：控制台 side-nav 11 个入口里 7 个是灰色"即将推出"，多数后端已 Done 却无前端任务票。
3. **设计稿未接产**（本次新确认）：用户做的 16 个 v1 页面只是 `design/.../pages/` 下的静态模拟稿，从未转化为可部署页面；且设计系统新 `styles.css` 零引用，生产用旧 `base.css`/`styles.css`。

**要让界面出现变化，需三者同时成立**：(a) 补建前端 UI 票并合入 main + (b) 跑一次 RND-237 发布导出 + (c) 把设计系统 `styles.css` 落地到生产模板。

## 3. 设计系统 = 页面真相源
路径：`design/Crowntime WeCom Archive Design System/`
- `reference/` = 设计系统自述"已上线"的 6 页：`review_console` / `messages` / `search` / `message_detail` / `diagnostics` / `message_detail_404`。**实测确认生产有模板+路由 = 真上线。**
- `pages/` = **v1 待建 16 页**（本次核心对象）。
- `styles.css` = 新设计系统（light/dark、完整组件层）。**实测：零 backend 模板引用 → 未落地生产。**
- 注：`pages/shell.js` 用一份配置渲染侧栏导航，设计意图是"生产里变成 Jinja include"。

## 4. 16 个 v1 页面 × 生产现状（已逐页核实 `backend/app/web/templates/`）

| v1 设计页 | 生产现状 | 后端状态 | 前端缺口 | Linear |
|---|---|---|---|---|
| `login` | ✅ 已建（模板+`/admin/login`） | Done | 无 | RND-277 等 |
| `forgot-password` | ✅ 已建（`forgot_password.html`+`reset_password.html`，文件名下划线差异） | Done | 无 | RND-302 相关 |
| `settings` | 🟡 部分（仅"改密码"卡片，缺"账户+偏好"整页） | A8-1/2 Done；偏好 RND-297 未合 | 补整页 | RND-302/297/244 |
| `users`（员工与坐席） | 🔧 后端 Done，**无 UI** | A3 Done | 建页+导航去灰 | RND-273/284/285/286 |
| `contacts`（外部联系人） | 🔧 后端 Done，**无 UI** | A4-1 Done | 建页+导航去灰 | RND-287 |
| `media`（媒体与附件） | 🔧 后端 Done，**无 UI** | A6-1 Done | 建页+导航去灰 | RND-291 |
| `audit-log`（审计日志） | 🔧 后端 Done，**无 UI 且无导航入口** | A7 Done | 建页+加导航 | RND-274/293/294/295 |
| `dashboard`（概览首页） | ❌ 无模板无路由 | 后端未做 | 全缺 | Line A P0 |
| `analytics`（用量分析） | ❌ 无模板无路由 | 后端未做 | 全缺 | Line A P0 |
| `contact-detail`（外部联系人详情） | ❌ 无模板 | 随 `contacts` UI | 全缺 | A4-2/3 |
| `search-advanced`（高级搜索） | ❌ 无模板（基础 `search` 已上线） | A5 Backlog | 基础 search 上加筛选+导出 | RND-290/299 |
| `platform`（平台总控台） | ❌ 无模板 | B1 实体 RND-306 Done 无 UI | 全缺 | RND-275/306 |
| `tenant-provisioning`（租户开通） | ❌ 无模板 | B2-1 RND-311 **BLOCKED F0** | 全缺 | RND-270/311 |
| `onboarding`（首次配置向导） | ❌ 无模板 | A9 RND-269；RND-304 **BLOCKED F0** | 全缺 | RND-269/304 |
| `onboarding-invite` | ❌ 无模板 | 随 A9 | 全缺 | RND-269 |
| `onboarding-done` | ❌ 无模板 | 随 A9 | 全缺 | RND-269 |

**汇总：✅ 已建 2（login / forgot-password）｜🟡 部分 1（settings）｜🔧 后端就绪只差 UI 4（users / contacts / media / audit-log）｜❌ 全缺 9（dashboard / analytics / contact-detail / search-advanced / platform / tenant-provisioning / onboarding / onboarding-invite / onboarding-done）。**

## 5. 依赖闸门（关键事实）
- **RND-237 发布导出** = 通往生产的唯一总闸门（Todo，耦合开源准备 RND-232）。
- **设计系统 `styles.css` 落地** = 通往"新外观"的第二闸门（未立项）。
- **RND-244 配置中心**（In Progress，子票 RND-245~256 全未落地）= 设置整页依赖；设计 brief 称其为 "F0"。
- **术语 "F0" 澄清（重要，避免混淆）**：设计 brief 的 "F0" = **配置中心 RND-244（未建成，硬阻塞 RND-304/311/318 等多票）**；另有 "F0 账号体系" RND-276~280 **已 Done**，二者不是一回事。推理时见到 "BLOCKED F0" 一律指 RND-244 配置中心。
- 已就绪后端：A7 审计 Done、A3 用户管理 Done、A4-1 外部联系人 Done、A6-1 媒体 Done（均只缺 UI）；RND-180 诊断页 Done。
- 冻结票 RND-104/107/108/129/130/175 被"企微企业名称变更"卡住，影响品牌改名而非页面可见性，不计入本问题。

## 6. 路线图波次（现有方案）
- **Wave 0 已上线**：reference 6 页 + login/forgot-password + RND-324 收尾（语言切换窄屏修复，In Progress）。
- **Wave 1（最快见效，建议优先）**：4 页后端就绪只差 UI —— `users` / `contacts`(+detail) / `media` / `audit-log`。纯写页面 + 导航去灰，不动后端。
- **Wave 2（进行中）**：设置整页（RND-244）+ 设计系统 `styles.css` 落地。
- **Wave 3（后端先行）**：`dashboard` / `analytics` / `search-advanced` / `platform` / `tenant-provisioning` / `onboarding`(+2) / 审阅任务与标记 / 导出记录。

## 7. 还差什么（缺口清单）
- **A1**：4 页只差 UI（最高优先级，不动后端）。
- **A2**：9 页全缺（需先补后端或解 F0 阻塞）。
- **B**：设计系统 `styles.css` 未落地（独立缺口，未立项）。
- **C**：设置页半截（仅改密码卡）。
- **D**：RND-237 发布闸门仍是 Todo。
- **E**：待拍板 ① 建 Wave 1 四张前端票 ② 解耦 RND-237 与开源准备。

## 8. 交给推理 agent 的开放问题（推理点，非指令）
1. 在 RND-237 发布闸门未解前，怎样排布才能最快让用户"看到变化"？是否应先解耦 RND-237？
2. Wave 1 四页应合并为一个 "UI 接线上线" epic 还是独立四票？票粒度如何？
3. 设计系统 `styles.css` 落地的最小可行方案（替换 `base.css` 引用 + `shell.js`→Jinja include）有哪些风险点？legacy 语义类（`.badge` 等）如何不破？
4. RND-244 配置中心是否应优先于开源准备推进（影响设置页 + 多个 BLOCKED 票）？
5. 16 页全量铺开 vs 先 Quick-win 四页，哪个更符合"用户可见价值"优先级？
6. 审计日志页当前连导航入口都没有 —— 导航结构本身是否要随这批发页重构（参考设计系统 `pages/shell.js` 的配置）？
