# RND-408 QA Summary — 官网公开售前 AI 客服

## Files changed

### 数据库与模型
- `backend/alembic/versions/0063_rnd408_public_ai_support.py`：新增匿名访客售前会话、消息、审计日志、转人工四张独立表。
- `backend/app/db/models.py`：新增 `AiPublicChatSession`、`AiPublicChatMessage`、`AiPublicQueryAuditLog`、`AiPublicHandoff` 模型。

### 知识库治理
- `backend/app/ai_kb/manifest_schema.py`：新增 `public` 访问级别。
- `backend/app/ai_kb/governance.py`：`public` 级资料可通过摄取允许列表。
- `backend/app/ai_kb/manifest.json`：新增 4 份公开产品资料（功能、价格、接入准备、开通流程）。
- `docs/kb/public/features.md`
- `docs/kb/public/pricing.md`
- `docs/kb/public/wecom-prerequisites.md`
- `docs/kb/public/getting-started.md`

### 配置与模型边界
- `backend/app/settings.py`：新增 `AI_PUBLIC_SUPPORT_ENABLED`、`AI_PUBLIC_SUPPORT_DAILY_TOKEN_BUDGET`、`AI_PUBLIC_SUPPORT_DAILY_TOKEN_BUDGET_PER_VISITOR`。
- `backend/app/services/ai/llm_provider.py`：新增 `ai_public_support_is_enabled()`，受总 AI 开关约束。

### 服务与路由
- `backend/app/services/ai/public_answer_service.py`：独立的公开 RAG 问答服务，只检索 `public` 级 chunk，独立审计与预算。
- `backend/app/routers/public_ai_support.py`：公开页面 `/public/support` 与匿名 API（会话、消息、转人工预览/提交、删除）。
- `backend/app/main.py`：注册公开 AI 支持路由。

### 前端
- `backend/app/web/templates/public_support.html`
- `backend/app/web/static/public_support.js`
- `backend/app/web/static/public_support.css`

### 保留期与运维
- `backend/scripts/run_ai_public_retention_sweep_once.py`：按 `AI_PUBLIC_RETENTION_DAYS` 清理过期公开会话。
- `deploy/systemd/wecom-ai-public-retention-sweep.service`
- `deploy/systemd/wecom-ai-public-retention-sweep.timer`

### 测试
- `backend/tests/test_public_ai_support.py`
- `backend/tests/test_ai_public_retention_sweep_script.py`
- `backend/tests/test_ai_kb_manifest.py`：更新 public 级断言。
- `backend/tests/test_ai_kb_governance.py`：新增 public 级摄取测试。
- `backend/tests/test_http_contract.py`：更新路由总数与快照。

### 任务文档
- `tasks/RND-408-dev-prompt.md`
- `tasks/RND-408-qa-summary.md`

## 验收标准检查

- [x] 未登录访客可在官网打开聊天入口并咨询价格、功能与配置  
  `/public/support` 无需登录，页面会设置匿名 `visitor_id` Cookie。
- [x] 回答仅引用 approved + public 的资料  
  `public_answer_service.py` 硬编码 `access_levels=["public"]`，只检索 public 级 chunk。
- [x] 未登录访客无法访问 `/admin/support` 或任何租户 AI 会话  
  `/admin/support` 仍要求管理员 session；公开路由不使用 `get_current_user`/`require_html_session`。
- [x] 公开客服无法读取租户数据、存档数据、内部文档或诊断信息  
  公开服务不引用租户模型、archive_messages、诊断工具或 customer/internal 文档；测试 `test_public_answer_service_uses_only_public_chunks` 断言检索结果 access_level 均为 public。
- [x] 关闭任一 AI 开关、模型故障、无证据或预算超限时，安全拒答并提供人工渠道  
  `ai_public_support_is_enabled()` 同时检查总开关与公开开关；disabled/budget_exceeded/insufficient_evidence/error 状态均返回明确文本，并在前端提示“转人工”。
- [x] 原有登录后 `/admin/support` 的问答、反馈和转人工功能回归通过  
  `tests/test_ai_support_router.py` 及全部 AI 相关测试通过。
- [x] 包含迁移、配置说明、测试、QA 总结和回滚说明  
  见本文件及以下章节。

## 命令运行

```bash
# 无 DATABASE_URL（离线 / SQLite 兼容测试）
make lint-diff          # pass
../.venv/bin/python -m pytest tests/ -q -rs --tb=short -p no:warnings
# 3300 passed, 158 skipped

# 有 DATABASE_URL（PostgreSQL 测试）
DATABASE_URL="postgresql://postgres@localhost:5432/wecom_archive_test_rnd408?host=/tmp" ../.venv/bin/python -m alembic upgrade head
DATABASE_URL="postgresql://postgres@localhost:5432/wecom_archive_test_rnd408?host=/tmp" ../.venv/bin/python -m alembic check   # No new upgrade operations detected.
DATABASE_URL="postgresql://postgres@localhost:5432/wecom_archive_test_rnd408?host=/tmp" ../.venv/bin/python -m pytest tests/test_public_ai_support.py tests/test_ai_public_retention_sweep_script.py tests/test_verify_alembic_head.py tests/test_readiness_health_endpoint.py -q --tb=short
# 31 passed
DATABASE_URL="postgresql://postgres@localhost:5432/wecom_archive_test_rnd408?host=/tmp" ../.venv/bin/python -m pytest tests/test_ai_support_router.py tests/test_ai_answer_service.py tests/test_ai_retriever.py tests/test_ai_llm_provider.py tests/test_ai_kb_ingestion_db.py tests/test_ai_kb_reindex_script.py tests/test_ai_escalation.py tests/test_ai_tools_registry.py tests/test_ai_tools_handlers.py -q --tb=short
# 129 passed
```

## 配置说明

部署前需设置环境变量（示例）：

```bash
# 总 AI 开关必须同时开启
AI_SUPPORT_ENABLED=true
AI_PUBLIC_SUPPORT_ENABLED=true

# 模型配置与现有 AI 客服共用
AI_LLM_PROVIDER=deepseek
AI_LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=...           # 不提交到仓库

# 公开流量独立预算（可选）
AI_PUBLIC_SUPPORT_DAILY_TOKEN_BUDGET=500000
AI_PUBLIC_SUPPORT_DAILY_TOKEN_BUDGET_PER_VISITOR=10000

# 公开会话保留期（默认 7 天）
AI_PUBLIC_RETENTION_DAYS=7
```

上线后需执行一次 reindex 使 public 资料进入检索索引：

```bash
cd backend
python scripts/run_ai_kb_reindex_once.py  # 或 CI/运维流程中触发
```

systemd 保留期清理（可选，默认未启用）：

```bash
sudo systemctl enable wecom-ai-public-retention-sweep.timer
sudo systemctl start wecom-ai-public-retention-sweep.timer
```

## 迁移说明

- 仅新增 alembic 0063，不修改既有表。
- 公开表与租户 AI 表完全隔离，无 tenant_id/admin_user_id，无 archive_messages 关联。
- 上线前 `alembic upgrade head` 自动创建表；回滚时 `alembic downgrade 0062` 删除公开表。

## 回滚说明

1. **功能回滚**：将 `AI_PUBLIC_SUPPORT_ENABLED` 置为 `false`（或 `AI_SUPPORT_ENABLED=false`）即可关闭公开入口，无需部署。
2. **代码回滚**：撤销本 PR / 执行 `git revert`。
3. **数据回滚**：`alembic downgrade 0062` 会删除 `ai_public_chat_sessions`、`ai_public_chat_messages`、`ai_public_query_audit_logs`、`ai_public_handoff` 四张表及索引。
4. **索引回滚**：如已 reindex 公开文档，可将 `kb_index_versions` 中对应版本标记为 `rolled_back`，并重新激活上一版本。

## 已知风险与缺口

- 公开客服当前仅支持中文（`locale="zh-CN"`），与现有 RND-354 一致。
- 公开转人工记录暂无平台管理员解析端点；当前由后台直接查询 `ai_public_handoff` 处理，后续可复用 `/api/platform/ai/handoffs` 模式扩展。
- 公开页面目前是独立页面 `/public/support`，官网如需内嵌浮窗需额外前端集成（本 PR 未改官网主站代码）。
- 未引入新的第三方依赖；DeepSeek 配置与现有 AI 客服共用。

## 安全与密钥确认

- 未提交任何 API Key、模型密钥、真实访客内容或生产数据。
- `.env` 未修改；新增配置通过环境变量读取。
- 公开文档经敏感内容扫描规则过滤，不含 Secret/Token/私钥示例。
