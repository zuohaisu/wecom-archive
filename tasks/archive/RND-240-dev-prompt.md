# RND-240 开发 agent 执行提示词

> 面向开发 agent（单人端到端实现 RND-240）。本文件即你的完整 brief。
> 全程**不执行 git commit / push**（由用户本人操作）。只改工作树，交用户 Review。
> 本版本已对照 2026-07-27 代码实际状态复核；重点修正了「路 B」——旧版提示词的聚合方案会**破坏 `/detail` 返回结构**，已重写。

---

## 一、任务（一句话）

消除「搜索结果点击一条记录 → 打开聊天记录详情页」约 **8 秒**的延迟：
- **前端** `focusMessage` 对「已在看的实体/会话」短路，跳过整条重链（主因，8s 几乎就是它的轮询兜底）；
- **后端** `GET /api/conversations/{id}/detail` 由「全量 materialize `ArchiveMessage` 全行」改为「`load_only` 投影 + 聚合 SQL」。

**对外契约零破坏**：URL / HTTP status / response body 结构 / OpenAPI / 租户隔离 / i18n / RND-229 跨页 arrival 语义全部不变。

---

## 二、精确落点 / 根因（已对照代码复核，附文件:行号）

### 根因 A — 前端 `focusMessage` 无条件重链（主因）
- `backend/app/web/static/console/console-entry.js:404` 的 `function focusMessage(msgid, convId, convType, entityId, entityType)`
- 每次点击搜索结果都**无条件**重跑整条链（已逐行核对）：
  1. `:406` `focusMsgId=msgid;`
  2. `:414` `if(convTypeFilter!=='all')setConvTypeFilter('all');`
  3. `:415-416` `targetMode = entityType==='staff'?'staff':'contact'; setMode(targetMode);`
  4. `:417-419` `waitForEntity` 轮询等实体卡片，间隔 200ms、上限 `focusAttempts>40` → **≈8 秒兜底**
  5. `:424` 命中后 `onEntityClick(el)` → `GET /api/conversations?mode=staff&staff_id=...` 重拉**该实体全部会话列表**
  6. `:427-435` `waitForConv` 轮询等会话卡片，间隔 100ms、上限 8000ms
  7. `:432` 命中后 `onConvClick(card)` → `loadTimeline` + `loadConversationDetail`（即 `/detail`）
- 即便你**已经在看该会话**，仍从零重拉「实体列表 + 会话列表」，纯属浪费；后端链慢时轮询烧到 8s 天花板。
- 用户实测 ~8s，几乎就是 `focusMessage` 的轮询兜底上限。

### 根因 B — 后端 `/detail` 全量扫描（次因）⚠️ 旧版提示词此处有误，已重写
- `backend/app/routers/conversations.py:472` 的 `get_conversation_detail` 当前实现（**不是**旧版描述的「只算 DISTINCT sender + decrypt_status」）：
  ```python
  messages = _fetch_conversation_messages(db, conversation_id, tenant_id)   # :497 返回该会话【每条 ArchiveMessage 全行】ORM 对象
  if not messages: raise HTTPException(404, ...)                            # :498-499
  recipients_map = _load_recipients_map(db, tenant_id, [m.id for m in messages])  # :501 仍按全量 message id 查收件人
  participant_ids = {m.sender for m in messages if m.sender}               # :502 senders
  for r in recipients_map.values(): participant_ids.update(r)             # :503-504 senders ∪ recipients
  display_names = _load_display_names_for_ids(db, tenant_id, participant_ids)  # :505
  staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)     # :506 员工/联系人判定
  buckets = _build_conversation_list(messages, recipients_map, display_names, staff_ids)  # :508 桶聚合
  bucket = next((b for b in buckets if b["conversation_id"]==conversation_id), buckets[0])  # :509-512
  participants = [ ConversationParticipantOut(...) for ... in bucket["monitored_account_ids"] ] \  # :514-522
                + [ ConversationParticipantOut(...) for ... in bucket["contact_ids"] ]
  decrypted_count = sum(1 for m in messages if m.decrypt_status == "success")   # :524 注意是 "success"，不是 "done"
  decrypted_percent = round(decrypted_count * 100.0 / len(messages), 1)        # :525
  return ConversationDetailOut(conversation_id, message_count=len(messages),
                              decrypted_percent=decrypted_percent, participants=participants)  # :527-532
  ```
- **关键事实（旧版提示词漏掉了）**：`participants` 由 `_build_conversation_list`（`backend/app/services/listing_service.py:369`）从 **senders ∪ recipients** 经员工/联系人分类后推导，并带 `display_name`；它只读取每条消息的 `id / sender / roomid / msgtime`（`listing_service.py:417-460`）。`decrypted_percent` 分母是**全部**消息数，分子是 `decrypt_status == "success"` 的计数。
- 会话几千条时，`_fetch_conversation_messages`（定义在 `backend/app/conversation_membership.py:504`）不带 limit 返回**每一行全字段 ORM 对象**，再叠加 Python 循环与收件人查询，就是秒级。
- ⚠️ **严禁**用「`SELECT DISTINCT sender` + `COUNT/SUM(decrypt_status)`」直接拼 `participants` —— 那会丢掉 recipients、员工/联系人分类与 display_name，直接破坏契约。正确做法见「路 B」。

---

## 三、阶段一：复现 + 测量（RED）

1. 启动后端 + 前端（按项目 Makefile / README）。
2. 准备一个**大会话**（>1000 条消息；用 `backend/tests` 既有数据工厂/夹具，或在测试库挑真实大会话）。
3. 打开 Archive Console v2 → 搜索 → 点一条属于该大会话的结果。
4. 用浏览器 DevTools **Network** 面板记录两条基线：
   - `GET /api/conversations/{id}/detail` 的响应时长（应明显 >1s，根因 B）
   - 从点击到时间线渲染完成的总时长（应 ≈8s，根因 A）
5. 把两个数字写进实现说明（交付时附）。

---

## 四、阶段二：实现（GREEN，两路，最小变更）

### 路 A — 前端短路（`console-entry.js` 的 `focusMessage`）
在 `setMode(targetMode)`（:416）**之前**插入短路判断。全局变量均在 `console-state.js` 声明并已核对：`mode`/`selEntityId`（:2）、`timelineConvId`/`timelineMsgs`（:12）、`focusMsgId`（:56）、`focusPending`（:62）、`convTypeFilter`（:97）。`onConvClick` 定义于 `conversation-list.js:125`、`onEntityClick` 于 `conversation-list.js:43`、`focusCheckRow` 于 `console-entry.js:443`。

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
- 仅当 `mode===targetMode && selEntityId===entityId && convTypeFilter==='all'` 才短路；否则**原样走全链**（保持 `setConvTypeFilter('all')`、`setMode` 等既有行为）。
- 短路分支仍须设置 `focusMsgId` 并触发 `focusCheckRow()` / `onConvClick()`，保持与现有「定位 / 高亮 / 定位栏 / 返回搜索结果 banner」语义一致。
- **不改 RND-229 的跨页 arrival 语义**（`focusIsUrlArrival` 由 `readFocusFromUrl` 控制，本改动不涉及）。

### 路 B — 后端聚合（契约保全版）⚠️ 已重写
目标：不再 materialize 全行，但**逐字段等价**于当前 `get_conversation_detail` 输出（`participants` 含 senders ∪ recipients、员工/联系人分类与 display_name；`decrypted_percent` 用 `decrypt_status == "success"`；`message_count` 为全部消息数；空会话仍 404）。

**步骤 1 — 抽出「消息成员解析」为共享 ID 解析器（仅重构内部，输出不变）**
在 `conversation_membership.py` 中，把 `_fetch_conversation_messages`（:504）里「会话成员判定」各分支（group room / direct pair / null-sender / 碰撞）的返回值由 `list[ArchiveMessage]` 改为 `list[int]`（消息主键 id），抽成新的私有函数：
```python
def _resolve_conversation_message_ids(
    db, conversation_id, tenant_id, *, mode=None, entity_id=None
) -> list[int]:
    """与 _fetch_conversation_messages 完全相同的成员解析逻辑，但只返回消息 id 列表。
    保留原 400 raise（碰撞且无实体上下文）与 [] 返回（实体不参与的碰撞分支）等所有控制流。"""
    ...  # 原 _fetch_conversation_messages 的各 return <ArchiveMessage 列表> 改为 return [m.id for m in <列表>]
```
然后让 `_fetch_conversation_messages` 复用于全量 ORM 行（**对外输出逐字节不变**，由 timeline / media 路由既有测试锁定）：
```python
def _fetch_conversation_messages(db, conversation_id, tenant_id, *, mode=None, entity_id=None):
    ids = _resolve_conversation_message_ids(db, conversation_id, tenant_id, mode=mode, entity_id=entity_id)
    return db.query(ArchiveMessage).filter(ArchiveMessage.id.in_(ids)).all()
```
> 注意：`.in_(ids)` 返回的 DB 顺序可能与原合并顺序不同，但 `_build_conversation_list` 与 timeline 均自排序；以现有 `test_rnd_228_*`、`test_search_*`、timeline 测试全绿为验收门槛。

**步骤 2 — `get_conversation_detail`（:472）改用「ID 解析 + 聚合 + 投影」**
```python
from sqlalchemy import func, case
# （func/case 若已 import 则复用；load_only 来自 sqlalchemy.orm）

def get_conversation_detail(conversation_id, db, auth):
    _, tenant_id = auth
    ids = _resolve_conversation_message_ids(db, conversation_id, tenant_id)
    if not ids:
        raise HTTPException(status_code=404, detail="Conversation not found")

    total = len(ids)
    decrypted_done = db.query(
        func.coalesce(
            func.sum(case((ArchiveMessage.decrypt_status == "success", 1), else_=0)), 0
        )
    ).filter(ArchiveMessage.id.in_(ids)).scalar() or 0
    decrypted_percent = round(decrypted_done * 100.0 / total, 1)

    # 仅取 _build_conversation_list 真正需要的列，避免 materialize 全行
    slim = (
        db.query(ArchiveMessage)
        .options(load_only("id", "sender", "roomid", "msgtime"))
        .filter(ArchiveMessage.id.in_(ids))
        .all()
    )
    recipients_map = _load_recipients_map(db, tenant_id, ids)        # conversation_membership.py:162
    participant_ids = {m.sender for m in slim if m.sender}
    for recipient_ids in recipients_map.values():
        participant_ids.update(recipient_ids)
    display_names = _load_display_names_for_ids(db, tenant_id, participant_ids)   # :130
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)       # :71
    buckets = _build_conversation_list(slim, recipients_map, display_names, staff_ids)  # listing_service.py:369
    bucket = next(
        (b for b in buckets if b["conversation_id"] == conversation_id),
        buckets[0],
    )
    participants = [
        ConversationParticipantOut(id=sid, raw_id=sid, display_name=name, role="staff")
        for sid, name in zip(bucket["monitored_account_ids"], bucket["monitored_account_display_names"])
    ] + [
        ConversationParticipantOut(id=cid, raw_id=cid, display_name=name, role="contact")
        for cid, name in zip(bucket["contact_ids"], bucket["contact_display_names"])
    ]
    return ConversationDetailOut(
        conversation_id=conversation_id,
        message_count=total,
        decrypted_percent=decrypted_percent,
        participants=participants,
    )
```
要点：
- `slim` 仅投影 `id/sender/roomid/msgtime` 四列，`_build_conversation_list` 只读这四列 + `recipients_map`（`listing_service.py:417-460`），故 `participants` 与原实现**完全一致**。
- `decrypted_percent` 用聚合 SQL（`decrypt_status == "success"`）等价替换原 Python 循环，分母 `total=len(ids)` 等价原 `len(messages)`。
- `message_count` = `total` = 全部解析消息数，等价原 `len(messages)`。
- 404 语义、字段名、结构全部不变，前端无需改动。

**步骤 3 — 新增契约等价回归测试**（建议放进 `backend/tests/test_archive_console_v2.py`）：
`test_rnd240_detail_aggregates_equivalent` —— 用现有夹具构造大会话，分别：
   (a) 走**新**路径拿 `ConversationDetailOut` JSON；
   (b) 走**旧**逻辑（`_fetch_conversation_messages` 全行 + 原 Python 计算）算出 `participants` / `decrypted_percent` / `message_count`；
   断言两者**逐字段相等**。这把「聚合改写不破坏契约」锁死。

---

## 五、阶段三：验证（GREEN + 回归 + make verify）

1. **功能验证**：回到阶段一的大会话，重测：
   - `GET /detail` 响应应 **<500ms**（聚合 + 投影，根因 B 消除）。
   - 前端整体（点击 → 时间线渲染）应 **<1.5s**（根因 A 消除；短路后不再重拉实体/会话列表）。
   - 定位 / 高亮 / 定位栏 / 返回搜索结果 banner 行为不变。
2. **回归测试**（必须全绿）：
   - `make verify`
   - 重点套件：`test_archive_console_v2.py`（含 `test_conversation_detail_group_returns_stats_and_participants` / `test_conversation_detail_is_tenant_scoped` / `test_conversation_detail_unknown_conversation_404s` 三个 `/detail` 契约测试 + 你新增的 RND-240 等价测试）、`test_search_*`、`test_rnd229_focus_locate`、`test_archive_console_v2_qa_fixes`、`test_rnd_228_*`、`test_http_contract`（**路由数 = 34 不变**）。
   - 确认 RND-229 跨页 arrival（`focusIsUrlArrival`）语义不变。
3. 若某测试因本次改动失败，**先修实现**，不要改测试来迁就（除非测试本身断言了旧的慢路径，那需在交付说明里标注并获用户确认）。

---

## 六、硬约束（违反即判失败）

- ❌ 不改 URL / HTTP status / response body 结构 / OpenAPI schema / 租户隔离 / i18n。
- ❌ 不破坏 `participants` 的「senders ∪ recipients + 员工/联系人分类 + display_name」语义（即 `_build_conversation_list` 输出）。
- ❌ 不破坏 `focusMessage` 的定位 / 高亮 / 定位栏 / 跨页返回语义。
- ❌ 不改动 `_fetch_conversation_messages` 的**对外输出**（全量 ORM 行）；只允许抽出内部 ID 解析器（由现有 timeline / media 测试锁死等价性）。
- ❌ 不执行 git commit / push。
- ✅ 与 RND-216（前端外置）、RND-220（timeline service）的**工作树改动无冲突**前提下最小化改动；若发现冲突，停下并在交付说明里报告，不要强行覆盖。

---

## 七、收尾（交付物）

向用户交付：
1. 阶段一测得的 **RED 基线数字**（/detail 时长、前端总时长）。
2. 阶段三测得的 **GREEN 数字**（同上两项）。
3. 改动文件清单（预期：`backend/app/web/static/console/console-entry.js`、`backend/app/routers/conversations.py`、`backend/app/conversation_membership.py`，及新增测试）。
4. `make verify` 通过日志（含 `/detail` 契约三测试 + 新增 RND-240 等价测试绿）。
5. 注明：未提交，待用户 Review 后自行 commit。

---

## 八、相关文档 / 上下文（供 agent 自取）

- 工作规则（绑定）：`DEV_AGENT_RULES.md`（git 规则、密钥、commit 规则、架构边界）。
- Agent 交接协议：`docs/AGENTS.md`（谁做什么、何时交接、升级路径）。
- 架构护栏测试：`backend/tests/test_architecture_boundary.py`（破坏边界即硬失败）。
- 本次落地源码（务必读真实代码，行号以 git 当前为准）：
  - `backend/app/web/static/console/console-entry.js`（focusMessage :404；console-state.js 全局变量）
  - `backend/app/routers/conversations.py`（get_conversation_detail :472；ConversationDetailOut 结构）
  - `backend/app/conversation_membership.py`（_fetch_conversation_messages :504；_resolve 抽出点；_load_recipients_map :162；_staff_ids_for_participants :71；_load_display_names_for_ids :130）
  - `backend/app/services/listing_service.py`（_build_conversation_list :369，读取 id/sender/roomid/msgtime）
- 关联 issue（仅上下文，不改其行为）：RND-229（跨页 arrival 语义）、RND-220（timeline service）、RND-216（前端外置）。
- 既有 `/detail` 契约测试：`backend/tests/test_archive_console_v2.py:90/131/142`；路由数基线 `backend/tests/test_http_contract.py:319`（== 34）。
