[Goal check] This work advances 开发（Development） by 交付租户开通前的连通性自检端点，复用既有 WeCom access_token 获取原语，避免开通后才发现凭据不可用。

# RND-312 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-312 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-312「B2-2 连通性自检」｜父 Epic RND-270（B2 租户开通）
- 优先级：Medium｜风险等级：**R1**｜milestone：R5 · 云商业化前台

## 背景与项目现状（已实地核实）

B2-1（<issue>RND-311</issue>，创建租户）**已 Done 并已上线**：`app/routers/platform.py:23` 的 `create_tenant`，创建时把 `secret`/私钥经 `app.crypto` 加密存进 `TenantWecomConfig`。

**WeCom 连通性检测原语已存在，不要重新实现调用企微 API 的逻辑**：`backend/app/auth.py:364` 的
```python
def get_wecom_token(corp_id: str, oauth_secret: str, cache_key: Optional[str] = None) -> str:
    """Return a cached or freshly-fetched WeCom access_token for the given corp_id."""
```
内部请求 `https://qyapi.weixin.qq.com/cgi-bin/gettoken`；成功返回 `access_token` 字符串，失败抛 `RuntimeError`（`errcode` 非 0 或请求异常）。**这正是"corp_id/agent/secret 可用性自检"的现成实现**——自检本质就是"能否成功换到 token"。

`TenantWecomConfig` 的解密访问器：`config.decrypted_app_secret`（`app/db/models.py:96`），**只在需要时懒解密，绝不记录日志**（模型 docstring 明确写着"Do NOT log either credential or their decrypted accessors"）。

**❗ 本项目高频踩坑：**
- **`platform.py` 是本波次共享文件**：RND-307/310/313/314 也会往这个文件加端点。开始前先 `git diff`/`git status` 看清哪些兄弟票的端点已落地，在文件末尾追加自己的函数。`test_http_contract.py` 的 `route_count` 用**当前实际值** +1 回填。
- **绝不记录密钥**：`decrypted_app_secret` 的值、`get_wecom_token` 抛出的异常信息里都不得包含明文 secret；`get_wecom_token` 内部已经做到"仅记录是否成功，不记录值"，自检端点的日志/响应也要遵守同样纪律。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个只读自检端点：给定已创建的租户，尝试用其存储的 `corp_id`/`agent_id`/`app_secret` 换取 WeCom access_token，返回"可用/不可用"及失败原因分类，不泄露任何密钥值。

## 范围边界

**In scope：**
1. `backend/app/routers/platform.py` **新增** `POST /tenants/{tenant_id}/connectivity-check`：
   - 鉴权：`require_platform_admin`。
   - 查目标租户的 `TenantWecomConfig`；不存在 → 404。
   - 调用 `get_wecom_token(config.corp_id, config.decrypted_app_secret)`（**不传** `cache_key`，自检不应读写缓存，每次都应是新鲜请求——若 `get_wecom_token` 的缓存行为无法在不改函数签名的前提下绕开，见下方 Escalation）。
   - 成功 → `{"ok": true}`；`get_wecom_token` 抛 `RuntimeError` → 捕获，返回 `{"ok": false, "reason": "<脱敏后的简要原因>"}`（**不把原始异常消息透传到响应**，`get_wecom_token` 内部日志已脱敏，但异常 message 里可能仍带 `errcode` 之外的东西，需要在本票内二次判断只透出安全的分类，如 `"invalid_credentials"` / `"network_error"` / `"unknown"`）。
2. 测试：`backend/tests/test_rnd312_connectivity_check.py`（**mock `get_wecom_token`**，不发真实企微请求）。

**Out of scope（显式非目标）：**
- 不改 `get_wecom_token` 本身（只调用）。
- 不做创建（<issue>RND-311</issue>）/ 列表（<issue>RND-314</issue>）/ 启停（<issue>RND-310</issue>）/ 跨租户聚合（<issue>RND-307</issue>）。
- 不做定时自动自检（本票只做手动触发端点）。

**本工单拥有的文件（只许写这些，`platform.py` 为共享追加）：**
- `backend/app/routers/platform.py` —— **仅新增** `POST /tenants/{tenant_id}/connectivity-check` 及其依赖
- `backend/app/schemas/tenant_provision.py`（或复用兄弟票已建的 schema 文件）—— 仅新增自检相关 schema
- `backend/tests/test_rnd312_connectivity_check.py`（新）
- `backend/tests/test_http_contract.py` —— 契约同步

**只读、绝不可写：** `app/auth.py`（只调用 `get_wecom_token`）、`app/db/models.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 自检可用**：合法凭据的租户 → `{"ok": true}`；测试用 mock 使 `get_wecom_token` 返回一个假 token 验证。
- **AC-2 失败分类**：`get_wecom_token` 抛 `RuntimeError` → `{"ok": false, "reason": ...}`，不 500。测试覆盖至少一种失败场景（mock 抛异常）。
- **AC-3 零泄露（关键）**：响应体不含 `app_secret`/`decrypted_app_secret`/`private_key_encrypted` 或任何原始密钥值；异常兜底不得把底层错误消息原样透传（避免间接泄露企微侧诊断信息）。须有测试断言响应中不含 secret 明文。
- **AC-4 目标不存在 → 404**：不存在的 `tenant_id` → 404。
- **AC-5 鉴权**：无凭据 / 错凭据 → 401。
- **AC-6 复用而非重写**：`grep -n "get_wecom_token" backend/app/routers/platform.py` 应有命中；**不得**在本票里另写一份调用 `https://qyapi.weixin.qq.com/cgi-bin/gettoken` 的逻辑。
- **AC-7 契约同步 + 回归**：`test_http_contract.py` 已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过；`create_tenant` 既有测试全绿。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd312_connectivity_check.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "get_wecom_token" backend/app/routers/platform.py    # AC-6
git diff -- backend/app/routers/platform.py    # 人工核对：只新增，未碰兄弟票端点
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-311（B2-1）**已 Done 并已核实落地**（`backend/app/routers/platform.py:23`）。`get_wecom_token`（`app/auth.py:364`）已就绪。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明失败原因分类的具体取值集合
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：自检失败信息透传过多，间接泄露企微侧诊断细节——由 AC-3 防守，只返回粗粒度分类。
- 风险：`get_wecom_token` 的内部缓存导致"自检"实际读的是旧缓存结果而非新鲜请求——实现时确认这一点，若无法干净绕开缓存，按 Escalation 上报。
- 回滚：纯新增，`git checkout -- backend/app/routers/platform.py` 即可；无迁移、无数据影响（测试全程 mock，不发真实请求）。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿即可，无需额外人工审阅（R1，只读自检，不改数据）。
- **Escalation**：若 `get_wecom_token` 的缓存机制导致自检无法保证读到新鲜结果、且不改函数签名就无法绕开 → `BLOCKED_NEEDS_HUMAN`，说明具体机制，**不要**为了绕开缓存去改 `get_wecom_token` 本身（它被生产解密链路依赖，改动风险高）。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/routers/platform.py`（先 `git diff`/`git status` 看兄弟票是否已落地）、`app/auth.py:364`（`get_wecom_token`）、`app/db/models.py:96`（`decrypted_app_secret`）。
2. 新增自检端点，调用 `get_wecom_token`，分类异常。
3. 写测试（mock `get_wecom_token`，覆盖成功/失败/404/401 四种场景）。
4. 同步 `test_http_contract.py`。
5. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试**必须 mock** `get_wecom_token`，绝不在测试中发真实企微 HTTP 请求。
- 不扩大 Scope：定时自检 / 创建 / 列表 / 启停 / 聚合一律 Out。
- 复用优先：`get_wecom_token` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
