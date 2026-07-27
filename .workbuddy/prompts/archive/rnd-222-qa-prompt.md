# RND-222 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-222 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-222-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。
> 权威依据：Linear 工单 **RND-222** 验收标准（CLI 输出/exit code 不变；worker/recipient/revoke/tenant/SDK mock tests 通过；tenant scope 有明确代码契约和测试；生产 systemd command 可继续使用）+ 代码硬约束。

---

## 0. 验收依据

- **Linear RND-222 验收（必须全过）**：
  1. CLI 输出 / exit code 不变；
  2. worker / recipient / revoke / tenant / SDK mock 测试通过；
  3. tenant scope 有明确代码契约和测试；
  4. 生产 systemd command 可继续使用。
- **有意的行为变更（且仅此一处）**：decrypt worker 的 tenant scope 修复——pending 查询、`repair_missing_recipients`、`reconcile_pending_revocations`、`pending_remaining` 统计改为按 `tenant_id` 过滤；decrypt 新增 `TenantWecomConfig` 缺失时 fail-fast（对齐 sync/media）。除此之外任何行为变化都是回归。
- **非目标不可被破坏**：不启用多 corp 调度（一次运行仍只处理一个 `(tenant_id, corp_id)`）；不修改生产 timer cadence / systemd unit / `run_archive_worker_once.py`。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已在待测分支/工作区，`git log`/`git diff` 可查。确认重构链上游（RND-219/220/221）状态与开发评论一致；若开发 agent 声明的前置未满足，标 BLOCKED 而非 FAIL。
2. 开发 agent 已自称通过 `make verify`。本 agent 无论如何**复跑一遍 `make verify`** 作为基线。
3. 验收在离线环境进行：测试不需要真实 WeCom SDK 二进制 / 真实 DB（既有测试全部离线，新测试也必须离线可跑）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### A 组 — systemd / CLI 兼容（工单验收 1、4）

- **C1 deploy 零改动**：`git diff HEAD~N -- deploy/`（N 覆盖本任务全部提交；或对照 main）为空；`deploy/systemd/wecom-archive-worker.service`、`wecom-archive-media-download.service`、两个 timer 的 ExecStart/OnCalendar/OnUnitActiveSec 与改造前逐字一致。
- **C2 编排器零改动**：`scripts/run_archive_worker_once.py` 无 diff（锁行为、子进程调用、exit code 传播不变）。
- **C3 CLI 入口存活**：三个脚本 `sync_wecom_archive_once.py` / `decrypt_wecom_messages_once.py` / `download_wecom_media_once.py` 仍有 `if __name__ == "__main__": main()`；media 的 argparse 选项集（`--count-only/--limit/--retry/--types/--since-hours/--newest-first/--skip-nested`）名称与默认值不变（`python scripts/download_wecom_media_once.py --help` 对照或读代码）。
- **C4 输出/exit code 等价（重点）**：
  - 对照 `git diff` 逐一核对三个 shell 的每一条 `print`：文案、顺序、`flush=True`、条件打印逻辑（计数为 0 不打印的行）与改造前一致；`[INFO]/[FAIL]/[PASS]` 前缀不变。
  - 全部 `sys.exit` 点语义不变：env 缺失/非法 → 1；SDK 失败 → 1；commit 失败 → 1；正常完成 → 0。
  - 存在 CLI 级测试（注入 fake 后跑 `main()`，capsys 断言输出 + `SystemExit.code`），且这些测试通过。
  - **允许的例外仅一处**：decrypt 在 `TenantWecomConfig` 无匹配时新增 `[FAIL]` + exit 1（须与 sync 的 `_require_tenant_id` 文案风格一致，且 runbook 已提及）。

### B 组 — Service 提取质量（worker core callable / fake 注入）

- **C5 三个 service 模块存在且签名合规**：`app/services/sync_worker.py` / `decrypt_worker.py` / `media_worker.py` 各暴露核心 callable；`tenant_id` 与 `corp_id` 为**显式必填参数**（无默认值、不可为 None）；session/sdk 经参数注入。
- **C6 service 纯度**：三个 service 模块源码无 `print(`、无 `sys.exit`、不 import `app.main` / `app.routers.*` / `scripts.*`（grep 证据；若开发写了纯度测试，跑之）。
- **C7 fake 注入可用**：存在可复用 fake（`tests/fakes.py` 或 conftest fixture）；新 service 测试通过**注入**（而非 monkeypatch 模块属性）驱动 fake SDK / session；`FakeWecomSdk` 覆盖三个 worker 各自用到的 SDK 函数面。
- **C8 事务边界等价（代码审查）**：sync 按次提交（`if records: commit`，seq 游标单独 session）；decrypt 整轮单次提交、commit 失败上抛由 shell 翻译 `[FAIL] Database commit failed` + exit 1；media 逐条提交、失败 rollback + 孤儿清理。与改造前模式一致。
- **C9 既有 import 面未破坏**：`tests/test_recipient_persistence.py` 的 `from scripts.decrypt_wecom_messages_once import _upsert_recipients, build_missing_recipient_repair_query, repair_missing_recipients` 与 `tests/test_download_wecom_media_once.py` 对脚本符号的 import 全部仍然成立（跑这两个文件即证）。

### C 组 — Tenant scope 审计（工单验收 3，安全核心）

- **C10 decrypt pending 查询已过滤**：代码审查——pending/failed 查询含 `ArchiveMessage.tenant_id == tenant_id`；`repair_missing_recipients(session, tenant_id)` 与 `reconcile_pending_revocations(session, tenant_id)` 均传实参；`pending_remaining` 统计也带 tenant 过滤；旧「Tenant-agnostic」注释已更新。
- **C11 多租户行为测试（重点，必须真实跑）**：存在并通过这样的测试——两租户 fixture 下以 tenant A 运行 decrypt service：
  - A 的 pending/failed 行被处理；
  - B 的行 `decrypt_status`/recipients/revocations **完全不动**（前后快照相等）；
  - A 的失败不影响 B 已提交数据。
  若测试缺失或断言不完整（如只断言 A 被处理、未断言 B 不动），判 FAIL。
- **C12 sync / media scope 未回归**：sync 的 `_require_tenant_id`、`(tenant_id, msgid)` 幂等、seq 游标 `(tenant_id, corp_id)` 维度不变；media 的 `select_candidates(session, tenant_id, …)` 传参不变（对照 `test_download_wecom_media_once.py` 的 tenant 隔离断言与 `test_tenant_sync_state.py`）。
- **C13 无多 corp 调度**：代码审查确认没有「遍历所有租户/所有 corp」的循环被引入；一次运行仍由 env `WECOM_CORP_ID` 决定唯一 scope。
- **C14 契约成文**：`docs/ARCHITECTURE.md` 已有 worker tenant/corp scope 契约小节；`docs/wecom_archive_worker_runbook.md` 提及 decrypt 的 fail-fast 新失败模式。

### D 组 — 回归与收口

- **C15 工单点名测试域全绿**（命令见 §3）：worker（`test_download_wecom_media_once.py` + 新增三个 service 测试）、recipient（`test_recipient_persistence.py`）、revoke（`test_revoke_reconciliation.py` 等）、tenant（`test_tenant_*` 五件套）、SDK mock（`test_wecom_sdk_media.py`、`test_qiniu_worker_integration.py`）。
- **C16 `make verify` 全绿**（lint-diff + typecheck + build + 全量 pytest）。
- **C17 领地未侵犯**：`git diff` 确认未改 `app/routers/*`、`app/schemas/*`、`app/main.py`、`app/conversation_membership.py`、`app/sdk/wecom_sdk.py`（实现）、`app/media_download.py` / `app/revoke_reconciliation.py`（实现——传参调用点除外，且调用点在 scripts/services 侧）。
- **C18 无新依赖 / 无 migration / 无 CI 变更**：未改 `pyproject.toml` / requirements、无 DB migration、未碰 `.github/`。

---

## 3. 测试方法

- **回归 + 新测试**（`cd backend` 后）：
  ```bash
  python -m pytest tests/test_download_wecom_media_once.py tests/test_recipient_persistence.py tests/test_revoke_reconciliation.py tests/test_revoke_concurrency.py tests/test_backfill_revoke_associations.py tests/test_check_message_revocations_integrity.py tests/test_message_revocations_tenant_integrity.py tests/test_tenant_foundation.py tests/test_tenant_isolation.py tests/test_tenant_sync_state.py tests/test_tenant_wecom_config_uniqueness.py tests/test_tenant_media_access.py tests/test_wecom_sdk_media.py tests/test_qiniu_worker_integration.py tests/test_reachability_audit.py -q
  ```
  再追加开发 agent 新增的 service/CLI 测试文件（从 `git status`/diff 找出，逐一跑）。
- **代码审查项（C1–C3 / C5 / C6 / C8 / C10 / C12 / C13 / C17 / C18）**：读 `git diff` 与 `app/services/{sync,decrypt,media}_worker.py`、三个脚本、`deploy/systemd/*`，逐项核对。
- **C4 输出等价**：以 `git show <改造前>:backend/scripts/X.py` 对照改造后，逐行核对 print/exit；再跑 CLI 级测试。
- **收口**：`make verify`。

---

## 4. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码；只**读**、**断言**与**跑测试**。发现需要改代码才能验证 → 记为「待测代码缺口」FAIL 项，不自己补。
- 不绕过租户隔离做测试（用合法多租户 fixture）。
- **绝不 `git commit` / `push`**；只输出验收结论与证据（提交由用户 Haisu 本人操作）。
- 不引入后台进程；不连接真实 WeCom SDK / 生产 DB。

---

## 5. 输出格式（必须结构化）

```
## RND-222 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支/commit <x> · 链上游 219/220/221 已合入：是/否 · make verify：通过/失败

| 编号 | 验收点 | 结果 | 证据（实测/命令/代码位置） |
|------|--------|------|---------------------------|
| C1   | deploy 零改动 | PASS | git diff -- deploy/ 为空 |
| C2   | 编排器零改动 | PASS | ... |
| C3   | CLI 入口/argparse 不变 | PASS | ... |
| C4   | 输出/exit code 等价 | PASS | 逐行对照 + CLI 测试全绿 |
| C5   | service 签名合规（tenant_id 必填） | PASS | ... |
| C6   | service 纯度 | PASS | grep 无 print/sys.exit |
| C7   | fake 注入可用 | PASS | tests/fakes.py + 注入式测试 |
| C8   | 事务边界等价 | PASS | ... |
| C9   | 既有 import 面未破坏 | PASS | recipient/media 测试全绿 |
| C10  | decrypt tenant 过滤四处到位 | PASS | 代码审查 |
| C11  | 多租户行为测试（B 不动） | PASS | test_decrypt_worker_service 全绿 |
| C12  | sync/media scope 未回归 | PASS | ... |
| C13  | 无多 corp 调度 | PASS | ... |
| C14  | 契约成文（docs） | PASS | ... |
| C15  | 点名测试域全绿 | PASS | pytest 输出 |
| C16  | make verify 全绿 | PASS | ... |
| C17  | 领地未侵犯 | PASS | git diff 清单 |
| C18  | 无新依赖/migration/CI | PASS | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- decrypt 的 TenantWecomConfig fail-fast 为声明过的有意变更 → 非缺陷。
- 非目标（多 corp 调度 / timer cadence / systemd）确认未触碰 → PASS 非缺陷。

### 结论与建议
- 可交付 Haisu 决策 commit / 需返工（列必须修项）/ 阻塞（缺链上游）。
```

- 无法复现的条目标 **NOT REPRODUCIBLE** 而非 FAIL；实现确有问题则 FAIL 并给可复现证据。
- 最终把报告作为 Linear RND-222 评论贴出（状态保持 In Progress，交还用户 Haisu 决策是否 commit——**QA 本身绝不 commit/push**）。
