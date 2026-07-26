# RND-240 开发 agent 执行提示词

> 面向开发 agent（单人端到端实现 RND-240）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户本人操作）。只改工作树，交用户 Review。

---

## 一、任务（一句话）

消除「搜索结果点击一条记录 → 打开聊天记录详情页」约 **8 秒**的延迟：前端 `focusMessage` 短路冗余重链 + 后端 `GET /api/conversations/{id}/detail` 改聚合查询。不破坏任何对外契约（URL / status / response body / OpenAPI / 租户隔离 / i18n）。

---

## 二、精确落点 / 根因（已定位，附文件:行号）

### 根因 A — 前端 `focusMessage` 无条件重链（主因，8s 几乎就是它的轮询兜底）
- `backend/app/web/static/console/console-entry.js:404` 的 `focusMessage(msgid, convId, convType, entityId, entityType)`
- 每次点击搜索结果都**无条件**重跑整条链：
  1. `setMode(targetMode)`（:415）→ 可能重拉整个实体列表
  2. `waitForEntity` 轮询等实体卡片出现，间隔 200ms、上限 `focusAttempts>40`（:419）→ **≈8 秒兜底**
  3. 命中后 `onEntityClick(el)`（:424）→ `GET /api/conversations?mode=staff&staff_id=...` 重拉**该实体全部会话列表**
  4. `waitForConv` 轮询等会话卡片，间隔 100ms、上限 8000ms（:435）
  5. 命中后 `onConvClick(card)`（:432）→ `loadTimeline` + `loadConversationDetail`（`/detail`）
- 即便你**已经在看该会话**，仍从零重拉「实体列表 + 会话列表」，纯属浪费；后端链慢时轮询烧到 8s 天花板。
- 用户实测 ~8s，几乎就是 `focusMessage` 的轮询兜底上限。

### 根因 B — 后端 `/detail` 全量扫描（次因）
- `backend/app/routers/conversations.py:472` `get_conversation_detail` 调 `_fetch_conversation_messages`（`backend/app/conversation_membership.py:504`）
- `_fetch_conversation_messages` **不带 limit，返回该会话每条 `ArchiveMessage` 全行**；`get_conversation_detail` 只拿它算两个聚合值：
  - `participants`：遍历所有行取 `DISTINCT sender`（:502）
  - `decrypted_percent`：遍历所有行数 `decrypt_status`（:493 注释 + 下方计算）
- 会话几千条时，materialize 上千个 ORM 对象 + Python 循环就是秒级。

---

## 三、阶段一：复现 + 测量（RED）

1. 启动后端 + 前端（按项目 Makefile / README）。
2. 准备一个**大会话**（>1000 条消息；可用 `backend/tests` 既有的数据工厂或脚本造数，或在测试库挑一个真实大会话）。
3. 打开 Archive Console v2 → 搜索 → 点一条属于该大会话的结果。
4. 用浏览器 DevTools **Network** 面板记录两条基线：
   - `GET /api/conversations/{id}/detail` 的响应时长（应明显 >1s，根因 B）
   - 从点击到时间线渲染完成的总时长（应 ≈8s，根因 A）
5. 把两个数字写进实现说明（交付时附）。

---

## 四、阶段二：实现（GREEN，两路，最小变更）

### 路 A — 前端短路（`console-entry.js` 的 `focusMessage`）
在 `setMode(targetMode)`（:415）**之前**插入短路判断：

```js
// RND-240: 目标实体已选中 且 目标会话卡片已在 DOM → 跳过整条重链
if (mode === targetMode && selEntityId === entityId && convTypeFilter === 'all') {
  var cb = document.getElementById('conv-body');
  var cards = cb ? cb.querySelectorAll('.conv-card') : [];
  var hit = null;
  Array.prototype.forEach.call(cards, function (card) {
    if (card.dataset.id === convId) hit = card;
  });
  if (hit) {
    focusMsgId = msgid;
    if (timelineConvId === convId && timelineMsgs.length) {
      focusCheckRow();            // 时间线已加载 → 直接滚动高亮
    } else {
      onConvClick(hit);          // 仅重载该会话时间线 + detail（不再重拉实体/会话列表）
    }
    return;
  }
}
```

约束：
- 仅当 `mode===targetMode && selEntityId===entityId && convTypeFilter==='all'` 才短路；否则**原样走全链**（保持 `setConvTypeFilter('all')` 等既有行为）。
- 短路分支仍须设置 `focusMsgId` 并触发 `focusCheckRow()` / `onConvClick()`，保持与现有「定位 / 高亮 / 定位栏 / 返回搜索结果 banner」语义一致。
- **不改 RND-229 的跨页 arrival 语义**（`focusIsUrlArrival` 由 `readFocusFromUrl` 控制，本改动不涉及）。
- 全局变量 `mode / selEntityId / convTypeFilter / timelineConvId / timelineMsgs / focusMsgId` 均已在 `console-state.js` 声明，确认作用域后使用。

### 路 B — 后端聚合（`conversations.py` + `conversation_membership.py`）
目标：不再 materialize 全行，用轻量聚合 SQL 算 `participants` 与 `decrypted_percent`。

1. 在 `conversation_membership.py` 中，**抽出** `_fetch_conversation_messages` 里「会话成员判定」的 WHERE 表达式为一个内部 helper（如 `_resolve_conversation_msg_filter(db, conversation_id, tenant_id, mode, entity_id) -> BinaryExpression`），返回与现在完全一致的过滤条件。
   - 重构后 `_fetch_conversation_messages` 仍用该 filter 取全行，**对外输出逐字节不变**（由现有 `test_*` 锁死）。
2. 新增 `_aggregate_conversation_stats(db, conversation_id, tenant_id, mode=None, entity_id=None) -> dict`：
   - `participants`：`db.query(ArchiveMessage.sender).filter(msg_filter).distinct().all()` → 收集非空 sender 去重成 set。
   - `decrypted_percent`：
     ```python
     total, done = db.query(
         func.count(),
         func.coalesce(func.sum(case((ArchiveMessage.decrypt_status == 'done', 1), else_=0)), 0),
     ).filter(msg_filter).one()
     percent = (done / total * 100) if total else 0.0
     ```
3. `get_conversation_detail`（`conversations.py:472`）改用 `_aggregate_conversation_stats` 取代 `_fetch_conversation_messages(...)` + 循环；无消息时仍 `raise 404`；返回结构与字段（`participants` 为推断、`decrypted_percent` 语义）**完全不变**，前端无需改。
4. **绝不改动** `_fetch_conversation_messages` 的既有语义（timeline 路由与 media 路由共用，属 RND-220 领地，本单不碰）。

---

## 五、阶段三：验证（GREEN + 回归 + make verify）

1. **功能验证**：回到阶段一的大会话，重测：
   - `GET /detail` 响应应 **<500ms**（聚合 SQL，根因 B 消除）。
   - 前端整体（点击 → 时间线渲染）应 **<1.5s**（根因 A 消除；短路后不再重拉实体/会话列表）。
   - 定位 / 高亮 / 定位栏 / 返回搜索结果 banner 行为不变。
2. **回归测试**（必须全绿）：
   - `make verify`
   - 重点套件：`test_search_*`、`test_rnd229_focus_locate`、`test_archive_console_v2_qa_fixes`、`test_rnd_228_*`、`test_http_contract`（路由数 = 33 不变）。
   - 确认 RND-229 跨页 arrival（`focusIsUrlArrival`）语义不变。
3. 若某测试因本次改动失败，**先修实现**，不要改测试来迁就（除非测试本身断言了旧的慢路径，那需在 PR 说明里标注并获用户确认）。

---

## 六、硬约束（违反即判失败）

- ❌ 不改 URL / HTTP status / response body 结构 / OpenAPI schema / 租户隔离 / i18n。
- ❌ 不改动 `_fetch_conversation_messages` 的既有对外输出（timeline + media 路由依赖）。
- ❌ 不破坏 `focusMessage` 的定位 / 高亮 / 定位栏 / 跨页返回语义。
- ❌ 不执行 git commit / push。
- ✅ 与 RND-216（前端外置）、RND-220（timeline service）的**工作树改动无冲突**前提下最小化改动；若发现冲突，停下并在交付说明里报告，不要强行覆盖。

---

## 七、收尾（交付物）

向用户交付：
1. 阶段一测得的 **RED 基线数字**（/detail 时长、前端总时长）。
2. 阶段三测得的 **GREEN 数字**（同上两项）。
3. 改动文件清单（预期：`backend/app/web/static/console/console-entry.js`、`backend/app/routers/conversations.py`、`backend/app/conversation_membership.py`）。
4. `make verify` 通过截图 / 日志。
5. 注明：未提交，待用户 Review 后自行 commit。
