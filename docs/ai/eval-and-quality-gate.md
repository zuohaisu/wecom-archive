# AI 客服质量门禁与持续改进闭环（RND-359 / T5）

> 权威实现：`backend/app/services/ai/eval.py`、`escalation.py`、`gap_report.py`。
> 本文档与实现冲突时，以代码与 `backend/tests/test_ai_eval.py`、`test_ai_escalation.py` 为准。

## 1. 离线评测

- 数据集：`backend/tests/fixtures/ai_eval_dataset.json`（`version` 字段标注版本），覆盖安装/自托管、企业微信接入、权限、同步、媒体、搜索、存储、计费、常见错误，并含 out-of-scope 负例。
- 运行：`backend/scripts/run_ai_kb_eval.py`（每日 05:00，`deploy/systemd/wecom-ai-kb-eval.{service,timer}`），结果写入 `ai_eval_runs` 表。
- **只评测检索，不调用真实 LLM**：因为本系统的引用（citation）就是检索命中的 chunk 本身，从不从模型输出里解析引用（见 `answer_service._citations_from_chunks`），所以检索命中率天然等价于引用正确率；"无依据回答"也完全由检索结果数量决定（零 chunk → 确定性走 `insufficient_evidence`，不经过模型）。这两点是本系统的架构保证，不需要额外的模型幻觉检测评测。

### 1.1 指标与阈值

| 指标 | 定义 | 阈值 | 性质 |
|---|---|---|---|
| `over_permission_count` | 检索结果中出现调用方无权限访问级别的 chunk 数 | **必须为 0** | 硬 blocker |
| `hit_rate` | in-scope 用例中检索到期望 topic 的比例 | ≥ 0.35 | 质量信号（见下方已知限制） |
| `abstention_rate` | out-of-scope 用例中正确不返回任何证据的比例 | ≥ 0.8 | 质量信号 |

`over_permission_count > 0` 直接判定整轮评测失败（`passed=False`），退出码非零。

### 1.2 已知限制

v1 检索后端是 Postgres `pg_trgm` 的 `word_similarity`（见 `retriever.py` 模块文档字符串），**不是**语义 embedding。字符级 trigram 相似度无法识别同义改写——例如"自己部署"与"自托管部署"字面重叠很少，`word_similarity` 只有 0.08，和完全无关问题的分数在同一区间，因此**不能靠调低相似度阈值来提高召回**，否则会让无关查询也检索到（进而引用）错误文档。这是 `HIT_RATE_THRESHOLD=0.35` 定得远低于理想值的原因——它是基于真实文档集的实测基线，不是目标值。

**后续改进路径（未实施）**：在 `Retriever` Protocol 之下接入基于 embedding 的检索实现（`retriever.py` 已经把接口和实现分开，替换不需要改 `answer_service`），或做查询侧同义词扩展。提高 `HIT_RATE_THRESHOLD` 必须伴随这类真实改进，不得只改数字。

## 2. 确定性转人工规则

实现：`app/services/ai/escalation.py::should_escalate`。按顺序判定，命中即返回原因码：

| 原因码 | 触发条件 |
|---|---|
| `no_evidence` | 检索结果为空 |
| `model_reported_insufficient_evidence` | 有检索结果，但模型判断证据不足（回答以 `INSUFFICIENT_EVIDENCE` 开头） |
| `high_risk_operation` | 问题命中高风险关键词（删除租户/退款/生产环境/运行命令等） |
| `privacy_concern` | 问题命中隐私相关关键词（身份证/手机号/查看其他租户等） |
| `no_citation` | 已回答但零引用（防御性分支，正常架构下不应触发） |
| `conflicting_sources` | 检索结果跨多个 topic 且最高相似度低于阈值 |
| `low_confidence` | 检索结果最高相似度低于阈值 |
| `daily_budget_exceeded` | 见第 4 节成本上限 |
| `user_requested` | 用户在 T3 界面主动点击"转人工"，未附带以上任何自动判定原因 |

转人工摘要生成：`app/services/ai/handoff.py::build_redacted_summary`，只取当前会话自身的问答与引用标题，从不读取归档聊天内容。摘要在提交前必须经用户预览、可编辑（T3 `/api/ai/support/sessions/{id}/handoff/preview` + `.../handoff`）。

## 3. 人工处理结果回流

内部人员通过 `POST /api/platform/ai/handoffs/{id}/resolve`（`require_platform_admin` 保护，跨租户）记录最终分类：

`doc_missing` | `doc_stale` | `product_bug` | `config_issue` | `external_platform` | `model_issue` | `user_misunderstanding`

`backend/scripts/run_ai_kb_gap_report.py`（每周一 06:00）聚合高频未解决问题、`helpful=false` 的回答数、上述分类计数、`ai_feedback` 分类，生成 Markdown 候选改进清单到 `docs/ai/reports/`（已加入 `.gitignore`，因含真实运营内容）。**只产出草稿文本，绝不调用 Linear API**——见 `gap_report.py` 模块文档字符串。

## 4. 成本上限与限流

- **限流**：每租户每分钟最多 20 条消息（`app/routers/ai_support.py::_RATE_LIMIT_MAX_MESSAGES`），进程内内存实现，超出返回 429。
- **成本上限**：`AI_DAILY_TOKEN_BUDGET_PER_TENANT` 环境变量（默认空 = 不限制）。`answer_service._tenant_over_daily_token_budget` 在调用模型前查询该租户当日（UTC）已消耗的 `ai_query_audit_logs.prompt_tokens + completion_tokens` 总和，超出则返回确定性的 `budget_exceeded` 状态，不再调用模型。

## 5. 灰度、回滚与关闭开关

- **总开关**：`AI_SUPPORT_ENABLED`（`app/services/ai/llm_provider.py::ai_support_is_enabled`）。关闭后：`answer_service` 直接返回确定性 `disabled` 状态（不调用检索/模型）；`run_ai_kb_reindex.py` 跳过重建；管理后台页面显示明确配置提示（不隐藏入口，见 RND-357）。此开关是唯一的"是否影响核心产品"边界——核心会话存档功能与 AI 客服完全解耦，关闭后无副作用。
- **模型切换**：`AI_LLM_PROVIDER` + `AI_LLM_MODEL`（`app/services/ai/llm_provider.py::get_llm_provider`），切换只需改环境变量，无需代码改动或数据迁移。
- **索引回滚**：`app/services/ai/ingestion.py::roll_back_to`——把任意历史 `kb_index_versions` 行重新标记为 `active`，旧版本内容立即恢复可检索，坏版本标记为 `rolled_back`（保留审计）。
- **灰度发布**：v1 未实现按租户/百分比的灰度开关；`AI_SUPPORT_ENABLED` 目前是全局开关。分阶段放量需要先接入 `Tenant` 级别的覆盖（可在 `AiSettings` 之上加一张租户白名单表），本轮未实施，记为已知缺口。

## 6. 保留期限、删除与访问审计

- **会话保留**：`AI_RETENTION_DAYS`（默认 90 天），`backend/scripts/run_ai_retention_sweep_once.py`（每日 03:30）删除超期的 `ai_chat_sessions` 及其 `ai_chat_messages`。`ai_query_audit_logs.session_id` / `ai_handoff.session_id` 均为 `ON DELETE SET NULL`（迁移 0060、0059），审计与转人工记录在会话删除后仍然保留。
- **用户主动删除**：`DELETE /api/ai/support/sessions/{id}`（RND-357），租户隔离，仅能删除自己的会话。
- **访问审计**：每次问答写入一条 `ai_query_audit_logs`（脱敏，不含正文之外的敏感值）；每次只读工具调用写入一条 `ai_tool_invocations`（只含字段名，不含字段值，见 RND-358）。
- **内部查看权限**：转人工处理（`/api/platform/ai/handoffs/{id}/resolve`）与知识缺口报告均限定 `require_platform_admin`，租户管理员不可见其他租户数据。
