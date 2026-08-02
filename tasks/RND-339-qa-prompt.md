[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-339 的 11 条 AC，证明自动可见性检查不会漏报、误关闭或阻断归档主链路，且 agent 接口保持最小安全与租户隔离。

# RND-339 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-339 的独立验收 agent，任务从你读到这句话开始。

- **不要**询问目的、是否开始或先等计划批准；直接核对依赖和 AC-1。
- 只读测试无需许可；migration 往返仅限明确 disposable DB，禁止实际 `systemctl`、真实 SDK、生产 worker/DB。
- 唯一允许不产出 PASS/FAIL 的情况，是按规则写出带证据的 BLOCKED verdict，不是向用户反问。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-339「自动化消息可见性检查与 agent 安全诊断接口」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-339/自动化消息可见性检查与-agent-安全诊断接口
- 风险等级：R2｜类型：后端、迁移、调度、运维与安全接口独立验收
- 依赖：RND-337 最终 QA PASS；RND-338 不阻塞且不得被本票修改。

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = AC-1 ~ AC-11 每条都有源码、测试、运行时/静态 unit 证据，并产出 PASS/FAIL/BLOCKED verdict。

## 你的角色与权限
- 只读验证 RND-339，不替开发补实现、测试、migration 或文档。
- 可以读仓库、运行测试/静态解析、在 disposable DB 做 migration 往返。
- 不修改任何实现/测试/工单，不 commit/push/建分支，不执行 systemctl/sudo/真实 worker/生产访问。
- 唯一允许写入 `tasks/RND-339-qa-verdict.json`。

## 输入
- `tasks/RND-339-dev-prompt.md` 的 AC-1 ~ AC-11。
- `tasks/RND-337-qa-verdict.json` 与最终 run/status/service 契约。
- 本票 diff：finding model/migration、automation service/CLI、archive worker hook、findings API/main、systemd units、runbooks、tests/HTTP contract。

## 共享工作树归因（先做）

提示词撰写前产品代码 clean；六个 RND prompt 为 PM artifacts。验收开始重记 status/diff/log/Alembic heads，并按 dev prompt 文件清单归因。

- RND-338 UI、classifier、旧 audit router、RND-337 schema/router/CLI、核心 worker unit、CI/deploy scripts 不属于本票。
- 他票/用户既有 diff 不得修改或误归因；本票新 commit/push/branch 仍 FAIL。

## 验收方法（证据优先）

### AC-1 — 依赖与迁移
- 证据：RND-337 verdict PASS；新 migration 从其实际唯一 head 线性延伸；model/migration 字段、tenant composite FK、unique/check/index 对齐；disposable DB upgrade→downgrade→upgrade。
- 判定：只新增本票 finding schema、往返无分叉 = PASS；依赖漂移、内部 reference 无同租户完整性、生产写入 = blocker FAIL。

### AC-2 — finding 幂等
- 证据：同 tenant/message/reason/version 连续观察仍一条 active，occurrence 和 last_seen/last_run 合法更新；不同租户相同内部 message id 各自独立；reason/algorithm version 行为有测试。
- 判定：事务幂等且 DB 不含 content/identity/error 字段 = PASS；重复记录爆炸或跨租户碰撞 = FAIL。

### AC-3 — incremental 与 watermark
- 证据：首次、无新数据、多批次、partial page、exception、retry fixtures；watermark 来源为最后一次 complete incremental frozen max，只有 complete 提交；失败重跑覆盖原范围且 finding 不重复。
- 判定：新候选不漏、失败不跳过 = PASS；用最新 started run 或失败 max 推进 = blocker FAIL。

### AC-4 — daily 完整复核与人类快照单调性
- 证据：7 天内候选、7 天外 active finding、late decrypt/backfill 都被 reconcile；完整覆盖 active set 的证明可见；partial/error/count mismatch 不 resolve 任何“缺席” finding。连续零问题 incremental 后 latest 仍是最近完整 manual/reconcile 快照；incremental 新发现问题后旧 healthy 立即失效为非健康，直到完整复核才可恢复。
- 判定：完整复核覆盖最近窗口 + 全部 active references，且局部正面证据不提升整体、局部负面证据不被旧 healthy 掩盖 = PASS；只扫 7 天、零数据覆盖完整快照或新问题后仍显示 healthy = blocker FAIL。

### AC-5 — 生命周期
- 证据：problem persists、recovers、reason changes、reappears、repeat reconcile 参数化 tests；resolved_at 只在完整证明后写，active/resolved counts 和 first/last timestamps 单调一致。
- 判定：状态转换确定且幂等 = PASS；incremental 未见即 resolve、partial 批量 resolve = blocker FAIL。

### AC-6 — 核心 worker 隔离
- 证据：子进程顺序/exit matrix 至少覆盖 sync fail、decrypt fail、all success+diagnostic success、diagnostic fail、lock-held no-op；sync/decrypt fail 不调用诊断，diagnostic fail 时核心 worker仍成功，安全状态/日志可追踪。
- 判定：归档真相不被诊断附属任务推翻 = PASS；诊断异常导致 worker exit 1 或 sync/decrypt fail 后仍扫 = blocker FAIL。

### AC-7 — systemd 与互斥
- 证据：静态解析新 service/timer：Type=oneshot、正确 User/WorkingDirectory/EnvironmentFile/ExecStart/hardening、Persistent=true、daily 且错峰；shared lock 在 incremental/reconcile/manual 并发 test 中只运行一次；git diff 无 enable/start side effect。
- 判定：与现有 pattern 一致、资源有界、未自动部署 = PASS。

### AC-8 — findings API
- 证据：默认 active、status/reason/time/limit filters、最大 limit、稳定 opaque cursor、多页插入/并列时间、非法/篡改 cursor；递归检查 JSON keys/values 只有 allowlist public id/reason/status/times/count/version/remediation/aggregate。
- 判定：无重复漏项、未知输入确定 4xx、无内部 id/内容/identity/path/error = PASS；base64 明文 tenant/internal id cursor 或通用模型 dump = blocker FAIL。

### AC-9 — 认证与租户隔离
- 证据：未登录 401/redirect 按 API 现有约定；A/B seed 后 list/filter/cursor/service upsert/resolve 均隔离；A 的 cursor/public id 对 B fail closed，不泄露存在性。
- 判定：每个 ORM read/update/delete 都带 tenant 边界 = PASS；只在 router response filter = blocker FAIL。

### AC-10 — 运维、架构与兼容
- 证据：runbook/DEPLOYMENT/ARCHITECTURE 与实际 unit/script 一致，包含 install/observe/manual/failure/rollback 且不含真实 secret；main 只 import/include；route count 恰好 +1；RND-337 POST/latest 与旧 audit API tests 通过。
- 判定：运维可执行、架构硬闸全绿、无契约破坏 = PASS。

### AC-11 — 回归与范围
- 证据：聚焦测试、worker/systemd/migration/HTTP/architecture、`make verify` exit 0；本票归因 diff 仅拥有文件；RND-338 UI/core worker units/CI/deploy scripts 无改动。
- 判定：全部成立 = PASS；越界或归因回归 = FAIL。

## 本项目专属检查（必查）
1. 搜索 automation service 中的 reachability reason/status 分支；必须调用现有 classifier/RND-337 service，不得复制分类。
2. `main.py` 只能 router wiring；service 不 import routers/main，router 不 import main。
3. `test_http_contract.py` 只应因一个 findings GET route增加 count/snapshot/auth/shape，不得删旧 expected/降低断言。
4. `run_archive_worker_once.py` 的 sync/decrypt原有 fail-fast 保持；只有新增诊断 hook best-effort。验证真实 return-code matrix，不只读注释。
5. systemd test 必须解析实际 unit，不以 runbook 文案代替；timer 不与已知 backup 03:17 和频繁 worker 明显同点。
6. API/DB/log/CLI 使用 content/payload/sender/recipient/room/msgid/archive_message_id/run_id/tenant_id/path/secret/traceback 哨兵，递归断言公开面缺失。
7. resolution 查询必须由 complete reconciliation 的冻结 candidate set 驱动，不能用“本次 upsert 后其余 active 全关闭”。

## 附加检查（Scope / Security）
- 修改 RND-338 UI、自动修复/重拉/通知/工单、findings 写删 API、外部 queue/daemon/credential → `SCOPE_VIOLATION`。
- 自动执行 systemctl/sudo/生产 migration/真实 SDK → `SECURITY_VIOLATION`。
- raw identifiers/content/tenant/internal ids/error/traceback 暴露或跨租户 cursor 可用 → blocker `SECURITY_VIOLATION`。
- 仅靠 HTTP 200/green tests 而无真实 JSON、DB rows、subprocess exit、unit 内容证据 → `INSUFFICIENT_TEST_COVERAGE`。

## 验证命令（只读，可运行）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_reachability_automation.py backend/tests/test_reachability_findings_api.py -q
test ! -f backend/tests/test_reachability_automation_cli.py || .venv/bin/python -m pytest backend/tests/test_reachability_automation_cli.py -q
.venv/bin/python -m pytest backend/tests/test_reachability_systemd_units.py backend/tests/test_archive_worker_reachability_hook.py -q
.venv/bin/python -m pytest backend/tests/test_reachability_checks.py backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py backend/tests/test_verify_alembic_head.py -q
(cd backend && .venv/bin/python -m alembic heads)
git diff --check
git diff --name-only
git status --short --branch
git log origin/main..HEAD
```

只在明确 disposable DB 下执行 migration upgrade/downgrade/upgrade。**不要执行** systemctl/sudo 或真实 `run_archive_worker_once.py`；subprocess 行为必须由隔离测试证明。

## 运维人工 review 点位
- 核对实际 timer calendar、Persistent、资源错峰、shared lock 和 service hardening。
- 逐条按 runbook 做“纸面演练”：安装/启用由人执行，status/journal 不泄密，diagnostic failure 不需停 core worker，rollback 先 disable 新 timer。
- 若未完成 R2 migration/unit/runbook 人工 review，notes 标 `HUMAN_DEPLOYMENT_REVIEW_PENDING`，不得声称已部署或完全交付。

## 产出
写入 `tasks/RND-339-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- AC 全 PASS、无 blocker/major、R2 migration/deployment review 有证据 → `verdict: PASS`，`recommended_next_state: PASS`。
- 任一 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`，只列最小 findings。
- RND-337 契约漂移、resolution/timer/权限需产品决策或两轮仍失败 → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。
- evidence 必须列出 watermark 前后值、finding lifecycle rows、worker exit matrix、API safe keys、route count、Alembic head、unit schedule 和命令 exit code。

## 禁止事项
- 除 verdict JSON 外不修改任何文件，不补实现/测试/migration/runbook。
- 不执行 systemctl、sudo、真实 worker/SDK、共享或生产 DB 写操作。
- 不放松“complete-only resolution、健康快照证据单调、failed-run no watermark advance、diagnostic non-blocking、tenant isolation、safe API”核心标准。
- 缺 partial retry、7 天外 active reconcile、worker exit matrix、cross-tenant cursor、sensitive sentinel 中任一自动化测试，直接记 `INSUFFICIENT_TEST_COVERAGE`。
