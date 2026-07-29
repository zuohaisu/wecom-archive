[Goal check] This work advances 开发（Development） by 给 GET /api/admin/media 补齐 msgid/conversation_id/session_title 三个字段（全部复用既有解析逻辑），解除 RND-329 缩略图预览的硬阻塞。

# RND-334 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-334 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-334「A6-1b 媒体列表补充会话定位字段（msgid / conversation_id / session_title）」｜Linear team `Builder`
- 优先级：Urgent｜风险等级：**R1**｜milestone：R1 · 前端快赢四页
- **本票 blocks RND-329**（media 页面缩略图预览无法拼 URL）

## 背景（已实地核实，来自 RND-329 开发 agent 的真实 BLOCKED 报告）

`GET /api/admin/media`（`backend/app/routers/media_library.py:42`，RND-291 交付）的响应缺三个字段，导致 RND-329 无法给缩略图拼出可用 URL：

- 现有缩略图/预览路由是 `/api/conversations/{conversation_id}/messages/{msgid}/media`（`backend/app/routers/media.py:150`），需要 `conversation_id` + `msgid`（`ArchiveMessage.msgid`，WeCom 消息 ID）。
- 列表接口目前给的是 `room_id`（= `message.roomid`）+ `message_id`（= `ArchiveMessage.id`，数据库主键，**不是** `msgid`）——两者语义不同，不能互换。
- 唯一 `media_id` 键控的路由是下载路由 `/api/admin/media/{media_id}/download`（RND-292 已交付），强制 `Content-Disposition: attachment`，浏览器会触发下载而不是内嵌预览，不适合缩略图场景。**这个路由已存在且已完工，本票不碰它。**
- 也没有返回「所属会话」可读标题。

**三个缺失字段都已有现成的解析逻辑可以复用，这不是从零设计：**

1. **`msgid`**：`list_media()` 里已经 join 出了完整的 `ArchiveMessage` 对象（`rows = db.execute(statement...)`，每行是 `(media_file, message)` 元组）。`message.msgid` 直接可用，**不需要新查询**。
2. **`conversation_id`**：
   - Group 会话（`message.roomid` 非空）→ `conversation_id = message.roomid`（编码规则见 `backend/app/routers/conversations.py:13` 的模块 docstring：`Group → conversation_id = roomid`）。
   - Direct 会话（`roomid` 为空）→ **必须复用** `backend/app/conversation_membership.py:169` 的 `_direct_conv_id(uid_a, uid_b)`：
     ```python
     def _direct_conv_id(uid_a: str, uid_b: str) -> str:
         """Stable conversation ID for a 1:1 pair regardless of sender/receiver order."""
         a, b = sorted([uid_a, uid_b])
         return f"direct__{a}___{b}"
     ```
     **不要重新实现这个函数。** `conversation_membership.py` 里有大段注释说明这个 ID 格式的 collision 处理、precedence 规则（搜 "direct__" 能看到），这些边界情况不是三言两语能重新推导对的。
3. **`session_title`**：**复用** `backend/app/display_names.py` 的 `resolve_room_display_name(roomid, room_name)`（group）/ `resolve_person_display_name(raw_id, name)`（direct）。同样不要重新实现展示名解析。

**❗ 本项目高频踩坑：**
- **架构边界硬闸**：service 层不得 import `app.routers.*`。`media_library.py` 是 router，可以 import `app.conversation_membership` 与 `app.display_names`（它们是扁平 service 模块，方向合法）。
- **Alembic schema drift 硬闸**：本票**不改模型、不需要迁移**——三个新字段只出现在响应 schema（Pydantic）里，不落库。

## 目标（Goal）
让 `GET /api/admin/media` 的每一项都带上足够信息，使前端能直接拼出既有缩略图/预览路由的合法 URL，且这些信息的计算逻辑与系统里其他地方完全一致（不是平行实现）。

## 范围边界

**In scope：**
1. `backend/app/schemas/media_library.py`：`MediaLibraryItem`（先读现有定义）新增 `msgid: str`、`conversation_id: str`、`session_title: str`（命名如与现有响应风格冲突可微调，但含义不变）。
2. `backend/app/routers/media_library.py` 的 `list_media()`：按上方「背景」的三条规则填充新字段。
3. **先确定 sender/receiver userid 从哪里取**：`ArchiveMessage` 本身可能没有直接的 receiver 字段（群聊场景下"接收方"是多个人）——查 `ArchiveMessageRecipient` 表或 `message.sender_userid`（先 grep 确认实际字段名，不要猜）。若 direct 会话的判定或参与者获取需要额外 join，加在 `list_media()` 的查询里，保持现有分页/排序/筛选行为不变。
4. 测试：`backend/tests/test_media_library.py`（若已存在则追加用例，先检查；若不存在则新建），覆盖 group 与 direct 两种会话类型。

**Out of scope（显式非目标）：**
- **不改下载路由**（`/api/admin/media/{media_id}/download`，RND-292 已交付，工作正常，不动）。
- **不改前端**（RND-329 独立工单，等本票交付后才能继续）。
- **不改 `_direct_conv_id` / `resolve_room_display_name` / `resolve_person_display_name` 的实现**——只调用，不修改语义、不改函数签名。
- **不改现有响应字段的含义**：`room_id`、`message_id` 等保留原样，只新增字段，不重命名不删除。
- 不改分页 / 排序 / 筛选参数的行为。

**本工单拥有的文件（只许写这些）：**
- `backend/app/schemas/media_library.py`
- `backend/app/routers/media_library.py`
- `backend/tests/test_media_library.py`（新建或追加）

**只读、绝不可写：**
- `backend/app/routers/media.py`（下载路由所在文件，RND-292 已交付，不动）
- `backend/app/conversation_membership.py`（只调用 `_direct_conv_id`，不修改）
- `backend/app/display_names.py`（只调用两个 resolve 函数，不修改）
- `backend/app/db/models.py`（本票不改模型）
- RND-327/328/330/331 拥有的任何文件

## 验收标准（Acceptance Criteria）

- **AC-1 三个新字段存在且非空**：`GET /api/admin/media` 响应每项含 `msgid`/`conversation_id`/`session_title`，正常数据下均非空（若底层数据缺失导致某字段无法计算，需在测试里显式覆盖该边界并说明降级行为，不得静默返回空字符串掩盖问题）。
- **AC-2 group 会话 conversation_id 正确**：`conversation_id == message.roomid`（有测试）。
- **AC-3 direct 会话 conversation_id 与独立计算一致（关键，证明复用而非重复实现）**：测试里**单独调用** `app.conversation_membership._direct_conv_id(uid_a, uid_b)` 算出期望值，断言与 `list_media()` 返回的 `conversation_id` 相等。**不是**测试内部再抄一遍拼接逻辑去比对（那样测不出"是否真的调用了共享函数"）。
- **AC-4 session_title 经既有函数产出**：代码审阅可见 `list_media()` 里有 `from app.display_names import resolve_room_display_name` 或等价 import 并调用；不是本票自己写的字符串拼接。
- **AC-5 新字段可拼出合法预览 URL**：测试里用返回的 `conversation_id` + `msgid` 拼出 `/api/conversations/{conversation_id}/messages/{msgid}/media`，对该 URL 发起请求，断言命中的是真实路由处理逻辑（非 404 due to 路由不存在——鉴权失败或数据不存在导致的其他状态码可以接受，只要证明路由匹配上了）。
- **AC-6 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；`GET /api/admin/media` 现有字段（`room_id`/`message_id`/`file_type`/... 等）与既有测试无回归；分页/筛选/排序行为不变。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_library.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/routers/media.py backend/app/conversation_membership.py backend/app/display_names.py backend/app/db/models.py   # 必须全无输出（只调用不修改，不动模型）
```
通过 = 6 条 AC 全满足且上述命令 Exit Code 均为 0。

## 依赖（Dependencies）
无前置阻塞，可立即开始。**本票阻塞 RND-329**，应优先完成。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-6 全满足，每条有对应测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的 3 个文件
- [ ] 产出 QA Summary，**说明最终确定的字段名**（若与本提示词草拟的名字不同，需说明原因，供 RND-329 后续对接）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险 1：重新实现 `_direct_conv_id` 或展示名解析而非复用 → 未来两处逻辑漂移，产生不一致的 conversation_id。**由 AC-3/AC-4 显式防守。**
- 风险 2：为取 sender/receiver 引入的额外 join 拖慢 `list_media()` 性能（该接口做分页，每页最多 200 条）——若需要 N+1 查询，改成批量 join 或批量预取，不要在循环里逐条查。
- 风险 3：`session_title` 计算依赖 `Contact` 表数据缺失时的行为要明确（回退到 userid 本身还是留空），并在测试中覆盖。
- 回滚：本票只新增响应字段，不改现有字段语义、不动模型、无迁移，`git checkout -- <files>` 即可，零生产影响。

## 人工点位
- **Trigger**：Haisu / PM 置 In Progress（已置）。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：若 sender/receiver userid 的获取需要比预期更大范围的查询改动（例如现有分页查询结构无法简单扩展）→ `BLOCKED_NEEDS_HUMAN`，说明具体困难，不要为了凑合而写一个 N+1 查询或跳过某些 direct 会话的处理。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`media_library.py` 全文、`conversation_membership.py` 里 `_direct_conv_id` 及其上下文注释、`display_names.py` 两个 resolve 函数、`conversations.py:1-25` 的 conversation_id 编码规则文档。
2. 确认 `ArchiveMessage` / `ArchiveMessageRecipient` 里 sender/receiver userid 的实际字段名（grep，不要猜）。
3. 改 schema 加三个字段。
4. 改 `list_media()`：加 `msgid` 直取；加 conversation_id 分支逻辑；加 session_title 调用。必要时扩展 join 以获取 sender/receiver。
5. 写测试覆盖 AC-1~AC-5（AC-3 是重点，必须独立调用共享函数比对）。
6. 跑 `make verify`，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据/密钥。
- 不扩大 Scope：下载路由、前端页面一律 Out。
- 复用优先：`_direct_conv_id` / `resolve_*_display_name` 只调用不重写——这是本票存在的核心原因。
- 证据优先，以 exit 0 / 测试通过为证。
