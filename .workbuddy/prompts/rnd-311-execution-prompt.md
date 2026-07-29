# RND-311 开发执行提示词 · B2-1 创建租户 + TenantWecomConfig（加密存储）

> 交付物：本文件（开发提示词）+ `rnd-311-qa-prompt.md`（验收提示词）。
> 你（开发 agent）只改代码、跑测试、写报告，**绝不 git commit / push**（硬规则）。
> 架构冻结 D1：本票纯后端，**无前端、无 React**。代码标识符加反引号。

---

## 0. 任务定位（来自 Linear RND-311 + Epic RND-270）

- **RND-311** = `B2-1 创建租户 + TenantWecomConfig（加密存储）`，隶属 **Epic RND-270（B2 多租户开通配置）**。
- AC：「租户 + 配置创建；secret 加密落盘。」Non-goals：「不洗历史」。
- 设计稿 `design/ui-v1/pages/tenant-provisioning.html` 表单字段：租户名 / 首位管理员邮箱 / `corp_id` / `agent_id` / **会话存档 Secret** / **RSA 私钥（PEM）** / 留存周期。本票消费 `corp_id`+`agent_id`+`secret`+`private_key`；留存周期属 B2 Epic 后续子票（A9 留存策略），OUT OF SCOPE。
- **本票是 B2 Epic 的基石**：B2-2（连通性自检）/ B2-3（激活邮件）/ B2-4（租户列表）全部 `[BLOCKED: B2-1]`，因此落点必须干净、契约必须稳定。
- 状态：`[BLOCKED: F0]` —— **硬阻塞**（不同于 RND-306 的软阻塞）：本票必须调用 F0 提供的**字段级加密原语**（Fernet/Vault），而该原语目前不存在。

---

## 1. 现状核查（已确认，作为事实锚点）

- **`Tenant` / `TenantWecomConfig` 模型已存在**（`backend/app/db/models.py:30-129`）：
  - `TenantWecomConfig` 现有列：`tenant_id` / `corp_id` / `agent_id` / **`app_secret`（Text, NOT NULL，当前明文！）** / `callback_domain` / `is_active`。
  - 模型 docstring（`models.py:56-58`）明写：`app_secret: Phase 1 stores plaintext ... Phase 3 must encrypt at rest using Fernet or Vault/KMS.` —— **本票即该 Phase 3**。
  - **RSA 私钥当前不在此模型**：私钥以**文件路径**存在 `KeyVersion.private_key_path`（`models.py:297`），运行时由 `decrypt_wecom_messages_once.py:178` 读 env `WECOM_PRIVATE_KEY_PATH` 加载。本票要把租户自己的 WeCom 应用 RSA 私钥（PEM）**加密入库**（新增列）。
- **`app_secret` 运行期零消费**：全仓 grep `app_secret` 仅出现在 `models.py`（定义）、`bootstrap_default_tenant.py`（一次性 upsert 写）、测试、`0001`/`0002` 迁移。**没有任何 router / sync / decrypt 代码从 DB 读 `app_secret`**（sync/decrypt 用 env）。→ 把 `app_secret` 改为「存密文」的爆裂半径极低，不会弄坏同步链路。
- **F0 加密原语不存在**：grep `fernet|encrypt|decrypt|cipher` 在 `app/` 下仅命中 `decrypt_isolation.py`（SDK 隔离，非字段加密）与迁移注释。**无 `app/crypto.py` / `config_service.encrypt_value`**。→ 硬阻塞证据。
- **`verify_platform_admin` 不存在**（RND-306 B1-1 仅备了提示词未实现）：grep `verify_platform_admin|require_platform_admin` 零命中。→ 本票需 RND-306 先落地（见 §2 前置校验）。
- **路由基线 = 49**（`backend/tests/test_http_contract.py:325` `assert route_count == 49`）。
- **契约 schema**（手工 sqlite 镜像）两处：`test_http_contract.py:89-95` 与 `fakes.py:252`，均含 `app_secret TEXT`（无 `private_key_encrypted`）。改模型列必须同步这俩。
- **迁移 head**：截至撰写时工作树 head = `0019`（`0019_audit_log.py`）。RND-306 提案迁移 `0020` 但**未合**；本票新迁移号按「实现时读实际 head」软指令（见 §2）。
- **架构边界**：新增 `routers/platform.py` 属 router 层（无需登记 `_FLAT_SERVICE_MODULES`）；`require_platform_admin` 加在 `app/auth.py`（已 allowlist 的扁平模块）。

---

## 2. 实现清单（按序勾选 `[x]`）

- [x] **前置校验（开工第一步，未满足即停下报告，严禁自造加密层 / 平台鉴权）**：
  1. `python -c "import app.crypto"`（或 F0 实际模块名，如 `app.config_service`）成功，且存在 `encrypt_value(plain: str) -> str` / `decrypt_value(cipher: str) -> str`（Fernet，密钥取自 env）。**若导入失败 → STOP，报告「F0 加密原语未就绪」，不自行实现加密**。
  2. `python -c "from app.auth import verify_platform_admin"` 成功（RND-306 已合并）。**若失败 → STOP，报告「RND-306 B1-1 未就绪」**。
  3. `alembic check` 绿（F0 + RND-306 迁移均已 upgrade head）。
  > 这三条是硬门槛；越界实现 F0 加密或 B1-1 实体 = 判失败。

- [x] **`models.py` 给 `TenantWecomConfig` 加列 + 加解密访问器**（紧随 `app_secret` 定义 `models.py:84` 之后）：
  ```python
  # —— RND-311 (B2-1)：新增加密私钥列；app_secret 改为「存密文」——
  private_key_encrypted = Column(Text, nullable=True)  # PEM 私钥密文；可空兼容既有行
  ```
  > 列名固定 `private_key_encrypted`。`app_secret` 列名**保留不变**（避免改契约 schema 列名、避免改 `bootstrap_default_tenant.py` 的 upsert SQL 列名）。

  在同模型文件末尾（`_validate_corp_id_on_update` 之后）加访问器：
  ```python
  # —— RND-311 (B2-1) 加解密访问器（依赖 F0 加密原语）——
  def set_credentials(self, secret: str, private_key_pem: str) -> None:
      """Encrypt + persist secret & RSA private key (Fernet, F0-provided)."""
      from app.crypto import encrypt_value  # F0 交付；若模块名不同请按前置校验结果替换
      self.app_secret = encrypt_value(secret)
      self.private_key_encrypted = encrypt_value(private_key_pem)

  @property
  def decrypted_app_secret(self) -> str:
      from app.crypto import decrypt_value
      return decrypt_value(self.app_secret)

  @property
  def decrypted_private_key(self) -> str:
      from app.crypto import decrypt_value
      return decrypt_value(self.private_key_encrypted)
  ```
  > 运行期消费者（B2-2 自检 / 未来 sync 改用 DB 配置时）一律经 `decrypted_*` 取明文；绝不直读 `app_secret` 当明文。

- [x] **新增迁移 `backend/alembic/versions/00xx_tenant_wecom_config_encryption.py`**：
  - `revision` / `down_revision`：**实现时 `git log --oneline -- backend/alembic/versions/` 读实际 head**；若 head 仍是 `0019` → `down_revision="0019"`、`revision="0020"`；若 RND-306 已占 `0020` → 顺延 `0021`，报告注明。
  - `upgrade()`：`op.add_column("tenant_wecom_configs", sa.Column("private_key_encrypted", sa.Text(), nullable=True))`。
  - `downgrade()`：`op.drop_column("tenant_wecom_configs", "private_key_encrypted")`。
  > 仅加一列；**不改 `app_secret` 列定义**（保持 NOT NULL、列名不变）→ 既有行与契约 schema 不动。

- [x] **`app/auth.py` 新增 `require_platform_admin` 依赖**（紧邻 `require_role` `auth.py:378` 之后）：
  ```python
  # —— RND-311 (B2-1) 平台超管门禁（复用 RND-306 的 verify_platform_admin）——
  def require_platform_admin(
      request: Request, db: Session = Depends(get_db)
  ) -> "PlatformAdmin":
      """Gate a platform-console route to an authenticated PlatformAdmin.

      Interim auth = HTTP Basic (email:password) -> verify_platform_admin.
      B1-2 (RND-305) may later issue a session cookie and resolve it here
      instead; this dependency is the single choke point to swap.
      """
      from app.db.models import PlatformAdmin  # lazy import 防环
      auth = request.headers.get("Authorization", "")
      if not auth.startswith("Basic "):
          raise HTTPException(status_code=401, detail="Platform admin auth required")
      try:
          raw = base64.b64decode(auth[6:]).decode("utf-8")
          email, _, password = raw.partition(":")
      except Exception:
          raise HTTPException(status_code=401, detail="Invalid auth encoding")
      admin = verify_platform_admin(db, email, password)
      if admin is None:
          raise HTTPException(status_code=401, detail="Invalid platform admin credentials")
      return admin
  ```
  > 需 `import base64`（若 `auth.py` 顶部未导入）。返回 `PlatformAdmin`（tenant-less，本票端点不需要 tenant_id 作用域）。

- [x] **新增 schema `backend/app/schemas/tenant_provision.py`**（仿 `schemas/media_library.py` 风格）：
  ```python
  from pydantic import BaseModel, EmailStr

  class TenantProvisionIn(BaseModel):
      name: str
      slug: str
      admin_email: EmailStr
      corp_id: str
      agent_id: str
      secret: str
      private_key_pem: str
      callback_domain: str = ""

  class TenantProvisionOut(BaseModel):
      tenant_id: str
      tenant_name: str
      tenant_slug: str
      config_id: str
      corp_id: str
      agent_id: str
      is_active: bool
  ```
  > 响应**绝不**含 `app_secret` / `private_key_encrypted` / `decrypted_*` 任何明文或密文（零泄露）。

- [x] **新增 `backend/app/routers/platform.py`**（平台控制台根路由）：
  ```python
  from fastapi import APIRouter, Depends, HTTPException, status
  from sqlalchemy.orm import Session
  from app.db.session import get_db
  from app.auth import require_platform_admin
  from app.db.models import Tenant, TenantWecomConfig, DuplicateCorpIdError
  from app.schemas.tenant_provision import TenantProvisionIn, TenantProvisionOut

  router = APIRouter()

  @router.post("/tenants", response_model=TenantProvisionOut,
               status_code=status.HTTP_201_CREATED)
  def create_tenant(
      payload: TenantProvisionIn,
      _admin=Depends(require_platform_admin),
      db: Session = Depends(get_db),
  ) -> TenantProvisionOut:
      # 幂等 slug 守护：同 slug 已存在 -> 409
      if db.query(Tenant).filter(Tenant.slug == payload.slug).first():
          raise HTTPException(409, "Tenant slug already exists")
      tenant = Tenant(id=_uuid(), name=payload.name, slug=payload.slug)
      db.add(tenant)
      cfg = TenantWecomConfig(
          id=_uuid(), tenant_id=tenant.id, corp_id=payload.corp_id,
          agent_id=payload.agent_id, callback_domain=payload.callback_domain,
      )
      cfg.set_credentials(payload.secret, payload.private_key_pem)  # 加密落盘
      db.add(cfg)
      try:
          db.commit()
      except DuplicateCorpIdError:
          db.rollback()
          raise HTTPException(409, "This WeCom CorpID is already assigned to another tenant.")
      db.refresh(cfg)
      return TenantProvisionOut(
          tenant_id=tenant.id, tenant_name=tenant.name, tenant_slug=tenant.slug,
          config_id=cfg.id, corp_id=cfg.corp_id, agent_id=cfg.agent_id,
          is_active=cfg.is_active,
      )
  ```
  > `_uuid()` = `str(uuid.uuid4())`（顶部 `import uuid`）。`DuplicateCorpIdError` 已由 `models.py:98` 的 ORM 事件守卫抛出，commit 时捕获转 409。
  > **不**写审计（A7 仅 epic 级前置，本票不 import `app/audit`）；**不**发激活邮件（B2-3）；**不**跑连通性自检（B2-2）。

- [x] **`main.py` 注册路由**（仿 `users_router` `main.py:19,104`）：
  - `from app.routers.platform import router as platform_router`
  - `app.include_router(platform_router, prefix="/api/platform")`
  > 端点最终路径 = `POST /api/platform/tenants`。

- [x] **契约测试三处同步**（`test_http_contract.py`）：
  1. **`route_count`**：L325 `== 49` → 改为 `== 50`（本票 +1）。**软指令**：实现时若别票先合使基线更高，读 `make verify` 报错给的真实 count 改 `N+1`，勿写死 50。
  2. **path 集合**：L334 `expected = sorted([...])` 内追加 `"/api/platform/tenants"`。
  3. **snapshot 列表**：L410 `expected = [...]` 内追加
     `("/api/platform/tenants", frozenset({"POST"}), "TenantProvisionOut", "None"),`
  4. **契约 schema 两处加列**：`test_http_contract.py:89-95` 的 `tenant_wecom_configs` CREATE 语句加 `private_key_encrypted TEXT,`（放在 `callback_domain TEXT,` 之后）；`fakes.py:252` 同位置加 `private_key_encrypted TEXT NOT NULL,`（fakes 用 NOT NULL 也可，因测试构造时本票会 set；若现有 fake 构造未设该列，用 `TEXT` 兼容即可，保持与模型 `nullable=True` 一致最稳）。
  > 第 4 点与 ORM 模型新增 `private_key_encrypted` 必须同步，否则契约 sqlite 缺列 → ORM 查询报错。

- [x] **新增测试 `backend/tests/test_rnd311_tenant_provision.py`**：
  - DB 门控（无 `DATABASE_URL` 则 `pytest.skip`，仿 `test_rnd278_password_reset.py`）。
  - **加密落盘硬证据**：经 `create_tenant`（或直构造 `TenantWecomConfig` + `set_credentials`）落库后，`SELECT app_secret, private_key_encrypted` 断言：
    - `app_secret != 入参 secret`（已加密）；
    - `private_key_encrypted != 入参 pem`（已加密）；
    - `cfg.decrypted_app_secret == 入参 secret` 且 `cfg.decrypted_private_key == 入参 pem`（可还原）。
  - **端点契约**：用 `TestClient` + Basic auth 头 `POST /api/platform/tenants` → 201，响应含 `tenant_id`/`corp_id`/`agent_id`，**不含** `secret`/`private_key`/`app_secret` 任何键。
  - **门禁**：无 `Authorization` / 错凭据 → 401。
  - **唯一性**：重复 `slug` → 409；重复 `corp_id`（active）→ 409（`DuplicateCorpIdError`）。
  - **零泄露**：响应 JSON 全键路径扫描，断言无 `app_secret`/`private_key`/`secret`/`decrypted`。

- [x] **`make verify` 全绿** + **`alembic upgrade head` + `alembic check` 全绿**（模型 `private_key_encrypted` 与迁移列一致）。

- [x] **范围守门自检**：`git diff --stat` 仅含 `models.py` + `00xx_*.py`(迁移) + `auth.py`(仅新增函数) + `schemas/tenant_provision.py` + `routers/platform.py` + `main.py`(仅 import+include_router) + 契约测试三处 + 新测试。不得改 `sync`/`decrypt` 运行路径、不得改 `app_secret` 列名、不得改既有 `Tenant`/`TenantWecomConfig` 其他列。

- [x] **不 commit**：停在这里，交 QA agent 验收；由用户本人决定提交。

---

## 3. 路由判定（防契约雷）

- 本票新增 1 路由（`POST /api/platform/tenants`）→ `route_count` 必须 +1；path 集合 + snapshot 必须同步（§2）。
- 契约 schema（`test_http_contract.py:89` + `fakes.py:252`）必须加 `private_key_encrypted`，否则契约 sqlite 缺列导致 ORM 查询失败。
- `app_secret` 列名保持不变 → 现有所有 `app_secret=` 测试构造无需改；仅本票新测试验证加密。

---

## 4. 范围边界（明确 OUT OF SCOPE）

| 项 | 归属 | 本票是否做 |
|---|---|---|
| `TenantWecomConfig.private_key_encrypted` 列 + 迁移 | RND-311 | ✅ |
| `app_secret` / `private_key` 加密落盘 + `decrypted_*` 访问器 | RND-311 | ✅ |
| `POST /api/platform/tenants` + `require_platform_admin` 门禁 | RND-311 | ✅ |
| F0 加密原语（`app/crypto.encrypt_value` 等） | RND-244 (F0) | ❌（硬前置，缺失即停） |
| `PlatformAdmin` 实体 + `verify_platform_admin` | RND-306 (B1-1) | ❌（硬前置，缺失即停） |
| 平台超管 HTTP 登录端点 / session cookie 签发 | B1-2 (RND-305) | ❌（`require_platform_admin` 暂用 Basic auth，留单点替换） |
| 连通性自检（调 WeCom API） | B2-2 (RND-312) | ❌ |
| 激活邮件（建 AdminUser 邀请） | B2-3 (RND-313) | ❌ |
| 已开通租户列表 | B2-4 (RND-314) | ❌ |
| 留存周期 / 冷存储策略落库 | A9 (RND-301) | ❌ |
| 洗历史 / 迁移既有明文 secret | — | ❌（Non-goal） |
| 任何前端 / React | D1 冻结 | ❌ |

> **可选加固（非硬要求，建议做）**：`scripts/bootstrap_default_tenant.py:135` 用 raw SQL 把 `WECOM_OAUTH_SECRET` 明文写入 `app_secret`。若 F0 密钥在 bootstrap 时可用，建议改为 `encrypt_value` 后写入，使默认租户也满足「secret 加密落盘」。若 bootstrap 时 F0 密钥未就绪，标注为已知缺口交付，不阻断本票 PASS。

---

## 5. 交付报告格式（交给 QA + 用户）

完成后输出，至少包含：
- 改动文件清单（`git diff --stat` 节选）。
- 前置校验结果：F0 加密模块导入成功截图 / `verify_platform_admin` 导入成功 / `alembic check` 绿。
- 新增列 `private_key_encrypted` 一览；迁移 revision + 实现时实际 head。
- 加密落盘证据：插入后 `app_secret`/`private_key_encrypted` ≠ 明文，且 `decrypted_*` 可还原（贴测试输出）。
- `make verify` + `alembic upgrade head` + `alembic check` 结果（贴关键行）。
- 契约同步核验：`route_count` 现值、path/snapshot 已加、`test_http_contract.py:89` + `fakes.py:252` 已加 `private_key_encrypted`。
- 范围守门：`sync`/`decrypt` 运行路径未改、`app_secret` 列名未变。
- 未 commit 声明 + 遗留 / 需用户决策项（如默认租户 bootstrap 加密缺口）。
