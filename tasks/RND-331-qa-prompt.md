[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-331 的 7 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-331 验收提示词（Acceptance / QA Prompt）

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-331「D1 存储计量修复」｜风险等级 **R2（含迁移与数据回填）**｜automated 验收
- **这是计费口径的地基。数字错了 = 发票错了，且错误会静默累积、事后极难发现。本票从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令、在**本地/测试库**跑迁移与测试。
- 不可以：改任何文件、commit、push、**对生产库或共享库执行任何操作**、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 回填脚本幂等且可 dry-run
- 证据：测试证明 `--dry-run` 报告行数且**不写库**；连跑两次第二次为 no-op。
- 判定：两项均有用例且通过 = PASS。缺 dry-run 或缺幂等用例 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-2 — 取不到字节数时明确报告，绝不填 0（**最高风险项**）
- 证据：构造「存储对象缺失」的用例，断言该行**未被填成 0**、被列入失败报告、脚本以非零退出或明确汇总提示结束。
- 代码审阅：`grep` 回填脚本中是否存在 `or 0` / `default=0` / `except: pass` 之类的静默兜底。
- 背景：填 0 会让发票金额系统性偏低，且因为不报错，可能几个月都没人发现。
- 判定：有显式失败路径 + 无静默填 0 = PASS。**发现任何把取不到的值当 0 处理 → FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）。**

### AC-3 — 迁移前置校验（**重点**）
- 证据：构造「库中仍有 `file_size IS NULL`」的场景跑迁移，断言迁移**明确失败并提示先跑回填**。
- 代码审阅：确认迁移里**没有** `UPDATE ... SET file_size = 0 WHERE file_size IS NULL` 这类静默填充。
- 判定：迁移明确失败 = PASS。**静默填 0 或直接放行 → FAIL（`IMPLEMENTATION_DEFECT`, blocker）。**

### AC-4 — NOT NULL 生效
- 证据：迁移后 schema 中 `file_size` 为 `NOT NULL`；插入 `file_size=None` 被数据库拒绝（有用例）。
- 判定：约束生效 + 有用例 = PASS。

### AC-5 — 汇总口径唯一
- 证据：存在按 `tenant_id` + 日期的汇总；**有测试证明**其数值等于 `SUM(media_files.file_size) GROUP BY tenant_id` 的同期结果。
- 背景：R3 dashboard 与 R5 账单要共用这一个数字。两处口径打架 = 客户看到的用量与账单不符。
- 判定：有等价性测试 = PASS。仅有汇总实现而无等价性断言 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-6 — schema 无 drift
- 证据：`cd backend && python -m alembic check` exit 0。
- 判定：exit 0 = PASS。非 0 = FAIL（`REGRESSION`）—— CI 会因此变红。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；迁移 `upgrade` 与 `downgrade` 均可执行。
- 判定：全部 exit 0 = PASS。缺 `downgrade()` 实现 = FAIL。

## 本项目专属检查（必查）
1. **未对生产库操作（R2 强制）**：确认脚本 / 测试中**无**生产 `DATABASE_URL`、无硬编码连接串、无对共享库的写操作 → 违反即 FAIL（`SECURITY_VIOLATION`, blocker）。
2. **架构边界**：service 层未 import `app.routers.*`。
3. **迁移双数据库兼容**：确认迁移写法兼容 PostgreSQL（生产）与 SQLite（部分测试），参考既有迁移。
4. **未越界做计费**：本票**不含**计费 / 订阅 / 支付 / dashboard。若 diff 出现这些 → FAIL（`SCOPE_VIOLATION`）。
5. **未碰 R1 页面票文件**：`git diff --stat -- backend/app/web/ backend/app/routers/admin_users_page.py backend/app/routers/admin_audit_page.py backend/app/routers/admin_media_page.py backend/app/routers/admin_contacts_page.py backend/app/assets/i18n.js` **必须全无输出**（否则会与并行的 4 张页面票冲突）。
6. **文件所有权**：`git status --porcelain` 改动文件应限于：回填脚本、新迁移、`models.py`（仅 `file_size` + 新汇总表模型）、汇总服务、新测试。出现清单外文件 → FAIL（`SCOPE_VIOLATION`）。

## 附加检查（Security）
- 无凭证 / 真实连接串 / 真实媒体数据进入代码或测试 → 否则 FAIL（`SECURITY_VIOLATION`）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读 / 仅本地库）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_file_size_backfill.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
cd backend && .venv/bin/python -m alembic check                    # AC-6
cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic downgrade -1   # AC-7，仅本地库
grep -nE 'or 0|default=0|except.*pass' backend/scripts/backfill_media_file_size_once.py   # AC-2 静默兜底排查
git status --porcelain
git log origin/main..HEAD                                           # 必须无输出
```

## 产出
写入 `tasks/RND-331-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中须记录：开发 agent 在哪个库上验证、回填了多少行、多少行取不到字节数。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **不得对生产库执行迁移或回填**（即使只是为了验证）。
- AC-2 / AC-3 若无「明确失败而非静默填 0」的证据 → 直接 FAIL。这两条是计费正确性的唯一防线。
