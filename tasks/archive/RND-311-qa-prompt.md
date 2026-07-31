# RND-311 QA / 验收 agent 提示词 · B2-1 创建租户 + TenantWecomConfig（加密存储）

---

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始下面的验收步骤，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发文档规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：确认工单号，然后直接进入验收核对步骤。

---

> 面向独立测试/QA agent。只读言、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `RND-311-dev-prompt.md` 产出的改动。

---

## 一、验收目标

确认：
1. `POST /api/platform/tenants` 能创建 `Tenant` + `TenantWecomConfig`；
2. **`app_secret` 与 RSA 私钥均加密落盘**（复用 **RND-333** 的 `app.crypto` 原语；2026-07-29 更正，原误写 F0），明文绝不入库；
3. 端点被平台超管门禁保护；错误/缺凭据 401；
4. 零泄露（响应不含任何 secret / 私钥）；唯一性 409；
5. 全局契约不变（`alembic check` 绿、route_count +1、schema 同步、scope 守门）。

---

## 二、逐条验收清单（PASS/FAIL，附证据）

### 前置依赖（硬门槛）
- [ ] P0 **RND-333 加密原语已合并**（2026-07-29 更正，原误写「F0」）：`python -c "from app.crypto import encrypt_value, decrypt_value"` 成功。若失败 → 整体 FAIL，附「RND-333 未就绪，开发 agent 应已停下报告」说明。**若发现开发 agent 自建了替代加密模块而非复用 `app.crypto` → 直接 FAIL（`SCOPE_VIOLATION`, blocker）**，即使功能上跑得通——那是重复实现，会与 RND-333 的产出打架。
- [ ] P1 **RND-306 `verify_platform_admin` 已合并**：`from app.auth import verify_platform_admin` 成功。若失败 → 整体 FAIL。
- [ ] P2 **`alembic check` 绿**：RND-333 + RND-306 迁移已 `upgrade head`，无 schema drift。

### 加密落盘（核心 AC）
- [ ] B1 **`app_secret` 加密入库**：构造一条 `TenantWecomConfig` 经 `set_credentials(secret, pem)` 落库后，`SELECT app_secret` 的值 ≠ 入参 `secret`（密文）。证据：直查 DB 或读测试输出。
- [ ] B2 **RSA 私钥加密入库**：`SELECT private_key_encrypted` ≠ 入参 PEM。证据同上。
- [ ] B3 **可还原**：`cfg.decrypted_app_secret == secret` 且 `cfg.decrypted_private_key == pem`（经 RND-333 的 `app.crypto` 对称加解密）。证据：单测 `test_rnd311_tenant_provision.py`。
- [ ] B4 **模型列存在**：`TenantWecomConfig` 有 `private_key_encrypted`（Text, nullable）。证据：grep `models.py` + `alembic check` 绿。

### 端点行为
- [ ] E1 **创建成功**：`POST /api/platform/tenants`（Basic auth）返回 201，body 含 `tenant_id`/`tenant_slug`/`corp_id`/`agent_id`/`is_active`。证据：TestClient。
- [ ] E2 **重复 slug → 409**：同 slug 二次提交 → 409。证据：curl/TestClient。
- [ ] E3 **重复 active corp_id → 409**：`DuplicateCorpIdError` 被捕获转 409。证据：测试。
- [ ] E4 **门禁 401**：无 `Authorization` / 错误凭据 → 401。证据：curl。
- [ ] E5 **路径正确**：路由注册为 `POST /api/platform/tenants`（前缀 `/api/platform`）。证据：`app.routes` 或 contract snapshot。

### 零泄露（SF 级）
- [ ] L1 **响应无 secret**：`E1` 响应 JSON 全键路径不含 `app_secret` / `secret` / `private_key` / `private_key_encrypted` / `decrypted_*`。证据：`jq 'paths(scalars)'` 或断言黑名单键。
- [ ] L2 **异常不泄密**：401/409 响应体不含明文 secret 或私钥片段。证据：curl 响应体检查。

### 契约与范围守门
- [ ] C1 **route_count**：`test_http_contract.py:325` 现值 = 实现时真实路由数（本票 +1）。证据：跑 `make verify` 该断言。
- [ ] C2 **path 集合**：`/api/platform/tenants` 已加入 `test_http_contract.py:334` expected。
- [ ] C3 **snapshot**：`("/api/platform/tenants", frozenset({"POST"}), "TenantProvisionOut", "None")` 已加入 `test_http_contract.py:410` expected。
- [ ] C4 **契约 schema 同步**：`test_http_contract.py:89` 与 `fakes.py:252` 的 `tenant_wecom_configs` CREATE 均含 `private_key_encrypted`。证据：grep 两处。
- [ ] C5 **`alembic check` 绿**：模型 `private_key_encrypted` 与迁移列一致。
- [ ] C6 **scope 守门**：`git diff --stat` 不含 `routers/sync.py` / `services/decrypt_worker.py` / `scripts/decrypt_wecom_messages_once.py` / `app_secret` 列名改名；仅含 models+migration+auth.py(新增函数)+schema+router+main.py(import)+契约三处+新测试。
- [ ] C7 **`make verify` 全绿**：lint + typecheck + build + test 全过。

### 架构边界
- [ ] A1 `require_platform_admin` 加在 `app/auth.py`（扁平模块，不新建反向依赖）；`routers/platform.py` 为 router 层，未误加 `_FLAT_SERVICE_MODULES`。
- [ ] A2 未改动 `Tenant` / `TenantWecomConfig` 既有列（`corp_id`/`agent_id`/`callback_domain`/`is_active` 不变）。

---

## 三、回归套件（必须全绿）

```
make verify
# 重点套件（确认无回归）：
pytest backend/tests/test_http_contract.py backend/tests/test_rnd311_tenant_provision.py \
       backend/tests/test_tenant_wecom_config_uniqueness.py \
       backend/tests/test_architecture_boundary.py -q
```
> `test_tenant_wecom_config_uniqueness.py` 用 `app_secret="secret"` 构造——确认本票未破坏该列名（仍 NOT NULL、列存在），测试仍绿。
> `test_architecture_boundary.py` 确认无新增反向依赖（auth.py 新增函数不 import routers/main）。

---

## 四、智能路由判定（每轮必给）

- **源码有 Bug**（如 `set_credentials` 未加密、`require_platform_admin` 误放行、迁移漏列）→ 反馈开发 agent 修复，附错误 + 失败测试 + 期望；不自行改实现。
- **测试代码有 Bug**（如契约 snapshot 旧路径、fakes 缺列）→ 可自行修正测试（仅当断言旧路径，须标注）；但 `private_key_encrypted` 缺失导致的契约失败属「实现未同步」，应退回开发 agent。
- **全部通过** → 报告 SUCCESS，附加密落盘证据（B1-B3）+ 契约同步核验（C1-C5）。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

---

## 五、交付报告格式

```
RND-311 验收结论：PASS / FAIL
前置：RND-333 加密原语 ___（就绪/缺失）  RND-306 verify_platform_admin ___（就绪/缺失）  alembic check ___（绿/红）
加密落盘：app_secret 密文≠明文 ___  私钥密文≠明文 ___  可还原 ___
端点：201 创建 ___  401 门禁 ___  409 唯一性 ___
零泄露：响应无 secret/私钥 ___
契约：route_count=__  path/snapshot 已同步 ___  schema 两处已加 private_key_encrypted ___  alembic check 绿 ___
范围：sync/decrypt 未改 ___  app_secret 列名未变 ___
回归：make verify ___（绿/红）
遗留：___
```
