# RND-230 执行提示词（单人端到端：复现 → 实现 → 验证）

> 用途：粘贴给单一开发智能体（hy3 / Claude Code / Codex 皆可），由其端到端跑完 RND-230。
> 工单：`RND-230「增强聊天搜索：支持多维 Filter 精准定位聊天内容」`，优先级 P3（注：Linear 实时状态为 **In Progress**，assignee Haisu Zuo），父脉络 RND-229（结果页）+ RND-228（search.py 可扩展性）。
> 设计规范权威来源：`design/RND-229-230-search-design-spec.md`（下称「规格」），其中 **§0 决策 / §5 契约 / §8 衔接点** 为本任务硬约束。

---

## 0. 任务与来源

- **目标**：在已上线的独立搜索结果页（RND-229，`/admin/search`）之上，增加四类 Filter（**日期 / 用户 / 员工 / 消息类型**），使结果可按维度精准收窄；支持多 Filter 同时生效（AND），可查看/移除/清除生效条件；支持**纯筛选模式**（不输入关键词，仅用筛选器）。
- **验收清单（来自 Linear 工单，逐条必过）**：
  - [ ] 搜索结果页提供日期 Filter
  - [ ] 搜索结果页提供用户 Filter
  - [ ] 搜索结果页提供员工 Filter
  - [ ] 搜索结果页提供消息类型 Filter
  - [ ] 支持多个 Filter 同时生效
  - [ ] Filter 后结果符合筛选条件
  - [ ] 用户可以查看当前生效条件
  - [ ] 用户可以清除 Filter 恢复搜索结果
  - [ ] 不影响现有搜索功能
  - [ ] 不影响租户隔离和权限控制
- **非目标（严禁，避免 gold-plate）**：不改搜索算法/ ranking；不引入新搜索引擎/AI 语义搜索；不改消息数据模型/存储结构；不新增权限模型；不重构整张 search router。重点是「在现有 `text` 索引上叠加可组合的筛选维度」。

### 前置依赖（开工前必须先确认已在 `main`）
1. **RND-229 已合并**：`backend/app/main.py` 存在 `_SEARCH_PAGE_HTML`（~2812）与 `GET /admin/search`（~3198），且结果页 JS 含 `doSearch()`（~2946）、`render()`（~2947）、`init()`（~2948）、`focusMessage` 跳转回对话逻辑（~2712）。若这些缺失 → **停下，先报告阻塞**，不要自己补 RND-229 的活。
2. **RND-228 已合并**：`backend/app/routers/search.py` 的 `search_messages`（~241）已用 `_collect_staff_ids(db, tenant_id)`（来自 `app/conversation_membership.py`）做 staff 解析，且查询在 **SQL 层 bounded**（RND-228 的可扩展性改造）。本任务必须**复用**该 helper 与 recipients 查询模型，禁止重复实现或退化成 Python 端全量过滤。

---

## 1. 精确落点（文件 / 函数级）

### 1.1 后端 · `backend/app/routers/search.py`
- `search_messages`（~241）当前签名：`q: str = Query(..., min_length=1)`（**必填**），且 WHERE 硬编码 `ArchiveMessage.msgtype == "text"`。
- **改动 A · `q` 变可选**：改为 `q: Optional[str] = Query(None, min_length=1, description="...")`。当 `q` 为 `None` 时，**跳过** `content_text.ilike(pattern)` 条件（不报错）。
- **改动 B · 新增筛选参数**（均 `Optional`、可重复，`Query(None)`）：
  - `date_range: Optional[str] = Query(None, pattern="^(1d|7d|30d|90d)$")` —— 预设；后端据当前时间推导 `date_from`（`msgtime >= from_ms`）。
  - `date_from` / `date_to: Optional[int]`（ms）—— 显式时间范围，与 `date_range` 二选一/可叠加（都给则取交集）。
  - `user: Optional[list[str]] = Query(None)` —— contact 侧参与者 userid，多选（AND 含义：消息参与者命中集合内任一即满足，见下）。
  - `staff: Optional[list[str]] = Query(None)` —— 员工/监控账号 userid，多选。
  - `msgtype: Optional[list[str]] = Query(None)` —— 消息类型，多选。
- **改动 C · 组合规则（租户内 AND）**：
  - `tenant_id == tenant_id`（永远来自 `get_current_user`，**绝不**接受请求传入 tenant）。
  - `decrypt_status == "success"`、`is_revoked.is_(False)` 保持。
  - `msgtype`：若 `msgtype` 参数**有值** → 用 `ArchiveMessage.msgtype.in_(msgtype)` **替换**原本的 `== "text"`；若**无值** → 保持 `== "text"`（现有关键词搜索语义不变）。
  - `date`：`msgtime >= date_from`（及 `<= date_to`）。
  - `user` / `staff`（参与者过滤）：消息满足「`sender ∈ 集合` **或** `存在 recipient ∈ 集合`」。实现上**复用 `app/conversation_membership.py` 中 `_load_recipients_map` 所用的 recipients 模型/表**做 SQL 层 `EXISTS`/子查询（`ArchiveMessage.id IN (SELECT message_id FROM <recipients> WHERE recipient_id IN :ids)`），**不要**先把全部消息拉进 Python 再过滤（会破坏游标分页与 RND-228 的 bounded 属性）。`staff` 集合应先与 `_collect_staff_ids(db, tenant_id)` 取交集以收紧范围；`user` 集合对应 contact 侧 userid。
  - `q` 与以上筛选条件之间也是 AND。
- **改动 D · 空请求防护**：若 `q is None` **且** 所有筛选参数均为 `None` → 返回 `400`（避免无约束全表扫描）。注意：前端在「无 q 且无筛选」时本就不该发起请求（见 1.2），但后端必须有此硬防线。
- **改动 E · 返回结构补全**：`MessageSearchResult`（~49）当前**无 `msgtype` 字段**（前端 footer 靠 `msgtypeLabel(r.msgtype)` 回退到「文本」）。新增 `msgtype: Optional[str] = None` 并在组装时从 `ArchiveMessage.msgtype` 填入，使类型筛选的效果可被前端正确呈现/可被测试断言。

### 1.2 前端 · `backend/app/main.py` 的 `_SEARCH_PAGE_HTML`（~2812）
- **筛选栏（sticky）**：在 `results-header`（~2902）与 `results-body`（~2916）之间插入筛选栏 DOM：四个触发器按钮（日期/用户/员工/消息类型），激活态加 `.active`（蓝边+浅蓝底）并显示数量角标；下方渲染已生效条件 chips（每个 chip = `--primary-soft` 背景 + `--primary` 文字 + `×` 移除按钮；存在任意激活条件时显示「清除全部」）。
- **四个 popover**：
  - 日期：单选（全部时间 / 近24小时 / 近7天 / 近30天 / 近90天）→ 映射到 `date_range=1d|7d|30d|90d`。
  - 用户：多选，选项**来自当前结果中出现的 contact 侧参与者**（遍历 `ALL`，收集 `entity_type==='contact'` 的 sender/联系人；带显示名），顶部带搜索框过滤选项。
  - 员工：多选，选项来自当前结果中出现的 staff 侧参与者（`entity_type==='staff'`）。
  - 消息类型：多选，选项 = `msgtypeLabel(t)`（~2928）的**全量静态类型**（text/image/voice/video/file/link/system/emotion/chatrecord/redpacket/miniprogram）——**不**按后端索引裁剪（规格 §0 决策 1）。
- **接线 `doSearch()`（~2946）**：把当前激活的筛选拼进请求参数（`q` 可空；`date_range`；`user`/`staff`/`msgtype` 可重复）。分页游标 `before` 原样透传。
- **纯筛选模式**：`init()`（~2948）已有「无 q 时 qLabel 显示『筛选结果』」的逻辑；补一处：当存在激活筛选时，**即使 `KEYWORD` 为空也调用 `doSearch()`**（当前空 q 会停在「请输入关键词开始搜索」空态）。仅当「无 q 且无任何筛选」才保留该空态提示。
- **chips 交互**：点击 `×` 移除单项并重新 `doSearch()`；「清除全部」清空所有筛选并恢复。筛选变化后更新 chips 与结果计数。
- **可选 · URL 状态**：把激活筛选反映到 `location.search`（如 `?q=&date_range=7d&staff=...`），便于浏览器后退/分享；非必须，但若做则保持与现有 `?focus=msgid` 跳转逻辑不冲突。

### 1.3 i18n · `backend/app/assets/i18n.js`
- `search.*` 与 `search.filter.*` 键**已存在**（zh-CN ~218–247、zh-TW ~490+，英文区段同理）。**不要重新创建**；直接复用 `I18N.t('search.filter.date')` 等。
- 新筛选栏文案优先接 `I18N.t(...)`（与全站 i18n 风格一致）；若往搜索页注入 `I18N_SCRIPT_TAG` 成本过高，可沿用本页现有硬编码中文风格，但**可见文案必须与规格 §6 的 zh-CN 值一致**（如「日期/用户/员工/消息类型/清除全部/已生效条件」）。

### 1.4 CSS
- 仅追加筛选栏/chip 所需类（`.filter-bar` / `.filter-btn` / `.active` / `.chip` / `.chip-x` 等），**全部复用 §1 令牌（`--primary` / `--primary-soft` / `--border` / `--text-*` / `--radius`）**。零新增颜色、零新增字体/圆角语言（规格 §8）。

---

## 2. 执行步骤（严格：复现(RED) → 实现 → 验证(GREEN)）

### 阶段一：复现 / 红灯（先证明缺口，禁止先改实现）
- 在 `backend/tests/` 新增 `test_rnd_230_search_filters.py`，断言「尚未实现」的筛选行为，预期**失败（RED）**：
  - T1：不带 `q`、带 `msgtype=text` 调 `/api/search/messages` → 当前因 `q` 必填返回 422，断言期望 **200**（证明 q 可选未实现）。
  - T2：带 `q` + `staff=<某员工id>` → 断言结果仅包含该员工作为参与者的消息（当前返回全部匹配 q 的消息，不过滤）。
  - T3：带 `q` + `msgtype=image`（非 text）→ 断言请求被 `msgtype.in_(...)` 接管（当前硬编码 `==text` 会忽略该参数；注意 v1 仅索引 text，此用例预期返回空列表而非报错——用来锁定「非 text 选中返回空」的正确降级）。
  - T4：带 `q` + `date_range=7d` → 断言结果 `msgtime` 均在近 7 天（当前忽略该参数）。
  - T5（租户隔离，必过基线）：跨租户 user 调带筛选的接口，断言只能见到本租户消息（沿用 `test_search_api.py::test_search_messages_tenant_isolation` 套路）。
- 运行该测试，确认 T1–T4 **RED**、T5 通过（现状隔离已正确）。若某项无论如何都通过 → 给出 `NOT REPRODUCED` 证据写进 Linear 评论，**绝不假装修好**。
- 同时锁定回归基线（应已 GREEN）：`test_search_api.py` 现有 `q`-only 用例（如 `test_search_messages_by_content` 195 / `test_search_messages_pagination` 332 / `test_search_messages_skips_non_text_types` 249）。

### 阶段二：实现（最小改动，严守 §1 + 硬约束）
- 按 1.1 改 `search_messages`：q 可选 + 五类参数 + AND 组合 + 400 防线 + 返回 `msgtype`。
- 按 1.2 改 `_SEARCH_PAGE_HTML`：筛选栏 DOM + 四个 popover + 接线 `doSearch` + 纯筛选模式 + chips 交互。
- 按 1.3 复用 i18n 键；按 1.4 追加 CSS。
- **必须更新既有回归测试**：`test_search_api.py::test_search_messages_empty_query_returns_422`（~237）原本断言空 `q` 返回 422；改为「空 q **且无筛选** → 400/422；空 q **但有筛选** → 200」。
- 前端改动保证 `backend/tests/test_search_page_js_syntax.py::test_search_page_script_parses`（~29）仍通过（JS 语法合法）。

### 阶段三：验证（不达标不收工）
- 阶段一 T1–T4 必须转 **GREEN**；T5 保持通过。
- 新增/扩展测试覆盖工单验收点：
  - 后端：日期/用户/员工/类型 各自过滤正确；多 Filter AND 组合；纯筛选（无 q）返回合理结果或空；`msgtype` 全量选项不裁剪；非 text 选中返回空不报错；`date_from/to` 与 `date_range` 行为。
  - 前端（Node 驱动或 Playwright）：筛选参数正确拼入请求；chips 渲染/移除/清除全部；纯筛选模式可触发搜索；排序仍工作。
- 运行既有套件确认无回归（先 `cd backend`）：
  - `python -m pytest tests/test_search_api.py tests/test_rnd_228_search_scalability.py tests/test_search_page_js_syntax.py tests/test_http_contract.py -q`
  - **重点盯 `test_rnd_228_search_scalability.py`**：其中的 `test_search_messages_staff_resolution_uses_shared_helper`（~177）等确保你没把 `_collect_staff_ids` 换成内联实现——本任务必须继续复用。
  - 收工前跑 `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
- 浏览器 smoke（项目既有 Playwright 脚本）：在结果页加筛选 → 结果收窄正确；移除/清除恢复；跳转回对话+高亮（RND-229）不受影响；RND-159 内联下拉不受影响。

---

## 3. 硬性约束（不可违反）
- **复用 RND-228 的 `_collect_staff_ids` 与 recipients 查询模型**；参与者过滤必须是 **SQL 层 bounded**（EXISTS/子查询），禁止把消息全量拉进 Python 再过滤，禁止 N+1。
- **租户隔离铁律**：`tenant_id` 永远来自 `get_current_user`；筛选条件**绝不**扩展现有隔离/权限分支；跨租户数据不可见。
- **`msgtype` 选项全量**（用 `msgtypeLabel` 静态全集），UI 不裁剪；非 `text` 类型选中后返回空是已知 v1 限制（规格 §0 决策 1），属正常降级，**不得报错或伪装结果**。
- **`q` 可选但必须有约束**：`q` 与至少一个筛选不同时存在 → 400；绝不允许无约束全表扫描。
- **不改 RND-159 内联下拉**、不改消息展示/分页/游标逻辑、**不改视觉令牌**（零新颜色）、**不改 RND-228 的 SQL 级 LIMIT 可扩展性**（新筛选同样必须在 SQL 层 bounded）。
- **不新增路由**：扩展既有 `GET /api/search/messages`，`test_http_contract.py:318` 的路由数（33）必须保持不变；不要为筛选新开 endpoint。
- 不引入后台进程；假设开发服务器已在运行；命令前台运行。
- 参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit` / `push`**（需用户显式授权）。

---

## 4. 收尾动作
- 在 Linear 把 RND-230 状态保持 `In Progress`（已置）；写一条评论：改动文件/函数级清单 + 红灯→绿灯证据 + `make verify` 结论 + 对「非 text 选中返回空」已知限制的重申。
- **不要自动合入/提交**，保留给用户（Haisu）人工 merge 关卡。
- 若实现中发现「用户/员工多选与游标分页的交互」需要比预期更大的改动，或在 v1 索引下某筛选实质无效，在评论中**如实标注边界**，不私自扩展范围。
