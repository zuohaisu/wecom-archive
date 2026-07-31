# RND-224 执行提示词（开发 / 实现）

> 用途：粘贴给实现智能体（按 `DEV_AGENT_RULES.md` 的 Claude Code 实现角色），由其独立完成 RND-224。
> 验收由独立 QA 智能体按 `.workbuddy/prompts/rnd-224-qa-prompt.md` 复验。
> 本任务是 **RND-212 模块化重构链的收口票（capstone）**：把已经形成的模块边界**写成仓库规则 + 由测试/CI 强制**，并把消息、媒体、decrypted payload、worker 等 **source-of-truth 文档与当前实现对齐**（修正语义漂移）。**本任务基本不改产品代码/业务逻辑，主要产出「护栏测试 + 规则文档 + ADR」。**
> 定位（用户 2026-07-24 11:49 修订）：本护栏的目的是**防 token 泄漏 / 上下文膨胀**——一旦业务逻辑重新回流 `main.py`/万能 router，agent 的加载上下文又会膨胀。护栏就是防止重构成果退化。

---

## 0. 任务与来源

- **Linear 工单**：RND-224「建立架构防退化规则并更新 Source-of-Truth 文档」，父任务 RND-212，负责人 Haisu Zuo，团队 Builder（RND）。
- **在重构链中的位置**：链 `RND-218 → {RND-219, RND-220, RND-221} → RND-222 → RND-223 → RND-224`（前置还有 RND-216 前端外置、RND-217 review-console JS 模块化）。**RND-224 是整条链最后一票。**
- **硬依赖**：RND-223（引入 App Factory 与分域 Typed Settings）**blocks** RND-224。RND-224 codify 的是「最终架构形态」，因此**必须在整条链（含 RND-223）合入 `origin/main` 之后才开工**（见 §0.5）。
- **目标（工单原文）**：将模块边界写入仓库规则并由 CI 检查，防止 AI Agent 再次把业务逻辑堆入 main/router 万能模块，同时修正文档与当前实现的语义漂移。
- **范围（工单原文）**：AGENTS.md architecture rules；architecture/import-boundary tests；禁止 service → router 依赖；禁止在 main.py 新增 route、SQLAlchemy query、inline HTML/CSS/JS；更新消息、媒体、decrypted payload、worker 等 source-of-truth 文档；必要 ADR 模板。
- **非目标（工单原文）**：不做全仓库文档重写，不以机械行数阈值阻止合理代码。
- **验收标准（工单原文）**：CI 能检测关键反向依赖和 composition-root 违规；规则允许简单 CRUD 而不过度抽象；文档与当前能力一致；完整测试通过。

### 0.1 现状架构（实测，开工前请自行再核对一遍）

> 以下为提示词准备时（2026-07-25）的实测结论。链上多票尚未合入，**开工时以你 rebase 到的最新 `origin/main` 的真实结构为准**——尤其模块布局会随 RND-219/220/221/222/223 落地而变。

- **模块布局**：当前为**扁平布局**——**尚无 `app/services/` 目录**。「service 层」是一组扁平模块散落在 `backend/app/` 下。
  - Routers：`backend/app/routers/`：`auth.py`、`conversations.py`（最大，含 API + 内联 Pydantic 模型）、`messages.py`、`search.py`、`reachability_audit.py`、`web.py`、`wecom_events.py`。
  - Schemas：`backend/app/schemas/`：仅 `messages.py` + `__init__.py`（多数 Pydantic 定义仍内联在 `routers/conversations.py`）。
  - DB 层：`backend/app/db/`：`base.py`、`models.py`、`session.py`、`contacts.py`、`schema_check.py`。
  - 扁平「domain/service-like」模块（示例）：`media_storage.py`、`media_download.py`、`media_classification.py`、`media_thumbnails.py`、`qiniu_storage.py`、`thumbnail_pipeline.py`、`revoke_reconciliation.py`、`structured_message_parser.py`、`message_type_registry.py`、`conversation_membership.py`、`display_names.py`、`wecom_contacts.py`、`html_helpers.py`、`i18n_assets.py`、`sdk/wecom_sdk.py`。
  - **注意**：RND-219/220 提示词落点用扁平命名（`app/conversation_listing.py`、`app/conversation_timeline.py`、`app/conversation_schemas.py`），而 RND-221 提示词落点用 `app/services/media_access.py`。**最终布局可能是「扁平 + 部分 services/ 混合」**——你的护栏规则必须**按合入后的真实布局**定义「service/domain 层」，不要照搬本文假设。
- **`backend/app/main.py`（约 136 行）现状**：已**接近纯 composition root**。
  - 仅 3 个 `@app.get` 健康探针路由：`/health/live`、`/health/ready`、`/health`（无 `@app.post`）。其余路由全部经 `include_router(...)` 注册。
  - **无**内联 HTML/CSS/JS 字符串字面量（静态资源经 `app.mount("/web/static", ...)` 提供）。
  - **无**直接 SQLAlchemy 查询（`db.query(...)`/`select(...)`/`session.execute(...)`）；DB 访问委托给 `full_readiness_check(get_engine())` 与 router 的 `Depends(get_db)`。
  - RND-223 合入后，`main.py` 会演进为 `create_app()` 工厂 + 分域 typed settings；**你的 composition-root 检查要对准合入后的真实入口**（工厂函数所在模块 + 路由注册处），健康探针路由属**合法例外**，需加白名单。
- **反向依赖现状**：`app/services|扁平模块 → app/routers` 与 `app/routers/* → app.main` **当前均为 0 处**。即护栏是**预防性**的（codify 现有干净边界），不是修既有违规。
- **既有架构测试**：仅 `backend/tests/test_http_contract.py`（RND-214）做**结构快照**：`test_router_count`（== 33，随 RND-159/RND-229 演进）、`test_routers_are_registered`（33 条路径集合快照）、`test_route_snapshot_with_real_model_names`（逐路由 path/methods/response_model/response_class 快照）。**没有任何 import-boundary / layering / architecture 测试**——本任务新增之。
- **CI / 构建**：
  - CI = `.github/workflows/deploy.yml`，job「CI Test Suite」的「Offline / SQLite-compatible tests」步骤执行 `python -m pytest tests/ -q`——**任何新加到 `backend/tests/` 的测试都会被自动纳入 CI 门禁**。
  - `Makefile`：`verify = lint-diff typecheck build test`；`make test` 跑 `backend/tests` 全量。
  - **关键结论**：把护栏做成 `backend/tests/test_architecture_boundary.py`，即可满足工单「由 CI 检查」——**无需改 `deploy.yml`**（改 CI 配置是 DEV_AGENT_RULES 明令的越界项，且 RND-227 已冻结 CI）。见 §2.1 与 §3。
- **文档现状（source-of-truth 落点）**：
  - 消息类型：`docs/DATA_MODEL.md`（`msgtype` 列）+ 代码 `app/message_type_registry.py` / `app/structured_message_parser.py`。
  - 媒体：`docs/ops/media_storage_ops.md` + `docs/wecom_archive_media_download_runbook.md` + `docs/research/rnd_185_media_storage_abstraction.md`；代码 `app/media_storage.py` / `app/media_download.py`（RND-221 后新增 `services/media_access.py` / `routers/media.py`）。
  - decrypted payload：`docs/DATA_MODEL.md`（`decrypted_payload`/`decrypt_status`）+ `docs/adr/0001-conversation-domain-model.md` + `docs/ARCHITECTURE.md`。
  - worker：`docs/wecom_archive_worker_runbook.md` + `docs/ai/known-pitfalls.md`（sync→decrypt 顺序）；代码 `backend/scripts/{run_archive_worker_once,sync_wecom_archive_once,decrypt_wecom_messages_once,download_wecom_media_once}.py`（RND-222 后抽出可测 application functions + tenant scope）。
  - ADR：`docs/adr/` 目录**已存在**，含 `0001-conversation-domain-model.md`，**但无模板文件**。
- **AGENTS.md 归属（重要歧义，见 §0.6）**：仓库**根目录无 `AGENTS.md`**；只有 `docs/AGENTS.md`（仅 agent 角色/交接协议，L4 明说「Full working rules are in `DEV_AGENT_RULES.md`」）。agent 实际遵循的**约束规则**在**根目录 `DEV_AGENT_RULES.md`**（v3），其**当前没有「架构」章节**。

---

## 0.5 前置条件（硬门，先确认，不满足则停下）

> 任何一条不满足，**停下并在 Linear 评论说明依赖未满足，等待合并后再开工**，不要自行补做前置任务，也不要基于半成品链去写护栏。

1. **整条 RND-212 重构链必须已合入 `origin/main`**——至少 **RND-223（App Factory + typed settings）已 merge**（它 blocks 本票），且 RND-218/219/220/221/222 均已落地。理由：本票 codify 的是「重构完成后的最终边界」；若在链未完成时写死规则，规则会与半成品结构冲突，且无法准确对齐文档。用 `git log --oneline origin/main | grep -Ei "RND-21[89]|RND-22[0-3]"` 核对；缺任一票则停下报告。
2. **工作树干净**（`git status` 无未提交改动）。当前仓库历史上多次出现「上一票未提交改动残留在工作树」的情况——开工前确认没有他票的半成品混入。
3. **开工方式**：从最新 `origin/main` rebase，直接在 `main` 上实现（`DEV_AGENT_RULES.md`：不建 task branch，除非 Haisu 明确要求）。
4. **不改 CI 配置、不 commit/push**（见 §3、§6）。护栏用 pytest 落地即自动进 CI 门禁，无需动 `.github/`。
5. **本票与链上其他票不并发**：确保没有其他引擎正在改 `main`。

---

## 0.6 一个必须先确认的决策点：「AGENTS.md architecture rules」落到哪个文件

工单字面写「AGENTS.md architecture rules」，但仓库现实是：根目录无 `AGENTS.md`；`docs/AGENTS.md` 只是角色/交接；真正的**约束规则**在根目录 `DEV_AGENT_RULES.md`。**推荐默认方案（除非 Haisu 另有指示，按此执行并在 Linear 评论说明）**：

- **(A) 主规则写入 `DEV_AGENT_RULES.md`**：新增一节「Architecture Boundaries（架构边界规则）」，作为**具约束力的规则来源**（与既有 Secrets / Commit / Out-of-Scope 各节并列）。内容见 §2.2。
- **(B) `docs/AGENTS.md` 增补一小节「Architecture Boundaries」**：3–5 行摘要 + 指向 `DEV_AGENT_RULES.md` 的对应节与护栏测试文件，保持「roster 文档只做索引」的既有分工。
- **(C)（推荐但请在评论标注为可选）新建极简根级 `AGENTS.md`**：很多 agent 运行时（Codex/Claude 等）会**自动加载仓库根 `AGENTS.md`**。建一份 ~15 行的根 `AGENTS.md`，只做「入口指路」：指向 `DEV_AGENT_RULES.md`（约束规则）+ `docs/AGENTS.md`（角色）+ `backend/tests/test_architecture_boundary.py`（自动护栏）。这让「架构规则」对自动读取根 AGENTS.md 的 agent 直接可见，契合工单字面。
  - 若 Haisu 不想新增根文件，退化为只做 (A)+(B)，并在评论说明。

> 无论选哪种，**规则的「唯一可执行真源」是 §2.1 的护栏测试**（文档是给人读的说明，测试是给 CI 执行的强制）。文档与测试必须一致。

---

## 1. 精确范围（四类交付物）

1. **架构护栏测试**（唯一可执行真源）：新建 `backend/tests/test_architecture_boundary.py`（详见 §2.1）。
2. **架构规则文档**：`DEV_AGENT_RULES.md` 新增「Architecture Boundaries」节 + `docs/AGENTS.md` 摘要（+ 可选根 `AGENTS.md`）（详见 §2.2 与 §0.6）。
3. **Source-of-truth 文档对齐**：更新消息类型 / 媒体 / decrypted payload / worker 四域文档，使其与**合入后的最终实现**一致（详见 §2.3）。
4. **ADR 模板 + 一条边界 ADR**：新增 `docs/adr/0000-template.md`（模板）+ `docs/adr/0002-module-boundaries-and-composition-root.md`（记录本次边界决策）（详见 §2.4）。

---

## 2. 各交付物详细规格

### 2.1 架构护栏测试 `backend/tests/test_architecture_boundary.py`

**实现方式（硬约束）**：用**纯 Python 标准库 `ast`** 静态分析模块的 `import`，**不得引入第三方依赖**（如 `import-linter`、`pytest-arch`、`grimp`——违反 DEV_AGENT_RULES「不加大依赖」，且给护栏本身加依赖是反模式）。检查器写成**纯函数** + 对其做单测，既检查真实代码，也**证明护栏确实能检出违规**（工单验收「CI 能检测…违规」）。

把逻辑拆成两层：
- **纯检查函数**（对任意路径/源码字符串工作，可喂合成样本）：例如
  - `imported_modules(py_source: str) -> set[str]`：AST 解析出所有 `import x` / `from x import y` 的顶层模块路径。
  - `find_reverse_dependency_violations(app_root: Path) -> list[Violation]`：扫描全 `app/` 树，按 §2.1.1 规则判定。
  - `find_composition_root_violations(main_module_path: Path) -> list[Violation]`：按 §2.1.2 规则判定。
- **真实仓库断言**（`test_*` 函数）：对当前 `backend/app/` 调上述纯函数，断言违规列表为空。
- **护栏自证**（`test_*` 函数，关键）：对**合成源码字符串/临时目录**喂入「故意违规」样本，断言检查函数**能返回该违规**；再喂「合法」样本，断言**不误报**。正反两组都要有。

#### 2.1.1 反向依赖规则（禁 service → router、禁 router → main）

- **禁止 `app/routers/*` import `app.main`**（router 不得反向依赖 composition root）。
- **禁止「service/domain 层」import `app/routers/*`**（禁止 service → router 依赖）。
  - 「service/domain 层」定义要**按合入后的真实布局**确定，并在测试顶部用**显式常量集合**列出（便于人读、便于日后维护）：
    - 若存在 `app/services/` 目录 → 该目录全部模块属 service 层。
    - 扁平 domain 模块（如 `conversation_listing`、`conversation_timeline`、`conversation_membership`、`media_*`、`qiniu_storage`、`structured_message_parser`、`message_type_registry`、`display_names`、`revoke_reconciliation` 等）也属「不得依赖 router」的层。
    - **允许**：`routers → services/domain`、`routers → schemas`、`routers → db`、`services → db`、`services → 其他 service/domain`、`main → routers/services/db`（composition root 可俯视一切）。
  - 判定用「模块所属层」而非目录硬编码：写一个 `layer_of(module_path) -> {"main","router","service","schema","db","other"}` 分类器，规则表驱动，未知模块归 `other`（`other` 不参与 router 依赖禁令，避免误伤）。
- 允许 **narrow 白名单**（若合入后确有合理的、经 Haisu 认可的例外）：用测试内显式 `ALLOWED_EXCEPTIONS` 集合承载，每条附注释说明理由。默认应为空。

#### 2.1.2 composition-root 规则（禁 main.py 变回万能模块）

对**合入后的 composition root**（RND-223 后可能是 `create_app()` 所在模块 + `main.py`；以真实入口为准）断言：
- **禁止新增业务路由**：除健康探针白名单（`/health`、`/health/live`、`/health/ready`）外，composition root 内**不得出现** `@app.get`/`@app.post`/`@app.put`/`@app.delete`/`@app.patch`/`@router.*` 路由装饰器（AST 检出装饰器 + 调用名）。业务路由必须在 `app/routers/*`。
- **禁止直接 SQLAlchemy 查询**：composition root 内不得出现 `*.query(...)`、`select(...)`、`session.execute(...)`、`db.execute(...)` 等 ORM/Core 查询调用（AST 检出 `Call`/`Attribute`）。DB 只允许在 db 层与 service 层。
- **禁止内联 HTML/CSS/JS**：composition root 内不得出现内联前端标记字符串字面量（启发式：字符串常量中含 `<html`、`<!DOCTYPE`、`<style`、`<script`、`</div>` 等标记）。前端资源必须走 `web/templates` + `web/static`。
- **健康探针白名单**：把允许的健康路由路径写成显式常量，注释说明这是**唯一**合法例外。

#### 2.1.3 「允许简单 CRUD、不过度抽象」与「不设机械行数阈值」（工单硬要求）

- **绝不**加「文件行数 / 函数行数 / 路由体行数」阈值类断言（工单非目标明确「不以机械行数阈值阻止合理代码」）。护栏只管**依赖方向**与 **composition-root 纯度**，不管代码量。
- 规则表述要允许「router 里直接写简单查询/简单 CRUD」——**不强制**每个端点都拆 service。护栏**只**禁止「service→router 反向依赖」与「main 变万能」，不禁止「router 里有业务逻辑」。在测试 docstring 与规则文档里把这条写清楚，防止后人误加过度约束。

#### 2.1.4 失败信息可读

违规断言失败时，输出**清晰的**信息：哪个模块、import 了什么、违反哪条规则、应如何修（例如「`app/media_download.py` imports `app.routers.media` — service 层禁止依赖 router；把共享逻辑下沉到 service/domain 或 db 层」）。这是给未来触发护栏的 AI agent 看的。

### 2.2 架构规则文档（`DEV_AGENT_RULES.md` + `docs/AGENTS.md`，见 §0.6）

在 `DEV_AGENT_RULES.md` 新增一节「Architecture Boundaries」，用**人话**写清（要与 §2.1 测试逐条对应，措辞不得比测试更严或更松）：

- **分层与依赖方向**：`main（composition root） → routers → services/domain → db`；schemas 可被 routers/services 依赖。**禁止**任何 service/domain 模块 import `app.routers.*`；**禁止**任何 router import `app.main`。
- **composition root 纯度**：`main.py`/`create_app()` 只做装配（include_router、middleware、static mount、健康探针）。**禁止**在其中新增业务路由、直接写 SQLAlchemy 查询、内联 HTML/CSS/JS。
- **明确允许**：router 内可写简单查询 / 简单 CRUD，**无需**为每个端点强制拆 service；**不以行数**衡量「是否该拆」。拆分的唯一标准是**功能内聚**（一域 = 一 service + 接口），不是文件大小（呼应 2026-07-24 11:49「过度碎片化会反向增 token」）。
- **强制方式**：这些规则由 `backend/tests/test_architecture_boundary.py` 在 `make test` / CI「Offline/SQLite tests」门禁自动执行；违规会 CI 失败。
- **例外**：健康探针路由是 composition root 内唯一允许的路由；任何其他例外须经 Haisu 认可并记入测试的显式白名单 + 一条 ADR。

在 `docs/AGENTS.md` 增补「Architecture Boundaries」小节（3–5 行）+ 指向上面 `DEV_AGENT_RULES.md` 节与护栏测试文件。（可选根 `AGENTS.md` 见 §0.6。）

### 2.3 Source-of-truth 文档对齐（修正语义漂移，**只改四域，不做全仓库重写**）

对每一域：**先读代码确认当前真实行为，再改文档使其一致**。漂移主要来自本重构链把代码搬了位置——因此对齐重点是 **(a) 模块/文件路径引用更新到重构后新位置**，**(b) 行为描述与当前实现一致**。逐域最小改动，不重写无关段落。

1. **消息类型**：`docs/DATA_MODEL.md`（`msgtype` 定义/枚举）核对 `app/message_type_registry.py` 与 `app/structured_message_parser.py` 的**实际支持类型集合与解析行为**；补齐/修正差异（例如 RND-173/177/195/210 引入的新消息类型与卡片渲染，若已合入）。
2. **媒体**：`docs/ops/media_storage_ops.md` + `docs/wecom_archive_media_download_runbook.md` 核对 RND-221 后的 `app/services/media_access.py` / `app/routers/media.py`（授权先于 provider、top-level/nested 双 schema、Qiniu/local 混合、signed-URL/no-store 行为）与 `app/media_storage.py` 错误分类真源；更新文件路径与流程描述。
3. **decrypted payload**：`docs/DATA_MODEL.md`（`decrypted_payload` JSONB / `decrypt_status` 状态机）+ `docs/ARCHITECTURE.md`（sync→decrypt→payload→normalize 管线）核对当前实现；如与 `docs/adr/0001-conversation-domain-model.md` 有冲突，以代码为准修文档并交叉引用。
4. **worker**：`docs/wecom_archive_worker_runbook.md` + `docs/ai/known-pitfalls.md` 核对 RND-222 后 worker 的 application functions 结构、tenant/corp scope 契约、CLI/systemd 调用兼容、sync→decrypt 顺序；更新入口脚本路径与「一次运行的 tenant scope」说明。

> **非目标护栏**：不动 SSL、部署、PRD、research 等无关文档；不为对齐而重排/润色未漂移的段落。每处改动都应能对应到一个「文档说 X，代码实际是 Y」的具体差异。

### 2.4 ADR 模板 + 边界 ADR

- **新增 `docs/adr/0000-template.md`**：标准 ADR 模板（Title / Status / Context / Decision / Consequences / Alternatives considered / Links）。对齐现有 `docs/adr/0001-conversation-domain-model.md` 的风格。
- **新增 `docs/adr/0002-module-boundaries-and-composition-root.md`**：记录本次决策——
  - Context：RND-212 重构把业务逻辑从 `main.py`/万能 router 拆出；需防止 AI agent 再度把逻辑回流导致 token/上下文膨胀（引用 2026-07-24 11:49 动机修订）。
  - Decision：确立 §2.2 的分层与 composition-root 规则，由 §2.1 测试强制；明确「按功能内聚拆分、不以行数衡量、允许简单 CRUD」。
  - Consequences：CI 会挡住反向依赖/万能 main 回流；例外须白名单 + ADR。
  - Alternatives：import-linter/grimp（弃，避免加依赖）；纯文档约定无强制（弃，会漂移）。
  - Links：RND-224、RND-212、`test_architecture_boundary.py`、`DEV_AGENT_RULES.md` 相应节。
- 若 `docs/adr/` 有既定编号/索引惯例（检查是否有 README/index），沿用之；`0002` 若被占用则顺延。

---

## 3. 非目标（严禁）

- **不改 CI/CD 配置**：不碰 `.github/workflows/*`、`deploy/`、`scripts/deploy_server.sh`。护栏经 pytest 自动进 CI 门禁即可（§0.1、§2.1）。改 CI 是 DEV_AGENT_RULES 明令越界项 + RND-227 冻结区。
- **不引入第三方依赖**（尤其 import-linter/grimp/pytest-arch）；不改 `pyproject.toml` / `requirements*`。
- **不做全仓库文档重写**；只对齐 §2.3 四域的真实漂移，不润色无关段落。
- **不设机械行数阈值**、不强制「每端点必拆 service」、不为满足护栏而做无谓抽象（工单非目标）。
- **不改产品/业务代码行为**：本票原则上不动 `app/` 运行时逻辑。若对齐文档时发现代码真有 bug/漂移，**不在本票顺手改代码**——记录到 Linear 评论并建议单开票。
- 不引入 DB migration / schema 变更；不碰企业微信企业名变更、WeCom 回调逻辑。
- 不自行 `git commit` / `push` / 开 PR（见 §6）。
- 不在链未完成（RND-223 未合入）时开工（§0.5）。

---

## 4. 执行步骤（严格按顺序）

### 阶段一：确认前置与现状（不写规则前先摸清最终形态）
1. 核对 §0.5 硬门：`git log` 确认 RND-218～223 已在 `origin/main`；`git status` 干净。不满足则停下报告。
2. 摸清**合入后的真实布局**：`app/services/` 是否存在、有哪些 service/domain 模块、`main.py`/`create_app()` 最终形态、健康路由集合。据此确定 §2.1 的 `layer_of` 分类表与白名单。
3. 先跑 `make verify` 建基线，确认全绿（含 `test_http_contract.py` route_count）。

### 阶段二：写护栏测试（唯一可执行真源）
4. 新建 `backend/tests/test_architecture_boundary.py`：实现 §2.1 的纯检查函数 + 真实仓库断言 + **护栏自证（正反合成样本）**。
5. 跑 `cd backend && python -m pytest tests/test_architecture_boundary.py -q`，确认：真实仓库断言全绿（现状 0 违规）、自证用例证明能检出违规且不误报。
6. 若真实断言意外失败（说明合入后的链有你未预期的反向依赖），**不要为过测而放宽规则**——记录到 Linear 评论，判断是「链遗留问题」（报告 Haisu，可能需单开修复票）还是「规则定义需按真实合理布局调整」（调 `layer_of`/白名单并注明理由）。

### 阶段三：写规则文档 + ADR
7. 按 §0.6 选定方案更新 `DEV_AGENT_RULES.md`（新增 Architecture Boundaries 节）+ `docs/AGENTS.md`（摘要）（+ 可选根 `AGENTS.md`）。措辞与 §2.1 测试逐条对齐。
8. 新增 `docs/adr/0000-template.md` + `docs/adr/0002-module-boundaries-and-composition-root.md`（§2.4）。

### 阶段四：对齐 source-of-truth 文档
9. 逐域（消息/媒体/decrypted payload/worker）：先读代码确认真实行为 → 最小改动对齐文档路径与描述（§2.3）。每处改动记下「文档说 X / 代码是 Y」。

### 阶段五：收口自测
10. 跑 `cd backend && python -m pytest tests/test_architecture_boundary.py tests/test_http_contract.py -q`，确认护栏 + 契约全绿。
11. 收口跑 `make verify`（lint-diff + typecheck + build + 全量 pytest），确认全绿、无新 lint 告警、无新依赖。
12. `git diff --stat` 复核：改动应集中在 `backend/tests/test_architecture_boundary.py`、`DEV_AGENT_RULES.md`、`docs/AGENTS.md`（+ 可选根 `AGENTS.md`）、`docs/adr/0000-template.md`、`docs/adr/0002-*.md`、四域 source-of-truth 文档；**不应**出现 `.github/`、`pyproject.toml`、`requirements*`、`app/` 运行时代码、migration。

---

## 5. 兼容性 / 验收契约（不可违反，验收据此判定）

- **护栏可检测**：`test_architecture_boundary.py` 含正反自证用例，证明能检出「service→router」「router→main」「main 新增 route/SQL/inline HTML」违规，且对合法样本不误报。
- **现状零违规**：对当前 `backend/app/` 的真实断言全绿（护栏是预防性的）。
- **route_count 不变**：`test_http_contract.py` 的 `test_router_count`（== 33，或合入链后的最新期望值）不被本票改变——本票不加/不删路由。
- **允许简单 CRUD / 无行数阈值**：护栏内无任何行数类断言；规则文档明确允许 router 内简单查询。
- **文档与实现一致**：四域文档路径与行为描述与合入后代码一致；无残留指向旧位置（如 media 逻辑仍写在 `conversations.py`）的描述。
- **无 CI 变更 / 无新依赖 / 无 migration / 无运行时代码变更**：`git diff` 可证。
- **`make verify` 全绿**。

---

## 6. 硬性约束（实现 agent 自身也要守）

- **护栏用标准库 `ast`，不加第三方依赖**；不改 CI 配置（pytest 自动进门禁）。
- **规则文档措辞 = 护栏测试语义**：不得比测试更严（会误伤）或更松（会漏防）。文档与测试是同一套规则的两种表述。
- **不为过测放宽规则**：真实断言失败先判因，别改护栏迁就半成品。
- **只 codify、不改行为**：不动 `app/` 运行时逻辑；发现代码问题记录不顺手修。
- 不引入后台进程；假设开发服务器已在运行；命令前台运行。
- 参考 `DEV_AGENT_RULES.md` 的 AI agent 工作流；**不要自行 `git commit`/`push`**（本项目铁律：一切提交/推送由 Haisu 本人操作，即便 QA 通过、即便被告知「可以提交」也不代劳，只提示用户自行 commit/push）。
- 必须满足 §0.5 前置硬门才可开工。

---

## 7. 收尾动作

- 在 Linear 把 RND-224 状态置 In Progress（如尚未）。
- 写一条评论说明：护栏测试落点与覆盖的规则、正反自证结论、`layer_of` 分层与白名单（及任何例外理由）、规则文档落到哪个文件（§0.6 的 A/B/C 选择及原因）、四域文档各自「文档说 X→改为 Y」的差异清单、ADR 模板与 `0002` 边界 ADR、`make verify` 结论、route_count 是否保持、以及**确认零 CI/依赖/migration/运行时代码变更**。
- 若发现链遗留的真实反向依赖或代码 bug，如实记录并建议单开修复票，**不在本票顺手改**。
- 不自动合入/提交；交还 Haisu 决策是否 commit/merge（本票为 RND-212 链收口票）。