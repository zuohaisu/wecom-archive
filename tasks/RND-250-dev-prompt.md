[Goal check] This work advances 开发（Development） by 交付配置中心的权限守卫与首次初始化引导，这是 R2 里程碑验收标准（陌生人 clone 后 30 分钟看到第一条真实消息）的直接实现路径。

# RND-250 开发提示词（Developer Prompt）— 配置中心 T6

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-250 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 本票是 R2 里程碑验收标准的关键路径，也是本 epic 风险最高的一票

R2（开源发布闭环）的验收标准是"陌生人 `git clone` 后 30 分钟内看到自己企业的第一条真实消息"。当前唯一能完成这件事的登录路径是 `POST /api/auth/password/login`（`app/routers/auth.py:565`，自己的 docstring 写着"Temporary username/password login"），它**直接从环境变量**读 `ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH`（`auth_settings.admin_username`/`admin_password_hash`），今天陌生人 clone 后必须**手动编辑 `.env`** 才能设置这两个值——这正是本票要解决的"无引导，必须手改源码/.env"问题。

**本票会触及 `app/routers/auth.py`——全站鉴权入口之一，登录路径改错会导致所有人登不进去。极度谨慎，改动必须最小化、可回滚。**

## 开工前必须先核实的前置条件
```bash
.venv/bin/python -c "from app.config.resolver import resolve, get_config_resolver, invalidate; from app.config.repository import upsert; from app.routers.settings import settings_router; print('OK')"
```
T4（<issue>RND-248</issue>）+ T5（<issue>RND-249</issue>）未就绪 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 任务身份
- 工单：RND-250「配置中心 T6：权限守卫 + 首次初始化引导」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R3**（触及全站登录入口，是本 epic 单票风险最高的一张）｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实）

**`initialized` 判定（冻结设计 G3）**：
```
(AUTH_MODE=password 且 ADMIN_PASSWORD_HASH 非空) 或 (AUTH_MODE=wecom 且 WECOM_CORP_ID/AGENT_ID/OAUTH_SECRET 均非空)
```
判定时**读取顺序同样是 DB > env**（即：`admin_password_hash`/企微三件套的当前值，用本票即将扩展的 resolver 读取路径来判断，不是只看裸 env）。

**`require_settings_admin`（冻结设计 G4）= 已认证会话**（MVP 不加 `is_admin` 列——`get_current_user`/`require_html_session` 的既有会话即满足条件，本票只是给这个语义起一个专用的依赖函数名，内部实现可以直接委托给既有的 `get_current_user`）。

**Bootstrap 端点未鉴权的例外情况**：`bootstrap-status`/`bootstrap` 端点在**未初始化**状态下必须**公开可访问**（没有任何账号能登录，无法要求先登录才能访问引导页——这是先有鸡还是先有蛋的问题，唯一的门禁是`initialized`本身：一旦已初始化，这两个端点直接 403，不管有没有登录）。

**`password_login` 需要的最小改动（谨慎）**：为了让 bootstrap 设置的账号密码真正在登录时生效，`admin_username`/`admin_password_hash` 的读取需要改成"**先查 config 中心的 DB 值，没有则退回现有的纯 env 读取**"——这是**唯一允许触及** `app/routers/auth.py` **的改动**，且必须是**纯新增的 DB-first 回退分支**，现有的纯 env 部署（从未跑过 bootstrap，DB 里没有这两个 key）行为必须**逐字保持不变**。

**❗ 本项目高频踩坑：**
- **`app/routers/auth.py` 是全站鉴权入口**：本票只允许改 `password_login` 函数体内读取 `admin_username`/`admin_password_hash` 的那两行，**不得**触碰会话创建、cookie 设置、密码校验（`verify_password`）等任何其他逻辑。
- **企微 OAuth 登录路径不受本票影响**：`AUTH_MODE=wecom` 时走的是完全不同的函数，本票不touch。
- 架构冻结 D1：`GET /settings/init` 是 web 外壳路由（HTML 页面），不要在这个票里做前端表单的完整交互（那部分复用 T8/T9 的组件即可，本票只负责路由 + 最小可用的 HTML/表单骨架，能跑通"设管理员账号 + 补企微三件套"两步）。

## 目标（Goal）
交付"是否已初始化"的判定逻辑 + 首次引导端点（未初始化时设管理员账号 + 补齐 P0 必填配置）+ 让引导设置的值在登录时真正生效，且已初始化的部署不受任何影响。

## 范围边界

**In scope：**
1. `backend/app/config/guard.py`（**新建**，避免直接塞进 `auth.py` 增加其复杂度）：
   - `def is_initialized(db: Session) -> bool`：按 G3 判定逻辑，读取顺序走 config resolver。
   - `def require_settings_admin(...)`：委托给既有 `get_current_user`/`require_html_session`（G4：无额外角色概念）。
2. `backend/app/routers/settings.py`（**追加**，同 T5 的共享文件约束）：
   - `GET /settings/bootstrap-status`：**无需鉴权**，返回 `{"initialized": bool}`。
   - `POST /settings/bootstrap`：**仅 `initialized==false` 时可用**，已初始化 → 403。请求体含管理员账号/密码 + 可选的企微三件套；账号密码经 `hash_password`（复用 `app/auth.py` 既有函数）后，**通过 T4 `repository.upsert()` 直接写入** `admin_username`/`admin_password_hash` 两个 key（**这两个 key 不在 T3 的 `CONFIG_REGISTRY` 里，是本票的特例写入路径，不走 T5 的 `PUT /settings` 校验通道**）；企微三件套如提供，走正常的 `CONFIG_REGISTRY` 校验+落库路径（可直接调用 T5 已有的写入逻辑复用，不要重复实现一遍字段校验）。
3. `backend/app/routers/web.py`（**追加**）：`GET /admin/settings/init`——未初始化时渲染一个最小可用的引导表单页（可复用 T8/<issue>RND-251</issue> 已有的 CSS class，不需要新设计一套视觉风格）；已初始化访问该路径 → 重定向到 `/admin/login`。
4. `backend/app/routers/auth.py`（**极小改动，谨慎**）：`password_login` 函数内，`admin_username`/`admin_hash` 的赋值那两行，改为"先用 config resolver 查 `admin_username`/`admin_password_hash`（走 DB>env 顺序），取不到（两者均为空）才退回当前的 `auth_settings.admin_username`/`admin_password_hash`"——**函数其余部分逐字不变**。
5. 测试：`backend/tests/test_rnd250_bootstrap.py`。

**Out of scope（显式非目标）：**
- 不做完整的引导表单交互美化（复用 T8/T9 的骨架和组件，本票只求"能跑通"）。
- 不做企微 OAuth 模式（`AUTH_MODE=wecom`）的引导流程细节（G3 判定逻辑已覆盖它的"是否初始化"判断，但企微模式下没有"设管理员密码"这一步，`bootstrap` 端点在该模式下只需要补齐三件套）。
- 不改 `password_login` 除"取值来源"外的任何逻辑。
- 不做解锁/重新初始化端点。

**本工单拥有的文件（只许写这些）：**
- `backend/app/config/guard.py`（新）
- `backend/app/routers/settings.py` —— **追加** `bootstrap-status`/`bootstrap` 两个端点；T5 已交付的 `GET`/`PUT /settings` 与既有 `POST /settings/password` **逐字不变**
- `backend/app/routers/web.py` —— **仅追加** `GET /admin/settings/init`
- `backend/app/routers/auth.py` —— **仅修改** `password_login` 函数体内 `admin_username`/`admin_hash` 赋值的那两行，函数其余部分与文件其他内容逐字不变
- `backend/app/web/templates/settings_init.html`（新，最小骨架）
- `backend/tests/test_rnd250_bootstrap.py`（新）

**只读、绝不可写：** `app/config/resolver.py`/`repository.py`/`schema.py`/`crypto.py`（只调用）、`app/auth.py`（`hash_password` 只调用，不修改该文件）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件、或需要动 `auth.py`/`settings.py` 里本清单未列出的任何一行 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 未初始化判定正确**：全新部署（无 DB 配置、无 env 密码）→ `is_initialized() == False`。
- **AC-2 已初始化判定正确**：`ADMIN_PASSWORD_HASH` 已设置（纯 env，模拟现有部署）→ `is_initialized() == True`，即使从未跑过本票的 bootstrap 流程。
- **AC-3 bootstrap 端点门禁（关键）**：`is_initialized()==True` 时，`GET bootstrap-status` 仍可读（用于前端判断要不要显示引导入口），但 `POST bootstrap` → 403，不能重新引导覆盖已有账号。
- **AC-4 bootstrap 设置账号后可登录（关键，端到端）**：全新部署 → `POST bootstrap` 设管理员账号密码 → 用该账号密码调用 `POST /api/auth/password/login` → 登录成功（拿到 session）。这是本票最核心的端到端验证，必须有一条测试走完整条链路。
- **AC-5 纯 env 部署零回归（关键）**：`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH` 只在 env 设置、DB 里没有对应 config 记录时，`password_login` 行为与本票改动前**完全一致**（须有测试模拟"未跑 bootstrap 的现有部署"场景，确认登录仍然只凭 env 值成功）。
- **AC-6 `password_login` 改动最小**：`git diff -- backend/app/routers/auth.py` 只应看到 `admin_username`/`admin_hash` 赋值那几行变化，函数签名、会话创建、cookie 设置、错误处理等其余部分逐字未变。
- **AC-7 `GET /admin/settings/init` 路由行为**：未初始化 → 200 渲染引导表单；已初始化 → 重定向 `/admin/login`。
- **AC-8 admin_username/admin_password_hash 不经过 T5 的 CONFIG_REGISTRY 校验通道**：这两个 key 的写入走本票的专用路径（`repository.upsert()` 直接调用），不在 `GET /settings`（T5）的响应里出现（它们不是"可配置项"，是身份凭据，不应该出现在设置页的常规字段列表里）。
- **AC-9 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；**全部既有 `auth.py`/`test_auth.py` 相关测试全绿**（这是本票最容易连累的既有测试套件）。

## 验证方式（Verification — 确定性闸）
```bash
.venv/bin/python -c "from app.config.resolver import resolve, get_config_resolver, invalidate; from app.config.repository import upsert; from app.routers.settings import settings_router; print('OK')"
make verify
.venv/bin/python -m pytest backend/tests/test_rnd250_bootstrap.py -q
.venv/bin/python -m pytest backend/tests/test_auth.py -q   # AC-5/AC-9：既有登录测试零回归，重点
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/routers/auth.py    # AC-6：人工逐行核对，只应有 admin_username/admin_hash 赋值的改动
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T4（<issue>RND-248</issue>）+ T5（<issue>RND-249</issue>）**必须先落地**。本票 **blocks** T11（<issue>RND-254</issue>）与 T12（<issue>RND-255</issue>）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-9 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**附完整的端到端引导流程截图/日志**（bootstrap → login 成功）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高，R3 定级的原因）**：`password_login` 改动引入回归，导致现有纯 env 部署的用户登不进去——由 AC-5/AC-6 严格防守，diff 必须最小化到可以一眼看完。
- **风险 2**：bootstrap 端点鉴权判断有误，允许已初始化的部署被重新引导，攻击者借此覆盖管理员账号——由 AC-3 防守，这是一个真实的权限提升风险点。
- **风险 3**：`admin_password_hash` 意外出现在 `GET /settings` 的常规响应里，暴露账号凭据结构信息——由 AC-8 防守。
- 回滚：`git checkout -- backend/app/routers/auth.py` 单独回滚这一个文件到改动前状态（其余新增文件删除即可）；无迁移。

## 人工点位
- **Trigger**：Haisu 置 In Progress（**建议 T4/T5 落地后**才派发）。
- **Gate（R3，最高强制）**：**本票必须经 Haisu 逐行审阅 `auth.py` 的 diff 后才能 approve commit**，不适用"测试绿就放行"。这是全站登录入口的改动。
- **Escalation**：T4/T5 未就绪、或发现 `password_login` 的改动无法控制在"仅两行赋值"范围内（例如现有逻辑耦合程度超预期）→ `BLOCKED_NEEDS_HUMAN`，说明具体情况，**不要**为了让端到端流程跑通而扩大对 `auth.py` 的改动范围。

## 开发 agent 执行指引
1. **先跑「开工前核实」命令**。
2. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/routers/auth.py:565`（`password_login` 全文，逐行理解）、`app/auth.py`（`hash_password`）、`app/settings.py`（`AuthSettings`/`WecomOAuthSettings`）、`scripts/bootstrap_default_tenant.py`（了解"默认租户"从哪来，bootstrap 流程依赖它已存在）。
3. 写 `config/guard.py`（`is_initialized`/`require_settings_admin`）。
4. 在 `settings.py` 追加 bootstrap 两个端点。
5. 在 `web.py` 追加 `GET /admin/settings/init` + 最小模板。
6. **谨慎修改** `auth.py` 的 `password_login`——只改赋值来源那两行。
7. 写测试覆盖 AC-1~AC-8（**AC-4 端到端登录链路 + AC-5 纯 env 零回归是重中之重**）。
8. 跑全部验证命令，输出 QA Summary（附端到端流程记录）+ `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假账号数据。
- 不扩大 Scope：不做完整表单美化、不做企微模式引导细节、不做重新初始化。
- 复用优先：`hash_password`/T4 resolver/T5 校验逻辑只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证，**尤其 AC-4/AC-5 需要真实跑通的测试证据，不接受"逻辑上应该没问题"**。
