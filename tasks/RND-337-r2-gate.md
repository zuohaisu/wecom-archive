# RND-337 · R2 人工闸凭据

本文件是 `tasks/RND-337-dev-prompt.md` Preflight **P-4**（原 P-3）所要求的人工确认记录。
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

## 仍然未解除的闸

- **P-2（一次性 PostgreSQL）尚未满足。** 本机 `.env` 的
  `DATABASE_URL` 指向 `wecom_archive`——那是**有数据的本地开发库**
  （1 tenant / 27 archive_messages），且其 `alembic_version` 为 `0005`，
  远落后于当前 head `0031`。对它执行 `alembic upgrade head` 会一次性套用 26 个
  migration，**禁止**。
- 因此 **AC-1d（migration upgrade→downgrade→upgrade 往返）暂不可验证**。
  其余子 AC 照常实现，AC-1d 在 QA Summary 中明确标注为待 PG 环境。
- 若 Haisu 后续提供一次性测试库，在此追加一行并注明库名（不写密码）。

## 部署闸（另一件事）

代码完成 ≠ 已部署。本票不涉及 systemd，但仍适用：agent 不执行 `systemctl`、
`sudo`、真实 worker/SDK、生产 DB 写操作。
