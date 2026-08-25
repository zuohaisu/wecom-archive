# 消息删除运维手册（RND-361 Epic / RND-364）

本文档说明回收站、30 天自动永久清理、对象存储清理失败重试的运行边界。

## 生命周期

```
选择消息 → 删除确认 → 进入回收站 → 30 天内恢复或主动永久删除 → 到期自动清理
```

- 首次删除为**软删除**：普通会话、搜索、导出、媒体库与统计默认排除已删除消息。
- 回收站默认保留 **30 天**；`purge_after = deleted_at + 30 天`。
- 到期后由后台任务执行**永久清理**，不可恢复。
- 删除只影响本系统存档副本，不影响企业微信原始数据。

## 自动清理任务

- 脚本：`backend/scripts/run_message_purge_once.py`
- systemd 单元：`deploy/systemd/wecom-message-purge.service`（oneshot）、
  `wecom-message-purge.timer`（每日 `02:45`，`Persistent=true`）。
- 每次运行按租户串行处理，每租户最多 `--limit`（默认 200）条；重复运行幂等
  （候选谓词 = 仍处于回收站且 `purge_after <= now`，不可能重复命中）。
- 永久清理顺序（对每条消息）：
  1. 确认仍处于本租户回收站；
  2. 删除派生数据：收件人（`archive_message_recipients`）、撤回关联
     （`message_revocations`）、保留锁（`retention_locks`）、可达性发现
     （`reachability_findings`）；
  3. 媒体对象：仅在存储提供方确认删除成功后删除 `media_files` 行；仍被其他
     媒体行引用的对象（共享/派生文件）不删除对象，仅删除本行；
  4. 对象存储删除失败 → 保留 `media_files` 行与消息行，写入
     `media_purge_retries` 重试状态，不伪报数据库已完整清理。

## 失败重试

- `media_purge_retries` 记录待重试对象；`retry_media_purges()` 在每次运行开始时
  先重试这些对象，成功后删除对应 `media_files` 行与重试行，消息行随后可被正常
  清理。
- 单次重试间隔指数退避（1 小时起步），最多 `PURGE_MAX_ATTEMPTS`（10）次。
- 单条失败不会阻塞整批；其余消息照常清理。

## 运行指标（`GET /api/admin/messages/recycle-bin/metrics`）

不含任何敏感值：

- `pending_purge_count`：待清理（已到期未清）消息数；
- `oldest_pending_age_days`：最老待清理项的逾期天数；
- `last_success_at` / `last_failure_at`：最近成功 / 失败时间（来自审计）；
- `recent_failure_count`：近 7 天对象清理失败次数；
- `storage_retry_pending`：对象存储待重试数量。

## 告警建议

- `storage_retry_pending > 0` 持续超过 24 小时 → 检查对象存储可用性。
- `oldest_pending_age_days > 3` → 可能清理任务未运行（检查 timer / 日志）。
- 审计动作 `messages.purge_media_failed` 出现即代表有对象未能确认删除。

## 暂停与回滚边界

- **暂停**：停用 `wecom-message-purge.timer`（`systemctl stop`）即可暂停自动清理；
  回收站恢复、导出等只读能力不受影响。
- **回滚**：永久清理**不可回滚**。误删恢复只适用于软删除阶段（30 天回收站内）。
- 生产启用真实永久删除前需另行批准与最小验证方案（Epic Definition of Done）。

## 批量清理任务（RND-370）

- 页面：`/admin/cleanup`；流程：条件筛选 → 影响预览 → 强确认 → 异步任务 →
  回收站按批次查看。
- 任务只把匹配消息**软删除移入回收站**，不执行永久删除；永久删除仍走回收站。
- 服务端白名单校验过滤条件（日期/快捷区间/会话/员工/外部联系人/单群聊/类型/
  媒体存在性与大小），不接受自由 SQL 或任意表达式；时间边界固定为 UTC 毫秒。
- 预览与执行通过短期 `preview_version` 绑定；创建任务时若匹配量相对预览漂移
  超过 10% 则拒绝并要求重新预览（HTTP 409）。
- 高风险操作需输入确认短语（`确认清理`）。
- 异步执行：`backend/scripts/process_message_cleanup_once.py`，由
  `deploy/systemd/wecom-message-cleanup.{service,timer}` 每 10 分钟调度，每任务每
  次最多推进 4 页（每页 500 条）；任务状态：queued → running →
  partial/completed/failed | canceled。
- 幂等：候选查询排除已软删除行，崩溃/重启后重跑不会重复处理；单条失败不回滚
  整批（当前实现按页推进并累计成功/跳过计数）。
- 取消：任务创建后可对 queued/running 任务调用取消接口，worker 在下一页边界
  停止；已完成的页不回滚（进入回收站后仍可在 30 天内恢复）。

## 合规保留

- 租户开启合规保留（`tenants.deletion_locked`）后，删除、恢复、主动永久删除、
  自动清理与批量清理任务一律失败关闭（HTTP 423），读取与导出不受影响。
- 审计日志随消息删除而保留，不记录完整正文或媒体内容。
