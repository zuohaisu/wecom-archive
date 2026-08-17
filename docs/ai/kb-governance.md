# AI 客服知识库内容治理规范（RND-355 / T1）

> 权威实现：`backend/app/ai_kb/manifest_schema.py` + `backend/app/ai_kb/governance.py`。
> 本文档与实现冲突时，以代码 + `backend/tests/test_ai_kb_*.py` 为准，请提 PR 同步修订本文档。

## 1. 唯一入口：manifest

AI 客服知识索引（RND-356 / T2）**只允许**从 `backend/app/ai_kb/manifest.json` 里
`status=approved` 且 `access_level` 不是 `forbidden` 的条目取内容。索引器不得扫描
`docs/`、`.qoder/repowiki` 或任何其它目录自行发现文档——manifest 是唯一权威清单，
不存在「隐式目录扫描」路径。

## 2. 访问级别（至少三档，已落地）

| access_level | 含义 | 示例 |
|---|---|---|
| `customer` | 客户可见，AI 对普通租户管理员可引用 | `docs/kb/customer/*.md` |
| `internal` | 仅内部客服/运维账号可见 | `docs/ARCHITECTURE.md`（架构说明） |
| `forbidden` | 永久禁止摄取（denylist），即使有人误标 `approved` 也会被拒 | 运维 runbook、含内部域名/操作细节的文档 |

`internal` 级别的检索/引用权限判定由 T2 的检索接口结合 T4 的角色区分
（`require_role` / `require_platform_admin`）执行；本票只负责在 manifest 层面
把「谁能看什么」的边界定义清楚。

## 3. 每份知识源必须记录的字段

`source_id`（稳定、永不复用）、`topic_id`（跨语言分组）、`title`、`path`、
`access_level`、`locale`、`translation_status`、`audience`、`version`、`status`、
`source_of_truth`、`owner`、`last_updated`。字段定义见
`manifest_schema.py::ManifestEntry` 与 `_REQUIRED_FIELDS`。

## 4. 语言处理与缺失翻译策略

产品实际支持的 3 个界面 locale 是 `zh-CN` / `zh-TW` / `en`（`backend/app/assets/i18n.js`
的 `LocaleRegistry`；工单文字提到的「日文」目前不是产品的真实 locale，本规范以
实际产品 locale 为准）。

- `zh-CN` 是当前唯一的 Source of Truth（`source_of_truth=true`，`translation_status=source`）。
- 请求 `zh-TW` / `en` 时，若同一 `topic_id` 下没有对应 locale 的 `approved` 条目，
  `governance.resolve_locale_fallback()` 回退到该 topic 的 source_of_truth 条目，
  并把结果标记为 `is_fallback=True`——调用方（T2/T3）必须据此展示「暂无该语言译文，
  以下为中文内容」提示，不得当作原生译文静默展示。
- 后续补充译文时，只需新增一条 `locale=zh-TW`/`en`、`translation_status=translated`
  的条目并 approve，无需改动已有条目。

## 5. 文档有效性门禁

- 只有 `status=approved` 的条目参与索引；`draft`/`deprecated` 永不作为当前答案依据。
- 重复内容、历史迁移说明等应显式标 `deprecated` 而不是从 manifest 删除——保留可审计的历史记录。

## 6. 敏感信息扫描

`governance.scan_for_sensitive_content()` 在 `resolve_ingestable_sources()` 内对
每份候选文档的内容做正则扫描（secret/token/ticket/连接串/私钥块/未脱敏日志行等，
见 `_SENSITIVE_PATTERNS`）。命中即整份文档被拒绝摄取，原因记录在
`RejectedEntry.reason`（只记录匹配到的**规则名和行号**，不落匹配到的原文，避免
扫描结果本身泄露敏感值）。

## 7. 变更、撤销与索引计划联动

`governance.compute_index_plan()` 对比「上次索引状态」与「当前 manifest + 摄取
允许结果」，产出 `add` / `update` / `remove` / `noop` 计划：

- 文档内容变化（哈希变化）→ `update`。
- 文档从 manifest 删除、被降级为 `forbidden`、被标记 `deprecated`，或原本合规的内容
  被敏感信息扫描拒绝 → `remove`（旧索引内容必须可撤销，由 T2 的索引版本机制执行实际撤销）。
- `access_level` 变化 → `update`（重新评估检索可见性）。

T2 在实现真实索引时直接消费这个 plan，不需要重新发明 diff 逻辑。

## 8. 首批批准知识源清单

见 `backend/app/ai_kb/manifest.json`。首批覆盖：用户指南、自托管指南、企业微信
接入与常见错误、FAQ、故障排查、配置说明（均为 `customer` 级）；另附一条
`internal` 示例（架构说明）与一条 `forbidden` 示例（归档 Worker 运维 Runbook），
用于验证三档访问级别与 denylist 机制均已生效。

## 9. 边界（Out of scope，沿用票面）

本票不实现 LLM 问答接口、不实现客服前端、不摄取归档聊天/媒体/客户私有资料、
不修改生产配置、不接触生产数据。
