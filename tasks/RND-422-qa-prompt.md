[Goal check] This work advances RND-422 independent QA by defining evidence for every approved repository-only acceptance criterion.

# RND-422 QA 计划 — 归档 Worker 运维硬化

## 权限与边界

QA 仅可读和运行离线测试；不得修改文件、commit、push、执行生产 `systemctl`、访问凭证或数据库。

## 必查验收

1. `deploy/systemd/MANAGED_UNITS` 对既有 `wecom-archive-worker.service` 和 `.timer` 各有且仅有一个条目；timer 的 `Unit` 仍指向同名 service，`OnCalendar=*:0/5`，service 保留 `ARCHIVE_WORKER_TRIGGER_SOURCE=timer`。
2. 静态/渲染证据证明 `scripts/deploy_server.sh` 只安装同名单元、仅 enable trigger unit，更新是幂等的；不存在第二个 archive timer、并发 worker 或调度节奏改动。
3. tenant-scoped archive/media/sync/decrypt 路径使用统一的固定安全类别：配置不可用、字段缺失、历史格式、key mismatch、未知；任何 stdout/stderr 和异常文本均不得含测试 secret、ciphertext、CorpID 或 raw tenant ID。
4. 确认不存在 RND-419 特定名称、真实凭证或环境值硬编码；worker 模式、tenant 路由、sync、cursor、支付和激活路径无功能变更。
5. 运行手册明确 archive `msgtime` 是 epoch 毫秒；测试以已知 epoch-ms 值验证转换必须除以 `1000`，且无 migration/数据改写。
6. 架构边界、聚焦测试和 `make verify` 全绿。

## 命令

```bash
.venv/bin/python -m pytest \
  backend/tests/test_rnd422_worker_hardening.py \
  backend/tests/test_rnd343_worker_scheduling.py \
  backend/tests/test_rnd387_multi_tenant_worker.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
make verify
git status --short
git diff --check
git diff --name-only
```

QA verdict 写入 `tasks/RND-422-qa-verdict.json`；实现不足时仅报告具体 finding，不补做实现。
