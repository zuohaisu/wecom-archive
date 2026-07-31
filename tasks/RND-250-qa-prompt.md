[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-250 的 9 条 AC（重点验证 password_login 改动最小化且纯 env 部署零回归、端到端 bootstrap→login 链路真实可用、bootstrap 门禁不可绕过）并产出带证据的 PASS/FAIL 判定。

# RND-250 验收提示词（Acceptance / QA Prompt）— 配置中心 T6

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-250「配置中心 T6：权限守卫 + 首次初始化引导」｜风险等级 **R3**（本 epic 单票风险最高——触及全站登录入口）
- **这是 R2 里程碑验收标准的直接实现路径。AC-3/AC-4/AC-5/AC-6 全部从严判定，任一失败都直接判 FAIL，不接受"大部分对了"。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 未初始化判定正确
- 判定：全新部署（无 DB 配置、无 env 密码）→ `is_initialized() == False` = PASS。

### AC-2 — 已初始化判定正确
- 判定：`ADMIN_PASSWORD_HASH` 已设置（纯 env）→ `is_initialized() == True`，即使从未跑过 bootstrap = PASS。

### AC-3 — bootstrap 端点门禁（关键，权限提升防线）
- 证据：`is_initialized()==True` 时调用 `POST bootstrap` → 403。测试须构造"已初始化"状态后尝试调用。
- 判定：符合 = PASS。**若已初始化状态下 `POST bootstrap` 仍可执行 → 直接 FAIL（`SECURITY_VIOLATION`, severity: blocker）**——这允许攻击者在部署已投入使用后覆盖管理员账号，是权限提升漏洞。

### AC-4 — 端到端 bootstrap→login 链路（关键）
- 证据：测试须完整走一遍：全新部署 → `POST bootstrap` 设账号密码 → 用该账号密码 `POST /api/auth/password/login` → 断言登录成功（拿到有效 session/cookie）。
- 判定：完整链路真实跑通 = PASS。**若测试只验证了 bootstrap 端点本身返回 200、没有验证登录真的能用这组凭据成功 → 判定证据不足，FAIL（`INSUFFICIENT_TEST_COVERAGE`）**——这正是本票存在的意义，不接受"两端分别测通"而没有串起来的证据。

### AC-5 — 纯 env 部署零回归（关键）
- 证据：测试模拟"现有部署"场景（`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH` 只在 env 设置，DB 里对应 config key 不存在），断言 `password_login` 仍然只凭 env 值成功登录，行为与本票改动前完全一致。
- 判定：符合 = PASS。**若这个场景登录失败或行为改变 → 直接 FAIL（`REGRESSION`, severity: blocker）**——这会让所有现有自托管部署的用户登不进去，是本票能造成的最严重后果。

### AC-6 — `password_login` 改动最小化（关键）
- 证据：`git diff -- backend/app/routers/auth.py` 逐行审阅，确认改动**只限于** `admin_username`/`admin_hash` 赋值那几行（从纯 `auth_settings.xxx` 改成"先查 resolver 再 fallback env"），函数签名、tenant 查询逻辑、session 创建、cookie 设置、错误处理、日志语句**逐字未变**。
- 判定：diff 范围精确符合描述 = PASS。**diff 涉及上述"逐字未变"的任何一部分 → 直接 FAIL（`SCOPE_VIOLATION`, severity: blocker）**——这是全站登录入口，改动必须能一眼审完。

### AC-7 — `GET /admin/settings/init` 路由行为
- 判定：未初始化 200 渲染引导表单、已初始化重定向 `/admin/login` = PASS。

### AC-8 — 凭据字段不进入常规 Settings 响应
- 证据：`GET /settings`（T5）的响应体中**不包含** `admin_username`/`admin_password_hash` 这两个 key。
- 判定：不包含 = PASS。**包含 → FAIL（`SECURITY_VIOLATION`, major）**——这两个是身份凭据，不应该混进"可配置项"列表暴露结构信息。

### AC-9 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；**`backend/tests/test_auth.py`（或等价覆盖 `password_login` 的既有测试）全绿**。
- 判定：全部符合 = PASS。**既有 auth 测试有任何变红 → 直接 FAIL（blocker）**。

## 本项目专属检查（必查）
1. **企微三件套写入路径复用 T5**：`POST bootstrap` 里企微三件套的写入应复用 T5 已有的校验/落库逻辑（调用而非重写一遍字段校验）。
2. **`admin_password_hash` 写入用了正确的哈希函数**：`grep -n "hash_password" backend/app/routers/settings.py`（bootstrap 端点范围内）应有命中，确认调用的是 `app/auth.py` 既有的 `hash_password`，不是自己实现了一套哈希逻辑。
3. **未新增角色列**：`git diff --stat -- backend/app/db/models.py` 应无输出——G4 冻结设计明确"MVP 不加 `is_admin` 列"。
4. **文件所有权**：`git status --porcelain` 中改动应限于 `app/config/guard.py`（新）、`routers/settings.py`（追加 bootstrap 两端点）、`routers/web.py`（仅追加 `GET /admin/settings/init`）、`routers/auth.py`（仅 `password_login` 两行）、`web/templates/settings_init.html`（新）、`tests/test_rnd250_bootstrap.py`（新）。

## 附加检查（Security）
- 测试中的账号密码为固定假凭据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。
- 确认 bootstrap 端点在未初始化状态下**不需要**任何鉴权即可访问（这是设计意图，不是漏洞——但要确认它在已初始化后确实被 403 拦住）。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd250_bootstrap.py -q
.venv/bin/python -m pytest backend/tests/test_auth.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/routers/auth.py    # AC-6：逐行核对，范围必须精确
git diff --stat -- backend/app/db/models.py    # 应无输出
grep -n "hash_password" backend/app/routers/settings.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-250-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中完整记录端到端 bootstrap→login 链路的验证过程（这是给 Haisu 做最终人工审阅的关键材料）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-5 若纯 env 部署登录行为有任何变化 → 直接 FAIL（blocker）**，不接受"理论上兼容"。
- **AC-6 若 `auth.py` 的 diff 超出两行赋值范围 → 直接 FAIL（blocker）**。
- **AC-4 若端到端链路没有真实测试证据 → 判 INSUFFICIENT_TEST_COVERAGE，不接受分段验证拼凑的论证**。
