[Goal check] This work advances 开发（Development） by 交付 GET /api/admin/external-contacts 列表+筛选端点，复用既有 ExternalContact 模型与 display_names 展示名逻辑，解除 RND-330 的硬阻塞。

# RND-288 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-288 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-288「A4-2 外部联系人列表/筛选 API」｜Linear team `Builder`
- 优先级：Urgent｜风险等级：**R1**｜milestone：R1 · 前端快赢四页
- **本票 blocks RND-330**（contacts 页面）

## 背景（已实地核实，来自 RND-330 开发 agent 的真实 BLOCKED 报告）

RND-330（contacts 页面）原计划对接 `GET /api/contacts` + `GET /api/search/contacts`，开发 agent 核实后发现两者都**不是** `ExternalContact` 数据：

- `GET /api/contacts`（`listing_service.list_contacts()`）返回**归档参与者**——从消息里出现过的 wecom_userid 推导，只有 `contact_id`/`display_name`/`raw_id` 三个字段，跟"外部联系人"这个业务概念完全不是一回事。
- `GET /api/search/contacts` 查询的是内部 `Contact` 表 + `AdminUser`（员工），同样不碰 `ExternalContact`。

`ExternalContact` 表（`backend/app/db/models.py:891-930`，A4-1/RND-287 已 Done 交付）里数据已经通过 WeCom 同步进来了，只是**从来没有 API 把它列出来**——这是纯粹的缺口，不是设计分歧。

**已实地核实的有利条件（直接决定你的实现方式）：**
1. `ExternalContact` 字段：`external_userid` / `name` / `company` / `tags` / `source` / `owner_wecom_userid` / `last_interaction_at` / `message_count` / `tenant_id`。
2. **已有 `name`、`company` 的 trigram 索引**（`ix_external_contacts_name_trgm` / `ix_external_contacts_company_trgm`，PostgreSQL GIN）——这张表设计时就是为搜索/筛选准备的，直接用 `ILIKE` 或等价查询即可吃到索引，**不需要新加索引、不需要新迁移**。
3. **`tags` 存的是 JSON 字符串**（`backend/app/services/external_contact_sync.py:90`：`json.dumps(tag_values, ensure_ascii=False)`），是一个标签名数组序列化后的结果，不是逗号分隔，也不是 JSONB 列。**按标签筛选时不能用裸 `LIKE '%value%'`**——会有子串误命中（筛 "VIP" 会连带命中 "VIP2026"）。用带引号的模式（如 `LIKE '%"VIP"%'`，注意转义）或反序列化后在应用层精确比对。**必须写测试证明不会子串误命中。**
4. **没有现成的 list/filter 查询函数可复用**——`backend/app/db/external_contacts.py` 目前只有 `upsert_external_contact`（sync worker 写入用）。本票需要新写查询逻辑，这不是"忘了复用"，是真的没有。

**❗ 本项目高频踩坑：**
- **架构边界硬闸**：router 不得 import `app.main`；新建的 `routers/external_contacts.py` 不需要登记 `_FLAT_SERVICE_MODULES`（它是 router 层，不是 service 层）。
- **租户隔离硬规则**：`tenant_id` 只能来自鉴权上下文（参考 `backend/app/routers/users.py` 里 `require_role()` 解包 tenant_id 的模式），**绝不**接受请求参数里的 tenant_id。
- **展示名逻辑必须复用**：`owner_display_name` 用 `app/display_names.py` 的 `resolve_person_display_name(raw_id, name)`——**不要**自己拼接或简化实现，这是本项目里第二次出现"展示名需要复用既有解析器"的情况（第一次是 RND-334），说明这条纪律是真实存在的重复性坑，不是巧合。

## 目标（Goal）
让 `GET /api/admin/external-contacts` 把 `ExternalContact` 表的数据以列表 + 筛选 + 分页的形式暴露出来，供 RND-330 的前端页面直接消费。

## 范围边界

**In scope：**
1. `backend/app/schemas/external_contact.py`（新）：定义列表响应 schema，字段对齐 `ExternalContact` 模型（见上），`tags` 在响应里应是**数组**（反序列化 JSON 字符串），不是原始 JSON 字符串。
2. `backend/app/routers/external_contacts.py`（新）：
   - `GET /api/admin/external-contacts`：列表 + 筛选（`company`、`tags`、`owner_wecom_userid` 均为可选查询参数）+ 分页（`offset`/`limit`，参考 `media_library.py` 的分页风格）。
   - 租户隔离：`tenant_id` 从鉴权上下文取。
   - `owner_display_name` 字段：调用 `resolve_person_display_name`。
3. `backend/app/main.py`：注册新 router，前缀 `/api/admin`。
4. `backend/tests/test_external_contacts_api.py`（新）：覆盖列表、三种筛选各自生效、标签筛选无子串误命中、分页、跨租户隔离。

**Out of scope（显式非目标）：**
- **不做详情时间线**（A4-3，RND-289，独立票，contact-detail 页面不是本票范围）。
- **不改 `ExternalContact` 模型**、**不改 `external_contact_sync.py`**、**不新增迁移**——数据已经在库里，本票只读不写。
- 不做导出功能（C2 范围）。
- 不改 `GET /api/contacts` 或 `GET /api/search/contacts`（那两个端点服务别的用途，继续保持原样）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/schemas/external_contact.py`（新）
- `backend/app/routers/external_contacts.py`（新）
- `backend/app/main.py`（仅 `include_router` 1-2 行）
- `backend/tests/test_external_contacts_api.py`（新）
- **`backend/tests/test_http_contract.py`（2026-07-30 追加授权，强制随附改动）** —— 本票新增 1 个路由，必须同步：① 第 326 行 `route_count`（**先跑 `make verify` 读出当前真实基线 N，改成 N+1；截至撰写时 main 上是 53，但以你实际读到的为准，禁止写死数字**）；② expected path 集合追加 `/api/admin/external-contacts`；③ snapshot 列表追加对应条目。**不得**改动他票条目。
- **`backend/tests/test_rnd280_rbac_scaffold.py`（2026-07-30 追加授权，强制随附改动）** —— 第 77-86 行 `test_require_role_is_attached_only_to_authorized_admin_routes` 是**闭世界白名单**：它遍历 `app/routers/*.py`，对不在 `{audit.py, media_library.py, users.py}` 里的文件断言 `"Depends(require_role" not in source`。本票的 `external_contacts.py` 用了 `require_role()`，**不加进白名单必然失败**。只在白名单加本票 router（照 `audit.py`/`media_library.py` 的 `assert "Depends(require_role())" in source` 写法），**不得**放松既有条目断言。

> **为什么这两个文件属于本票**（2026-07-30 更正）：首轮实现漏了它们，QA 判 FAIL 并建议"另开契约维护票"——那个建议是错的，拆开会让 `main` 在两票之间持续红灯。契约测试同步是新增路由的**强制随附改动**，不是别人的活。详见 `docs/ticket-autopilot-workflow.md` §3.3。

**只读、绝不可写：**
- `backend/app/db/models.py`（不改模型）
- `backend/app/services/external_contact_sync.py`（不改 sync 逻辑）
- `backend/app/db/external_contacts.py`（不改既有 upsert 帮助函数）
- `backend/app/display_names.py`（只调用 `resolve_person_display_name`，不修改）
- `backend/app/routers/conversations.py`、`backend/app/routers/search.py`（不改现有 `/api/contacts`、`/api/search/contacts`）
- RND-327/328/329/330/331 拥有的任何文件

## 验收标准（Acceptance Criteria）

- **AC-1 真实数据**：列表由 `external_contacts` 表真实查询填充（非 mock）。
- **AC-2 三种筛选生效**：`company`、`tags`、`owner_wecom_userid` 各自独立筛选正确（有测试）。
- **AC-3 标签筛选精确匹配（关键）**：筛 `"VIP"` 不得连带命中标签含 `"VIP2026"` 的行——必须有一条测试构造这个反例并断言不误命中。
- **AC-4 owner_display_name 经既有函数产出、且参数传对（2026-07-30 加强）**：代码可见 `from app.display_names import resolve_person_display_name` 的 import 与调用，**且实参顺序/语义正确**。
  > ⚠️ **首轮实现疑似在此出错，务必看清签名**：`resolve_person_display_name(raw_id: Optional[str], name: Optional[str] = None)`——第二个参数是**展示名**（通常取自 `Contact.name`），**不是 `tenant_id`**。函数体是「`name` 非空就直接返回 `name`，否则回退到 `raw_id`」。若误传 `tenant_id` 作第二个参数，每一行的 `owner_display_name` 都会渲染成**租户 UUID**，而且不报错——页面上看起来"有值"，实际全是错的。
  > 归属员工的真实展示名需要从 `Contact` 表按 `owner_wecom_userid` 查（参考 `conversation_membership.py` 的 `_load_display_names_for_ids` 批量取法，避免 N+1），查不到时传 `None` 让它回退到 raw_id。
  - **必须有测试断言**：构造一条 `Contact`（`wecom_userid=owner_wecom_userid`, `name="张三"`）+ 一条 `ExternalContact`，断言返回的 `owner_display_name == "张三"`；再构造一条无对应 `Contact` 的，断言回退为 `owner_wecom_userid` 本身。**断言 `owner_display_name != tenant_id`**（防住上面那个误传 bug）。
- **AC-5 分页**：`offset`/`limit`（或等价参数）存在且生效，大于总数时不报错、返回空列表而非异常。
- **AC-6 租户隔离**：跨租户请求不返回其他租户的 `ExternalContact` 行（有测试直接构造两个租户的数据并验证隔离）。
- **AC-7 回归（含契约测试同步）**：`make verify` **全绿**；`test_architecture_boundary.py` 通过；`test_http_contract.py` 与 `test_rnd280_rbac_scaffold.py` 均已按上方授权同步且通过。
  > 若 `make verify` 因**他票的在途未提交改动**而失败（本项目并行方案是共享工作树，见 `docs/ticket-autopilot-workflow.md` §3.4）：先 `git status --porcelain` 分离归因，在交付报告里如实说明"哪些失败属于本票、哪些来自他票"，**不要**为了让整棵树变绿去改他票的文件。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_external_contacts_api.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py -q          # 契约同步后必须绿
.venv/bin/python -m pytest backend/tests/test_rnd280_rbac_scaffold.py -q   # 白名单同步后必须绿
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git status --porcelain    # 先看清哪些改动是自己的（共享工作树可能有他票在途改动）
git diff --stat -- backend/app/db/models.py backend/app/services/external_contact_sync.py backend/app/db/external_contacts.py backend/app/display_names.py backend/app/routers/conversations.py backend/app/routers/search.py   # 必须全无输出
```
通过 = 7 条 AC 全满足且上述命令 Exit Code 均为 0。

## 依赖（Dependencies）
A4-1（RND-287）已 Done，无剩余前置，可立即开始。**本票阻塞 RND-330**，应优先完成。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有对应测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的 4 个文件
- [ ] 产出 QA Summary，**说明最终端点响应的确切字段名**（供 RND-330 后续对接）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险 1：`tags` 用裸子串匹配筛选，产生假阳性命中，导致筛选结果不可信。**由 AC-3 显式防守。**
- 风险 2：忘记租户隔离，导致跨租户数据泄露——这是 PII（外部联系人姓名/企业/标签属于客户数据）。**由 AC-6 显式防守，务必写测试而不是口头保证。**
- 风险 3：大表全量拉取无分页，随租户规模增长拖垮响应。**由 AC-5 防守。**
- 回滚：纯新增文件 + `main.py` 一行注册，`git checkout -- <files>` 即可，无数据/迁移影响。

## 人工点位
- **Trigger**：Haisu / PM 置 In Progress（已置）。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：若 `tags` 的 JSON 文本匹配方式在 PostgreSQL 下有更好的原生方案（如转 jsonb 做 containment 查询）但需要额外迁移改列类型 → 先按字符串模式匹配实现（不改列类型），把"是否值得后续迁移成 JSONB"记入 QA Summary 供 Haisu 决定，不要在本票里顺手改列类型。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`ExternalContact` 模型全文（`models.py:891-930`）、`external_contact_sync.py`（理解 `tags` 的写入格式）、`display_names.py` 的 `resolve_person_display_name`、`media_library.py`（分页/筛选风格参考）、`users.py`（租户隔离与鉴权模式参考）。
2. 写 schema。
3. 写 router：列表查询 + 三种筛选 + 分页 + owner_display_name。
4. 写 `main.py` 注册。
5. 写测试覆盖 AC-1~AC-6，**尤其 AC-3 的标签子串反例、AC-6 的跨租户反例**。
6. 跑 `make verify`，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据/密钥。
- 不扩大 Scope：详情时间线、导出、修改 sync 逻辑一律 Out。
- 复用优先：`resolve_person_display_name` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
