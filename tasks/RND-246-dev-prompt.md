[Goal check] This work advances 开发（Development） by 交付配置中心的 DB 存储表（app_config_store），为 T4 解析器/T5 Settings API 提供持久化底座。

# RND-246 开发提示词（Developer Prompt）— 配置中心 T1

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-246 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-246「配置中心 T1：新增 app_config_store 表与 Alembic 迁移」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R1**（新表 + 新迁移，但表本身在本票不接任何端点，零业务副作用）｜milestone：R2 · 开源发布闭环

## 背景与项目现状

配置中心存储模型是**混合**设计（已冻结，见 RND-244 描述）：`app_config_store`（DB）为应用级配置**主存储**；env 仅作 bootstrap（`DATABASE_URL`/`SETTINGS_ENCRYPTION_KEY`/首次管理员）与兼容老部署；运行时解析顺序 `DB > env > 默认`（T4/RND-248 实现，本票只建表）。

**冻结设计 G2（不要偏离）**：`app_config_store` **全局单例、不加 `tenant_id`**——这是应用级/实例级配置（一个自托管部署一份），不是租户级配置（不要和 <issue>RND-318</issue> 的 `RetentionConfig`、<issue>RND-311</issue> 的 `TenantWecomConfig` 混淆，那两个是真正的租户级配置，本表不是）。

**迁移序位**：当前 alembic head 在 <issue>RND-318</issue>（`0027`）之后。<issue>RND-319</issue>/<issue>RND-304</issue> 若同天并行推进，可能已占用 `0028`/`0029`。**本票预留** `0030`。**实现时先 `git pull` 确认实际 head**，按真实值顺延，不要死磕 `0030` 这个数字。

**❗ 本项目高频踩坑：**
- 不要给这张表加 `tenant_id`（G2 冻结设计，YAGNI）。
- 架构冻结 D1：纯后端，本票不涉及任何端点或前端。

## 目标（Goal）
交付一张全局单例的应用配置存储表，供 T4 解析器读写。

## 范围边界

**In scope：**
1. **迁移** `0030_app_config_store.py`（或实际顺延版本号）：新表 `app_config_store`：
   - `key`（`String`, **主键**，对应 <issue>RND-247</issue> `CONFIG_REGISTRY` 里的 key）
   - `group`（`String`，冗余存一份分组，便于查询时不必每次 join 元数据）
   - `value`（`Text`，nullable——密钥字段落库前由 T2/<issue>RND-245</issue> 的 crypto 模块加密，本表只存密文，不关心是否加密，那是上层的事）
   - `is_secret`（`Boolean`, not null, default `False`）
   - `value_type`（`String`）
   - `requires_restart`（`Boolean`, not null, default `False`）
   - `updated_at`（`DateTime(timezone=True)`, server_default `now()`, onupdate `now()`）
   - `updated_by`（`String`, nullable——记录改动者，存 `AdminUser.id` 或等价标识，**不建外键约束**（配置中心是全局单例，不属于任何租户，`AdminUser` 是租户内实体，两者强关联没有意义，只存一个字符串标识即可，避免引入错误的所有权语义）。
2. `app/db/models.py` **新增** `AppConfigStore` ORM 类。
3. 测试：`backend/tests/test_rnd246_app_config_store.py`。

**Out of scope（显式非目标）：**
- 不做任何读写这张表的业务逻辑（属 T4/<issue>RND-248</issue>）。
- 不做加密（属 T2/<issue>RND-245</issue>，本表只管"存文本"，不管文本是不是密文）。
- 不加 `tenant_id`（G2 冻结）。
- 不建外键约束到 `admin_users`（见上）。

**本工单拥有的文件（只许写这些）：**
- `backend/alembic/versions/0030_app_config_store.py`（新，若 head 漂移则改用实际顺延版本号）
- `backend/app/db/models.py` —— **仅新增** `AppConfigStore` 类
- `backend/tests/test_rnd246_app_config_store.py`（新）

**只读、绝不可写：** `app/config/schema.py`（<issue>RND-247</issue> 拥有，只作为字段命名参照，不修改）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 建表成功**：迁移后表存在，字段类型/约束与上述定义一致。
- **AC-2 无 `tenant_id`（关键）**：`grep -n "tenant_id" backend/alembic/versions/0030_*.py backend/app/db/models.py`（限定 `AppConfigStore` 类范围）**应无命中**——违反 G2 冻结设计。
- **AC-3 无外键约束**：`updated_by` 是纯字符串列，不是 `ForeignKey("admin_users.id")`。
- **AC-4 CRUD 基本可用**：测试直接用 ORM 插入一条配置项、更新、查询，行为符合预期（不经过任何业务层，只测表本身）。
- **AC-5 迁移可逆**：`upgrade`/`downgrade` 均可执行；`alembic check` 无 drift。
- **AC-6 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
git pull   # 确认 alembic head，见「迁移序位」
make verify
.venv/bin/python -m pytest backend/tests/test_rnd246_app_config_store.py -q
.venv/bin/python -m alembic check
grep -n "tenant_id" backend/app/db/models.py | grep -A2 -B2 "AppConfigStore"   # AC-2：人工核对上下文属于 AppConfigStore 类范围内应无命中
git diff -- backend/app/db/models.py    # 人工核对：只新增 AppConfigStore
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
无剩余前置（可与 <issue>RND-247</issue>/<issue>RND-245</issue> 并行）。本票 **blocks** T4（<issue>RND-248</issue>）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明实际使用的迁移版本号（供并行的 <issue>RND-319</issue>/<issue>RND-304</issue> 核对是否冲突）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：迁移版本号与并行的 <issue>RND-319</issue>/<issue>RND-304</issue> 冲突——实现前 `git pull` 核实。
- 回滚：`downgrade` 迁移即可；表本身本票内无任何业务代码读写，回滚零影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿即可，无需额外人工审阅（R1，新表但零业务接入）。
- **Escalation**：若三张迁移票（本票/<issue>RND-319</issue>/<issue>RND-304</issue>）版本号已经冲突且无法自动顺延判断谁在前 → `BLOCKED_NEEDS_HUMAN`，说明冲突情况，不要自行决定顺序。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/db/models.py`（`TenantWecomConfig`/`RetentionConfig` 作为新表定义风格参考，但**不要**照抄它们的 `tenant_id` 列）。
2. `git pull` 确认 alembic head，写迁移。
3. 新增 `AppConfigStore` 模型。
4. 写测试覆盖 AC-1~AC-4。
5. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥。
- 不扩大 Scope：不写加密、不写解析器、不写端点。
- 复用优先：表定义风格仿现有租户级配置表，但不加 `tenant_id`。
- 证据优先，以 exit 0 / 测试通过为证。
