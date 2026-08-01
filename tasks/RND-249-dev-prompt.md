[Goal check] This work advances 开发（Development） by 交付配置中心的 GET/PUT Settings API，把 T4 的解析器接到 HTTP 层，供 T8/T9 前端联调消费。

# RND-249 开发提示词（Developer Prompt）— 配置中心 T5

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-249 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 关键前提 1：`app/routers/settings.py` 已经存在，本票是追加不是新建

`backend/app/routers/settings.py` **已经存在**（<issue>RND-302</issue> 交付），内含 `settings_router = APIRouter()` 与 `POST /settings/password`（修改密码），已在 `app/main.py:119` 挂载 `app.include_router(settings_router, prefix="/api/admin")` 且**已在生产使用**。**本票在同一个 `settings_router` 上追加新端点**（`GET/PUT /settings`），**不得**新建一个同名文件或重复挂载一个新的 router 变量——那会导致路由冲突或两套 `/api/admin/settings/*` 并存的混乱局面。

## ⚠️ 2026-08-01 追加：契约测试同步已授权（回应 dev agent 的 BLOCKED_NEEDS_HUMAN 上报）

dev agent 正确指出：新增 `GET`/`PUT /api/admin/settings` 触发 `docs/ticket-autopilot-workflow.md` §3.3 的强制契约同步规则——`backend/tests/test_http_contract.py`（route_count/path 集合/snapshot）与 `backend/tests/test_rnd280_rbac_scaffold.py`（本票用 `Depends(require_role())`，白名单需加 `settings.py`——注意 `settings.py` 此前**没有**因为既有的 `POST /settings/password` 而进入白名单，因为那个端点用的是 `get_current_user`，不是 `require_role`，本票是第一次让这个文件触发该断言）。**这两个文件本就属于本票范围，是我在第一版 dev prompt 里遗漏了，不是需要另开工单或继续 BLOCKED 的情况**——比照本 epic 其余票（如 T1/T11）已经建立的先例处理，见下方文件所有权清单更新。

## ⚠️ 关键前提 2：开工前必须先核实 T3/T4 已经落地

```bash
.venv/bin/python -c "from app.config.schema import CONFIG_REGISTRY; from app.config.resolver import resolve, get_config_resolver, invalidate; print('OK')"
```
不通过 → **停止**，`BLOCKED_NEEDS_HUMAN`，不要现造简化版解析逻辑。

## 任务身份
- 工单：RND-249「配置中心 T5：后端 Settings API（GET/PUT + 字段级校验 + 掩码）」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R2**（写入侧涉及密钥落库与字段校验，出错会导致配置损坏或密钥泄露）｜milestone：R2 · 开源发布闭环

## 背景与项目现状

**GET 语义**：返回全部 `CONFIG_REGISTRY` 字段的当前值（经 T4 `resolve()`），按分组组织，标注每个字段的"来源"（`default`/`env`/`db`——**这个来源信息 T4 的 `resolve()` 目前只返回值本身，本票需要扩展调用方式或另写一个"resolve 并带来源标签"的薄包装**，不要求改 T4 的公开接口签名，可以在本票里对每个 key 分别检查 DB 有没有值、env 有没有值来推断来源）。密钥字段（`is_secret=True`）**返回掩码后的值**（调用 T2 的 `mask()`），**绝不返回明文**。

**PUT 语义**：接收若干字段的新值，逐项按 `CONFIG_REGISTRY` 的 `value_type`/`required` 强校验；密钥字段值先经 T2 `encrypt_value()` 加密再落库（调用 T4 `repository.upsert()`）；成功后调用 T4 `invalidate()` 失效缓存；返回本次涉及的字段里哪些 `requires_restart=True`（前端据此展示"需重启"提示）；任一字段校验失败 → 整体 400，响应体 `{"errors": [{"key": ..., "message": ..., "code": ...}]}`（**不要**部分字段成功部分失败——全部通过校验才落库，防止半成品配置状态）。

**PUT 空字符串语义（重要，冻结设计 §7）**：secret 字段传空串 = **保留原值不变**（避免前端"看不到明文只能重新输入"导致误清空）；非 secret 字段传空串 = **清空该字段**。两种语义不同，不要混用一套处理逻辑。

**❗ 本项目高频踩坑：**
- **不要重写 `POST /settings/password`**：`git diff` 中这个既有端点应该逐字未变，本票只是在同一文件里追加新端点。
- 架构边界：router 不得 import `app.main`。

## 目标（Goal）
交付 `GET/PUT /api/admin/settings`：读取当前全部配置（密钥掩码），写入时强校验 + 加密落库 + 失效缓存 + 返回重启提示。

## 范围边界

**In scope：**
1. `backend/app/routers/settings.py`（**修改，追加**）：
   - `GET /settings`（挂载后即 `GET /api/admin/settings`）：`require_role()`（读取不限角色，MVP "已认证会话=可管理"，G4 冻结设计——不新加角色列）。返回 `{"groups": {...}}`，每个字段含 `key`/`value`（密钥已掩码）/`source`/`requires_restart`。
   - `PUT /settings`：同样 `require_role()`（G4：MVP 不区分角色，任何已登录会话都能改，这是本期冻结的简化）。请求体 `{"updates": {key: value, ...}}`，逐项校验 → 加密落库 → 失效缓存 → 返回 `{"ok": true, "restart_required_keys": [...]}`；校验失败 → 400 + `{"errors": [...]}`。
2. `backend/app/schemas/settings.py`（**新建**，若已有同名 schema 文件先检查，避免重名冲突）：`SettingsGetOut`/`SettingsUpdateIn`/`SettingsUpdateOut`/`SettingsErrorItem`。
3. 测试：`backend/tests/test_rnd249_settings_api.py`。

**Out of scope（显式非目标）：**
- 不做权限分级（G4 冻结：MVP 已认证=可管理，不加 `is_admin` 列——这是 T6/<issue>RND-250</issue> 的 `require_settings_admin` 要处理的，本票先用 `require_role()` 占位，T6 落地后再替换成 `require_settings_admin`，**不要在本票里预先造这个依赖名**，会和 T6 实际交付的签名对不上）。
- 不做连通性自检（属 T7/<issue>RND-252</issue>）。
- 不做导入导出（属 T10/<issue>RND-256</issue>）。
- 不改 `POST /settings/password`。

**本工单拥有的文件（只许写这些）：**
- `backend/app/routers/settings.py` —— **仅追加** `GET`/`PUT /settings`；`POST /settings/password` 逐字不变
- `backend/app/schemas/settings.py`（新）
- `backend/tests/test_rnd249_settings_api.py`（新）
- `backend/tests/test_http_contract.py` —— **强制随附**（见 `docs/ticket-autopilot-workflow.md` §3.3）：`route_count` 读当前实际基线 +2（GET+PUT，不要硬编码数字）、expected path 集合追加 `/api/admin/settings`（GET/PUT 各一条）、snapshot 追加对应条目
- `backend/tests/test_rnd280_rbac_scaffold.py` —— **强制随附**：白名单新增 `settings.py`（本票是它第一次因为 `require_role()` 触发这条断言，既有的 `POST /settings/password` 用的是 `get_current_user`，不受影响，不要动那部分）

**只读、绝不可写：** `app/config/resolver.py`/`repository.py`/`crypto.py`/`schema.py`（T2/T3/T4 拥有，只调用）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 GET 返回全部字段**：响应含 `CONFIG_REGISTRY` 全部 key，按分组组织。
- **AC-2 密钥掩码（关键）**：`is_secret=True` 的字段，GET 响应中的 `value` 是掩码后的值，**绝不是明文**。须有测试断言响应 JSON 序列化字符串中不含测试写入的原始密钥值。
- **AC-3 来源标注正确**：分别构造"只有默认值"/"只有 env"/"DB 有值覆盖 env"三种场景，`source` 字段各自正确标注 `default`/`env`/`db`。
- **AC-4 PUT 强校验**：非法值（类型不匹配/必填缺失）→ 400 + `errors` 数组含具体 key 和原因；**任一字段失败则整体不落库**（须有测试验证：一批更新里一个字段非法，断言其余合法字段也**没有**被写入 DB）。
- **AC-5 密钥加密落库**：PUT 密钥字段后，直接查 `AppConfigStore.value` 断言是密文（不等于明文，且 `is_encrypted()` 返回 True）。
- **AC-6 空串语义正确（关键）**：secret 字段 PUT 空串 → 原值不变；非 secret 字段 PUT 空串 → 该字段被清空为空串。两种行为分别有测试覆盖。
- **AC-7 失效缓存**：PUT 成功后，紧接着的 GET（或直接调用 `resolve()`）应读到新值，不是 T4 缓存里的旧值（验证 `invalidate()` 被正确调用）。
- **AC-8 重启提示**：PUT 涉及 `requires_restart=True` 的字段时，响应 `restart_required_keys` 包含该字段；不涉及则不包含。
- **AC-9 未改既有密码端点**：`POST /settings/password` 的行为、DOM 无关，纯后端——`git diff` 中该函数体逐字未变。
- **AC-10 契约同步 + RBAC 同步 + 回归**：`test_http_contract.py`（route_count 当前基线 +2）与 `test_rnd280_rbac_scaffold.py`（白名单加 `settings.py`）均已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过；既有测试（含覆盖 `POST /settings/password` 的）全绿。

## 验证方式（Verification — 确定性闸）
```bash
.venv/bin/python -c "from app.config.schema import CONFIG_REGISTRY; from app.config.resolver import resolve, get_config_resolver, invalidate; print('OK')"
make verify
.venv/bin/python -m pytest backend/tests/test_rnd249_settings_api.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/routers/settings.py    # 人工核对：POST /settings/password 逐字未变，只新增
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T3（<issue>RND-247</issue>）+ T4（<issue>RND-248</issue>）**必须先落地**。本票 **blocks** T6（<issue>RND-250</issue>）、T7（<issue>RND-252</issue>）、T10（<issue>RND-256</issue>）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-10 全满足，每条有测试（含 AC-10 的契约/RBAC 同步）
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，附 GET/PUT 的确切响应/请求体结构（供 T7/T9/T10 对接）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：GET 返回明文密钥——由 AC-2 防守，这是本票最高安全风险项。
- **风险 2**：PUT 部分成功导致配置处于不一致的半成品状态——由 AC-4 防守，要么全部通过校验再落库，要么整体拒绝。
- 风险 3：空串语义（secret 保留 vs 非 secret 清空）写反——由 AC-6 防守。
- 回滚：`git checkout -- backend/app/routers/settings.py` 会连既有密码端点一起回滚到修改前状态（因为是同一文件）——**这是本票唯一需要注意的回滚代价**，若只想回滚本票新增部分，需要手工分离 diff，不能整体 `git checkout`。

## 人工点位
- **Trigger**：Haisu 置 In Progress（**建议在 T3/T4 落地后**才派发）。
- **Gate（R2）**：涉及密钥落库，建议 Haisu 审阅 AC-2/AC-5/AC-6 的测试证据后再 approve commit。
- **Escalation**：T3/T4 未就绪 → `BLOCKED_NEEDS_HUMAN`。若发现 `POST /settings/password` 与新端点在鉴权模型上有冲突（例如未来需要不同的权限级别）→ `BLOCKED_NEEDS_HUMAN`，不要为了统一而改动既有密码端点的鉴权。

## 开发 agent 执行指引
1. **先跑「关键前提 2」的 import 核实命令**。
2. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/routers/settings.py`（**逐行**，现有内容）、`app/config/resolver.py`/`schema.py`/`crypto.py`。
3. 在 `settings.py` 追加 `GET`/`PUT /settings`，不动既有端点。
4. 写 `schemas/settings.py`。
5. 写测试覆盖 AC-1~AC-9（**AC-2/AC-4/AC-6 是重点**）。
6. 同步 `test_http_contract.py`（route_count 读当前实际值 +2）与 `test_rnd280_rbac_scaffold.py`（白名单加 `settings.py`）。
7. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假配置值。
- 不扩大 Scope：不做权限分级、不做自检、不做导入导出。
- 复用优先：解析/加密/校验元数据全部调用 T2/T3/T4，不重写。
- 证据优先，以 exit 0 / 测试通过为证。
