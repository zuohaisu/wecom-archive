# GH-57 QA Summary — 时间线显示外部联系人昵称

## Root cause

会话列表会以租户和当前员工上下文加载外部联系人昵称/备注；时间线只查询了 `contacts.name`。因此已经在会话列表中解析成功的外部联系人，在时间线发送方或接收方位置会退回显示原始 ID。

## Files changed

- `backend/app/services/timeline_service.py`：时间线的当前实现及紧急回退实现均合并外部联系人显示名；员工视图使用该员工的显式备注，联系人视图使用客户当前昵称。
- `backend/tests/test_external_contact_identity.py`：覆盖员工视图的出入站消息以及联系人视图，确保时间线与会话列表采用相同的名称解析规则。
- `tasks/GH-57-qa-summary.md`：本 QA 记录。

## Acceptance criteria

- [x] 时间线中的外部联系人发送方和接收方显示已解析名称，不回退为原始 ID。
- [x] 员工视图显示该员工的活动备注，与会话列表一致。
- [x] 联系人视图显示客户当前昵称，不任意选取其他员工的备注。
- [x] 外部联系人名称查询保持显式租户范围。
- [x] `WEARCHIVE_LEGACY_TIMELINE` 回退实现同步采用相同的名称解析。

## Commands run

- `python -m ruff check backend/app/services/timeline_service.py backend/tests/test_external_contact_identity.py` → PASS。
- `python -m compileall -q backend/app/services/timeline_service.py backend/tests/test_external_contact_identity.py` → PASS。
- `python -m pytest backend/tests/test_external_contact_identity.py backend/tests/test_conversation_display_names.py -q` → PASS (`30 passed`)。
- `python -m pytest backend/tests -q` → PASS (`3326 passed, 219 skipped`)。
- `git diff --check` → PASS。

## Manual verification

- 审阅时间线请求携带的 `mode`/`staff_id`/`contact_id` 上下文：员工上下文仅传入对应员工备注，联系人上下文不传入员工备注。
- 审阅名称优先级：外部联系人身份记录覆盖通用 `contacts` 缓存名称，行为与会话列表一致。

## Risks or gaps

- 无。外部联系人身份表在旧测试/迁移前数据库中不可用时，既有 helper 仍安全降级为原有 `contacts`/原始 ID 行为。

No secrets introduced: confirmed
Only intentional files changed: confirmed
