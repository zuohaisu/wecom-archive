[Goal check] This work advances GitHub issue #53 by delivering configurable, direct external-contact pagination with passing repository verification.

# GH-53 QA Summary — 外部联系人分页优化

## Files changed
- `backend/app/web/templates/contacts.html`：分页显示当前位置、结果区间与总数；提供相邻页/首末页数字按钮、页码跳转输入框，以及每页 20/50/100 位选择器。所有翻页和变更页大小都通过既有 `offset`/`limit` 参数重新请求服务端。
- `backend/app/assets/i18n.js`：为简体中文、繁体中文、英语补齐分页控件文案。
- `backend/tests/test_contacts_page.py`：覆盖分页页数计算、直接跳页、服务端页大小切换和三语文案契约。

## Acceptance criteria
- [x] 显示当前页、总页数、当前结果区间与总联系人数量。
- [x] 可点击可见页码，且可输入任意有效页码直接跳转；首尾页在大结果集下仍可到达。
- [x] 可选择每页 20、50 或 100 位联系人；切换时回到第一页，并以新 `limit` 重新请求 API。
- [x] 筛选、租户隔离和既有 API 契约未改动。
- [x] 三种已支持语言均包含分页文案。

## Commands run
- `make verify` → PASS (`3325 passed, 219 skipped`)
- `node --check /tmp/issue-53-contacts.js` → PASS（从实际内联模板脚本提取）
- `node --check backend/app/assets/i18n.js` → PASS
- `git diff --check` → PASS

## Manual verification
- 审阅分页状态机：页码跳转使用 `(page - 1) * limit` 生成服务端 `offset`；每页数量改变后将 `offset` 重置为 0。
- 审阅窄屏样式：分页控制组换行，并从右对齐改为左对齐，避免控件被横向挤出。

## Risks or gaps
- 无。分页仅使用既有 API 已支持的 `offset` 和最大值 200 以内的 `limit`，未改后端或数据模型。

No secrets introduced: confirmed
Only intentional files changed: confirmed
