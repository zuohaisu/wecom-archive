# RND-230 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-230 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-230-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-230** 的 10 项 checklist + `design/RND-229-230-search-design-spec.md` 的 **§0 决策 / §5 契约 / §8 衔接点**。

---

## 0. 验收依据

- **Linear RND-230 checklist（10 项，必须全过）**：日期/用户/员工/消息类型四类 Filter 存在；多 Filter 同时生效；筛选结果符合筛选条件；可查看当前生效条件；可清除 Filter 恢复；不影响现有搜索；不影响租户隔离与权限。
- **规格硬约束**：
  - §0 决策 1：`msgtype` 筛选项**全量**覆盖企微全部类型（text/image/voice/video/file/link/system/emotion/chatrecord/redpacket/miniprogram），UI 不裁剪；非 `text` 选中返回空是已知 v1 限制（不报错）。
  - §0 决策 2：`q` 可选（纯筛选模式）；后端至少在存在一个筛选条件时允许 `q` 缺省。
  - §5 契约：`GET /api/search/messages` 新增 `date_from`/`date_to`(ms) 或 `date_range`(1d|7d|30d|90d)、`user`(可重复)、`staff`(可重复)、`msgtype`(可重复)；租户内 AND；不改权限/隔离/分页。
  - §8 衔接点：权限/租户隔离、消息展示与分页、RND-159 内联下拉、视觉令牌零新增——「不动」的部分不能被破坏。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支，且 **RND-229 / RND-228 已在 `main`**（否则 RND-230 的前端/后端基础不存在 → 直接判 **BLOCKED** 并写明缺哪块）。
2. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动 **前台** 进程后再验（不后台化、不加 `&`）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 后端 API（直接打 `GET /api/search/messages`）
- **C1 日期 Filter 可用**：带 `q` + `date_range=7d` 返回 200，且每条 `msgtime` 均 ≥ 当前时间−7天；`date_from`/`date_to`(ms) 同样生效；非法 `date_range`（如 `99d`）应 422/400。
- **C2 用户 Filter（contact 侧）**：构造含多个 contact 参与者的消息，带 `q` + `user=<contactA>` → 结果仅含 contactA 参与者；`user=<A>&user=<B>` 多选 → 含 A 或 B 即满足（参与者语义，非消息全匹配）。
- **C3 员工 Filter（staff 侧）**：带 `staff=<员工id>` → 结果仅含该员工作为参与者的消息；复用 `_collect_staff_ids` 的范围正确（不越权到其他租户 staff）。
- **C4 消息类型 Filter 全量**：`msgtype` 接受全量类型值（`text/image/voice/video/file/link/system/emotion/chatrecord/redpacket/miniprogram`）且不裁剪；选 `text` 返回文本结果；选非 `text`（如 `image`）在 v1 仅索引 text 下**返回空列表（200，非报错）**——这是 §0 决策 1 的已知限制，判 PASS。
- **C5 多 Filter AND**：同时给 `staff=A` + `msgtype=text` + `date_range=7d` → 结果**同时**满足三者（交集），不能只满足其一。
- **C6 结果与筛选一致**：对 C2/C3/C5 的返回逐条校验 `entity_id`/`conversation` 参与关系确实命中筛选（抽查 ≥5 条）。
- **C7 纯筛选模式**：**不带 `q`**，仅 `staff=A`（或 `msgtype=text` 或 `date_range=7d`）→ 返回 200 且结果正确；`q` 缺省 + 无任何筛选 → 400（后端空请求防线）。
- **C8 不影响现有搜索**：`q=天气`（或既有 fixture 关键词）仅带 `q` 时，行为与改造前一致（数量/分页/摘要与 `test_search_api.py` 基线一致）；`test_search_messages_skips_non_text_types` 等旧用例仍 PASS。
- **C9 租户隔离与权限**：用**另一个租户**的认证调用带筛选的接口，断言只能见到本租户数据（沿用 `test_search_api.py::test_search_messages_tenant_isolation` 套路）；筛选参数**绝不**能绕过 `tenant_id`（请求里即使塞 `tenant_id` 参数也应被忽略）。
- **C10 返回结构**：响应含 `msgtype` 字段（前端 footer 不再是固定「文本」）；`conversation_id`/`entity_id`/`entity_type` 等既有导航字段不受影响。

### 前端 UI（`/admin/search` 真实浏览器，Playwright）
- **C11 四个 Filter 触发器存在**：结果页有「日期 / 用户 / 员工 / 消息类型」四个按钮；激活态有 `.active` 蓝边蓝底 + 数量角标。
- **C12 用户/员工选项来源**：用户 popover 选项 = 当前结果中出现的 contact 侧参与者；员工 popover = staff 侧参与者；均多选 + 顶部搜索框过滤选项。
- **C13 消息类型选项全量**：类型 popover 列出**全部**类型（与 `msgtypeLabel` 一致，含 image/voice/video/…），不出现「只列已索引项」的裁剪。
- **C14 已生效条件可见**：选择筛选后渲染 chips（背景 `--primary-soft`、文字 `--primary`、带 `×`）；存在任意激活条件时显示「清除全部」。
- **C15 清除恢复**：点 chip `×` 单项移除并重新请求；「清除全部」清空所有筛选并恢复原结果。
- **C16 纯筛选可用（UI）**：搜索框为空、仅点筛选器 → 触发搜索并出结果/空态（不再卡在「请输入关键词开始搜索」）。
- **C17 状态与交互**：加载骨架 / 空态 / 错误态正确；排序选择器仍工作；点击结果卡仍能跳转回对话并高亮（RND-229 的 `focusMessage` 不受影响）。
- **C18 i18n**：`search.filter.*` 键在 zh-CN / zh-TW / en 均存在且一致（见 `backend/app/assets/i18n.js` ~218–247 / ~490+ / 英文区段）；可见文案与规格 §6 的 zh-CN 值一致。
- **C19 视觉令牌零新增**：筛选栏/chip 仅使用现有 `--primary`/`--primary-soft`/`--border`/`--text-*`/`--radius`，**无**新颜色/字体/圆角（对照 `design/RND-229-230-search-design-spec.md` §1 令牌表）。

### 回归（不破坏既有能力）
- **C20 RND-228 可扩展性仍在**：`test_rnd_228_search_scalability.py` 全绿，尤其 `test_search_messages_staff_resolution_uses_shared_helper`（~177）——证明新增筛选**没有**把 `_collect_staff_ids` 替换成内联/Python 端全量过滤；大租户下搜索仍是 SQL 层 bounded（无全量拉取/N+1）。
- **C21 RND-229 跳转不受影响**：从结果页点卡片 → 回 `/admin/conversations` 并 `focusMessage` 滚动+高亮。
- **C22 RND-159 内联下拉不受影响**：控制台搜索框输入仍触发就地导航下拉（Enter→结果页的新增行为不变，下拉旧行为不变）。
- **C23 路由数不变**：`test_http_contract.py:318` 断言路由数 == 33 仍 PASS（RND-230 未新增路由）。
- **C24 前端 JS 合法**：`test_search_page_js_syntax.py::test_search_page_script_parses` 仍 PASS。

---

## 3. 测试方法

- **后端**：`cd backend` 后跑
  - `python -m pytest tests/test_rnd_230_search_filters.py tests/test_search_api.py tests/test_rnd_228_search_scalability.py tests/test_http_contract.py tests/test_search_page_js_syntax.py -q`
  - 若 `test_rnd_230_search_filters.py` 不存在（开发 agent 未交付测试）→ 本 agent **自行补最小验收测试**覆盖 C1–C10，再判定。
- **前端 / 集成**：用项目既有 Playwright 脚本做真实浏览器 smoke，覆盖 C11–C19（必要时本 agent 编写针对性 Playwright 步骤或最小脚本）。
- **收口**：跑 `make verify` 确认 lint-diff + typecheck + build + 全量 pytest 全绿。

---

## 4. 硬性约束（验收 agent 自身也要守）
- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过租户隔离做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-230 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 是否含 RND-229/RND-228：是/否 · make verify：通过/失败

| 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|------|--------|------|---------------------------|
| C1   | 日期 Filter | PASS | ... |
| C2   | 用户 Filter | PASS | ... |
| ...  | ...    | FAIL | 复现：<步骤>；期望：<X>；实际：<Y> |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 非 text 类型选中返回空（v1 索引限制，§0 决策1）：已确认不报错 → 视为 PASS 非缺陷。
- 其他观察到的限制：<…>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺 RND-229 或 RND-228）。
```

- 若某条无论如何无法复现（如环境问题导致 C11–C19 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-230 评论贴出（状态保持 In Progress，交还用户 Haisu 决策合并）。
