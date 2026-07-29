# RND-317 QA 验收提示词 — C2-3 导出审计钩子（合规评审）

> 本文档是交给**独立 QA agent** 的验收 brief，对应开发 brief `RND-317-dev-prompt.md`。
> 你**只验收、不改代码**。所有结论基于：实际运行测试 + 读取改动文件 + 与 Linear RND-317 的 AC 比对。

---

## 0. 验收口径（先读）

- **Linear RND-317**：`[BLOCKED: A7] C2-3 导出审计钩子`；Dependencies A7；**AC：导出被记录**；0.75w；labels backend/export/audit。
- **本票本质 = "审计钩子"**：把"导出被执行"写入 `audit_logs`。**不生成文件**（C2-1）、**不实现 gate**（RND-316）。验收焦点 = "一次导出是否被如实、合规地记录"。
- **A7 已合并**（工作树有 `AuditLog` + `write_audit`）→ `BLOCKED: A7` 已非硬阻塞，开发应已实现。若 A7 缺失 → 判 BLOCKED（见 §5）。
- **RND-316（gate）可能已合并或尚未合并** → 影响 `gate_enforced` 路径，两种情形都要验（见 §3）。

---

## 1. GREEN 判定清单（必须全过）

### 1.1 功能（AC：导出被记录）
- [ ] `AuditAction.EXPORT = "export.executed"` 与 `AuditObjectType.EXPORT = "export"` 已加入 `app/audit.py`。
- [ ] `record_export_audit(...)` 存在并封装 `write_audit`。
- [ ] `POST /api/admin/export/record`（路由 `export_audit_router`，`prefix="/api/admin"`）存在且返回 200 + `{"recorded": true, ...}`。
- [ ] 调用该端点后，`audit_logs` **确实新增一行**：`action='export.executed'`、`object_type='export'`、正确 `tenant_id` + `admin_user_id`、`detail` 含 `format` / `record_count` / `params_hash` / `gate_enforced`（含 `approval_ref` 当带 token 时）。
- [ ] `record_count` 为真实计数：用 `func.count(ArchiveMessage.id)` 按 `tenant_id` + scope 过滤得到，与手工 `SELECT count(*)` 一致。

### 1.2 合规 / 安全（SF-1 数据最小化）
- [ ] `audit_logs.detail` **不含**任何消息体 / 解密内容：断言不存在键 `content` / `payload` / `body` / `decrypted` / `content_text` / `decrypted_payload` / `raw_encrypted_payload`。
- [ ] 计数查询**未** `SELECT content_text` / `decrypted_payload`（仅 `func.count(id)` + 元数据列过滤：roomid/msgtime/msgtype）。
- [ ] `approval_ref`（若记录）是令牌 SHA-256，**非明文** token。
- [ ] 跨租户隔离：用租户 A 的会话调用，审计行 `tenant_id` 必为 A；租户 B 无法在 A 的审计下留下记录（计数与审计均强制 `tenant_id ==`）。

### 1.3 路径 / 范围守门
- [ ] 端点路径为 `/api/admin/export/record`；**无** `/approve` / `/execute`（RND-316 占用，不得撞车）。
- [ ] 改动文件**仅限**：`app/audit.py`（常量+函数）、`app/routers/export_audit.py`（新建）、`app/main.py`（import+注册各一行）、`tests/test_rnd317_export_audit.py`（新建）。**不得**触碰 `backend/scripts/`、前端、`.env.example`、其他 flat service 模块。
- [ ] **无新 Alembic 迁移**（复用 `audit_logs`）。若发现任何 `00xx_*.py` 新迁移 → 判退回（§5）。

### 1.4 路由基线（不写死）
- [ ] `tests/test_http_contract.py:326` 当前基线 `== 52` → 改为 `== 53`（注释追加 `# RND-317: +1 export audit record route.`）。
- [ ] **禁止写死记忆值**：若实际基线与 brief 预期不符（如其它 W1 路由票先合并导致 >52），以**运行时真实 N** 为准改 `N+1`，并在报告中说明。基线测试 `test_router_count` 必须 PASS。

### 1.5 回归
- [ ] `python -m pytest backend/tests/test_architecture_boundary.py -q` 通过（本票不新增 flat 模块 → 无回归）。
- [ ] `python -m pytest backend/tests/ -q` 全量无回归（重点 auth、现有 audit 列表测试）。
- [ ] `app/audit.py` 的 `write_audit` 既有行为未被破坏（其他审计动作 login/config 等仍正常）。

---

## 2. 测试执行（QA 实操）

```bash
cd /Users/zuohaisu/Documents/code/wecom-archive-365/backend
source ../.venv/bin/activate && source ../.env   # 若项目用 venv + .env
python -m pytest backend/tests/test_rnd317_export_audit.py -q
python -m pytest backend/tests/test_architecture_boundary.py backend/tests/test_http_contract.py -q
python -m pytest backend/tests/ -q
```
- 核对 `audit_logs` 行：直接查测试库或让开发提供一条样例 `detail` JSON，确认字段与 SF-1 断言。
- 跨租户：用两个 tenant 的会话分别调用，查 `audit_logs` 的 `tenant_id` 是否正确隔离。

---

## 3. 双情形验收（RND-316 gate 状态未知，两种都验）

### 情形 A：RND-316（审批 gate）**已合并**
- `app.export_approval` 可导入。
- 带**有效** `approval_token` 调 `/record` → 200，`gate_enforced=True`，`approval_ref` 为 token SHA-256。
- 带**无效 / 已消费 / 过期** token → **403**（被 `require_export_approval` 拒绝）。
- 不带 token → `gate_enforced=False` + warning 日志，仍 200 记录（软闸门预期行为，非缺陷；但需在报告中标注"生产须先合并 RND-316 才强制 gate"）。

### 情形 B：RND-316 **未合并**
- `app.export_approval` 不可导入。
- `/record` 以 `gate_enforced=False` 成功记录（打 warning 日志），这是钩子**独立验收路径**，属预期。
- 报告中明确：本票无需 RND-316 即可验收"导出被记录"；RND-316 合并后 `gate_enforced` 自动升级为 `True`。

> QA 在报告里写明**实际情形 A 还是 B**，并据此解释 `gate_enforced` 取值是否合理。

---

## 4. 交付报告格式（QA 输出）

```
## RND-317 验收报告
- 结论：PASS / FAIL / BLOCKED
- 情形：A(RND-316 已合并) / B(RND-316 未合并)
- AC「导出被记录」：满足 / 不满足（附证据）
- 改动文件 diff 集合：<list，须与 §1.3 一致>
- SF-1 断言：detail 无消息体键 = 通过 / 失败（附 detail 样例）
- 租户隔离：通过 / 失败
- 路由基线：52→53 / 实际 N→N+1（注明真实值）
- 新迁移：无 / 有(判退回)
- 回归：全绿 / 有失败（列失败项）
- 备注：RND-316 状态、gate_enforced 实际值、任何超出范围改动
```

---

## 5. 判退回 / BLOCKED 条件

- **BLOCKED**：`app.audit.write_audit` 或 `AuditLog` 模型缺失 → 报 `BLOCKED: A7 未就位`，开发不得自建审计。
- **退回**：出现新 Alembic 迁移；改动超出 §1.3 文件清单；`detail` 含消息体/解密键；端点撞 `/approve`/`/execute`；路由基线写死错误值；跨租户隔离失效；引入新依赖。
- **通过标准**：§1 全绿 + §3 对应情形成立 + §4 报告完整。

---

## 6. 与兄弟票的边界（避免误判）

- 本票**不验** PDF/Excel 生成（C2-1 / RND-315，尚未实现，不在范围）。
- 本票**不验** gate 本身的令牌签发/校验逻辑（RND-316）；仅验"本票端点是否**正确消费** gate（情形 A）或不崩（情形 B）"。
- 若发现 RND-316 的 `export_approval.py` 被本票改动 → 退回（本票只消费、不修改 RND-316 模块）。
