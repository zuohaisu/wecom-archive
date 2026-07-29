[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-334 的 6 条 AC（重点验证 conversation_id/session_title 确实复用既有函数而非平行实现）并产出带证据的 PASS/FAIL 判定。

# RND-334 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-334「A6-1b 媒体列表补充会话定位字段」｜风险等级 R1｜automated 验收
- **本票 blocks RND-329。若复用检查走过场，代价是 RND-329 拿到一个自己实现的 conversation_id，未来与系统其他地方（如对话审阅台）算出的值不一致——这类 bug 很隐蔽，多半是生产环境才会暴露。AC-3/AC-4 从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 三个新字段存在且非空
- 证据：测试断言 `GET /api/admin/media` 响应每项含 `msgid`/`conversation_id`/`session_title`，正常场景下均非空；数据缺失的边界场景有显式测试且行为可解释（不是静默空字符串）。
- 判定：字段齐全 + 正常与边界场景均有测试 = PASS。

### AC-2 — group 会话 conversation_id 正确
- 证据：测试断言 group 消息返回的 `conversation_id == message.roomid`。
- 判定：有此断言 = PASS。

### AC-3 — direct 会话 conversation_id 与独立计算一致（**关键，本票存在的核心理由**）
- 证据：测试里**必须能看到两次独立计算**——一次是调用 `list_media()` 拿到的 `conversation_id`，另一次是测试代码**单独 import 并调用** `app.conversation_membership._direct_conv_id(uid_a, uid_b)`，然后断言两者相等。
- **反模式排查（重点）**：读测试代码，确认它不是"在测试里重新拼一遍 `f'direct__{a}___{b}'` 字符串"去比对——那样只能证明字符串格式对，证明不了代码真的调用了共享函数（未来 `_direct_conv_id` 的规则变了，这种测试也不会失败，形同虚设）。
- **代码审阅**：`grep -n "_direct_conv_id\|from app.conversation_membership import" backend/app/routers/media_library.py` 应该有命中。
- 判定：测试确实独立调用共享函数比对 + 代码确实 import 并调用它 = PASS。**若代码里有一段独立的 conversation_id 拼接逻辑（哪怕格式一样）而不是调用 `_direct_conv_id` → FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——这是平行实现，正是本票要避免的问题。

### AC-4 — session_title 经既有函数产出
- 证据：`grep -n "resolve_room_display_name\|resolve_person_display_name\|from app.display_names import" backend/app/routers/media_library.py` 应有命中。
- 判定：确实调用了这两个函数之一（按会话类型分支）= PASS。若 `session_title` 是本票自己写的字符串拼接或格式化逻辑 → FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-5 — 新字段可拼出合法预览 URL
- 证据：测试用返回的 `conversation_id` + `msgid` 拼出 `/api/conversations/{conversation_id}/messages/{msgid}/media`，对该 URL 发起请求，断言路由被正确匹配（非"路由不存在"的 404；鉴权失败或数据未找到的其他状态码可接受）。
- 判定：有此端到端拼接验证 = PASS。缺失 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）——否则无法证明这三个字段真的解决了 RND-329 的问题。

### AC-6 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；`GET /api/admin/media` 现有字段（`room_id`/`message_id`/`file_type`/分页/筛选/排序）无回归。
- 判定：全部 exit 0 + 既有字段与行为不变 = PASS。

## 本项目专属检查（必查）
1. **架构边界**：`media_library.py` import `app.conversation_membership` / `app.display_names` 属合法方向（router → 扁平 service 模块）；确认未反向 import `app.routers.*`。
2. **未改共享函数本身**：`git diff --stat -- backend/app/conversation_membership.py backend/app/display_names.py` **必须无输出**。只调用，不修改。
3. **未改下载路由**：`git diff --stat -- backend/app/routers/media.py` **必须无输出**（RND-292 已交付，本票不动）。
4. **未改模型/无迁移**：`git diff --stat -- backend/app/db/models.py` **必须无输出**；`backend/alembic/versions/` 无新文件。
5. **未越界做前端**：diff 中不得出现 `templates/media.html` 或 `admin_media_page.py`（那是 RND-329）。
6. **文件所有权**：`git status --porcelain` 改动应限于 `schemas/media_library.py`、`routers/media_library.py`、`tests/test_media_library.py`。清单外文件 → FAIL（`SCOPE_VIOLATION`）。

## 附加检查（Security）
- 测试中无真实用户 userid/真实会话数据，一律固定假数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_library.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/routers/media.py backend/app/conversation_membership.py backend/app/display_names.py backend/app/db/models.py   # 必须全无输出
grep -n "_direct_conv_id\|from app.conversation_membership import" backend/app/routers/media_library.py   # AC-3
grep -n "resolve_room_display_name\|resolve_person_display_name" backend/app/routers/media_library.py    # AC-4
git status --porcelain
git log origin/main..HEAD                                          # 必须无输出
```

## 产出
写入 `tasks/RND-334-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：最终字段名是否与 dev-prompt 草拟的一致（若不同，需说明并确认 RND-329 已知晓）。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **AC-3 若测试只是"重新拼一遍字符串比对"而非独立调用共享函数 → 直接 FAIL**，不接受"格式看起来一样"。
- AC-5 若无端到端 URL 拼接验证 → 直接 FAIL。
