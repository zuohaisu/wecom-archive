# Ticket Autopilot 工作流（本项目适配版）

> ## ⚠️ 已停用（2026-08-28）
>
> **本文档描述的 Ticket Autopilot 流程当前不再使用。** 现行流程是：dev 角色 agent
> 实现完成后**自行 QA**（跑 `make verify`、写 QA Summary），推交付分支开 PR，**由
> required CI 作为判定闸**；CI 红则打回同一个 dev 会话修复。不再生成
> dev/qa 提示词对，不再有独立 QA agent 与 `qa-verdict.json`。
>
> 本文档保留为休眠参考（"暂时用不上"，非作废），因为 `tasks/` 下大量历史提示词引用
> 它。**其中两条与 autopilot 无关、至今仍然成立的项目不变量已上收至 `AGENTS.md`：**
>
> - 原 §3.3「新增路由必须同步两个契约测试」→ `AGENTS.md` § Test integrity →
>   *The one legitimate test change: route contract tests*
> - 原 §3.4「`make verify` 必须在本票交付 worktree 中运行」→ `AGENTS.md` § Required checks
> - 原 §8「`tasks/` 目录约定」→ `AGENTS.md` § Ticket artifacts（产物清单已按新流程改写）
>
> **与 `AGENTS.md` 冲突时一律以 `AGENTS.md` 为准。** 恢复本流程前请先与 Haisu 确认。

---

> 移植自 `AI-Operations` 项目的 Ticket Autopilot v0.1，按本项目的 `AGENTS.md` 适配。
> 目的：把每张 GitHub Issue 工单自动跑成「已通过确定性验证 + 独立 AI QA」的单一可追踪 commit，再把一个或多个相关工单 commit 组成 required CI 通过的可合并 PR。
> 建立：2026-07-29 ｜ 权威操作定义，与 `AGENTS.md` 冲突时以 `AGENTS.md` 为准。

---

## 1. 闭环

```
GitHub Issue [RND-<n>]（九字段合同齐全）
  → 人工置 In Progress（唯一启动信号）
  → 进入已分配的非 main 交付 worktree/分支（新建时基于最新 origin/main）
  → 开发 agent 执行 dev-prompt（当前工单未经批准禁 commit）
  → 确定性验证闸：make verify 全绿
  → 独立 QA agent 执行 qa-prompt（只读，不改任何文件）
  → 产出 qa-verdict.json（PASS / FAIL / BLOCKED）
  → FAIL 则有界修复（最多 2 轮，只修 findings）
  → Haisu 人工 review → 批准本工单的唯一 commit
  → 可选：同一 Epic 的下一张相关工单在此 worktree 串行重复上述闭环
  → 批准 push 交付分支 → 创建/更新 PR（列出工单↔commit 映射）
  → required CI 全绿 → 保留各工单 commit，人工 merge main
  → deployable paths 的 merge 触发 CD（纯 docs/tasks merge 不部署）
```

## 2. 项目适配（重要）

| 维度 | AI-Operations 原版 | 本项目 | 原因 |
|---|---|---|---|
| 隔离 | 每个 Run 独立 git worktree + `agent/*` 分支 | **交付 worktree/分支可承载一张或同 Epic 多张相关工单；同一 worktree 内严格串行** | 一票仍对应一个 commit；并行工单使用不同 worktree，避免未提交改动互相污染。 |
| PR | Controller 自建 PR，人工 merge | **Haisu 批准 commit/push；PR required CI；人工 merge** | 保留 owner gate，同时禁止直推 `main`。 |
| 验证闸 | `pytest` | **`make verify`**（lint-diff → typecheck → build → test） | 本项目既有的复合验收入口，见 `Makefile`。 |

其余原则原样保留：**确定性动作不委托 LLM**、**只依据证据推进状态**、**R0/R1 自动执行，R2/R3 转人工（`BLOCKED_NEEDS_HUMAN`）**、**一次性 Run，不实现 Resume**。

## 3. Worktree 隔离与文件所有权协议

Worktree/branch 是交付容器，不是工单身份。它可以只承载一张工单，也可以承载一个
同 Epic 交付批次中的多张相关工单；每张工单仍必须有且只有一个最终 commit，且单个
commit 不得混票。同一 worktree 内的工单必须串行：上一票完成 QA 并形成获批 commit
后，下一票才能开始，任何时刻不得同时存在多票的未提交改动。真正并行执行的工单
必须使用不同 worktree/branch。

文件所有权仍用于控制 scope、发现跨票依赖并降低 PR 合并冲突：任意两张计划并行
执行或 merge 的工单，其写入文件集合原则上应无交集；有交集时必须显式串行或记录
交接顺序。

### 3.1 R1 波次的所有权矩阵

RND-326 是**唯一**允许写共享文件的工单，它一次性把 4 个页面所需的公共骨架全部预建好；随后 4 张页面工单只写各自独占的文件，因此可 4 路并行。

> ⚠️ **排序约束：RND-324 必须先于 RND-326 完成并合入。**
> RND-324 修 `review_console.html:69` 的 `.lang-menu` 样式；而语言切换 markup 位于第 375–377 行，在 `<nav class="side-nav">`（~350）到 `</nav>`（382）**内部**。RND-326 会把整段 `<nav>` 换成 `__SIDENAV__` 并由 `render_sidenav()` 重新生成 —— **会连带重写语言切换 markup，静默覆盖 RND-324 的修复**，且因为是纯视觉问题，没有任何测试能发现。
> 处理：先合 RND-324；RND-326 以合入后的文件为基线，并在 `render_sidenav()` 输出中保留左对齐修复。

| 文件 | 写入所有者 | 其他工单 |
|---|---|---|
| `backend/app/web/templates/review_console.html` — `.lang-menu` 样式块 | **RND-324**（须先合入） | 只读 |
| `backend/app/web/sidenav.py`（新建，含全部 4 个导航项） | **RND-326** | 只读 |
| `backend/app/web/templates/review_console.html`（`.lang-menu` 之外的其余部分，含 `<nav>` → `__SIDENAV__`） | **RND-326** | 只读 |
| `backend/app/web/static/design-system.css`（新建，设计系统落地；**不是** `styles.css`——那个名字已被现存 login/forgot/reset/settings 用了） | **RND-326** | 只读 |
| `backend/app/main.py`（include 4 个 stub router） | **RND-326** | 只读 |
| `backend/app/assets/i18n.js` — 全部 `nav.*` 键 + 4 个锚点注释 | **RND-326** | 只读 |
| `backend/app/routers/web.py`（2026-07-29 追加，仅限 `render_template("review_console", ...)` 调用新增 `sidenav=` 一个参数） | **RND-326** | 只读 |
| `backend/tests/_rnd216_web_shims.py`（2026-07-29 追加，仅限 `review_console_html()` 同样新增 `sidenav=` 一个参数） | **RND-326** | 只读 |
| `backend/app/assets/i18n.js` — `users.*` 键（在 RND-327 锚点下） | RND-327 | 仅限自己锚点区间 |
| `backend/app/assets/i18n.js` — `audit.*` 键（在 RND-328 锚点下） | RND-328 | 仅限自己锚点区间 |
| `backend/app/assets/i18n.js` — `media.*` 键（在 RND-329 锚点下） | RND-329 | 仅限自己锚点区间 |
| `backend/app/assets/i18n.js` — `contacts.*` 键（在 RND-330 锚点下） | RND-330 | 仅限自己锚点区间 |
| `backend/app/routers/admin_users_page.py` | RND-327 | 只读 |
| `backend/app/routers/admin_audit_page.py` | RND-328 | 只读 |
| `backend/app/routers/admin_media_page.py` | RND-329 | 只读 |
| `backend/app/routers/admin_contacts_page.py` | RND-330 | 只读 |
| `templates/users.html` / `audit_log.html` / `media.html` / `contacts.html` | 各自工单 | 只读 |
| `backend/tests/test_*_page.py` | 各自工单 | 只读 |
| `backend/app/db/models.py` + 新 migration | RND-331 | 只读 |
| `backend/app/routers/media.py`（下载端点审计钩子） | RND-292 | 只读 |

### 3.2 硬规则

- **只写自己拥有的文件。** 若实现过程中发现必须改他人拥有的文件 → 停止，标记 `BLOCKED_NEEDS_HUMAN`，说明原因。不要「顺手改一下」。
- **i18n 锚点纪律：** RND-326 会在 `i18n.js` 的 3 个 locale 块（`zh-CN` / `zh-TW` / `en`）里各插入 4 行锚点注释，形如 `/* RND-327 users page keys — insert below */`。页面工单**只在自己的锚点正下方插入**，不得改动锚点本身、不得在他人锚点区间内写入。这样 4 个 agent 对同一文件的编辑区间互不重叠。
- **导航自动点亮：** `sidenav.py` 按「路由是否已注册」决定导航项渲染成链接还是灰色占位（见 RND-326 AC-4）。页面工单注册自己的路由后，导航项**自动**变为可点击 —— 页面工单因此完全不需要碰 `sidenav.py`。
- **拆所有权时要找到"实际调用点"，不只是"内容所在的文件"（2026-07-29 实例）：** 给 RND-326 划所有权清单时，只列了 `review_console.html`（模板文件本身），漏了真正调用 `render_template("review_console", ...)` 的两处代码——`backend/app/routers/web.py`（生产路由）与 `backend/tests/_rnd216_web_shims.py`（测试 shim）。模板加了 `__SIDENAV__` token 后，这两处不跟着传 `sidenav=` 参数就会 `KeyError`。开发 agent 正确地停在 `BLOCKED_NEEDS_HUMAN`，而不是猜测着去改清单外的文件——**这是设计里的期望行为**，说明"文件所有权錯峰"本身没问题，只是这次划分时漏看了模板与其调用点之间的间接依赖。已授权补齐，改动严格限定为各新增一行 `sidenav=render_sidenav(...)`。以后拆所有权前，对任何"新增模板变量"类工单，先 `grep -rn 'render_template("<模板名>"' backend/` 把全部调用点找全，再定清单。

### 3.3 ⚠️ 全项目不变量：新增路由必须同步两个契约测试

**这不是可选项，是数学上的必然。** 任何新增路由的工单，**必须**在自己的所有权清单里包含下面两个文件（**范围严格限定为下述最小改动**），否则 `make verify` 必然红，且不是实现的问题：

| 文件 | 为什么必须改 | 允许的改动范围 |
|---|---|---|
| `backend/tests/test_http_contract.py` | 第 326 行 `assert route_count == <N>` 是**硬编码基线**；每新增一个路由必然对不上。另有 expected path 集合与 snapshot 列表两处需同步。 | 只改 `route_count` 数值（**读当前真实值 N，改 N+本票新增数**，禁止写死具体数字）+ 在 expected/snapshot 里追加本票的新路由条目。**不得**删改他票的条目。 |
| `backend/tests/test_rnd280_rbac_scaffold.py` | 第 77-86 行 `test_require_role_is_attached_only_to_authorized_admin_routes` 是**闭世界白名单**：它遍历 `app/routers/*.py`，对不在 `{audit.py, media_library.py, users.py}` 名单里的文件断言 `"Depends(require_role" not in source`。**任何新 router 只要用了 `require_role` 就必然失败。** | 只在白名单里加本票的 router 文件名 + 对应断言。**不得**放松或删除既有条目的断言。 |

**判定纪律：**
- 新增路由但**没改** `route_count` → 是实现不完整，判 **FAIL（`REGRESSION`）**，不是"契约测试该由别人维护"。
- 契约测试更新**不可以**拆成独立工单——那会让 `main` 在两张票之间持续处于红灯状态，违反「main 始终可部署」。它是新增路由的**强制随附改动**。
- 只用 `require_html_session` 的页面路由（如 4 张 R1 页面票）**不触发** RBAC 白名单，但**仍然触发** `route_count`。

> **沿革（2026-07-29）**：RND-288 首轮 QA 因此判 FAIL 并建议"另开契约维护票"——**该建议是错的**（会让 main 长期红灯），但 QA 判 FAIL 本身是对的（`make verify` 确实红）。根因是本项目自己写的提示词漏了这条随附改动；反观更早的 WorkBuddy 提示词（如 `tasks/RND-311-dev-prompt.md` §2「契约测试三处同步」）**本来就写对了**，是新体系没继承这个约定。已回填进本节与 `tasks/_templates/dev-prompt-template.md`。

### 3.4 ⚠️ `make verify` 必须在分配给本票的交付 worktree 中运行

若该 worktree 只承载本票，分支基线通常是 `origin/main`。若它承载同 Epic 的多票，
HEAD 可以包含之前已通过 QA 且获批的工单 commit，但 working tree 只能包含当前本票
的未提交改动。`make verify` 验证的是当前交付分支的整体集成状态。

QA 开始时必须确认当前分支不是 `main`，并用 `git status`、`git diff` 与
`git log origin/main..HEAD` 分离「当前本票 diff」和「更早的获批工单 commit」。当前
本票已提交时，改用 `git show <本票-commit>` 核对范围。若多票未提交改动混在一起、
分支含未获批 commit，或当前票出现所有权清单外改动，判
`BLOCKED_NEEDS_HUMAN`，不得把它们混作本票交付。

## 4. 风险分级与自动化边界

| 等级 | 含义 | 处理 |
|---|---|---|
| R0 | 纯文档 / 注释 | 自动执行 |
| R1 | 小而隔离、可逆的代码改动（本波次绝大多数） | 自动执行 |
| R2 | 触及鉴权、迁移、跨模块契约 | 转人工确认后执行 |
| R3 | 触及生产数据 / 密钥 / CI / 部署 | 必须由 Haisu 明确批准范围；未批准即 `BLOCKED_NEEDS_HUMAN`。批准修改仓库配置不等于批准生产操作。 |

`RND-331`（含 Alembic migration + NOT NULL 收紧）按 **R2** 处理：迁移脚本需 Haisu 审阅后才可执行。

## 5. 强制 Goal check

每个 agent 的工作输出，第一行非空内容必须是：

```text
[Goal check] This work advances <闭环阶段> by <可测量证据>.
```

可用以下命令回溯校验任一产出文件：

```bash
artifact=path/to/work-update.md
awk 'NF {print; exit}' "$artifact" | grep -Ex '\[Goal check\] This work advances .+ by .+[.。]'
```

退出码 0 才算合规。这是审计约定，不是 CI 闸。

> ⚠️ 句末标点必须同时接受 ASCII `.` 与全角 `。` —— 本项目提示词以中文书写，句号是 `。`。原版 AI-Operations 的正则只写了 `\.`，直接套用会把所有中文 Goal check 判为不合规（本仓库首次跑批校验时 14 个文件误报 12 个）。

批量校验全部提示词：

```bash
for f in tasks/RND-*.md; do
  awk 'NF {print; exit}' "$f" | grep -Eq '\[Goal check\] This work advances .+ by .+[.。]' \
    && echo "OK   $f" || echo "FAIL $f"
done
```

## 6. 确定性验证闸（本项目唯一权威）

```bash
make verify          # = lint-diff → typecheck → build → test，任一失败即停
```

补充命令：

```bash
make test                                    # 仅 pytest 全量
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q   # 架构边界硬闸
.venv/bin/python -m pytest backend/tests/<本工单新增测试>.py -q
```

**`test_architecture_boundary.py` 失败 = 硬停止**，不得绕过（见 `AGENTS.md`）。

## 7. 独立 QA 与有界修复

- **qa 角色 agent**（见 `AGENTS.md` 的 Roles）**只读**：可读文件、可跑只读命令，**不得**改任何文件、不得 commit、不得放松 AC。
- 产出 `tasks/RND-<n>-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。
- `verdict: FAIL` → 开发 agent **只修 findings**，不扩大范围，最多 2 轮。2 轮仍 FAIL → `BLOCKED_NEEDS_HUMAN`。
- QA agent 不得替开发 agent 补做缺失的实现或测试。

## 8. 目录约定

**`tasks/` 是开发提示词、验收提示词、QA 判定与 QA 报告的唯一归属地。** 不得在仓库其它位置（根目录、`.workbuddy/`、`deliverables/`）另建提示词或 QA 产物。

```
tasks/
├── _templates/
│   ├── dev-prompt-template.md      # 开发提示词模板（九字段合同）
│   ├── qa-prompt-template.md       # 验收提示词模板
│   └── qa-verdict.schema.json      # QA 判定输出 schema
├── RND-246-dev-prompt.md           # ← 仅「未完成工单」留在 tasks/ 根
├── RND-246-qa-prompt.md
└── archive/                        # ← 工单 Done/Canceled 后整组下沉至此
    ├── RND-326-dev-prompt.md
    ├── RND-326-qa-prompt.md
    ├── RND-326-qa-verdict.json     # QA 运行后产出
    ├── RND-316-qa-report.md        # QA 叙述性报告（如有）
    └── ...
```

**命名规范（强制）**：`RND-<n>-dev-prompt.md` / `RND-<n>-qa-prompt.md` / `RND-<n>-qa-verdict.json` / `RND-<n>-qa-report.md`。前缀大写 `RND-`。同一工单有多份开发提示词时用后缀区分，如 `RND-229-dev-prompt-search-pagination-flake.md`。

**归档规则**：工单在 Linear 置为 Done 或 Canceled 后，把该工单的**全部**文件（dev/qa prompt + verdict + report）一次性 `git mv` 到 `tasks/archive/`。`tasks/` 根目录始终只剩「还要用的」，一眼可见待办面。归档只搬文件、不改内容，仅修正跨文件引用路径。

### 8.1 双产线合并收口（2026-07-29 起，2026-07-31 完成）

历史上存在两套提示词产线：本文档定义的 `tasks/`，以及先于本文档存在的 `.workbuddy/prompts/`（由 WorkBuddy 自动化每日读 Linear、写「执行提示词 / QA 提示词」、派发 agent，完成后移进 `.workbuddy/prompts/archive/`；命名为小写 `rnd-<n>-execution-prompt.md`，验收结果写回 Linear 评论、不落 `qa-verdict.json`）。

- **2026-07-29**：把 `.workbuddy/prompts/` 中 11 张仍开放工单（22 个文件）迁入 `tasks/`，改名为 `RND-<n>-dev-prompt.md` / `RND-<n>-qa-prompt.md`。已归档的 108 个 Done 工单当时留在原处。
- **2026-07-31（收口）**：`.workbuddy/prompts/` **已整个撤销并删除**。其 `archive/` 下 108 个文件全部 `git mv` 到 `tasks/archive/`，并统一改名为大写 `RND-` 前缀、`-execution-prompt.md` → `-dev-prompt.md`；同时把仓库根目录散落的 4 份 QA 报告（`rnd-172` / `rnd-295` / `rnd-297` / `RND-316`）也收进 `tasks/archive/`。合计归档 200 个文件，`tasks/` 根仅保留 14 张未完成工单的 29 个文件。

**自此仓库内只有一套产线、一个目录。** 任何 agent 或自动化若仍按 `.workbuddy/prompts/**` 旧路径读写，都是错的——该目录不再存在，写入会重新制造分叉。**行动项（Haisu）**：在 WorkBuddy 侧确认自动化的提示词产出路径已指向 `tasks/`。

## 9. 明确不做（v0.1 边界）

不含：resume、webhook 触发、并行 Run 编排器、自动 merge、自动迁移、自动挑下一张票。
每票唯一 commit、push 交付分支、创建/合并 PR 均由 Haisu 授权或人工执行；Autopilot
本身不直接触发部署。多票 PR 必须列出工单与 commit 的一一映射，并使用保留各工单
commit 的 merge 策略，禁止 squash 成一个 commit。PR merge 到 `main` 且包含
deployable paths 时由仓库 CD 自动部署；纯 docs/tasks merge 不部署。凭证只从环境
变量读取，永不写入任何配置文件或提示词。
