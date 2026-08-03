[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-339 的 11 条 AC，证明自动可见性检查不会漏报、误关闭或阻断归档主链路，且 agent 接口保持最小安全与租户隔离。

# RND-339 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-339 的独立验收 agent，任务从你读到这句话开始。

- **不要**询问目的、是否开始或先等计划批准；直接核对依赖和 AC-1。
- 只读测试无需许可；migration 往返仅限明确 disposable DB，禁止实际 `systemctl`、真实 SDK、生产 worker/DB。
- 唯一允许不产出 PASS/FAIL 的情况，是按规则写出带证据的 BLOCKED verdict，不是向用户反问。

---

## ⚠️ 2026-08-03 追加：P-4 timer 排期的权威值

`tasks/archive/RND-339-dev-prompt.md` 的 2026-08-03 追加已把 AC-10c 需要的具体 `OnCalendar`
值定为 **`*-*-* 04:30:00`**。AC-10c 判定时以这个值为准：dev 实现里若断言的是这个字符串
→ PASS；若断言了别的值、或只有模糊断言（"看起来错开了"）→ 分别按值不符/证据形态不
合格处理，不要因为提示词原文没写具体值而当作本票设计未定。

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
- 唯一允许写入 `tasks/archive/RND-339-qa-verdict.json`。

## 输入
- `tasks/archive/RND-339-dev-prompt.md` 的 AC-1 ~ AC-11。
- `tasks/archive/RND-337-qa-verdict.json` 与最终 run/status/service 契约。
- 本票 diff：finding model/migration、automation service/CLI、archive worker hook、findings API/main、systemd units、runbooks、tests/HTTP contract。

## Preflight（先做，全部做完再进 AC-1）

### P-1 现场采集，不要相信任何文档里的快照

本提示词**不记录**工作树状态与 Alembic head。自己采集，并与开发 agent 报告的
开工 baseline 对照：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
(cd backend && ../.venv/bin/python -m alembic heads)
```

> **关于这条命令（照做，不要自行判断）**：`alembic.ini` 在 `backend/`，所以必须
> `cd backend`；但 venv 在**仓库根**（`Makefile:31` 的 `BACKEND_PY ?= $(CURDIR)/.venv/bin/python`），
> 所以进了 `backend/` 之后要用 `../.venv/`。
> **`alembic heads` 只读 `backend/alembic/versions/`，不连接数据库**——`DATABASE_URL`
> 为空**不影响**它。采集不到 head 时先排查 venv 路径，**不要**把它和 P-2 的
> 数据库缺口混为一谈，也**不要**因此 BLOCK。

- `tasks/` 下的提示词文件是 PM 产物，任何时候都不归因于开发实现。
- RND-338 UI、classifier、旧 audit router、RND-337 schema/router/CLI、核心 worker unit、
  CI/deploy scripts 不属于本票（所有权见 `tasks/WAVE-ownership.md`）。
- RND-338 可能与本票并发进行；`backend/app/web/`、`i18n.js`、`routers/web.py` 有 diff
  是预期的，**不得**记在 RND-339 账上。
- 他票/用户既有 diff 不得修改或误归因；本票新 commit/push/branch 仍 FAIL。
- AC-1b 的判据是「`down_revision` 指向**你采集到的**前一 head」，不是任何写死的 revision 号。

### P-2 依赖闸

```bash
cat tasks/archive/RND-337-qa-verdict.json
```

缺失或 `verdict != "PASS"` → `verdict: BLOCKED`，不必往下走 AC。

### P-3 一次性数据库（决定 AC-1d 怎么判）

```bash
echo "DATABASE_URL=[${DATABASE_URL}]"
```

**按 `docs/agent-test-database.md` 执行。** 你和开发 agent 一样，**可以自建**
一次性测试库来验证 AC-1d，不要因为 `DATABASE_URL` 为空就直接判 BLOCKED。

- 该文件 §2 的三条硬性否决命中任意一条 → 不许用那个库；§5 是绝对禁止清单。
- 凭据在 **`.env` 文件**里，不在环境变量里（§3.1）；**没有 `psql`/`createdb`
  不是 BLOCK 理由**，用 §3.2 的 psycopg2 脚本建库（已实测）。先走完 §3.3 的
  排除表再决定要不要 BLOCK。
- 按 §3 自建空库并 `export DATABASE_URL`，跑往返，正常判 AC-1d。
- 只有本机根本没有可用 PG 实例时，AC-1d 才记为未验证：notes 写
  `HUMAN_MIGRATION_REVIEW_PENDING`，按 R2 风险判 `BLOCKED`；不依赖真库的
  AC-1c/AC-1e 照常判定。**不得**把「没跑」当作 PASS，也**不得**因环境缺口判 FAIL。
- 另需核对开发 agent 是否遵守了同一套规则：若它对开发库/共享库执行过迁移
  → blocker `SECURITY_VIOLATION`。

### P-4 你自己也不许碰生产

不执行 `systemctl`、`sudo`、真实 `run_archive_worker_once.py`、真实 SDK、
共享或生产 DB 的写操作。subprocess 行为一律由隔离测试证明。

## 验收方法（证据优先）

**判定单位是子 AC，不是大项。** `tasks/archive/RND-339-dev-prompt.md` 的「验收标准」已把
11 个大项拆成 AC-1a ~ AC-11h 的原子断言。**本节不重述断言内容**（重述必然与 dev
prompt 漂移）——去读 dev prompt 的原文，本节只规定**每类断言需要什么形态的证据**
和**怎么判**。

evidence 里每一条子 AC 独立成行：`AC-5a | test_reachability_automation.py::test_empty_incremental_does_not_override_snapshot | PASS`。

### 通用证据形态

| 断言形态 | 可接受的证据 | 不可接受 |
|---|---|---|
| 「只有一条 active finding」 | 实际 DB 行数与字段值 | mock 断言 upsert 被调用 |
| 「watermark 不推进」 | 失败前后 watermark 的实际持久化值 | 代码阅读 |
| 「reconcile 覆盖 X」 | 被重新判断的候选 ID 集合断言 | 「调用了 reconcile」 |
| 「不 resolve 任何 finding」 | partial run 前后 active 集合完全相同 | 抽查一条 |
| 「快照不被覆盖」 | 连续 incremental 后 `GET latest` 的实际响应 | service 层单测 |
| 「worker exit matrix」 | 真实 subprocess return code（隔离环境） | 读注释、读 docstring |
| 「timer 错峰」 | 断言具体的 `OnCalendar` 字符串 | 「看起来错开了」 |
| 「cursor 无重复漏项」 | 多页遍历后的 ID 集合与全量集合比对 | 单页 200 |
| 「递归不含 X」 | `docs/agent-data-minimization.md` §5 的递归遍历断言 | 只查顶层 key |
| 「tenant scoped」 | A/B 双租户 seed 后的实际读写隔离 | 只看 router 出口过滤 |
| 「route +1」 | 采集到的当前真实 `route_count` 与改后值之差 | 写死数字 |

### 逐大项的判定重点

- **AC-1**：AC-1b 用**你采集到的** head 判。AC-1c 若内部 archive message reference
  缺少同租户完整性约束 = blocker FAIL。AC-1d 按 Preflight P-3 处理。
- **AC-2**：AC-2c 跨租户相同内部 message id 碰撞 = FAIL。
- **AC-3**：AC-3d 若用「最近一次 started run」或「失败 run 的 max」推进 watermark
  = **blocker FAIL**（会造成永久漏扫）。AC-3f 必须有失败后重跑的实际 fixture。
- **AC-4**：AC-4b（7 天窗口**之外**的 active finding 也要复核）是最容易漏的一条——
  只扫 7 天 = **blocker FAIL**。AC-4e 若 partial run 批量 resolve = **blocker FAIL**。
  另查 AC-4d 的实现：resolution 必须由完整 reconciliation 的**冻结候选集**驱动，
  不能是「本次 upsert 之后把其余 active 全关掉」。
- **AC-5**：本票最容易出错的一项，两个方向都要有测试。
  零数据 incremental 覆盖了完整快照 = **blocker FAIL**；
  incremental 发现新问题后仍显示 healthy = **blocker FAIL**。
- **AC-6**：AC-6c「incremental 未见即 resolve」= **blocker FAIL**。
- **AC-7**：AC-7f 必须验证真实 return-code matrix，**不只读注释**。
  诊断异常导致 worker exit 1 = blocker FAIL；sync/decrypt 失败后仍触发诊断 = blocker FAIL。
- **AC-8**：AC-8f 若 cursor 是 base64 明文的 tenant/internal id，或响应是通用模型 dump
  = **blocker FAIL**。
- **AC-9**：AC-9d 只在 router response 层过滤 = **blocker FAIL**。
- **AC-10**：AC-10a/10b 必须**解析实际 unit 文件**，不得以 runbook 文案代替。
- **AC-11**：AC-11d 若删除或弱化 `test_http_contract.py` 既有 expected 条目 = FAIL；
  只允许追加本票这一条。AC-11h 复制 classifier = FAIL。
  共享工作树里他票的失败先按 P-1 隔离归因再写 notes。

## 本项目专属检查（必查）
1. 搜索 automation service 中的 reachability reason/status 分支；必须调用现有 classifier/RND-337 service，不得复制分类。
2. `main.py` 只能 router wiring；service 不 import routers/main，router 不 import main。
3. `test_http_contract.py` 只应因一个 findings GET route增加 count/snapshot/auth/shape，不得删旧 expected/降低断言。
4. `run_archive_worker_once.py` 的 sync/decrypt原有 fail-fast 保持；只有新增诊断 hook best-effort。验证真实 return-code matrix，不只读注释。
5. systemd test 必须解析实际 unit，不以 runbook 文案代替；timer 不与已知 backup 03:17 和频繁 worker 明显同点。
6. API / DB / log / CLI 四个受管面适用 `docs/agent-data-minimization.md` §2，
   用该文件 §5 的哨兵组递归断言缺失。本票唯一授权的例外是 public finding id 与
   public run id；内部 `archive_message_id` 只允许留在 DB 内部引用列，
   出现在 API、日志或 cursor 中即 blocker。
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
(cd backend && ../.venv/bin/python -m alembic heads)
git diff --check
git diff --name-only
git status --short --branch
git log origin/main..HEAD
```

只在 Preflight P-3 确认为一次性 DB 时执行 migration upgrade/downgrade/upgrade。**不要执行** systemctl/sudo 或真实 `run_archive_worker_once.py`；subprocess 行为必须由隔离测试证明。

## 运维人工 review 点位
- 核对实际 timer calendar、Persistent、资源错峰、shared lock 和 service hardening。
- 逐条按 runbook 做“纸面演练”：安装/启用由人执行，status/journal 不泄密，diagnostic failure 不需停 core worker，rollback 先 disable 新 timer。
- 若未完成 R2 migration/unit/runbook 人工 review，notes 标 `HUMAN_DEPLOYMENT_REVIEW_PENDING`，不得声称已部署或完全交付。

## 产出
写入 `tasks/archive/RND-339-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- **全部子 AC** PASS、无 blocker/major、R2 migration/deployment review 有证据 → `verdict: PASS`，`recommended_next_state: PASS`。
- 任一子 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`；findings **按子 AC 编号定位**（如 `AC-4b`），只列最小修复。
- Preflight P-3 无法满足（**本机根本没有 PG 实例**，不是「`DATABASE_URL` 恰好为空」）→ `verdict: BLOCKED`，notes 写 `HUMAN_MIGRATION_REVIEW_PENDING`；其余子 AC 判定照常写进 evidence。
- RND-337 契约漂移、resolution/timer/权限需产品决策或两轮仍失败 → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。
- evidence 必须逐子 AC 成行，并列出 watermark 前后值、finding lifecycle rows、worker exit matrix、API safe keys、route count 前后值、采集到的 Alembic head、unit schedule 和命令 exit code。不得用「AC-4 全部通过」这类聚合表述。

## 禁止事项
- 除 verdict JSON 外不修改任何文件，不补实现/测试/migration/runbook。
- 不执行 systemctl、sudo、真实 worker/SDK、共享或生产 DB 写操作。
- 不放松“complete-only resolution、健康快照证据单调、failed-run no watermark advance、diagnostic non-blocking、tenant isolation、safe API”核心标准。
- 缺 partial retry、7 天外 active reconcile、worker exit matrix、cross-tenant cursor、sensitive sentinel 中任一自动化测试，直接记 `INSUFFICIENT_TEST_COVERAGE`。
