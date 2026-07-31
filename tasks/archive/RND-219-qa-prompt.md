# RND-219 验收提示词（独立 QA agent）

> 用途：粘贴给独立 QA / Code Review 智能体，在开发 agent 完成 RND-219 后、用户 commit 前做验收。
> 边界：**只读言、不改实现、不 `git commit`/`push`**；只报告发现与放行 / 退回建议。

---

## 0. 角色与边界
- 你是独立验收者。开发 agent 已完成 RND-219 的提取实现，你来验收。
- **只验证、不改代码**：发现问题时在 Linear 评论 + 本回复列出，由开发 agent 修复；你不得直接改 `conversations.py` / `services/` / `schemas/`。
- **不 commit / push**：git 提交与推送一律由用户（Haisu）操作。
- 验收口径：RND-219 是**行为保持的结构性重构**，验收核心是「提取后与提取前逐字节等价 + 结构收敛」，不是功能新增。

## 1. 验收依据
- Linear 工单 RND-219 验收标准原文：
  > ordering、group-wins、seat status、display names、tenant isolation 与现状等价；router 只处理 HTTP 参数与返回；query count 不得恶化。
- 非目标：不做查询性能优化，不改 timeline / media。
- 配套开发提示词（`rnd-219-execution-prompt.md`）定义的提取范围与耦合红线。

## 2. 检查清单

### 2.1 行为等价（最高优先级）
- [ ] **ordering**：monitored accounts 的 active-first / latest_message_time desc 排序；conversations 的 last activity desc 排序，与重构前一致（对比 `tests/test_staff_seats.py` / `tests/test_conversation_display_names.py` 的基线断言）。
- [ ] **group-wins**：`_derive_conversation_membership` 的会话归并规则等价（direct 双方 / group roomid），无会话被错并或漏并。
- [ ] **seat status**：active/history 判定与 `conversation_count` 与现状一致。
- [ ] **display names**：person / room display name 解析路径与降级（缺名时 `resolve_person_display_name` 行为）一致。
- [ ] **tenant isolation**：跨租户不可见彼此 monitored accounts / contacts / conversations（核对 `tests/test_tenant_isolation.py`）。
- [ ] **response contract**：`MonitoredAccountOut` / `ContactOut` / `ConversationOut` 字段名、类型、顺序、OpenAPI schema 未变；`test_http_contract.py` 的 route snapshot 仍通过。

### 2.2 结构收敛
- [ ] 三个端点（`get_monitored_accounts` / `get_contacts` / `get_conversations`）已变为薄封装：仅参数校验 + 调 service + 返回 `response_model`，原查询 / 聚合逻辑已下沉。
- [ ] 新建 `app/schemas/listing.py` 承载 listing 响应模型；新建 `app/services/listing_service.py` 承载逻辑；import 与 `response_model` 引用正确更新。
- [ ] **耦合红线**：仅搬了三端点独占的 helper；被 timeline 共享的 helper 未被误搬（如有 shared helper，确认放在内部共享模块且 Linear 已标注供 RND-220 消费）。
- [ ] 未触碰 timeline cursor（`_encode_message_cursor` / `_decode_message_cursor`）、消息时间线路由、media / timeline 逻辑（非目标）。

### 2.3 性能 / 质量门槛
- [ ] **query count 不恶化**：三端点的 DB 查询次数与重构前一致（核对开发 agent 的 query-count 说明；必要时用 SQL echo 自测）。
- [ ] `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
- [ ] 无新增第三方依赖；无 secrets 引入；仅预期文件变更。

### 2.4 风险 / 回归
- [ ] 既有相关套件全绿：`tests/test_staff_seats.py tests/test_conversation_display_names.py tests/test_contact_sync.py tests/test_tenant_isolation.py tests/test_http_contract.py`（及 `make verify` 全量）。
- [ ] 浏览器 smoke：会话列表 / 联系人 / 监控账号页渲染正常（参照项目 Playwright 脚本）。

## 3. 验证命令（在 `backend/` 下）
- 定向：
  ```bash
  python -m pytest tests/test_staff_seats.py tests/test_conversation_display_names.py tests/test_contact_sync.py tests/test_tenant_isolation.py tests/test_http_contract.py -q
  ```
- 全量收尾：`make verify`
- query count 抽查：如项目有 SQL echo / query-count 工具则启用；否则基于开发 agent 给的查询序列逐项核对期望 vs 实际。

## 4. 放行 / 退回标准
- **放行**：上述 2.1–2.3 全部满足，`make verify` 绿，且未越界（未动 timeline/media、未搬 shared helper、未自 commit/push）。在 Linear 评论给出「验收通过」+ 关键核对点。
- **退回（打回开发 agent）**：任一出现回归——行为不等价（ordering / group-wins / seat / display name / tenant）、response contract 变化、query count 恶化、误搬 shared helper、触碰非目标代码、或测试失败。列出具体差异（期望 vs 实际），不自行改实现。
- 无论放行与否，**不要 commit/push**；提交与合并由用户决定。
