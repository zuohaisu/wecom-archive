# RND-240 QA / 验收 agent 提示词

> 面向独立测试 / QA agent（按项目 DEV_AGENT_RULES 的「Codex 验收」角色）。
> 你**只读言、不改实现、不 commit/push**。验收通过后由用户决定是否合入。
> 验收对象：开发 agent 按 `rnd-240-execution-prompt.md` 产出的改动。

---

## 一、验收目标

确认 RND-240「搜索结果点击 → 聊天详情页 ~8s」已消除，且**零回归**：
- 后端 `GET /api/conversations/{id}/detail` 由全量扫描改为聚合查询。
- 前端 `focusMessage` 对「已在看的实体/会话」短路，不再重拉实体列表 + 会话列表。
- 对外契约（URL / status / body / OpenAPI / 租户隔离 / i18n / RND-229 跨页语义）完全不变。

---

## 二、逐条验收清单（PASS / FAIL，附证据）

### 后端（根因 B）
- [ ] **B1** `get_conversation_detail`（`conversations.py:472`）不再调用 `_fetch_conversation_messages` 全量取行；改用聚合（`DISTINCT sender` + `COUNT/SUM(decrypt_status)`）。
  - 证据：读源码确认；`grep -n "_fetch_conversation_messages" backend/app/routers/conversations.py` 在 detail handler 内应**无命中**。
- [ ] **B2** 大会话下 `GET /detail` 响应 **<500ms**（RED 基线应 >1s）。
  - 证据：用测试库造 >1000 条消息的会话，curl / 测试计时；附前后对比数字。
- [ ] **B3** `/detail` 返回结构与字段（`participants` / `decrypted_percent`）**逐字段等价**于旧实现。
  - 证据：对同一会话分别跑新旧路径（旧路径可临时用 `WEARCHIVE_LEGACY_TIMELINE` 类开关或 git stash 对照），断言 JSON 一致。
- [ ] **B4** `_fetch_conversation_messages` 既有语义**未变**（timeline + media 路由共用）。
  - 证据：`test_rnd_228_*`、`test_search_*` 中涉及该函数的用例全绿。

### 前端（根因 A）
- [ ] **A1** `focusMessage`（`console-entry.js:404`）在「`mode===targetMode && selEntityId===entityId && convTypeFilter==='all'` 且目标 `.conv-card` 已在 DOM」时**短路**，不调用 `setMode`/`onEntityClick` 重链。
  - 证据：读源码确认短路分支存在且 early-return；`js-syntax` 测试通过。
- [ ] **A2** 短路分支仍正确设置 `focusMsgId` 并触发 `focusCheckRow()`（时间线已加载）或 `onConvClick()`（否则重载）。
- [ ] **A3** 用户实测前端整体（点击 → 时间线渲染）< **1.5s**（RED 基线 ≈8s）。
  - 证据：手测 / 端到端计时；附数字。
- [ ] **A4** 定位 / 高亮 / 定位栏 / 「← 返回搜索结果」banner 行为不变；RND-229 跨页 arrival（`focusIsUrlArrival`）语义不变。
  - 证据：`test_rnd229_focus_locate`、`test_archive_console_v2_qa_fixes` 全绿；手动跨页点击核对 banner。

### 全局契约
- [ ] **C1** 路由数不变（`test_http_contract` 断言 = 33）。
- [ ] **C2** 租户隔离未被破坏（`test_search_messages_tenant_isolation` 等）。
- [ ] **C3** i18n 未动（无新增/删除 key 需求）。
- [ ] **C4** `make verify` **全绿**。

---

## 三、回归套件（必须全绿）

```
make verify
```
重点确认（任一失败即 FAIL，附失败栈）：
- `test_search_api.py`
- `test_rnd229_focus_locate.py`
- `test_archive_console_v2_qa_fixes.py`
- `test_rnd_228_search_scalability.py`
- `test_http_contract.py`（路由数 = 33）

---

## 四、智能路由判定（每轮测试后必须给出）

- **源码有 Bug** → 反馈给开发 agent（寇豆码）修复，附具体错误 + 失败测试名 + 期望行为。**不自行改实现**。
- **测试代码有 Bug** → 你可自行修正测试（仅当测试断言了旧的慢路径；须在报告标注并说明依据）。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 数字对比。

最多 2 轮：第 1 轮发现问题反馈修复，第 2 轮回归验证；2 轮仍不过则输出报告标注遗留问题。

---

## 五、交付报告格式

```
RND-240 验收结论：PASS / FAIL
RED 基线：/detail ___ms，前端总 ___ms
GREEN：   /detail ___ms，前端总 ___ms
回归：make verify ___（绿/红，附失败项）
契约：URL/status/body/OpenAPI/租户/i18n/RND-229 跨页 —— 不变
遗留：___（若有）
```
