> ⚠️ **已被取代（2026-08-07）**
>
> 本文档的**产品前提**（开源自托管版 + 云上商业版双交付）已被
> [ADR-0003](../docs/adr/0003-product-strategy-hosted-only.md) 推翻：
> 不开源，只做面向 <5 座席小微企业的全自动云服务，定位是防飞单威慑而非合规存档。
> 执行路线图见 [roadmap-first-10-customers-2026-08-07.md](roadmap-first-10-customers-2026-08-07.md)。
>
> **本文档仍然有效的部分**：§1 核实基线、§4 四个隐形阻塞——那些是实地读代码得到的事实，
> 与产品前提无关。**已失效的部分**：§3 的 R0–R5 排序、§5 的双产品建议。

---

# 最终路线图 · 365 企微会话存档
## 双产品交付：开源自托管版 + 云上商业版

> 生成：2026-07-29 ｜ 组织方式：**按前端页面**，而非后端模块
> 依据：本人实地读取 `backend/app/` 全部 router / models / templates / scripts + 设计系统目录，非二手转述
> 目标：(1) 正式开源发布 (2) 上线可售卖的云版本，客户无需自行部署

---

## 0. 一句话结论

**当前代码是一套"给一家公司用、已经跑得不错的单租户系统"。**
它离"开源发布"差 3 件小事（LICENSE / 容器化 / 首配向导），离"云上可售卖"差 1 件大事（**密钥与凭据体系是单租户硬编码，第二家客户接不进来**）。

页面清单对两个产品是同一份，但**客户旅程完全不同**——开源客户的第一屏是 README，云客户的第一屏是官网。今天这两屏都不存在：一个陌生人访问生产环境，看到的是一个**没有任何途径拿到账号的登录框**。

因此本路线图的排序原则不是"哪个页面容易做"，而是：**沿客户旅程从左往右补齐，先让一个陌生人能完整走通一次。**

---

## 1. 核实基线（我读代码得出的事实）

### 1.1 生产真实存在的页面 = 9 条路由 / 10 个模板

| 路由 | 模板 | 定义位置 |
|---|---|---|
| `/admin/login` | `login.html` | [auth.py:189](backend/app/routers/auth.py#L189) |
| `/admin/forgot-password` | `forgot_password.html` | [auth.py:395](backend/app/routers/auth.py#L395) |
| `/admin/reset-password` | `reset_password.html` | [auth.py:404](backend/app/routers/auth.py#L404) |
| `/admin/conversations` | `review_console.html` | [web.py:39](backend/app/routers/web.py#L39) |
| `/admin/messages` | `messages.html` | [messages.py:19](backend/app/routers/messages.py#L19) |
| `/admin/messages/{msgid}` | `message_detail.html` / `_404` | [messages.py:64](backend/app/routers/messages.py#L64) |
| `/admin/search` | `search.html` | [web.py:72](backend/app/routers/web.py#L72) |
| `/admin/diagnostics/reachability` | `diagnostics.html` | [web.py:88](backend/app/routers/web.py#L88) |
| `/admin/settings` | `settings.html`（**仅"改密码"一张卡**） | [web.py:62](backend/app/routers/web.py#L62) |

### 1.2 后端已就绪、只等 UI 的端点（这是最便宜的可见增量）

| 端点 | 位置 | 可直接点亮的页面 |
|---|---|---|
| `GET /api/admin/users`、`PATCH /users/{id}`、`POST /users/{id}/reset-password` | [users.py:23](backend/app/routers/users.py#L23) | **users** |
| `POST /api/admin/users/invite`、`/accept` | [auth.py:312](backend/app/routers/auth.py#L312) | **users**（邀请流） |
| `GET /api/admin/audit-logs` | [audit.py:52](backend/app/routers/audit.py#L52) | **audit-log** |
| `GET /api/admin/media` | [media_library.py:42](backend/app/routers/media_library.py#L42) | **media** |
| `GET /api/contacts`、`GET /api/search/contacts` | [conversations.py:325](backend/app/routers/conversations.py#L325)、[search.py:177](backend/app/routers/search.py#L177) | **contacts**（列表可做，详情缺端点） |
| `POST /api/admin/settings/password`、`PUT /api/auth/me/preferences` | [settings.py:24](backend/app/routers/settings.py#L24)、[auth.py:996](backend/app/routers/auth.py#L996) | **settings** 整页 |

### 1.3 后端完全没有的（页面做不出来，得先补端点）

- **无任何聚合/统计端点** → `dashboard`、`analytics` 是从零开始
- **`PlatformAdmin` 模型存在（[models.py:269](backend/app/db/models.py#L269)）但零个 router** → `platform` 总控台无任何后端
- **无租户开通端点** → `tenant-provisioning` 无后端
- **无计费/订阅/配额的任何代码**（全仓库 grep `billing|subscription|stripe|quota|plan_tier` 只命中 i18n 文案和研究文档）→ 云版本商业化前台是纯新建

### 1.4 与旧路线图的差异（旧文档没抓到的）

旧 `page-launch-roadmap.md` 把 16 页盘得很准，但它是**在单租户前提下**盘的。针对"要卖云版本"这个新目标，有 4 个**页面上看不见、但决定能不能卖**的阻塞（见 §4），旧文档一个都没提。

---

## 2. 客户旅程 × 页面地图

> 读法：**从左到右就是一个新客户的真实路径。** 任何一格是 ❌，客户就在那里掉队。

### 阶段 0 · 发现与获取（客户还不是客户）

| 页面 | 开源版 | 云版 | 现状 |
|---|---|---|---|
| 项目主页 / README | ✅ 必需 | — | 🟡 README 完整但**无 LICENSE 文件** → 法律上还不算开源 |
| 文档站 / 安装页 | ✅ 必需 | — | ❌ `docs/` 有 17 份内部文档，但无面向外部的安装入口 |
| 官网落地页 | — | ✅ 必需 | ❌ 全缺（`static_site/` 存在但未核为对外站） |
| 定价页 | — | ✅ 必需 | ❌ 全缺 |
| 注册页 | — | ✅ 必需 | ❌ 全缺 |

### 阶段 1 · 开通与首配（0→1，决定留存的一屏）

| 页面 | 现状 | 后端 | 说明 |
|---|---|---|---|
| `login` | ✅ 已上线 | Done | — |
| `forgot-password` / `reset-password` | ✅ 已上线 | Done | — |
| `onboarding`（首配向导） | ❌ 无模板 | ❌ 无 | **最关键缺口**：企微配置 → 密钥上传 → 首次同步验证，三步走不通就没有下文 |
| `onboarding-invite` | ❌ 无模板 | 🟡 invite 端点已有 | 端点在，页面没有 |
| `onboarding-done` | ❌ 无模板 | ❌ 无 | — |
| `tenant-provisioning`（超管开通） | ❌ 无模板 | ❌ 无 | 云版专属；无此页 = 每个客户手工跑脚本 |

> 今天的真实情况：新装一套系统，要求用户手动编辑 58 个环境变量、跑 `alembic upgrade head`、再跑 `bootstrap_default_tenant.py`。**这不是产品，这是一个需要工程师陪跑的项目。**

### 阶段 2 · 日常使用（证明价值）

| 页面 | 现状 | 后端 | 波次 |
|---|---|---|---|
| `dashboard`（总览首页） | ❌ 无模板 | ❌ 无聚合端点 | R3 — **销售演示的门面页** |
| `review_console`（对话审阅） | ✅ 已上线 | Done | R1 换肤 |
| `messages` / `message_detail` | ✅ 已上线 | Done | R1 换肤 |
| `search`（基础） | ✅ 已上线 | Done | R1 换肤 |
| `search-advanced`（筛选+导出） | ❌ 无模板 | 🟡 `/api/search/messages` 在，筛选/导出缺 | R3 |
| `media`（媒体与附件） | ❌ 无模板 | ✅ **端点已就绪** | **R1 快赢** |
| `contacts`（外部联系人） | ❌ 无模板 | ✅ **列表端点已就绪** | **R1 快赢** |
| `contact-detail` | ❌ 无模板 | ❌ 无单体详情端点 | R3 |
| `diagnostics` | ✅ 已上线 | Done | R1 换肤 |

### 阶段 3 · 管理与合规（决定续费——这是本产品的核心买点）

| 页面 | 现状 | 后端 | 波次 |
|---|---|---|---|
| `users`（员工与坐席） | ❌ 无模板 | ✅ **端点已就绪** | **R1 快赢** |
| `audit-log`（审计日志） | ❌ 无模板、**导航里连入口都没有** | ✅ **端点已就绪** | **R1 快赢（合规客户第一个问的页面）** |
| `settings`（整页） | 🟡 仅改密码卡 | ✅ 密码+偏好端点均就绪 | R2 |
| `analytics`（用量分析） | ❌ 无模板 | ❌ 无 | R3 |
| `exports`（导出记录） | ❌ 无模板 | ❌ 无 | R3+ |

### 阶段 4 · 平台运营（**仅云版需要**）

| 页面 | 现状 | 后端 | 波次 |
|---|---|---|---|
| `platform`（平台总控台） | ❌ 无模板 | 🟡 模型有、**零端点** | R5 |
| 账单 / 订阅页 | ❌ 无模板 | ❌ **零代码** | R5 |

### 页面总账

| 状态 | 数量 | 明细 |
|---|---|---|
| ✅ 已上线 | 8 | login / forgot / reset / review_console / messages / message_detail / search / diagnostics |
| 🟡 半截 | 1 | settings |
| 🔧 **后端就绪只差 UI** | 4 | **users / contacts / media / audit-log** |
| ❌ 需前后端全建 | 8 | dashboard / analytics / contact-detail / search-advanced / platform / tenant-provisioning / onboarding(+invite/done 算 1 组) |
| ❌ 云商业化新增 | 4 | 官网 / 定价 / 注册 / 账单 |

---

## 3. 路线图（R0–R5）

> 每一波的验收标准都写成"**客户能做到什么**"，不是"哪张票 Done 了"。

### ⚠️ 更正（2026-07-29 追加）：R0"发布管道冻结"的判断是错的

首版路线图曾把"生产界面一直没变化"的头号根因归为"发布管道被冻结，只能靠 RND-237 导出"。这个判断没有查 `.github/workflows/` 就下了结论，是错的——现已核实并撤回。

**实际情况**：`.github/workflows/deploy.yml` + `scripts/deploy_server.sh` 是一套已经在运行、相当成熟的 `main → 生产` 自动部署流水线：push 到 main 触发测试门禁（含 Alembic 迁移、schema-drift 检测）→ 通过后 SSH 到 ECS，用 `flock` 部署锁防止并发部署冲突，`git merge-base --is-ancestor` 校验 fast-forward，失败可回滚。**RND-237 本身也从来不是这个流水线的一部分**——它是一张单纯的"补 LICENSE / 公开 README / .gitignore 复核"票，与部署无关；已在 Linear 里更新了它的描述澄清这点，且保留在 RND-232 之下（本来就不需要拆分）。

真正的根因，从一开始就只有 §0 说的两条，且已在 Linear 里独立验证：**没人为已就绪的后端建过前端票**（RND-273/274 等 epic 明确写着"Scope（后端）"，Done 状态只代表后端做完，从未包含前端），以及**设计系统 CSS 从未接入生产模板**。R0 因此合并进 R1。

---

### R1 · 设计系统落地 + 四页快赢（目标 2–3 周）

**为什么排第一**：ROI 最高，且不依赖任何外部决策。四个页面的后端端点我逐个核实过，全部已存在，纯写前端。做完导航从"7 灰 4 亮"变成"3 灰 9 亮"，产品观感从"半成品"跳到"完整系统"。

**Linear**：Epic RND-325，前置任务 RND-326（设计系统落地），四页 RND-327（users）/ RND-328（audit-log）/ RND-329（media）/ RND-330（contacts）。存储计量修复 RND-331 也排进本波（见下方"计费口径提前"）。

**R1-a 设计系统接产**（一次性做完，之后所有新页面自动继承）
- `design/.../styles.css`（372 行）接入模板，替换旧 `base.css`
- `pages/shell.js`（91 行导航配置）转成 Jinja include `_sidenav.html` —— **今后加页面只改一个文件**
- 风险点：legacy 语义类（`.badge`、`.side-nav-*` 等）与新 `.sidenav-*` 命名不一致，需一轮全页视觉回归

**R1-b 四页快赢**（可四人并行，互不依赖）

| 页面 | 直接对接 | 附加动作 |
|---|---|---|
| `users` | `GET/PATCH /api/admin/users` + invite/accept | 导航去灰 |
| `audit-log` | `GET /api/admin/audit-logs` | **导航新增入口**（当前完全没有） |
| `media` | `GET /api/admin/media` | 导航去灰 |
| `contacts` | `GET /api/contacts` + `/api/search/contacts` | 导航去灰；详情页留 R3 |

> **验收：导航 12 项中 9 项可点击，全站统一新外观（含深色模式）。**

---

### R2 · 首配闭环 = 可以交给第一个外部客户（目标 3–4 周）

**这一波才是真正的"开源发布"。** 把代码推上 GitHub 不叫发布，**陌生人能装起来才叫发布。**

**Linear**：D2 KeyProvider 抽象 RND-332（**先于 onboarding 完成**，见下方"密钥托管提前"），RND-237（LICENSE/README/CLA/SDK 说明，已更新为 AGPL-3.0）。

- `onboarding` 向导三页：① 企微应用配置 ② 存档密钥上传（**必须走 RND-332 的 `KeyProvider` 接口，不能直接读文件路径**） ③ 首次同步验证（带实时进度与失败诊断）
- `onboarding-invite` / `onboarding-done`（invite 端点已存在，直接接）
- `settings` 补成整页：账户 + 偏好 + 密码（两个端点都已就绪）
- **补 LICENSE 文件**（当前仓库根目录没有 → 法律上不构成开源）
- **补 `docker-compose.yml`**（当前全仓库唯一的 Dockerfile 是 `ssl-renew/`，主应用没有容器化）
- 文档站/安装页：把 `docs/` 里 17 份内部文档收敛出一条对外的"30 分钟上手"路径
- 商标合规复核：`docs/trademark-checklist-zuozheng.md` 已有清单，开源前走完

> **验收：一个从没见过这个项目的人，`git clone` 后 30 分钟内看到自己企业的第一条真实消息。这一刻才叫开源发布。**

---

### R3 · Dashboard + Analytics = 可演示、可卖（目标 3 周，可与 R4 并行）

**为什么需要**：今天登录后第一屏是对话审阅列表——它对使用者友好，但**对决策者毫无说服力**。销售需要一页把"我们帮你归档了多少、覆盖率多少、合规风险在哪"讲完。

- `dashboard`：归档总量 / 今日增量 / 同步健康度 / 存储用量 / 待审阅 / 可达性告警。**需新建后端聚合端点（当前零）**，注意预聚合，别在首页跑全表扫描
- `analytics`：按时间/部门/员工的用量趋势
- `search-advanced`：在已上线的基础 search 上加筛选 + 导出
- `contact-detail`：需补单体详情端点

> **验收：销售只用 dashboard 一页，能讲完产品价值。**

---

### R4 · 多租户地基 —— 页面看不见，但决定云版能不能存在（目标 4–6 周，与 R3 并行）

**这一波是纯工程，没有新页面，但它是"卖云版本"的先决条件。** 详见 §4。做完之前，云版本一个客户都接不了。

- 密钥体系租户化（**当前最硬的阻塞**）
- `TenantWecomConfig.app_secret` 加密落库
- 同步 worker 从"env 单 corp"改为"按租户调度"
- 跨租户数据隔离的自动化测试（合规产品必须有，且是销售材料）

> **验收：两家不同 corp 的公司在同一套部署里各自同步、互相完全不可见，且有测试证明。**

---

### R5 · 云商业化前台（目标 4 周，依赖 R4）

- 官网落地页 + 定价页 + 注册页
- `tenant-provisioning`（超管开通页，接 R4 的租户化能力）
- `platform` 平台总控台（`PlatformAdmin` 模型已有，需从零建 router）
- 账单/订阅页 + 计费模型（**当前零代码**，需先定计费维度：按坐席数？按存储量？按消息条数？）

> **验收：客户自助注册 → 付费 → 开通 → 首配 → 看到消息，全程零人工介入。**

---

## 4. 四个隐形阻塞（页面上看不见，但直接决定"能不能卖云版"）

这四条是我读代码新发现的，旧路线图完全没有覆盖。**它们不解决，R5 做得再漂亮也接不了第二个客户。**

### 🔴 阻塞 1：密钥与解密路径是单租户硬编码（最严重）

- 解密路径读的是**环境变量** `WECOM_PRIVATE_KEY_PATH` + `WECOM_PUBLIC_KEY_VERSION`（[decrypt_wecom_messages_once.py:178](backend/scripts/decrypt_wecom_messages_once.py#L178)）——一套部署只能有一把私钥。
- `key_versions` 表虽然存在，但**解密路径根本不用它**，是一张事实上的死表；且 `publickey_ver` 是**全局唯一**（[models.py:332](backend/app/db/models.py#L332)）。每家企微 corp 的 `publickey_ver` 都从 1 开始 —— **第二个租户直接主键冲突。**
- `private_key_path` 存的是文件系统路径，云环境需要的是加密密钥托管。

> **影响：这是"卖云版本"的头号硬阻塞。** 会话存档私钥是客户最敏感的资产，托管它既是技术问题也是信任问题——需要有明确的密钥隔离方案，且这个方案本身就是销售材料的一部分。

### 🔴 阻塞 2：`app_secret` 明文存库

模型自己的注释写着 "Phase 1 stores plaintext (internal deployment only). Phase 3 must encrypt at rest"（[models.py:56-57](backend/app/db/models.py#L56)）。自用可以，**收费托管别人的企微凭据不行**。

### 🟠 阻塞 3：同步 worker 是 env 单 corp + systemd 单实例

`sync_wecom_archive_once.py` 从 `WECOM_CORP_ID` 环境变量解析租户（[sync_wecom_archive_once.py:88](backend/scripts/sync_wecom_archive_once.py#L88)），`deploy/systemd/` 里是一套定时器对应一套部署。云版需要按租户调度、隔离失败、单租户配额。

> 好消息：**数据层的多租户地基是真的**——`tenant_id` 在 13 个 router 里出现 133 次，`Tenant` / `TenantWecomConfig` / `PlatformAdmin` 模型齐全，`corp_id → tenant` 有唯一约束和应用层双保险。**要改的是凭据与调度层，不是数据模型。** 这让 R4 是"4–6 周"而不是"重写"。

### 🟠 阻塞 4：开源发布的三件硬缺失

- **无 LICENSE 文件** → 当前状态下别人法律上不能用
- **主应用无 Dockerfile / docker-compose** → 自托管门槛过高
- **Makefile 无发布相关 target** → RND-237 零代码

---

## 5. 关键决策与建议

### 建议 1：**先开源单租户版，云版本随后。不要等两者一起发。**

代码在密钥层是单租户构成的。硬把多租户塞进开源版，会让自托管用户白白承担复杂度——他们**只有一个 corp**。

> 开源版 = 单租户，一家公司一套部署，简单可审计。
> 云版本 = 多租户，是**你的**增值实现。这既是最省力的技术路径，也是最清晰的商业分界。

### 建议 2（已撤回）：~~R0 立刻做，且必须与开源准备解耦~~

**撤回**：核实 `.github/workflows/deploy.yml` 后确认发布管道本来就没冻结、RND-237 本来就不是部署闸门，这条建议的前提不成立，见上方 R0 更正说明。

### 建议 3：R1 四页建成**一个 epic 四张票**，不是四个独立 epic

它们共享同一套表格/筛选/分页组件和同一个 `_sidenav.html`。第一张票顺带把组件层做出来，后三张各自成本降一半。串行做第一张、后三张并行。

**已在 Linear 落地**：Epic RND-325 下，前置票 RND-326（设计系统）blocks 四张页面票 RND-327/328/329/330。

### 建议 4：R4 与 R3 并行，别串行

R4 是纯后端、无页面产出；R3 是纯前端 + 少量聚合端点。两条线人员不重叠。串行会白白多花 6 周。

### 建议 5：`audit-log` 优先级要提到 R1 最前

这是**合规采购客户第一个索要的页面**，后端已 100% 就绪，而今天它连导航入口都没有。对一个卖"合规存档"的产品，这是最不该缺的一页。

### 已拍板的三件事（2026-07-29）

| # | 决策 | 结论 | 被提前的工作 |
|---|---|---|---|
| 1 | 计费维度 | **按存储量付费** | 计量必须在 R1 就正确，不能等 R5 |
| 2 | 密钥托管 | **云版由我们托管客户私钥** | 密钥抽象层必须在 R2 就位，不能等 R4 |
| 3 | 开源许可证 | **AGPL-3.0** | R2 需补 CLA + SDK 获取说明 |

#### D1 → 提前到 R1：存储计量必须先可信

计费口径定义为 **`SUM(media_files.file_size) GROUP BY tenant_id`**。但 [models.py:607](backend/app/db/models.py#L607) 显示 `file_size` 是 **nullable** —— **不能拿一个允许为空的列去开发票**。必须在数据量继续增长前做完：

1. 回填历史 NULL（从存储 provider 取真实字节数）
2. 收紧为 `NOT NULL`
3. 建每租户存储用量的日滚动汇总表（首页和账单共用同一个数字，避免两处口径打架）

> 越晚做越贵：今天回填是一次脚本，一年后是一次跨 provider 的对账事故。

**已在 Linear 落地**：RND-331，排入 R1 · 前端快赢四页 milestone，无前置依赖，可立即开始。

#### D2 → 提前到 R2：onboarding 向导必须建在密钥抽象层之上

"我们托管客户私钥"是本产品**信任成本最高的一个承诺**，因此：

- R2 的 onboarding 第 2 步（密钥上传）**必须走 `KeyProvider` 接口**，两个实现：
  - `local_file` —— 开源自托管版，读本地 PEM（等价于今天的行为）
  - `kms_envelope` —— 云版，信封加密 + 托管 KMS，私钥永不落盘为明文
- **每一次解密都要写审计行**（`audit_logs` 后端已就绪，直接接）
- 密钥访问的隔离设计要写成对外安全说明 —— 托管密钥时，这份文档本身就是销售材料

> 如果 R2 的向导直接读文件路径，R4 就得把它整个重写一遍。加一层接口现在只多 1–2 天。

**已在 Linear 落地**：RND-332，排入 R2 · 开源发布闭环 milestone，`blocks` RND-269（首次配置向导 epic）——onboarding 向导第 2 步不能在此票之前上线。

#### D3 → R2 补两项

- **CLA（贡献者许可协议）**：AGPL + 自营云的标准配置。没有 CLA，外部贡献进来后你将失去单方面调整授权的能力。
- **SDK 获取说明**：`backend/vendor/wecom_sdk/` 是腾讯的专有 SDK（两个 tgz，共 11.8MB），已被 [.gitignore:221](.gitignore#L221) 排除 —— **AGPL 仓库不会误发腾讯二进制，这点是干净的**。但代价是：自托管用户必须自行从腾讯获取 SDK，Docker 镜像也不能内置。安装文档必须把这一步写清楚，否则 R2 的"30 分钟上手"验收会卡在这里。

**已在 Linear 落地**：两项都并入 RND-237 的清单（不新建独立票——都是同一次"开源包装" PR 里的文件），且已把 License 从"MIT 或 Apache-2.0，需 Haisu 定"更新为 AGPL-3.0。

---

## 6. 时间线概览

```
第 1–3 周    R1 设计系统 + 四页快赢 + 存储计量修复   ← 导航 9/12 点亮（RND-325~331）
第 4–7 周    R2 首配闭环 + KeyProvider + LICENSE/CLA ← ★ 开源正式发布（RND-332/237）
第 8–10 周   R3 Dashboard/Analytics ┐
第 8–13 周   R4 多租户地基          ┘ 并行
第 14–17 周  R5 云商业化前台                        ← ★ 云版本可售卖
```

**两个里程碑**：
- **第 7 周 —— 开源发布**：陌生人能装、能跑、能看到自己的数据。
- **第 17 周 —— 云版本可售**：客户自助注册到看到消息，全程无人工。

> R0（原"打通发布管道"）已撤销并入 R1——核实后发现 CI/CD 自动部署本来就是通的，详见 §3 R0 更正说明。整体时间线因此比首版提前约 1 周。
