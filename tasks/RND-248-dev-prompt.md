[Goal check] This work advances 开发（Development） by 交付配置解析器（DB>env>默认 + 短 TTL 缓存），把 T1 的存储表、T2 的加解密、T3 的注册表三者接成一条可用的"读配置"链路，供 T5 Settings API 消费。

# RND-248 开发提示词（Developer Prompt）— 配置中心 T4

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-248 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 开工前必须先核实的前置条件

本票依赖 T1（<issue>RND-246</issue>，`AppConfigStore` 表）、T2（<issue>RND-245</issue>，`app/config/crypto.py`）、T3（<issue>RND-247</issue>，`CONFIG_REGISTRY`）**三者全部落地**。开工第一步：
```bash
.venv/bin/python -c "from app.db.models import AppConfigStore; from app.config.crypto import encrypt_value, decrypt_value; from app.config.schema import CONFIG_REGISTRY; print('OK')"
```
- 三者若有任一 import 失败 → **停止**，`BLOCKED_NEEDS_HUMAN`，说明缺哪个。**不要**为了不等待而自己现造一份简化版存储/加密/注册表。

## 任务身份
- 工单：RND-248「配置中心 T4：配置仓储 + 解析器（DB>env>默认 + 缓存失效）」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R2**（解析顺序错了会导致敏感配置被明文缓存，或线上配置被 env 悄悄覆盖）｜milestone：R2 · 开源发布闭环

## 背景与项目现状

**解析顺序（冻结设计）**：`DB > env > 默认`——DB 里有值就用 DB，DB 没有就退回 env（`app/settings.py` 的既有 `get_xxx_settings()`），env 也没有就用 T3 注册表里的 `default`。**env 层必须走既有的 `get_xxx_settings()` 函数**，不要绕过它们直接 `os.environ.get`——那些函数保留了 `monkeypatch.setenv` 语义（每次调用现读，不缓存），本票的短 TTL 缓存只应包在**解析器整体**外面，不要把这个"现读"特性从 env 层里去掉。

**❗ 本项目高频踩坑：**
- **key → env 访问器的映射需要本票自己建**：T3 的 `CONFIG_REGISTRY` 只登记了"有哪些字段"，**没有**登记"这个字段该从哪个 `get_xxx_settings()` 类的哪个属性读"——这层映射是本票的核心工作，不是已经现成的。建一个内部映射（如 `_ENV_ACCESSORS: dict[str, Callable[[], str]]`，每项是一个 lambda 调用对应的 `get_xxx_settings().属性名`），逐一对上 `CONFIG_REGISTRY` 里的每个 key。
- **密钥解密时机**：`AppConfigStore.value` 里存的是密文（T2 加密后落库），解析器读出来后要用 `app.config.crypto.decrypt_value` 解密才是明文——**不要**返回密文给调用方。
- 架构边界：`app/config/repository.py`/`resolver.py` 不得 import `app.routers.*`/`app.main`。

## 目标（Goal）
交付一个统一的配置读取入口：调用方给一个 key，拿回按 `DB > env > 默认` 顺序解析出的**明文**值，带短 TTL 缓存，且支持显式失效。

## 范围边界

**In scope：**
1. `backend/app/config/repository.py`（**新建**）：对 `AppConfigStore` 的薄 CRUD 封装——`get_raw(db, key) -> Optional[AppConfigStore]`、`upsert(db, key, value, *, is_secret, requires_restart, updated_by) -> AppConfigStore`、`list_all(db) -> list[AppConfigStore]`。
2. `backend/app/config/resolver.py`（**新建**）：
   - `_ENV_ACCESSORS`：`CONFIG_REGISTRY` 每个 key → 对应 `get_xxx_settings()` 属性访问的映射。
   - `def resolve(db: Session, key: str) -> Optional[str]`：按 `DB > env > 默认` 解析单个 key，DB 命中且 `is_secret` 则调用 `decrypt_value` 解密后返回。
   - 进程内短 TTL 缓存（30s，`CONFIG_REGISTRY` 之外自建一个简单的 `{key: (value, expires_at)}` 字典 + 锁，不要引入 Redis 之类的外部依赖）。
   - `def invalidate(key: Optional[str] = None) -> None`：清失效缓存（不传 key 则清全部）。
   - `def get_config_resolver()`：模块级单例访问器（供 T5 依赖注入，返回值本身是本模块，不是一个类实例——保持简单，不要为了"看起来像依赖注入"而过度设计）。
3. 测试：`backend/tests/test_rnd248_config_resolver.py`。

**Out of scope（显式非目标）：**
- 不做任何 HTTP 端点（属 T5）。
- 不做写入侧的校验逻辑（属 T5，本票的 `upsert` 只是薄封装，不做字段级校验）。
- 不改 `app/settings.py` 里任何既有 `get_xxx_settings()` 函数的实现。

**本工单拥有的文件（只许写这些）：**
- `backend/app/config/repository.py`（新）
- `backend/app/config/resolver.py`（新）
- `backend/tests/test_rnd248_config_resolver.py`（新）

**只读、绝不可写：** `app/db/models.py`（T1 拥有，只读取 `AppConfigStore`）、`app/config/crypto.py`（T2 拥有，只调用）、`app/config/schema.py`（T3 拥有，只读取 `CONFIG_REGISTRY`）、`app/settings.py`（只调用既有 `get_xxx_settings()`，不修改）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 DB 优先**：`AppConfigStore` 里有值时，`resolve()` 返回 DB 值，即使 env 也设置了不同的值。
- **AC-2 env 兜底**：DB 无值、env 有值时，返回 env 值（经对应 `get_xxx_settings()` 读取，不是绕过它直接读 `os.environ`）。
- **AC-3 默认值兜底**：DB 和 env 都无值时，返回 `CONFIG_REGISTRY` 里定义的 `default`。
- **AC-4 密钥自动解密（关键）**：`is_secret=True` 的字段，DB 命中时 `resolve()` 返回**明文**（内部已调用 `decrypt_value`），不是返回密文。须有测试：写入加密值，`resolve()` 读出应等于原始明文。
- **AC-5 缓存与失效生效**：连续两次 `resolve()` 同一 key，第二次命中缓存（可用 mock 断言底层 DB 查询只发生一次）；调用 `invalidate(key)` 后下一次 `resolve()` 应重新查库。
- **AC-6 `monkeypatch` 语义不受影响**：测试用 `monkeypatch.setenv` 改一个环境变量后，在**未命中缓存**的情况下调用 `resolve()`，应读到新值（证明 env 层没有被本票的缓存机制"冻结"）。
- **AC-7 未改 env settings 函数**：`git diff --stat -- backend/app/settings.py` 必须无输出。
- **AC-8 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
.venv/bin/python -c "from app.db.models import AppConfigStore; from app.config.crypto import encrypt_value, decrypt_value; from app.config.schema import CONFIG_REGISTRY; print('OK')"   # 开工前必须先跑
make verify
.venv/bin/python -m pytest backend/tests/test_rnd248_config_resolver.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/settings.py    # AC-7：必须无输出
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
T1（<issue>RND-246</issue>）、T2（<issue>RND-245</issue>）、T3（<issue>RND-247</issue>）**必须全部先落地**，见上方「开工前必须先核实」。本票 **blocks** T5（<issue>RND-249</issue>）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-8 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，附 `_ENV_ACCESSORS` 映射表（每个 key 对应哪个 `get_xxx_settings()` 属性），供 T5 review
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：解析顺序写反（env 优先于 DB），导致用户在 UI 里改了配置却不生效——由 AC-1/AC-2 防守。
- **风险 2**：密钥字段忘记解密，把密文当明文用（例如直接拿去调 WeCom API）——由 AC-4 防守。
- 风险 3：缓存把 `monkeypatch.setenv` 测试语义冻住，导致既有测试套件大面积变红——由 AC-6 防守。
- 回滚：纯新增文件，`rm app/config/repository.py app/config/resolver.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress（**建议在 T1/T2/T3 均已落地后**才派发）。
- **Gate（R2）**：解析顺序是配置中心的核心正确性保证，建议 Haisu 过一遍 `_ENV_ACCESSORS` 映射表再 approve commit。
- **Escalation**：T1/T2/T3 未全部就绪 → `BLOCKED_NEEDS_HUMAN`（见上方开工前核实）。

## 开发 agent 执行指引
1. **先跑「开工前核实」的 import 命令**，确认 T1/T2/T3 都已落地。
2. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/config/schema.py`（`CONFIG_REGISTRY`）、`app/settings.py`（全部 `get_xxx_settings()`）、`app/config/crypto.py`。
3. 写 `repository.py`（薄 CRUD）。
4. 写 `resolver.py`：建 `_ENV_ACCESSORS` 映射 + `resolve()` + 缓存 + `invalidate()`。
5. 写测试覆盖 AC-1~AC-6（**AC-4 密钥解密、AC-6 monkeypatch 语义是重点**）。
6. 跑全部验证命令，输出 QA Summary（含映射表）+ `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假配置值。
- 不扩大 Scope：不写端点、不写字段校验。
- 复用优先：env 层只调用既有 `get_xxx_settings()`，不重新读 `os.environ`。
- 证据优先，以 exit 0 / 测试通过为证。
