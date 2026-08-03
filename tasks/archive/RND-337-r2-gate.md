# RND-337 · R2 人工闸凭据

本文件是 `tasks/archive/RND-337-dev-prompt.md` Preflight **P-4**（原 P-3）所要求的人工确认记录。
dev agent 读到本文件即视为 R2 闸已满足，可以开始改产品代码。

## 批准

- **批准人**：Haisu
- **批准时间**：2026-08-02
- **批准范围**：RND-337 的 run schema、后台 runner 执行方式、migration 方案。
- **原话**：「批准，直接开工」

## 被批准的方案（以 dev prompt 正文为准，此处只做锚定）

1. **run schema** — 新增 `reachability_audit_runs`：不可猜测 public run id、tenant id、
   状态、触发来源、algorithm version、scope from/to、冻结的 max message id、
   matching/checked/reachable/unreachable counts、reason-count JSON、
   started/completed/created timestamps、受控 `safe_error_code`。
2. **后台执行方式** — 复用仓库现有的 one-shot Python script + subprocess 模式
   （对标 `backend/scripts/run_archive_worker_once.py`）。**不引入** Celery / Redis /
   Kafka / 外部队列 / 常驻 daemon。后台进程只接收 public run id，自行从 DB 解析
   tenant 与 scope。
3. **migration 方案** — 从实际唯一 Alembic head 线性新增**一个** migration。
   `downgrade` 只删除本票新增的表。

## P-2（一次性 PostgreSQL）：由 dev agent 自行准备

- Haisu 明确指示：**不由人工代建，交给执行本票的 dev agent 在其所在开发机上自建。**
- 操作规程见 `docs/agent-test-database.md`：借 `DATABASE_URL` 的 user/host/port，
  只换库名，`createdb` 一个带 `test` 标记的空库，本次会话内 `export`。
- **⚠️ 已知陷阱（写提示词的机器上实测）**：`.env` 里的 `DATABASE_URL` 可能指向
  **有数据的开发库**（该机上是 `wecom_archive`：1 tenant / 27 archive_messages，
  且 `alembic_version = 0005`，落后当前 head `0031` 整整 26 个版本）。
  对它执行 `alembic upgrade head` 属破坏性操作，**绝对禁止**。
  这正是 `docs/agent-test-database.md` §2 三条硬性否决要拦的情况。
- 只有当所在机器**根本没有可用的 PG 实例**时，才就 P-2 上报 `BLOCKED_NEEDS_HUMAN`。

## 部署闸（另一件事）

代码完成 ≠ 已部署。本票不涉及 systemd，但仍适用：agent 不执行 `systemctl`、
`sudo`、真实 worker/SDK、生产 DB 写操作。
