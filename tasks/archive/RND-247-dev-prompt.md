[Goal check] This work advances 开发（Development） by 交付配置中心的元数据契约（配置项注册表 + 常量），作为 T1/T4/T5/T8 等后续所有配置中心子票共同依赖的第一层地基。

# RND-247 开发提示词（Developer Prompt）— 配置中心 T3

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-247 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-247「配置中心 T3：配置项元数据注册表与常量」｜父 Epic RND-244（配置中心/Settings 设置页）
- 优先级：Medium｜风险等级：**R0**（纯新增元数据定义，无端点、无 DB 写入、零业务副作用）｜milestone：R2 · 开源发布闭环
- **这是本 epic（T1-T12）的第一层地基——T4/T5/T8 都要读这份注册表。本票错了，后面全错。**

## 背景与项目现状（已实地核实）

`backend/app/settings.py` 已有一批**按域名分组的 env 读取类**（`DatabaseSettings`/`AuthSettings`/`EmailSettings`/`WecomOAuthSettings`/`WecomCallbackSettings`/`MediaStorageSettings`/`ThumbnailSettings`/`VoiceTranscodeSettings`/`EventMediaDownloadSettings`），每个类对应一组 env 变量。这些类**不会被本票删除或替换**——T4（配置仓储/解析器）会把它们作为"env 层"的真实来源继续使用（`get_xxx_settings()` 的语义原样保留），本票只是给其中**允许通过 Settings UI 配置**的那部分字段，建一份独立于这些类之外的**元数据描述**（哪个字段属于哪个分组、是不是密钥、必填与否、改了要不要重启）。

**P0 配置项的权威来源 = `app/settings.py` 里已有的类**，不要凭空发明字段名。**明确排除**在外、不进本票注册表的（这些是 bootstrap-only，只能改 env/重启进程，不给 UI 编辑权限——见 T6/RND-250 的 G3/G4 冻结设计）：
- `DatabaseSettings.database_url`（改了必须重启，且是连接自身的凭据，UI 层面编辑这个有鸡生蛋悖论）
- `AuthSettings.auth_mode` / `admin_password_hash`（认证模式本身，T6 的 bootstrap 逻辑专门处理这两个，不归本注册表管）
- 未来的 `SETTINGS_ENCRYPTION_KEY`（T2/RND-245 会读它，同理是 bootstrap-only，不进 UI）

**其余分组建议**（对应 RND-244 描述的"通用/域名、第三方 API key 插槽、对象存储、企业微信密钥、其他/高级"五组）：
- **企微密钥组**：`WecomOAuthSettings`（`wecom_corp_id`/`wecom_agent_id`/`wecom_oauth_secret`）+ `WecomCallbackSettings`（`wecom_callback_token`/`wecom_callback_encoding_aes_key`）——**这四个是 T6 判定"是否已初始化"的关键字段，必须标 `required=True`**。
- **通用/域名组**：`WecomOAuthSettings.admin_domain`。
- **对象存储组**：`MediaStorageSettings` 的 `qiniu_access_key`/`qiniu_secret_key`/`qiniu_bucket`/`qiniu_domain`/`qiniu_region`/`storage_backend`/`media_storage_provider`。
- **第三方组**：`EmailSettings` 的 `smtp_host`/`smtp_port`/`smtp_user`/`smtp_password`/`smtp_from`。
- **其他/高级组**：`ThumbnailSettings`/`VoiceTranscodeSettings`/`EventMediaDownloadSettings` 的字段（这些不阻断"看到第一条消息"，标 `required=False`）。

**❗ 本项目高频踩坑：**
- 架构冻结 D1：本票不涉及前端，但输出是前端 T8/T9 的直接消费契约，字段命名务必和 `app/settings.py` 里的 snake_case 属性名保持**逐字一致**（不要发明驼峰或改写）。
- 架构边界硬闸：`app/config/` 是**新建包**，其中的模块不得 import `app.routers.*` 或 `app.main`（会被 `test_architecture_boundary.py` 拦）。

## 目标（Goal）
交付一份可被后续所有配置中心子票（T4 解析器、T5 API、T8 前端）共同读取的配置项元数据注册表，把"哪些字段可配置、怎么分组、是不是密钥、改了要不要重启"这件事**只定义一次**。

## 范围边界

**In scope：**
1. `backend/app/config/__init__.py`（新建空包初始化文件）。
2. `backend/app/config/constants.py`（新建）：
   - `RESOLVE_ORDER = ("db", "env", "default")`（解析优先级常量，供 T4 使用）。
   - `RESTART_REQUIRED_KEYS: frozenset[str]`（改动后需要重启进程才生效的 key 集合——**上面列出的 P0 字段大多数是热加载的，只有类似 `STORAGE_BACKEND` 这种切换存储后端实现的字段才需要重启；逐项判断，不要偷懒全标 True 或全标 False**）。
   - `class ConfigGroup(str, Enum)`：`GENERAL`/`THIRD_PARTY`/`STORAGE`/`WECOM`/`ADVANCED`（对应上面五组）。
3. `backend/app/config/schema.py`（新建）：
   - `@dataclass(frozen=True) class ConfigItemSpec`：字段至少含 `key: str`（如 `"wecom_corp_id"`，与 `app/settings.py` 属性名逐字一致）、`group: ConfigGroup`、`value_type: Literal["string","secret","int","bool"]`、`is_secret: bool`、`required: bool`、`conditional_on: Optional[str]`（如 `"auth_mode=wecom"` 这种简单表达式，本票只需定义字段存在，具体解析逻辑留给 T6）、`restart_required: bool`、`default: Optional[str]`。
   - `CONFIG_REGISTRY: dict[str, ConfigItemSpec]`：**按上面「P0 配置项的权威来源」逐项登记**，`key` 全部取自 `app/settings.py` 现有属性名。
   - `def validate_registry() -> None`：模块加载时自检——`key` 不重复、`is_secret=True` 的项 `value_type` 必须是 `"secret"`、`conditional_on` 引用的 key 必须存在于 `CONFIG_REGISTRY` 本身。**这是给注册表自身做的完整性检查，不是给用户输入做校验**（用户输入校验是 T5 的事）。
4. 测试：`backend/tests/test_rnd247_config_schema.py`。

**Out of scope（显式非目标）：**
- 不做任何 DB 表 / 迁移（属 T1/RND-246）。
- 不做加密逻辑（属 T2/RND-245）。
- 不做实际的"从 DB/env 读取当前值"逻辑（属 T4/RND-248，本票只定义"有哪些字段"，不定义"怎么读"）。
- 不做任何 HTTP 端点（属 T5/RND-249）。
- 不做前端（属 T8/RND-251）。

**本工单拥有的文件（只许写这些，全部新建）：**
- `backend/app/config/__init__.py`
- `backend/app/config/constants.py`
- `backend/app/config/schema.py`
- `backend/tests/test_rnd247_config_schema.py`

**只读、绝不可写：** `app/settings.py`（只读取其现有属性名作为注册表 key 的来源，不修改该文件本身）。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 注册表结构完整**：`CONFIG_REGISTRY` 中每一项都有完整的 `ConfigItemSpec` 七个字段，无遗漏。
- **AC-2 key 与 `app/settings.py` 一致（关键）**：`CONFIG_REGISTRY` 里每个 `key` 都能在 `app/settings.py` 对应的 Settings 类里找到同名属性——须有测试遍历注册表逐项 `getattr` 对应的 Settings 实例，确认属性存在（不是拼写错误/臆造字段名）。
- **AC-3 密钥字段类型一致**：`is_secret=True` 的项 `value_type` 必须是 `"secret"`；反之非密钥字段不应误标 `is_secret=True`（如 `wecom_corp_id` 不是密钥，`wecom_oauth_secret`/`qiniu_secret_key`/`smtp_password` 才是）。
- **AC-4 企微四件套均为必填**：`wecom_corp_id`/`wecom_agent_id`/`wecom_oauth_secret`/`wecom_callback_token`/`wecom_callback_encoding_aes_key` 的 `required` 均为 `True`（T6 的初始化判定依赖这个）。
- **AC-5 `validate_registry()` 自检生效**：测试构造一个故意重复 key 的假注册表调用该函数 → 抛异常；正常注册表调用 → 不抛。
- **AC-6 无重复 key**：`CONFIG_REGISTRY` 字典本身不可能重复（Python dict 语法上不允许），但**若多个 `ConfigItemSpec` 描述了同名字段来自不同 Settings 类** → 测试断言不存在这种歧义。
- **AC-7 架构边界 + 回归**：`app/config/*` 未 import `app.routers.*`/`app.main`；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd247_config_schema.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -rn "from app.routers\|from app.main\|import app.routers\|import app.main" backend/app/config/   # 应无命中
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
无前置。**本票是配置中心 epic（RND-244）的第一层地基，T4（RND-248）/T5（RND-249）/T8（RND-251）都直接消费本票产出。**

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，附完整的 `CONFIG_REGISTRY` 字段清单（分组/是否密钥/是否必填/是否重启）表格，供 T4/T5/T8 直接引用
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：字段命名与 `app/settings.py` 不一致，导致 T4 读取时 `getattr` 失败——由 AC-2 防守。
- 风险：`restart_required` 标注不准确，导致用户改了本该热加载的字段却被要求重启（体验劣化），或改了本该重启的字段却没提示（配置不生效却不自知）——需逐项判断，不要图省事全标一个值。
- 回滚：纯新增文件，直接删除 `app/config/` 目录即可；零数据/零端点影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿即可，无需额外人工审阅（R0，纯元数据定义）。
- **Escalation**：若发现某个 P0 字段在 `app/settings.py` 里实际上不存在、或字段语义与本文档描述冲突 → `BLOCKED_NEEDS_HUMAN`，列出具体字段，**不要**自行决定加入或跳过。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/settings.py`（全文，逐个 Settings 类核对属性名）。
2. 建 `app/config/` 包，写 `constants.py` + `schema.py`。
3. 按「P0 配置项的权威来源」逐项登记 `CONFIG_REGISTRY`。
4. 写 `validate_registry()` + 测试覆盖 AC-1~AC-6。
5. 跑全部验证命令，输出 QA Summary（含字段清单表格）+ `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥。
- 不扩大 Scope：不写 DB 模型、不写加密、不写端点、不写前端。
- 复用优先：字段名 100% 取自 `app/settings.py`，不臆造。
- 证据优先，以 exit 0 / 测试通过为证。
