[Goal check] This work advances RND-422 implementation by defining the approved, testable R3 repository-only change set.

# RND-422 开发计划 — 归档 Worker 运维硬化

## 已批准范围与边界

本票按 R3 仓库部署配置变更执行。批准仅覆盖代码、版本化部署模板、自动化测试和文档；不授权生产 `systemctl`、凭证、数据库或部署操作。

- 仅纳管既有同名 `wecom-archive-worker.service` 与 `wecom-archive-worker.timer`；不新建并行 timer 或 service。
- 保持生产既有约五分钟归档调度：版本化 worker timer 必须是 `OnCalendar=*:0/5`，并复用既有 `Unit=wecom-archive-worker.service`。不触碰 worker 的锁、模式选择、tenant 路由、同步、cursor、支付或激活语义。
- `ARCHIVE_WORKER_TRIGGER_SOURCE=timer` 仅是安全的触发来源归因。
- 受控部署必须保持幂等：同名 unit 更新后只由既有逻辑 `daemon-reload`，且只对 timer 执行 `enable --now`；不得产生重复 enable、重复触发或并发 worker。以静态/渲染测试证明，不以生产观察代替测试。
- 凭证分类只使用通用、脱敏错误类别；不得硬编码 RND-419、任何 tenant、环境变量值或历史凭证内容。
- `archive_messages.msgtime` 只补 epoch 毫秒契约和 `/1000` 转换测试；不迁移或改写历史数据。

## 预期写入文件

- `deploy/systemd/MANAGED_UNITS`：加入既有 archive worker service/timer 各一次。
- `deploy/systemd/wecom-archive-worker.timer`：把受控模板与生产既有五分钟节奏对齐，不新建第二个 unit。
- `backend/app/services/tenant_credentials.py`：增加通用、无敏感数据的 tenant archive credential 解析与安全错误类别。
- `backend/scripts/{run_archive_worker_once,sync_wecom_archive_once,decrypt_wecom_messages_once,download_wecom_media_once}.py`：使用统一分类；不改变 worker 功能路径。
- `backend/tests/test_rnd343_worker_scheduling.py`：更新版本化五分钟调度断言。
- `backend/tests/test_rnd387_multi_tenant_worker.py`：更新旧宽泛错误类别的兼容断言。
- `backend/tests/test_rnd422_worker_hardening.py`：覆盖同名 unit 纳管/幂等静态契约、五分钟节奏、五种安全凭证类别、脱敏输出及 epoch-ms `/1000` 转换。
- `docs/{DEPLOYMENT.md,ARCHITECTURE.md,wecom_archive_worker_runbook.md}`：同步受控安装、单一 timer、五分钟节奏和 msgtime 契约。
- `tasks/RND-422-{dev-prompt,qa-prompt}.md`：本票交付和 QA 计划。

## 验收标准

1. `MANAGED_UNITS` 恰好列出既有 archive worker service/timer 各一次；timer 继续触发同名 service，受控模板保持 `*:0/5` 与 `ARCHIVE_WORKER_TRIGGER_SOURCE=timer`。
2. 静态测试证明既有部署逻辑只会对 trigger unit 执行 `enable --now`，不会启用 oneshot service；同名单元更新不会产生第二个 timer 或并发 worker。
3. tenant-scoped worker 路径对配置缺失/停用、必要字段缺失、历史未加密或异常格式、当前 key 无法解密、其他受控凭证异常产生不同的固定安全错误类别；输出不含 secret、密文、key、CorpID、UserID 或原始 tenant ID。
4. 既有宽泛 `tenant_credentials_unreadable` 调用方改用上述受控类别；缺字段不会被标成 key mismatch。
5. 运维文档明确 `archive_messages.msgtime` 是 epoch 毫秒；自动化测试证明时间转换使用 `/1000`。
6. `make verify`、聚焦测试与架构边界测试通过。

## 验证

```bash
.venv/bin/python -m pytest \
  backend/tests/test_rnd422_worker_hardening.py \
  backend/tests/test_rnd343_worker_scheduling.py \
  backend/tests/test_rnd387_multi_tenant_worker.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
make verify
```

## 交付纪律

完成后先提交 QA Summary 与工作树状态，等待 Haisu 对本票唯一 commit 和分支 push 的明确批准。合并后任何生产部署、`daemon-reload`、`enable`、`restart` 或只读核验均须再次单独批准。
