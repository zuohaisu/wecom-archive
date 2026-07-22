# RND-159: 搜索联系人昵称与聊天内容 — 实现计划

## 摘要

为 365 企业微信会话存档系统增加全局搜索能力：按联系人昵称/显示名搜索，按聊天文本内容搜索。后端新增 `/api/search/contacts` 和 `/api/search/messages` 两个 API，前端在顶部栏增加搜索入口。

---

## 一、当前状态分析

### 1.1 项目架构

- **单体 FastAPI 应用**，无独立前端。前端是内嵌在 `backend/app/main.py` 中的原生 JS（ES5）+ CSS string literal。
- 数据库: PostgreSQL，SQLAlchemy ORM，Alembic 管理迁移。
- 认证: Cookie-based session 通过 `get_current_user()` FastAPI dependency 注入 `(AdminUser, tenant_id)` 元组。
- 租户隔离: 所有查询必须按 `tenant_id` 过滤，`tenant_id` 仅来自 session，不接受请求参数。

### 1.2 关键数据模型

| 模型 | 表 | 关键字段 |
|------|-----|---------|
| `Contact` | `contacts` | `wecom_userid`, `name` (可空), `tenant_id` |
| `ArchiveMessage` | `archive_messages` | `content_text`, `msgtype`, `sender`, `roomid`, `msgtime`, `tenant_id`, `id` |
| `ArchiveMessageRecipient` | `archive_message_recipients` | `message_id`, `receiver_userid`, `tenant_id` |
| `AdminUser` | `admin_users` | `wecom_user_id`, `name`, `tenant_id` |

### 1.3 现有搜索/查询能力

- **`GET /api/messages?q=keyword`** (`main.py` line 2635-2655): 用 `ILIKE` 搜索 `content_text`，但只返回原始消息字段（无发送人名称、无会话信息、无高亮）。
- **`GET /admin/messages?q=keyword`** (`main.py` line 204-266): 同上的 HTML 版。
- **`Contact.name` 字段已存在**但无搜索端点。
- **GIN tsvector index** 已建在 `content_text` 上 (`ix_archive_messages_content_text_fts`)，但当前查询未使用。

### 1.4 前端浏览流程

```
┌────────────┐    ┌──────────────┐    ┌─────────────────┐
│ 左列 220px  │    │ 中列 304px    │    │ 右列 flex        │
│             │    │              │    │                 │
│ [员工] [联系人]│───▶│ 会话列表       │───▶│ 消息时间线        │
│ 实体列表      │    │              │    │                 │
└────────────┘    └──────────────┘    └─────────────────┘
```

- `setMode()` → `loadEntityList()` → `onEntityClick()` → `loadConversations()` → `onConvClick()` → `loadTimeline()`
- 搜索应作为独立入口，不打断现有三列流程。搜索命中的联系人可直接进入联系人→会话流程；搜索命中的消息可直接打开对应会话时间线。

### 1.5 涉及的现有 API Router

| Router | 前缀 | 文件 |
|--------|------|------|
| `auth_router` | `/api/auth/*` | `app/routers/auth.py` |
| `conversations_router` | `/api/conversations/*`, `/api/monitored-accounts`, `/api/contacts` | `app/routers/conversations.py` |
| `reachability_audit_router` | `/api/admin/reachability-audit` | `app/routers/reachability_audit.py` |
| `wecom_events_router` | `/api/wecom/archive/events` | `app/routers/wecom_events.py` |
| (inline in main.py) | `/api/messages`, `/api/messages/{msgid}` | `app/main.py` |

---

## 二、涉及文件清单

### 新增文件

| 文件 | 用途 |
|------|------|
| `backend/app/routers/search.py` | 搜索 API 路由（`/api/search/contacts`, `/api/search/messages`） |
| `backend/tests/test_search_api.py` | 搜索 API 测试（联系人搜索、消息搜索、租户隔离、分页） |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `backend/app/main.py` | 1. 注册 `search_router`；2. 在 `_REVIEW_CONSOLE_HTML` 的 `.top-bar` 中增加搜索输入框；3. 增加搜索相关的 JS 逻辑（debounce、结果渲染、高亮、导航） |
| `backend/app/assets/i18n.js` | 增加搜索相关的 i18n key（中/英/繁） |

---

## 三、实现方案

### 3.1 后端 API

#### 3.1.1 联系人搜索 — `GET /api/search/contacts`

**Query params:**
- `q` (required, min_length=1): 搜索关键词

**逻辑:**
```python
SELECT wecom_userid, name
FROM contacts
WHERE tenant_id = :tenant_id
  AND (name ILIKE '%' || :q || '%'
       OR wecom_userid ILIKE '%' || :q || '%')
ORDER BY
  CASE WHEN name ILIKE '%' || :q || '%' THEN 0 ELSE 1 END,  -- name 匹配优先
  name ASC
LIMIT :limit
```

**响应:**
```json
[
  {
    "wecom_userid": "zhangsan",
    "display_name": "张三",
    "match_field": "name"   // "name" | "wecom_userid"
  }
]
```

**Pydantic model:**
```python
class ContactSearchResult(BaseModel):
    wecom_userid: str
    display_name: str
    match_field: str  # "name" or "wecom_userid"
```

- 搜索 `Contact.name` 和 `Contact.wecom_userid`，name 匹配优先排序。
- 当 `name` 为空时，兜底显示 `wecom_userid`（使用 `display_names.resolve_person_display_name`）。
- `limit` 默认 20。

#### 3.1.2 消息搜索 — `GET /api/search/messages`

**Query params:**
- `q` (required, min_length=1): 搜索关键词
- `limit` (default=20, 1-100)
- `before` (optional): 游标分页 (`msgtime:id` 格式)

**逻辑:**
```python
SELECT am.msgid, am.sender, am.content_text, am.msgtime, am.roomid, am.id
FROM archive_messages am
WHERE am.tenant_id = :tenant_id
  AND am.content_text ILIKE '%' || :q || '%'
  AND am.msgtype = 'text'              -- 仅搜索文本消息
  AND am.decrypt_status = 'success'    -- 仅搜索已解密消息
  AND am.is_revoked = FALSE            -- 排除已撤回消息
ORDER BY am.msgtime DESC, am.id DESC
LIMIT :limit
```

**响应:**
```json
[
  {
    "msgid": "xxx",
    "sender": "zhangsan",
    "sender_display_name": "张三",
    "content_snippet": "...关键词前后各80字符...",
    "msgtime": 1711000000000,
    "roomid": "wrxxxxxx",
    "conversation_id": "wrxxxxxx",
    "conversation_type": "group",
    "conversation_name": "产品讨论群"
  }
]
```

**Pydantic model:**
```python
class MessageSearchResult(BaseModel):
    msgid: str
    sender: Optional[str]
    sender_display_name: str
    content_snippet: str          # 关键词上下文片段（含前后80字符）
    msgtime: Optional[int]
    roomid: Optional[str]
    conversation_id: str          # 可直接用于跳转的会话 ID
    conversation_type: str        # "group" | "direct"
    conversation_name: str        # 会话显示名
    match_position: int           # 关键词在 content_text 中的起始位置（用于前端高亮）
```

**分页:** 使用游标分页 `before` 参数（`{msgtime}:{id}`），与现有 conversations timeline 分页模式一致。

**关键细节:**
- 只搜索 `msgtype='text'`，跳过图片、视频、音频、文件、mix 等。
- 不搜索 `decrypted_payload`（只搜已提取的 `content_text`）。
- 只搜索已成功解密的、未被撤回的消息。
- `content_snippet` 取匹配位置前后各 80 字符。
- `conversation_id` 和 `conversation_type` 的计算复用 `conversation_membership._derive_conversation_membership` 的逻辑（但轻量级内联，不做完整会话聚合）。
- sender display_name 从 `contacts` 表 + `display_names.resolve_person_display_name` 解析。
- conversation name: group 用 `display_names.resolve_room_display_name`，direct 用对方 display_name。

#### 3.1.3 租户隔离

两个端点均:
- 使用 `auth: Tuple = Depends(get_current_user)` → `_, tenant_id = auth`
- 所有 DB 查询强制加 `.filter(Model.tenant_id == tenant_id)`
- 不接收任何用户提供的 tenant_id

#### 3.1.4 Router 注册

在 `main.py` 中:
```python
from app.routers.search import router as search_router
app.include_router(search_router)
```

### 3.2 前端实现

#### 3.2.1 搜索入口位置

在 `.top-bar` 中，`.refresh-bar` 和 `.top-bar-user` 之间加入搜索组件:

```html
<div class="search-bar" id="search-bar">
  <input type="text" id="search-input" placeholder="搜索联系人或聊天内容..." />
  <div class="search-results" id="search-results" style="display:none"></div>
</div>
```

搜索栏 CSS:
```css
.search-bar{position:relative;flex:0 1 320px}
.search-bar input{width:100%;padding:.25rem .6rem;border:1px solid #3a4a5a;border-radius:4px;
  background:#0a1a2e;color:#ddd;font-size:.8rem;outline:none}
.search-bar input:focus{border-color:#1890ff;background:#0d2137}
.search-bar input::placeholder{color:#5c7185}
.search-results{position:absolute;top:110%;left:0;right:0;background:#fff;border:1px solid #e8e8e8;
  border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.15);max-height:480px;overflow-y:auto;z-index:100}
```

#### 3.2.2 搜索行为

1. **Debounce**: 输入 300ms 后触发搜索。
2. **两阶段搜索**: 同时发起两个请求:
   - `GET /api/search/contacts?q=xxx&limit=5`
   - `GET /api/search/messages?q=xxx&limit=10`
3. **空输入**: 不发起请求，隐藏结果面板。
4. **Loading 状态**: 搜索过程中显示加载状态。
5. **Empty 状态**: 无结果时显示 "未找到匹配结果"。
6. **Error 状态**: 请求失败时显示错误信息。

#### 3.2.3 结果展示

下拉面板分为两个区:
```
┌──────────────────────────────┐
│  联系人 (N)                   │
│  ├─ 张三 · zhangsan          │
│  └─ 李四 · lisi              │
│                              │
│  消息 (M)                     │
│  ├─ 张三 · 产品讨论群         │
│  │  ...这是**关键词**的内容...  │
│  │  2024-01-15 10:30          │
│  └─ 王五 · 单聊              │
│     ...另一个匹配...           │
│     2024-01-14 09:00          │
└──────────────────────────────┘
```

#### 3.2.4 关键词高亮

对 `content_snippet` 中的关键词用 `<mark>` 标签包裹（或 `<b style="color:#e00">`），在 `esc()` 处理后再插入高亮标签。

```
实现: 用正则做不区分大小写的替换，保留原始大小写:
snippet.replace(new RegExp('(' + escapedKeyword + ')', 'gi'), '<mark>$1</mark>')
```

**注意安全:** snippet 先经过 `esc()` HTML 转义，关键词也需转义后再构造正则。

#### 3.2.5 点击导航

- **点击联系人结果**: 切换到对应 mode（staff 或 contact），选中该实体，加载其会话列表。
  - 如果该 wecom_userid 在 staff_ids 中 → 切到 staff mode
  - 否则 → 切到 contact mode
- **点击消息结果**: 
  - 如果消息所在会话已在中间列有卡片，直接选中。
  - 如果不在当前视图，需要先加载实体→会话→消息时间线。
  - 跳转后滚动到目标消息（如果可能），或至少展示包含该消息的时间线。

#### 3.2.6 不影响现有流程

- 搜索是覆盖层（dropdown），不改变三列布局。
- 点击搜索结果切换到目标会话后，搜索面板关闭，恢复正常浏览流程。
- 现有 `Staff → Conversation → Message` 流程不变。

### 3.3 i18n

在 `backend/app/assets/i18n.js` 中增加以下 key:

| Key | 中文 | English | 繁体 |
|-----|------|---------|------|
| `search.placeholder` | 搜索联系人或聊天内容… | Search contacts or messages... | 搜尋聯絡人或聊天內容… |
| `search.noResults` | 未找到匹配结果 | No results found | 未找到匹配結果 |
| `search.loading` | 搜索中… | Searching... | 搜尋中… |
| `search.error` | 搜索失败，请重试 | Search failed, retry | 搜尋失敗，請重試 |
| `search.contacts` | 联系人 | Contacts | 聯絡人 |
| `search.messages` | 消息 | Messages | 訊息 |

---

## 四、测试计划

### 4.1 后端测试 (`backend/tests/test_search_api.py`)

复用 `test_reachability_audit.py` 的 sqlite schema 和 fixtures。

#### 联系人搜索测试
1. **`test_search_contacts_by_name`** — 按 name 搜索匹配的联系人
2. **`test_search_contacts_by_wecom_userid`** — 按 wecom_userid 搜索匹配的联系人
3. **`test_search_contacts_no_match`** — 无匹配关键词返回空列表
4. **`test_search_contacts_empty_query`** — 空关键词返回 422
5. **`test_search_contacts_tenant_isolation`** — 租户 A 搜不到租户 B 的联系人

#### 消息搜索测试
6. **`test_search_messages_by_content`** — 按 content_text 搜索
7. **`test_search_messages_no_match`** — 无匹配返回空
8. **`test_search_messages_empty_query`** — 空关键词返回 422
9. **`test_search_messages_tenant_isolation`** — 租户隔离
10. **`test_search_messages_skips_non_text`** — 不搜索 image/video/voice/file 等类型消息
11. **`test_search_messages_skips_revoked`** — 不搜索已撤回消息
12. **`test_search_messages_pagination`** — 游标分页正确

### 4.2 前端测试

现有前端测试模式：在 `tests/test_search_frontend.py` 中通过 Node.js (`subprocess`) 执行嵌入式 JS 代码段来验证行为。

13. **`test_search_debounce`** — 验证 300ms debounce
14. **`test_search_results_rendering`** — 验证结果 DOM 结构
15. **`test_search_empty_state`** — 验证空状态展示
16. **`test_search_keyword_highlight`** — 验证 `<mark>` 高亮

---

## 五、风险点与假设

### 风险点

1. **性能**: `ILIKE '%keyword%'` 全表扫描，大量消息时可能慢。已有 `content_text` 索引但 `ILIKE` 左侧通配符 `%keyword%` 无法使用 B-tree 索引。
   - **缓解**: 限制 `limit` (max 100)；只搜索 `msgtype='text'` 且已解密的；后续可迁移到 `pg_trgm`。

2. **Content name 为空**: 多数 `Contact.name` 可能为空（因为 WeCom 联系人同步被 blocked by RND-130 的 trusted domain 配置）。
   - **缓解**: 搜索同时覆盖 `wecom_userid`，且 `display_name` 兜底返回 raw ID。

3. **消息搜索结果到会话的导航**: 搜索命中消息需要计算出 `conversation_id` 才能跳转。这需要调用 `_derive_conversation_membership` 逻辑（需要知道 recipients）。
   - **缓解**: 在搜索 API 中，查出消息后，同时查询 `archive_message_recipients` 来推导 conversation_id。

4. **前端搜索面板与现有三列布局的交互**: 搜索结果点击后需要切换到不同的 mode/entity/conversation，涉及现有状态变量较多。
   - **缓解**: 复用现有的 `setMode()`、`onEntityClick()`、`onConvClick()` 流程，而不是写新的导航逻辑。

5. **不破坏现有流程**: 搜索 UI 是独立覆盖层，不改变现有 DOM 结构。

### 假设

1. 联系人搜索仅查 `contacts` 表（已缓存的 WeCom 用户身份），不实时调用 WeCom API。
2. 消息搜索仅按 `content_text` ILIKE 匹配，暂不使用 PostgreSQL FTS。
3. 搜索结果点击导航时，前端会加载完整时间线（复用现有 `loadTimeline`）。
4. Contact name 为空时用 wecom_userid 兜底显示（复用现有 `resolve_person_display_name`）。

---

## 六、不做的范围

- **不做**: PostgreSQL Full Text Search、trigram index（下一期）
- **不做**: ES/Lucene 等外部搜索引擎
- **不做**: 搜索图片/视频/音频/文件的文字描述
- **不做**: 高级搜索过滤器（日期范围、发送人、会话类型等）
- **不做**: 搜索历史/搜索建议
- **不做**: 高亮定位到消息确切位置（精确定位需滚动时间线，复杂度过高，本期只打开会话）
- **不做**: 修改 contacts 表结构或增加索引（`contacts` 表数据量不大，ILIKE 可接受）

---

## 七、实现步骤

### Step 1: 创建 `backend/app/routers/search.py`
- 定义 `ContactSearchResult`、`MessageSearchResult` Pydantic models
- 实现 `GET /api/search/contacts` endpoint
- 实现 `GET /api/search/messages` endpoint
- 注册为 `APIRouter()`

### Step 2: 在 `backend/app/main.py` 中注册 search router
- `from app.routers.search import router as search_router`
- `app.include_router(search_router)`

### Step 3: 更新 i18n — `backend/app/assets/i18n.js`
- 添加搜索相关翻译 key

### Step 4: 更新前端 — `backend/app/main.py` 的 `_REVIEW_CONSOLE_HTML`
- 在 `.top-bar` 中添加 `.search-bar` HTML
- 添加搜索相关 CSS
- 添加搜索相关 JS: debounce, fetch, render, highlight, navigate

### Step 5: 创建后端测试 — `backend/tests/test_search_api.py`
- 联系人搜索测试用例
- 消息搜索测试用例
- 租户隔离测试用例

### Step 6: 创建前端测试 — `backend/tests/test_search_frontend.py`
- debounce 测试
- 渲染测试
- 空状态测试
- 高亮测试

### Step 7: 运行测试验证
- `pytest backend/tests/test_search_api.py -v`
- `pytest backend/tests/test_search_frontend.py -v`
- 确保所有现有测试仍然通过

---

## 八、已知限制

1. 联系人搜索仅覆盖 `contacts` 表中已缓存的用户，不包括从未出现在 archive 中的 WeCom 用户。
2. 消息搜索仅覆盖 `msgtype='text'`，不包括 mixed/rich 消息中的文本片段（mixed 消息的文本在 `decrypted_payload` JSON 中，不在 `content_text` 列）。
3. 使用 ILIKE 而非 FTS，中文分词效果有限（但 `simple` dictionary 的 GIN tsvector index 已存在，后续可升级）。
4. 搜索结果点击后无法精确定位到目标消息（时间线需要滚动查找），只能打开对应会话。
