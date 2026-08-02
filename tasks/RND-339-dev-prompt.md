[Goal check] This work advances 开发（Development） by 在归档同步解密后自动检查新增消息、每日完整复核未解决问题，并向 agent 暴露不含内容与原始标识的安全 findings 契约。

# RND-339 开发提示词（Developer Prompt）

> 开始前必须读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、RND-337 dev prompt 与最终 QA verdict、archive worker/runbook/systemd patterns。RND-339 blocked by RND-337；依赖未 PASS 时不得猜 run schema 或提前改 worker。

## ⚡ 立即执行，不要询问意图

你现在收到的是已经批准、待执行的任务指令。你就是 RND-339 开发 agent。先核对 RND-337 最终 QA PASS、实际 Alembic head、R2 migration/部署人工闸；任一未满足则输出 `BLOCKED_NEEDS_HUMAN`，不改产品代码。闸满足后直接实现。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-339「自动化消息可见性检查与 agent 安全诊断接口」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-339/自动化消息可见性检查与-agent-安全诊断接口
- 优先级：Medium｜风险等级：**R2**
- 所属波次：消息可见性闭环 · 自动化与 agent 消费
- 当前关系：blocked by RND-337；RND-337 PASS 后可与 RND-338 并行，禁止修改 RND-338 UI 文件。

## [Goal check]
本工作推进「开发实现」阶段，证据 = 11 条 AC 覆盖增量触发、daily reconciliation、finding 生命周期、完整性保护、租户隔离、安全 API、调度互斥、非阻塞 archive pipeline 与运维回滚。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）

- RND-337 应已交付持久化 run、冻结范围、六态、后台 runner 和 latest API；本票必须复用实际 service/model，不建立第二套 reachability classifier/run 状态机。
- `backend/scripts/run_archive_worker_once.py:103-125,135-151` 以共享 flock 串行执行 sync → decrypt；任一现有核心子进程失败会使 worker exit 1。自动诊断应在 decrypt 成功后触发，但诊断失败不得把已成功的同步/解密变成失败。
- `deploy/systemd/wecom-archive-worker.service:6-25` 定义 oneshot/user/path/hardening，`wecom-archive-worker.timer:4-7` 每 5 分钟执行核心 worker；`docs/wecom_archive_worker_runbook.md:5-16,96-143`、`docs/DEPLOYMENT.md:22-25,133-145`、`docs/ARCHITECTURE.md:274-275` 记录当前运维契约。
- 当前没有 durable reachability finding、增量 watermark、daily full reconciliation 或 agent findings API。
- RND-338 会把 RND-337 latest API 当作人类健康结论；高频 incremental run 不能用“本批次零问题/no data”覆盖最近一次完整健康快照。证据语义必须单调：局部发现问题可立即让旧 healthy 失效，但局部未发现问题不能把整体提升为 healthy。
- `backend/tests/test_http_contract.py:326-327,336-419,443-634` 对 route count/schema 有硬快照；新增 findings GET route 必须最小更新，不能借机改旧 route。
- 目标部署资源有限；不得引入 Redis/Celery/Kafka/独立常驻 daemon。复用 one-shot Python + systemd timer。

**共享工作树基线：**提示词撰写前产品代码 clean；六个 RND-337/338/339 prompt 是 PM artifacts。开发开始时重记 status、实际 RND-337 diff/head 和用户变更；不得修改/revert RND-338 前端或无关 dirty files。

**本项目已知的高频踩坑点：**
- ❗ **没有模板引擎 / 3 locale**：本票无 UI，不得顺手改 template/i18n。
- ❗ **架构边界是硬闸**：新 API 在 router，业务/SQL 在 service，main 只 import/include；service 不 import routers/main。
- ❗ **架构冻结 D1**：不引入前端框架或新基础设施；调度采用现有 systemd one-shot 模式。
- ❗ **诊断不是归档主链路**：sync/decrypt 成功后诊断失败必须可观测但不得令核心 worker 失败或回滚已归档数据。

## 目标（Goal）

让消息可见性检查从“用户手动点一次”变成低成本持续守护：每次成功 sync/decrypt 后增量检查新候选，每日完整复核最近窗口和全部 active findings；将问题保存为可追踪生命周期，并为 agent/支持提供租户隔离、最小且安全的结构化读取接口。

## 范围边界

**In scope（交付物）：**

1. **finding 持久化。** 从 RND-337 最终 head 线性新增 migration/model。finding 至少包含不可猜测 public id、tenant id、内部 archive message reference（只在 DB，带同租户完整性约束）、reason code、active/resolved status、first/last seen、resolved time、occurrence count、first/last run reference、algorithm version、created/updated time。禁止保存消息内容、payload、sender/recipient/room/raw msgid/path/error text。
2. **幂等生命周期。** 同一 tenant + internal message + reason + algorithm version 重复观察只更新 last_seen/count/last_run，不重复建 active finding。reason 改变时旧 active finding只有在完整复核证明后才能 resolved，新 reason 建/更新。所有更新 tenant scoped、事务化。
3. **增量检查。** 新 one-shot automation runner 支持 `incremental`：在 archive worker 的 sync、decrypt 均成功后调用，使用 RND-337 最后一次完整 incremental run 的 frozen max id 作为 durable watermark，检查新 ID 到本次冻结 max；失败/partial 不推进 watermark，下一次重试。late-decrypt/历史回填由 daily reconciliation 兜底。
4. **完整 daily reconciliation 与人类快照兼容。** 新 systemd oneshot/timer 每日运行 `reconcile`，覆盖最近 7 天全部成功解密候选 **以及所有 active findings 对应消息**；只有 run 完整且所有 active 候选都被重新判断后，才可 resolve 已恢复 finding。partial/error/incomplete 只能 upsert 已观察问题，绝不能批量 resolve 或宣布健康。incremental 零问题不得取代 RND-337 latest 中最近一次完整 manual/reconcile 快照；incremental 一旦发现 active 问题，旧 healthy 必须被失效为非健康状态，直到完整 manual/reconcile 给出新结论。
5. **非阻塞核心 worker。** archive worker 在 decrypt 成功后以受控 best-effort 方式运行 incremental；诊断成功/无数据/已被共享锁跳过均不改变核心 worker 成功语义，诊断失败记录安全 exit/status 但核心 worker仍 exit 0。sync 或 decrypt 失败时不得触发 incremental。
6. **互斥与资源。** incremental/reconcile/manual run 共享可审计的 tenant-active-run/进程锁规则，不能并发重复扫同租户。systemd unit 复用当前 user/path/env/hardening，timer `Persistent=true`、每日一次并避开已知 03:17 backup 与 5 分钟 worker 高峰；不自动 enable/start。
7. **agent 安全 API。** 新增 `GET /api/admin/reachability-findings`，默认只返回 active，支持 allowlisted status、reason、时间窗口、limit 与稳定 opaque cursor；响应包含安全聚合、public finding id、reason/status、first/last/resolved timestamps、occurrence count、algorithm version、固定 remediation code，不含内部 archive/run/tenant IDs 或内容/身份/path/error。
8. **认证/租户隔离/分页。** 复用 admin session tenant context，不接受 tenant selector；A 无法读取/影响 B。默认/最大 page size 有界；cursor 与过滤条件绑定或验证，篡改/跨租户 cursor fail closed，排序稳定且无重复/漏项。
9. **安全可观测。** automation 日志只输出 run public id、mode、状态和 counts；不打印 tenant id、finding内部 reference、消息/成员标识、payload、secret、traceback。失败映射到 RND-337 allowlisted safe error 状态。
10. **运维文档。** 新 runbook 与 DEPLOYMENT/ARCHITECTURE 最小更新，写清 install/enable/status/journal/manual dry/local invocation、锁、timer、失败不阻断 worker、回滚；所有 `sudo systemctl` 仅作为人类命令，不由 agent 执行。
11. **测试。** 覆盖首次/重复 incremental、watermark、partial retry、late-decrypt daily catch-up、active resolution、reason change、跨租户、cursor、安全 sentinel、worker child ordering/failure semantics、systemd unit/static runbook 和 route contract。

**Out of scope（显式非目标）：**
- 不改 RND-338 template/JS/CSS/i18n/nav，不新增 human-facing findings UI。
- 不做自动修复/重拉/重解密、通知、邮件/企微告警、工单创建或 SLA。
- 不返回逐条消息内容、raw sender/recipient/room/msgid、内部 archive_message_id/run_id/tenant_id、storage path、traceback。
- 不新增 findings 写/删/resolve API；lifecycle 只由自动检查服务管理。
- 不引入 Redis/Celery/Kafka、常驻 daemon、云 scheduler 或新凭证。
- 不自动复制/启用/restart systemd，不执行生产 migration，不修改部署脚本/CI/CD。

**本工单拥有的文件（只许写这些）：**
- `backend/app/db/models.py`（仅新增 finding 模型/约束；复用 RND-337 run）
- `backend/alembic/versions/0033_reachability_findings.py`（按 RND-337 实际唯一 head 调整文件名/revision；只能一个线性 migration）
- `backend/app/services/reachability_check_service.py`（仅为 trigger source/watermark/reconcile 所需的向后兼容扩展）
- `backend/app/services/reachability_automation_service.py`（新建）
- `backend/app/schemas/reachability_findings.py`（新建）
- `backend/app/routers/reachability_findings.py`（新建）
- `backend/scripts/run_reachability_automation_once.py`（新建 incremental/reconcile one-shot + shared lock）
- `backend/scripts/run_archive_worker_once.py`（仅 decrypt 成功后 best-effort incremental 调用）
- `backend/app/main.py`（仅 import + include findings router）
- `deploy/systemd/wecom-archive-reachability-check.service`（新建）
- `deploy/systemd/wecom-archive-reachability-check.timer`（新建）
- `docs/reachability_automation_runbook.md`（新建）
- `docs/DEPLOYMENT.md`（仅新 unit install/verify/rollback）
- `docs/ARCHITECTURE.md`（仅新增 automation 数据流/unit 条目）
- `backend/tests/test_reachability_automation.py`（新建）
- `backend/tests/test_reachability_findings_api.py`（新建）
- `backend/tests/test_reachability_automation_cli.py`（可新建；不用则不建空文件）
- `backend/tests/test_reachability_systemd_units.py`（新建静态 unit/runbook contract）
- `backend/tests/test_archive_worker_reachability_hook.py`（新建）
- `backend/tests/test_http_contract.py`（仅一个 findings GET route 的 count/schema/auth/shape）

**本工单只读、绝不可写的文件：**
- `backend/app/reachability_audit.py`、`backend/app/routers/reachability_audit.py` — classifier/旧技术 API
- `backend/app/schemas/reachability_checks.py`、`backend/app/routers/reachability_checks.py`、`backend/scripts/run_reachability_check_once.py`、`backend/alembic/versions/0032_reachability_audit_runs.py`、`backend/tests/test_reachability_checks.py`、`backend/tests/test_reachability_check_cli.py`（若存在）— RND-337；除上列 service 扩展外均只读，migration 文件名以实际落地为准
- `backend/app/web/templates/diagnostics.html`、`backend/app/web/static/diagnostics.js`、`backend/app/web/static/diagnostics.css`、`backend/app/assets/i18n.js`、`backend/app/routers/web.py`、`backend/app/web/sidenav.py` — RND-338
- `deploy/systemd/wecom-archive-worker.service`、`deploy/systemd/wecom-archive-worker.timer` — 不改核心 unit；只改 worker Python hook
- `backend/tests/test_rnd280_rbac_scaffold.py` — findings router 使用现有 `get_current_user`；若需 `require_role`，先确认权限模型
- CI/CD、部署脚本、`.env`、生产数据库、密钥

## 验收标准（Acceptance Criteria）

- **AC-1 依赖/迁移**：RND-337 PASS 且从实际唯一 head 线性 migration；finding 模型/migration/tenant composite integrity/唯一性/索引对齐，upgrade/downgrade 往返只影响本票表。
- **AC-2 finding 幂等**：同问题重复 incremental 只有一个 active finding，occurrence/last_seen/last_run 单调更新；跨租户相同 internal ID 不碰撞；DB/response 无内容字段。
- **AC-3 incremental/watermark**：sync→decrypt 成功后才触发；只检查上次完整 watermark 到冻结 max；complete 才推进，partial/error 不推进且重试不漏不重建；无新候选为安全 no-op。
- **AC-4 daily 完整复核/快照单调性**：timer run 覆盖 7 天候选 + 全部 active finding messages，包括 late decrypt/backfill；只有完整复核可 resolve，partial/error/incremental 缺席绝不 resolve。零问题 incremental 不覆盖最近完整健康快照；incremental 新问题会使旧 healthy 立即失效且不能在完整复核前恢复 healthy。
- **AC-5 生命周期正确性**：问题持续时 active；恢复后在完整 reconcile resolved；reason 变化建立新 reason 且旧项只在证明后 resolved；重复 reconcile 幂等，timestamps/count 合法。
- **AC-6 核心 pipeline 隔离**：sync/decrypt 失败不触发；二者成功才触发；diagnostic exit nonzero/lock held/exception 不改变核心 worker成功 exit，且有安全可观测记录。
- **AC-7 systemd/互斥**：unit 路径/user/env/hardening 与现有模式一致，service oneshot、timer daily+Persistent、避开已知高峰；manual/incremental/reconcile 同时触发不重复扫描；仓库实现不执行 systemctl。
- **AC-8 findings API**：默认 active；filters/limit/opaque cursor 稳定无重复漏项；response 仅 allowlist 安全字段/remediation code；未知 filter/cursor 确定 4xx，raw identifiers/content/error sentinel 不出现。
- **AC-9 认证/租户隔离**：未登录失败；tenant A 不能读取/更新/resolve B；request/cursor 不接受或携带可用 tenant selector；service 全部查询/更新显式 tenant scoped。
- **AC-10 运维/架构/兼容**：runbook 可执行且含安装、观察、手动、本地安全、故障、回滚；main 只注册 router；HTTP route count 恰好 +1；RND-337 POST/latest 和旧 audit API 回归通过。
- **AC-11 回归/范围**：聚焦 tests、migration、worker、HTTP contract、architecture、`make verify` 全绿；RND-338 UI、核心 systemd unit、CI/deploy scripts 无本票归因 diff。

## 验证方式（Verification — 确定性闸）
- 类型：**automated + R2 migration/deployment review**
- 命令：
  ```bash
  make verify
  .venv/bin/python -m pytest backend/tests/test_reachability_automation.py backend/tests/test_reachability_findings_api.py -q
  test ! -f backend/tests/test_reachability_automation_cli.py || .venv/bin/python -m pytest backend/tests/test_reachability_automation_cli.py -q
  .venv/bin/python -m pytest backend/tests/test_reachability_systemd_units.py backend/tests/test_archive_worker_reachability_hook.py -q
  .venv/bin/python -m pytest backend/tests/test_reachability_checks.py backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py backend/tests/test_verify_alembic_head.py -q
  (cd backend && .venv/bin/python -m alembic heads)
  git diff --check
  git status --short --branch
  ```
- migration 往返只在 disposable local/test DB 执行；禁止 `systemctl`、真实 worker/SDK、生产 DB。
- 通过 = AC-1 ~ AC-11、唯一 Alembic head、全部适用命令 exit 0、R2 migration/unit/runbook 人工 review 完成。

## 依赖（Dependencies）
- **硬 blocker：RND-337 最终 QA PASS。** 以实际 run fields、trigger source、algorithm version、complete/status 语义为准。
- RND-338 不阻塞且可并行；两票拥有文件必须保持 disjoint。
- **R2 人工闸**：Haisu 确认 finding schema、resolution 规则、worker best-effort 语义、timer/runbook 后才能改产品代码；部署/enable 是另一次人工动作。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-11 有自动化证据
- [ ] incremental watermark、partial retry、late-decrypt full catch-up、active resolution 全覆盖
- [ ] sync/decrypt/diagnostic 退出矩阵有测试，核心成功不被诊断失败推翻
- [ ] findings API 有 tenant/cursor/security sentinel tests
- [ ] migration local/test 往返、唯一 head、route +1、架构硬闸通过
- [ ] systemd unit/runbook 经人工 review，未执行 systemctl/生产迁移
- [ ] `make verify` 全绿，归因 diff 只在拥有文件
- [ ] QA Summary 包含发现/持续/恢复 counts、watermark、timer、回滚和安全字段清单
- [ ] 未 commit、未 push、未建分支

## 风险与回滚（Risk & rollback）
- 风险：增量范围没扫到就误 resolve；incremental 永不 resolve 缺席项，只有完整 reconcile 可 resolve。
- 风险：每 5 分钟零数据 incremental 把 UI 变成 no_data/伪健康；latest 只允许完整 manual/reconcile 提升健康，局部负面证据只允许降级。
- 风险：失败推进 watermark 永久漏扫；仅 complete transaction 提交 watermark，失败沿用上次完整值。
- 风险：daily 与 worker 竞争 2C2G 资源；共享锁、错峰 timer、有界 batch；lock held 为安全 no-op。
- 风险：诊断拖垮归档；worker hook best-effort，核心 exit 语义以 sync/decrypt 为准。
- 风险：agent API 变成数据外泄面；allowlist schema、opaque cursor、tenant scope、敏感 sentinel test。
- 回滚：人类先 disable 新 timer，再回退 worker hook/router/service/model/docs，最后按审阅方案 downgrade 本 migration；RND-337 手动检查继续可用。agent 不自行执行部署回滚。

## 人工点位（Human touchpoints）
- **Trigger / R2 Gate**：RND-337 PASS 后，Haisu 审阅并批准 migration、resolution、worker failure matrix、timer schedule。
- **Deploy Gate**：Haisu 单独决定复制/enable/restart 新 unit；代码完成不等于已部署。
- **Commit Gate**：Haisu 审阅 QA Summary 后批准 commit；agent 不自行 commit。
- **Escalation**：需要修改 classifier/RND-337 schema/RND-338 UI/核心 systemd unit、需新凭证/queue、无法保证 full resolution、head 漂移或两轮失败时，`BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引（步骤）
1. 核对 RND-337 verdict/actual code、Alembic head、worker/systemd/runbooks 和 dirty baseline。
2. 先写 migration/model/tenant integrity、finding lifecycle、watermark/full reconciliation/worker matrix/API security tests；等待 R2 review。
3. 实现 automation service 与 one-shot lock；复用 RND-337 scanner/run 状态，不复制 classifier。
4. 在 worker decrypt 成功后加 best-effort incremental；新增 daily unit/timer、findings router/schema/main wiring 和 HTTP +1。
5. 更新 runbook/DEPLOYMENT/ARCHITECTURE，跑 local/test migration 和全闸；输出 QA Summary + 归因 status；不要 systemctl/commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不执行 systemctl/生产迁移；不改 CI/CD、部署脚本、`.gitignore`。
- 不改 RND-338 UI，不复制 classifier，不新增外部 queue/daemon/credential。
- incremental/partial/error 永不因“未观察到”而 resolve；只有完整 reconciliation 能关闭 finding。
- 诊断失败不阻断成功 sync/decrypt；sync/decrypt 失败不触发诊断。
- API/DB/log/CLI 不暴露内容、身份、内部 ids、path、secret、traceback；findings 无写删 API和自动修复。
