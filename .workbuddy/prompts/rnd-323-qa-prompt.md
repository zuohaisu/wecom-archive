# RND-323 QA 验收提示词 —— 登录后自动选中监控账号（单账号默认 / 多账号恢复上次选中，按租户+用户双重隔离）

> 面向测试 agent（独立验收 RND-323）。本文件即你的完整验收 brief。
> 依据开发提示词（`rnd-323-execution-prompt.md`）的实现进行验收。全过程中**不 `git commit` / `git push`**。
> 代码标识符一律加反引号。真实验证优先于「源码里出现了某函数」式的检查。
>
> ⚠️ 本票 issue 已修订（status=Todo），权威决策 = 持久化 key `rnd.lastEntity.<tenantId>.<userId>`（双重隔离），含 `/api/auth/me` 补 `id` 的小后端改动。以下按修订后决策验收。

## 一、验收映射（AC → 验证方式）

| # | Linear AC（修订后） | 验证 |
|---|---------------------|------|
| 1 | 仅 1 个监控账号：登录后该账号被自动选中，会话列表+时间线自动加载，不显示「选择监控账号或联系人」空态 | 构造 1 项 `monitored-accounts`；`renderEntityList` 后断言 `selEntityId===items[0].staff_id`、`conv-body` 非 `console.selectAccountOrContact`、且 `loadConversations` 被调用（fetch `/api/conversations?mode=staff&staff_id=...`） |
| 2 | 多个监控账号：首次进入显示空态；手动选 A 后刷新/重新登录 → 自动选中 A | 构造 >1 项且无 stored key → `selEntityId===null`、空态可见；手动 `onEntityClick(A)` → `localStorage['rnd.lastEntity.<tid>.<uid>']===A`；再次 `renderEntityList`（模拟刷新）后 `selEntityId===A` |
| 3 | **按租户隔离**：租户 X 的选中偏好不作用于租户 Y（同浏览器切换账号/租户不串味） | 用 `tenant_id=T1` 渲染并选 A，再切 `tenant_id=T2` 渲染 → 读 `rnd.lastEntity.T2.<uid>` 不应是 A（或为空态）；各自 key 独立 |
| 4 | **按用户隔离**：同租户内 U1 选 A、U2 选 B → 各自恢复，互不覆盖（同浏览器不同账号登录验证） | `tenant_id=T`、`userId=U1` 选 A；`userId=U2` 选 B；分别读 `rnd.lastEntity.T.U1`/`rnd.lastEntity.T.U2` 互不串；各自 `renderEntityList` 恢复各自选择 |
| 5 | 多账号且上次选中账号已不存在（被删）→ 回退空态，不报错 | stored key = 不存在的 id（且 `tenant_id`/`userId` 齐全）；`renderEntityList` 后 `selEntityId===null`、空态可见、无异常 |
| 6 | contact 模式行为不变（仍手动选） | 切 `mode='contact'` 后任意 `renderEntityList` **不**触发自动选中（无 `selectEntityIfPresent` 调用、`selEntityId` 保持原值） |
| 7 | 切换语言（`applyLocale`）后已选中账号保持，不闪空态 | 选中 A 后调 `applyLocale()`：`selEntityId===A`、`conv-body` 不显示 `console.selectAccountOrContact` |
| 8 | 相邻回归：搜索后选中（`onSearchContactItemClick`）、会话列表刷新（`refresh.js`）、RND-320 chatrecord 折叠不受影响 | 见 §四回归 |
| 9 | **后端回归**：本票改动的 `/api/auth/me` 返回体与现有前端（settings、导航栏显示名）兼容；RND-110/RND-112 登录回归全绿 | `/api/auth/me` 仍 200；现有前端消费 `display_name` 处（导航栏 `#current-user`）仍正确；跑 RND-110/RND-112 相关登录测试 |

## 二、安全 / 契约

- key 双重隔离 `rnd.lastEntity.<tenantId>.<userId>`；**仅持久化实体 `id` 与登录身份 `tenant_id`/`id`，无 PII / 无消息内容泄露**。
- `tenant_id`/`user_id` 缺失时（AC 健壮性）**不写入也不读取**，降级为空态（不自动选第一个）。
- `localStorage` 不可用（隐私模式 / 禁用）时读写 `try/catch` 静默跳过，不抛错、不阻塞渲染。
- `/api/auth/me` 新增 `id` 字段：不破坏既有前端（导航栏/settings 用 `display_name`，不用 `id`）。
- 复用 `selectEntityIfPresent`，其 `onEntityClick` 副作用（重置 conv/timeline、调 `loadConversations`）符合自动选中预期。

## 三、架构 / 范围守门

- diff 应含：`backend/app/routers/auth.py`（一行，补 `id`）+ `backend/app/web/static/console/{conversation-list.js, console-state.js, api-client.js}` + 本票测试文件。
- **无** Alembic 迁移、**无**新 router 文件、**无** `main.py` 改动、**无**新依赖、**未**引 React（D1 冻结）。
- `git diff --stat` 期望仅上述文件。`/api/auth/me` 之外无其他端点 diff。
- 若发现 diff 触碰 `password_login`/`wecom_login`/`logout`/`selectEntityIfPresent`/`applyLocale` 语义、或任何迁移/依赖 → **判失败**，要求开发 agent 撤销越界改动（违反硬约束）。

## 四、回归（务必跑）

1. **搜索后选中（`onSearchContactItemClick`，`console-entry.js:289`）**：恰有 1 个监控账号时，搜索某联系人并点击 → 最终应落在该搜索实体（contact 模式选中），**不被**单账号 auto-select 抢占显示错账号。验证：构造 1 项 staff + 触发 search-then-select 流程，断言最终 `selEntityId` 为搜索目标而非自动选中的 staff id；若发现 race，要求开发 agent 以 `!searchActive`（或等价搜索态标志，见 `setSearchActive`）守卫 `maybeAutoSelectEntity`。
2. **会话列表刷新（`refresh.js` 自动刷新）**：刷新会再次 `loadEntityList` → `renderEntityList`；因 `!selEntityId` 守卫，已选中时**不重复**触发 auto-select、不闪空态、不重置已加载会话。验证：选中 A 后模拟一次 refresh 路径，断言 `selEntityId` 仍为 A 且 `loadConversations` 未被 auto-select 二次调用。
3. **RND-320 chatrecord 折叠态（`message-renderers.js`）**：auto-select 不触碰消息渲染，理论上无耦合；回归确认：选中含 chatrecord 消息的会话后，折叠态仍不渲染媒体（无 `<img>/<video>/<audio>`），与 RND-320 验收一致。
4. **后端 `/api/auth/me` 兼容（RND-110/RND-112）**：用真实浏览器或 node 跑 `console-state.js` 的 `loadCurrentUser`，断言导航栏 `#current-user` 显示名仍正确（用 `display_name`）；跑项目登录相关回归测试（RND-110/RND-112）全绿。

## 五、测试落地（node harness —— 与现有 console JS 测试同范式）

项目已有 console JS 的 node 测试范式：`backend/tests/test_archive_console_v2_qa_fixes.py` 用 `from tests._node_runner import run_node` + `from tests._rnd216_web_shims import review_console_js_source`，以内存 DOM stub 跑**真实** `console/*.js`。

**新增 `backend/tests/test_rnd323_entity_autoselect.py`**，镜像该范式：

```python
"""
RND-323 验收：登录后自动选中监控账号（staff 模式，按租户+用户双重隔离）。
加载真实 console/*.js，内存 DOM + localStorage + fetch stub（/api/auth/me 返回
tenant_id+id，/api/monitored-accounts 返回桩数据）。
运行：pytest tests/test_rnd323_entity_autoselect.py -v
"""
from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source, review_console_html
# ...提供 in-memory localStorage / fetch(/api/auth/me 返回 {tenant_id,id,...};
#    /api/monitored-accounts 返回桩；/api/conversations 记录调用)...
# 断言见 §一 9 项 + §四 回归 1/2/3
```

断言要点（用 `run_node` 在 stub 环境执行真实 JS）：

- AC1：1 项 → `selEntityId` 设为该 `staff_id`，`loadConversations` 被触发。
- AC2：>1 项无 stored → `selEntityId===null`；`onEntityClick(A)` 后 `localStorage['rnd.lastEntity.<tid>.<uid>']===A`；再次 `renderEntityList` → `selEntityId===A`。
- AC3（租户隔离）：以 `tenant_id=T1` 选 A，切 `tenant_id=T2` 渲染 → `rnd.lastEntity.T2.<uid>` 不串 A。
- AC4（用户隔离）：同 `tenant_id=T`，`userId=U1` 选 A、`userId=U2` 选 B → 各自 key 独立、各自恢复。
- AC5：`tenant_id`/`userId` 齐全但 stored=不存在 id → `selEntityId===null`、无异常。
- AC6：`mode='contact'` → 任意 `renderEntityList` 不触发自动选中。
- AC7：`applyLocale()` 后 `selEntityId` 保持、空态不出现。
- 回归1/2：search-then-select 落点正确；refresh 路径不二次 auto-select。

运行：`pytest tests/test_rnd323_entity_autoselect.py -v` 必须**全绿**。

## 六、RED → GREEN

- **RED（未实现）**：上述 node harness 全部断言失败——`selEntityId` 恒 `null`、无 `rnd.lastEntity.<tid>.<uid>` 写入、`/api/auth/me` 无 `id` 字段、单账号仍显空态。
- **GREEN（实现正确）**：§一 9 项 + §四 回归 1/2/3/4 全绿；`pytest tests/test_rnd323_entity_autoselect.py -v` 通过；`make verify`（或项目等效整体测试，含 RND-110/RND-112 登录回归）无回归。

## 七、智能路由 / 验收结论

- 若 §三 范围守门被违反（无关端点 diff / 迁移 / 新依赖 / 改 `selectEntityIfPresent` 语义）→ **直接判 FAIL**，不进入功能验证，要求开发 agent 撤销越界改动。
- 若单账号 auto-select 与 search-then-select 存在 race（§四回归1）→ 判 FAIL 并要求加 `!searchActive` 守卫，而非改搜索流程。
- 若 tenant/user 隔离失效（§一 AC3/AC4）→ 判 FAIL，要求确认 `auth_me` 的 `id`/`tenant_id` 已落到全局且 key 拼接正确。
- 全部通过 → 给「PASS」结论 + 列出已验证的 AC 编号（含 3/4 双重隔离）；不 commit，交用户 Review。
