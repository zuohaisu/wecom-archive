[Goal check] This work advances 开发（Development） by 在归档同步解密后自动检查新增消息、每日完整复核未解决问题，并向 agent 暴露不含内容与原始标识的安全 findings 契约。

# RND-339 开发提示词（Developer Prompt）

> 开始前必须读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、RND-337 dev prompt 与最终 QA verdict、archive worker/runbook/systemd patterns。RND-339 blocked by RND-337；依赖未 PASS 时不得猜 run schema 或提前改 worker。

## ⚡ 立即执行，不要询问意图

你现在收到的是已经批准、待执行的任务指令。你就是 RND-339 开发 agent。先执行 Preflight；任一闸未满足则输出 `BLOCKED_NEEDS_HUMAN`，不改产品代码。闸满足后直接实现。

---

## Preflight（开工第一步，先做完再碰代码）

### P-1 现场采集工作树与 Alembic 基线 —— 不要相信任何文档里的快照

本提示词**不记录**工作树状态与 Alembic head。你自己采集：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
(cd backend && ../.venv/bin/python -m alembic heads)
ls backend/alembic/versions/ | sort | tail -5
```

> **关于这条命令（照做，不要自行判断）**：`alembic.ini` 在 `backend/`，所以必须
> `cd backend`；但 venv 在**仓库根**（`Makefile:31` 的 `BACKEND_PY ?= $(CURDIR)/.venv/bin/python`），
> 所以进了 `backend/` 之后要用 `../.venv/`。
> **`alembic heads` 只读 `backend/alembic/versions/`，不连接数据库**——`DATABASE_URL`
> 为空**不影响**它。采集不到 head 时先排查 venv 路径，**不要**把它和 P-2 的
> 数据库缺口混为一谈，也**不要**因此 BLOCK。

归因规则：

1. 不在下方「本工单拥有的文件」清单里的一切改动 → 标为「非本票」，写进 QA Summary
   的 notes，**不修改、不回滚、不覆盖、不算作本票交付**。
2. 采集**之后**新增的 commit / push / branch 才归因于你（你不该产生任何一个）。
3. 你的新 migration 的 `down_revision` 指向**你采集到的实际 head**（正常情况下是
   RND-337 落地的那个），不是本提示词正文提到的任何 revision 号。
   `alembic heads` 多于一个 → 立即 `BLOCKED_NEEDS_HUMAN`。
4. RND-338 可能并发进行（见 `tasks/WAVE-ownership.md` §2）。看到
   `backend/app/web/`、`backend/app/assets/i18n.js`、`backend/app/routers/web.py`
   有 diff 是预期的，**不是**你的越界，也不要去改。

### P-2 依赖闸：RND-337 必须 QA PASS

```bash
cat tasks/RND-337-qa-verdict.json
```

- 缺失或 `verdict != "PASS"` → `BLOCKED_NEEDS_HUMAN`，零产品代码改动。
- PASS → 读**实际落地的** `backend/app/services/reachability_check_service.py`、
  run model、六态与 `safe_error_code` 目录，以真实代码为准，不以本提示词描述为准。

### P-3 一次性数据库（migration 往返需要，硬前置）

```bash
echo "DATABASE_URL=[${DATABASE_URL}]"
```

**按 `docs/agent-test-database.md` 执行**——你**可以也应该自建**一次性测试库，
不要因为 `DATABASE_URL` 为空就整票 BLOCK。

- 该文件 §2 的三条硬性否决命中任意一条 → 不许用那个库。
- 按 §3 自建空库并在本次会话内 `export DATABASE_URL`；§5 是绝对禁止清单。
- 只有本机根本没有可用 PG 实例才 `BLOCKED_NEEDS_HUMAN`。
- 按 §6 记录库名/host（不写密码）、是否自建、是否已清理。

### P-4 R2 人工闸

Haisu 必须已确认 finding schema、resolution 规则、worker best-effort 语义、
timer/runbook。未确认 → `BLOCKED_NEEDS_HUMAN`，零产品代码改动。

**部署是另一次独立的人工动作**：代码完成 ≠ 已部署。你**不执行** `systemctl`、
`sudo`、真实 worker、真实 SDK 调用。

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

**共享工作树基线：**见 Preflight P-1。基线与 Alembic head 由你现场采集，本节不做任何快照断言。
`tasks/` 下的提示词文件是 PM 产物，任何时候都不归因于开发实现。

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
- 不在 DB 列、API 响应、日志或 CLI/进程参数中出现 `docs/agent-data-minimization.md` §2 的
  任何字段族。本票**唯一**的例外是不可猜测的 public finding id 与 public run id
  （由 AC-8c 显式授权）。内部 `archive_message_id` 只允许存在于 DB 内部引用列，
  绝不进入 API、日志或 cursor。
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
- `backend/app/web/templates/diagnostics.html`、`backend/app/web/static/diagnostics.js`、`backend/app/web/static/diagnostics.css`、`backend/app/routers/web.py` — **所有者 RND-338**（可能与本票并发进行）
- `backend/app/assets/i18n.js`、`backend/app/web/sidenav.py`、`backend/tests/test_sidenav.py` — **所有者 RND-336**（见 `tasks/WAVE-ownership.md` §3）
- `backend/app/audit.py`、`backend/app/routers/audit.py` 及 RND-335 拥有的其它 routers/services — **所有者 RND-335**
- `deploy/systemd/wecom-archive-worker.service`、`deploy/systemd/wecom-archive-worker.timer` — 不改核心 unit；只改 worker Python hook
- `backend/tests/test_rnd280_rbac_scaffold.py` — findings router 使用现有 `get_current_user`，按 `docs/ticket-autopilot-workflow.md` §3.3 **不触发**闭世界白名单；若需 `require_role`，先确认权限模型
- CI/CD、部署脚本、`.env`、生产数据库、密钥

> 跨票所有权与并发矩阵的权威来源是 `tasks/WAVE-ownership.md`；本清单与它冲突时以它为准。
> 本票与 RND-337 在 `models.py`、`main.py`、`test_http_contract.py`、
> `reachability_check_service.py`、`alembic/versions/` 上是**波次内顺序共享**（§4）：
> 你在 RND-337 已落地的版本上继续，只做本票增量。

## 验收标准（Acceptance Criteria）

> **每条子 AC 是一个原子断言，独立 pass/fail。** QA 按子 AC 编号逐条判定，findings
> 也按子 AC 编号定位。

### AC-1 依赖与迁移
- **AC-1a**：`tasks/RND-337-qa-verdict.json` 为 PASS。
- **AC-1b**：新 revision 的 `down_revision` 指向 Preflight P-1 采集到的**实际唯一** head；之后仍只有一个 head。
- **AC-1c**：finding 模型与 migration 逐字段对齐；含 tenant composite 完整性约束（内部 archive message reference 必须与 finding 同租户）、public id 唯一、状态枚举、非负计数、查询索引。
- **AC-1d**：Given P-3 的一次性 PG，When `upgrade → downgrade → upgrade`，Then 三步都成功且只影响本票新增的表。**P-3 未满足时本条不得标记通过。**
- **AC-1e**：finding 表中不存在消息内容、payload、sender/recipient/room/raw msgid、path、error text 字段。

### AC-2 finding 幂等
- **AC-2a**：Given 同一 tenant + internal message + reason + algorithm version 被连续观察 N 次，Then 始终只有 1 条 active finding。
- **AC-2b**：AC-2a 过程中 `occurrence_count` 单调递增，`last_seen` / `last_run` 单调更新，`first_seen` 不变。
- **AC-2c**：Given 两个租户各有一条**相同内部 message id** 的问题，Then 两条 finding 互相独立，不碰撞、不互相更新。
- **AC-2d**：DB 行与 API 响应均不含 `docs/agent-data-minimization.md` §2 的字段族。

### AC-3 incremental 与 watermark
- **AC-3a**：Given sync 失败，Then **不**触发 incremental。
- **AC-3b**：Given sync 成功但 decrypt 失败，Then **不**触发 incremental。
- **AC-3c**：Given sync 与 decrypt 都成功，Then 触发 incremental。
- **AC-3d**：incremental 的检查区间下界取自**最后一次 complete incremental run 的 frozen max id**，不是最近一次 started run、也不是失败 run 的 max。
- **AC-3e**：Given run complete，Then watermark 在**同一事务**中推进。
- **AC-3f**：Given run partial 或 error，Then watermark **不**推进；下一次重试覆盖原区间，且不产生重复 finding。
- **AC-3g**：Given 区间内无新候选，Then 安全 no-op（不建 run 垃圾、不报错、不推进到未检查的位置）。

### AC-4 daily 完整复核（覆盖面）
- **AC-4a**：reconcile 覆盖最近 7 天内的全部成功解密候选。
- **AC-4b**：reconcile **额外**覆盖全部 active finding 对应的消息，**包括 7 天窗口之外的**。
- **AC-4c**：late decrypt / 历史回填产生的候选被 reconcile 捕获（incremental 漏掉的由它兜底）。
- **AC-4d**：只有 run complete **且**全部 active 候选都被重新判断过，才允许 resolve。
- **AC-4e**：Given reconcile partial / error / count mismatch，Then **不** resolve 任何 finding（一条都不行），只 upsert 已观察到的问题。

### AC-5 人类快照单调性（本票最容易出错的一项）
- **AC-5a**：Given 连续多次零问题的 incremental run，Then RND-337 `GET latest` 返回的仍是**最近一次完整 manual/reconcile 快照**，不被 incremental 的 `no_data` 或局部 healthy 覆盖。
- **AC-5b**：Given 一次 incremental 发现了新的 active 问题，Then 旧的 `healthy` **立即失效**为非健康状态。
- **AC-5c**：AC-5b 之后，在下一次**完整** manual/reconcile 给出结论之前，状态**不得**恢复为 `healthy`。
- **AC-5d**：一句话规则的测试化表达——局部正面证据不提升整体健康，局部负面证据可立即降级。两个方向都要有测试。

### AC-6 finding 生命周期
- **AC-6a**：问题持续存在期间，finding 保持 active。
- **AC-6b**：问题恢复后，在一次**完整** reconcile 中被 resolved，`resolved_at` 被写入。
- **AC-6c**：Given 同一消息的 reason 发生变化，Then 建立/更新新 reason 的 finding；旧 reason 的 active finding **只有**在完整复核证明其已消失后才 resolved。
- **AC-6d**：Given 已 resolved 的问题再次出现，Then 行为确定（新建或重开，二选一并有测试固定），timestamps 与 counts 合法。
- **AC-6e**：重复执行 reconcile 幂等——第二次不改变任何 finding 的状态与计数。

### AC-7 核心 worker 隔离
- **AC-7a**：exit matrix 至少覆盖五种情形并各有断言：sync fail / decrypt fail / 全成功且诊断成功 / 全成功但诊断失败 / 锁被占用。
- **AC-7b**：诊断 exit 非零时，核心 worker 仍 exit 0（归档真相不被附属任务推翻）。
- **AC-7c**：诊断抛异常时，核心 worker 仍 exit 0。
- **AC-7d**：锁被占用时视为安全 no-op，核心 worker 仍 exit 0。
- **AC-7e**：上述失败情形都有安全的可观测记录（状态/日志），且记录本身不违反数据最小化。
- **AC-7f**：`run_archive_worker_once.py` 中 sync/decrypt 原有的 fail-fast 语义**未被改动**——只新增诊断 hook。

### AC-8 findings API
- **AC-8a**：不带参数时默认只返回 active。
- **AC-8b**：status / reason / 时间窗口 / limit filters 行为正确；limit 有默认值与上界。
- **AC-8c**：响应字段是 allowlist：public finding id、reason、status、first/last/resolved timestamps、occurrence count、algorithm version、固定 remediation code、安全聚合。递归断言不含内部 archive/run/tenant id、内容、身份、path、error。
- **AC-8d**：opaque cursor 翻页稳定——多页遍历无重复、无漏项，包括「并列 timestamp」与「翻页期间插入新行」两种 fixture。
- **AC-8e**：cursor 与过滤条件绑定或验证；篡改过的 cursor fail closed。
- **AC-8f**：cursor 内容不可解码出 tenant id 或内部 id（base64 明文内部 id = 失败）。
- **AC-8g**：未知 filter 值 / 非法 cursor → 确定性 4xx。

### AC-9 认证与租户隔离
- **AC-9a**：未登录访问 findings API 失败。
- **AC-9b**：request 与 cursor 均不接受、也不携带可用的 tenant selector。
- **AC-9c**：Given tenant A/B 同时 seed，Then A 的 list / filter / cursor 全路径不返回 B 的数据。
- **AC-9d**：service 层的 upsert / resolve 全部显式 tenant scoped（不是只在 router 出口过滤）。
- **AC-9e**：A 的 cursor 或 public finding id 对 B fail closed，且不泄露存在性。

### AC-10 systemd 与互斥
- **AC-10a**：静态解析新 service unit：`Type=oneshot`，User / WorkingDirectory / EnvironmentFile / ExecStart / hardening 与现有 `wecom-archive-worker.service` 模式一致。
- **AC-10b**：静态解析新 timer unit：每日一次、`Persistent=true`。
- **AC-10c**：timer 时间避开已知 03:17 backup 与 5 分钟 worker 高峰（断言具体 `OnCalendar` 值，不是「看起来错开了」）。
- **AC-10d**：incremental / reconcile / manual 并发触发时，共享锁保证同租户只实际扫描一次。
- **AC-10e**：仓库内没有任何自动执行 `systemctl` / `sudo` / enable / start 的代码或脚本改动。

### AC-11 运维、架构、兼容与回归
- **AC-11a**：runbook 含安装、观察（status/journal）、手动执行、本地安全调用、故障处理、回滚六节，且与实际 unit/script 一致，不含真实 secret。
- **AC-11b**：`DEPLOYMENT.md` 与 `ARCHITECTURE.md` 的新增内容与实际实现一致。
- **AC-11c**：`main.py` 的 diff 只有 findings router 的 import 与 include。
- **AC-11d**：新增 route 恰好 1 个；`test_http_contract.py` 的 `route_count` 由**采集到的当前真实值** +1 得到，不是写死数字，且未删除或弱化既有 expected 条目。
- **AC-11e**：RND-337 的 POST/latest 与旧 `/api/admin/reachability-audit` 回归测试全部通过。
- **AC-11f**：`make verify`、聚焦测试、worker、systemd、migration、HTTP contract、architecture 全部 exit 0。
- **AC-11g**：本票归因 diff 仅限拥有文件；RND-338 UI、核心 systemd unit、CI/deploy scripts 无本票改动。
- **AC-11h**：automation service 中不存在重新定义的 reachability status/reason 分支——分类只来自 RND-337 service 与 `app.reachability_audit`。

## 验证方式（Verification — 确定性闸）
- 类型：**automated + R2 migration/deployment review**
- 命令：
  ```bash
  make verify
  .venv/bin/python -m pytest backend/tests/test_reachability_automation.py backend/tests/test_reachability_findings_api.py -q
  test ! -f backend/tests/test_reachability_automation_cli.py || .venv/bin/python -m pytest backend/tests/test_reachability_automation_cli.py -q
  .venv/bin/python -m pytest backend/tests/test_reachability_systemd_units.py backend/tests/test_archive_worker_reachability_hook.py -q
  .venv/bin/python -m pytest backend/tests/test_reachability_checks.py backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py backend/tests/test_verify_alembic_head.py -q
  (cd backend && ../.venv/bin/python -m alembic heads)
  git diff --check
  git status --short --branch
  ```
- **migration 往返只能对 Preflight P-3 自建的一次性测试库执行。** 跑之前再确认一次
  `DATABASE_URL` 指向的是那个库，不是开发库。只有本机根本没有 PG 实例时才 BLOCK，
  AC-1d 保持未完成、静态 schema 断言照常。
  **禁止** `systemctl`、`sudo`、真实 worker/SDK、生产 DB。
- 通过 = 全部子 AC 满足、唯一 Alembic head、全部适用命令 exit 0、R2 migration/unit/runbook 人工 review 完成。

## 依赖（Dependencies）
- **硬 blocker：RND-337 最终 QA PASS。** 以实际 run fields、trigger source、algorithm version、complete/status 语义为准。
- RND-338 不阻塞且可并行；两票拥有文件已在 `tasks/WAVE-ownership.md` §2 核对为 disjoint。
- 与 RND-337 在 `models.py`、`main.py`、`test_http_contract.py`、`reachability_check_service.py`、`alembic/versions/` 上是波次内顺序共享（§4），不是并发冲突。
- **R2 人工闸**：Haisu 确认 finding schema、resolution 规则、worker best-effort 语义、timer/runbook 后才能改产品代码；部署/enable 是另一次人工动作。

## 完成定义（Definition of Done）
- [ ] Preflight P-1（含实际 Alembic head）、P-2、P-3、P-4 结论已记录
- [ ] AC-1a ~ AC-11h **每一条子 AC** 都有自动化证据（不是每个大项一条）
- [ ] incremental watermark、partial retry、late-decrypt full catch-up、active resolution 全覆盖
- [ ] AC-5 快照单调性两个方向都有测试（局部正面不提升、局部负面可降级）
- [ ] sync/decrypt/diagnostic 退出矩阵五种情形有测试，核心成功不被诊断失败推翻
- [ ] findings API 有 tenant/cursor/`docs/agent-data-minimization.md` §5 哨兵 tests
- [ ] migration 在 P-3 确认的一次性 DB 往返、唯一 head、route +1、架构硬闸通过
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
1. 跑 Preflight P-1 / P-2 / P-3 / P-4 并记录结论；读实际 RND-337 代码与 worker/systemd/runbooks。
2. 先写 migration/model/tenant integrity、finding lifecycle、watermark/full reconciliation/worker matrix/API security tests；等待 R2 review。
3. 实现 automation service 与 one-shot lock；复用 RND-337 scanner/run 状态，不复制 classifier。
4. 在 worker decrypt 成功后加 best-effort incremental；新增 daily unit/timer、findings router/schema/main wiring 和 HTTP +1。
5. 更新 runbook/DEPLOYMENT/ARCHITECTURE，跑 local/test migration 和全闸；输出 QA Summary + 归因 status；不要 systemctl/commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不执行 systemctl/生产迁移；不改 CI/CD、部署脚本、`.gitignore`。
- 不改 RND-338 UI，不复制 classifier，不新增外部 queue/daemon/credential。
- incremental/partial/error 永不因“未观察到”而 resolve；只有完整 reconciliation 能关闭 finding。
- 诊断失败不阻断成功 sync/decrypt；sync/decrypt 失败不触发诊断。
- 数据最小化以 `docs/agent-data-minimization.md` 为准（DB 列、API、日志、CLI 参数四个面），例外只有 public finding id 与 public run id；findings 无写删 API 和自动修复。
