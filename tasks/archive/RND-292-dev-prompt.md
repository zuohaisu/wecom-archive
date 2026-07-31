[Goal check] This work advances 开发（Development） by 交付媒体下载端点与下载审计钩子，使每次媒体下载都留下不可篡改的审计记录。

# RND-292 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-292「[BLOCKED: A6-1] A6-2 下载端点 + 审计钩子」｜Linear team `Builder`
- 优先级：Medium｜风险等级：**R1**｜父 epic：RND-267（A6 媒体资源库）
- 波次：R1 · fast-follow —— **不阻塞 RND-329（media 页面），可在其后或并行进行**

## 背景与项目现状
A6-1（RND-291，`GET /api/admin/media` 列表）已 Done。本票补齐 A6 的最后一块：**下载 + 审计**。

**既有可复用资产（先读，不要重造）：**
- `backend/app/routers/media.py` —— **已有多个媒体访问端点**（如 `GET /api/conversations/{cid}/messages/{msgid}/media`，见 `media.py:91`、`:201`、`:342`、`:426`）。**复用这些，不要新建一套平行的取流逻辑。**
- `backend/app/media_storage.py` / `qiniu_storage.py` —— 存储抽象（`local` / `qiniu_kodo` 双 provider）。
- **审计基础设施已就绪**（RND-293/294，A7 epic 已 Done）：`AuditLog` 模型见 `backend/app/db/models.py:236`，写入钩子已在多处使用 —— **照既有钩子的用法写，不要发明新的审计写法**。

**❗ 本项目高频踩坑：**
- **架构边界硬闸**：router 不得 import `app.main`；service 层不得 import `app.routers.*`。
- **审计是只读追加、不可篡改**（RND-274 设计）—— 只写不改不删。
- **双 provider**：下载必须走存储抽象层，**不得**在响应里泄露七牛签名密钥或让客户端直连绕过访问控制。
- 若改了 `models.py` 就必须有对应 Alembic migration（CI 跑 `alembic check`）。本票**预期不需要改模型**（`AuditLog` 已存在）。

## 目标（Goal）
让管理员可从媒体库下载归档媒体，且**每一次下载都产生一条不可篡改的审计记录**（谁、何时、下载了哪个文件）。

## 范围边界

**In scope：**
1. 下载端点（复用 `backend/app/routers/media.py` 的既有取流能力，新增或扩展一个明确的「下载」语义端点，带 `Content-Disposition: attachment`）。
2. 下载审计钩子：每次下载成功写一条 `AuditLog`，至少含操作人、时间、动作类型（下载）、目标媒体标识、租户。
3. 测试 `backend/tests/test_media_download_audit.py`。

**Out of scope（显式非目标）：**
- **不实现媒体库页面 UI**（那是 RND-329，独立工单）。
- 不改媒体存储层、不做转码、不改缩略图流水线。
- 不改 `AuditLog` 模型或审计基础设施（已就绪，只调用）。
- 不实现批量 / 打包下载（本期不做）。
- 不碰 4 张页面票拥有的任何文件。

**本工单拥有的文件（只许写这些）：**
- `backend/app/routers/media.py`
- `backend/app/services/<媒体下载服务>.py`（若需要，新建）
- `backend/tests/test_media_download_audit.py`（新）

**只读、绝不可写：** `sidenav.py`、`main.py`、`admin_*_page.py`、`templates/*.html`、`i18n.js`、`db/models.py`、`media_storage.py`、`qiniu_storage.py`、`routers/audit.py`。

> 若发现必须改他人文件（尤其 `models.py`）→ **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）
- **AC-1 下载可用**：下载端点返回媒体字节流，带正确的 `Content-Type` 与 `Content-Disposition: attachment`；本地与七牛两种 provider 均可下载。
- **AC-2 每次下载写审计**：Given 一次成功下载，When 请求完成，Then `audit_logs` 中新增恰好一条记录，含操作人、时间、动作类型、目标媒体标识、租户。
- **AC-3 失败不误记**：Given 下载失败（媒体不存在 / 无权限），When 请求完成，Then **不写**「下载成功」审计记录（可写失败记录，但不得把失败记成成功）。
- **AC-4 鉴权与租户隔离**：未认证请求被拒；A 租户无法下载 B 租户的媒体（有用例证明跨租户被拒）。
- **AC-5 无凭证泄露**：响应体、响应头、日志中均不出现七牛 AK/SK 或任何长期凭证；客户端不被引导直连对象存储绕过访问控制。
- **AC-6 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；既有媒体访问端点行为不变（既有媒体测试全绿）。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_download_audit.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m pytest backend/tests/ -k "media" -q                 # 既有媒体测试无回归
git diff --stat -- backend/app/db/models.py backend/app/media_storage.py backend/app/qiniu_storage.py   # 预期无输出
```

## 依赖（Dependencies）
- A6-1（RND-291）**已 Done**，列表端点可用。
- A7 审计基础设施（RND-293/294）**已 Done**，`AuditLog` 可直接写。
- **不阻塞、也不被 RND-329 阻塞**（页面与端点可各自推进）。

## 完成定义
- [ ] AC-1 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] 未新增 Alembic migration（若确实需要 → 说明原因并上报）
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1**：审计漏记 → 合规产品的核心承诺失效，且事后无法补记。**由 AC-2 显式防守。**
- **风险 2**：失败被记成成功 → 审计记录不可信，比没有审计更糟。**由 AC-3 防守。**
- **风险 3**：跨租户越权下载 → 严重数据泄露。**由 AC-4 防守。**
- **风险 4**：为了「方便」返回七牛直链 → 绕过访问控制与审计。**由 AC-5 防守。**
- 回滚：`git checkout -- <files>`；无数据迁移，无生产影响。

## 人工点位
- **Trigger**：Haisu / PM 置 In Progress。
- **Gate**：Haisu 审阅后批准 commit。此改动触及访问控制与审计，**必须人确认**。
- **Escalation**：若既有媒体端点的鉴权模型不足以支撑租户隔离 → `BLOCKED_NEEDS_HUMAN`，**不要自行放宽鉴权**。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`routers/media.py`（全部既有端点，理解取流与鉴权方式）、`db/models.py:236` 的 `AuditLog`、既有审计钩子调用点（`grep -rn "AuditLog" backend/app/`）。
2. 复用既有取流能力实现下载语义端点（`Content-Disposition: attachment`）。
3. 成功路径写 `AuditLog`；失败路径**不写成功记录**。
4. 写测试覆盖 AC-1~AC-5，尤其跨租户拒绝与失败不误记。
5. 跑 `make verify` + 既有媒体测试，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假数据，**不得**引入真实归档媒体。
- 凭证只从环境变量读，绝不写入代码、测试或响应。
- 不扩大 Scope：页面 UI、批量下载一律 Out。
- 证据优先，以 exit 0 / 测试通过为证。
