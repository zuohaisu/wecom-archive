# RND-287 开发执行提示词 — A4-1 ExternalContact 实体 + WeCom 外部联系人同步

> 本文件是交给**专门开发 agent** 的执行 brief。请勿自行 commit/push（见末尾纪律）。
> 所有代码标识符（函数/表/列/模块名）均加反引号，方便 grep 落点。
> 架构冻结 D1：SSR + 原生 JS，本票为纯后端实体 + 同步 worker，**不引入任何前端/React 改动**。

---

## 1. 任务概述（来自 Linear RND-287）

- **标题**：`[BLOCKED: F0] A4-1 ExternalContact 实体 + WeCom 同步`
- **父 epic**：RND-264（A4 外部联系人（含详情））。**RND-287 是该 epic 的基础票**——RND-296（A4-2 列表/筛选 API）与 RND-298（A4-3 详情时间线）均 `[BLOCKED: A4-1]`，必须等本票落地。
- **目标**：新建 `ExternalContact` 实体（姓名/企业/标签/来源/归属员工/最后互动/会话数），并实现从**企业微信外部联系人 API** 同步的 worker/service，写入相关表。
- **验收标准（AC）**：① 实体建好；② 同步填充。
- **Estimate**：1.5w；**Labels**：backend, external-contact。

⚠️ **范围纪律（最关键）**：本票只做 **实体（model + migration）+ 同步 worker/service**，把 WeCom 数据拉进来。**不要**做列表/筛选 API（那是 RND-296）、**不要**做详情时间线（RND-298）、**不要**新增任何 router（list API 由 RND-296 建）。同步通过 `python -m app.services.external_contact_sync` 可手动触发并被测试覆盖即可满足「同步填充」。

---

## 2. 基线核实（已实现，开发 agent 可直接复用，勿重复造轮子）

| 现状 | 落点 | 复用方式 |
|---|---|---|
| WeCom 外部联系人 REST 形状已部分解析 | `backend/app/wecom_contacts.py:58` `fetch_external_contact_display_name` 已解析 `externalcontact/get` 的 `follow_user[].remark` 与 `external_contact.name` | **扩展本文件**，不要新建 flat 模块 |
| access_token 缓存（支持第二密钥） | `backend/app/auth.py:265` `get_wecom_token(corp_id, oauth_secret, cache_key=None)` | 外部联系人密钥用 `cache_key=f"{corp_id}:external_contact"`（既有约定，见 `sync_contact_display_names_once.py:177`） |
| 外部联系人密钥 env | `WECOM_EXTERNAL_CONTACT_SECRET`（`scripts/sync_contact_display_names_once.py:23,157`） | 复用同名 env；缺失即跳过同步（与既有 display-name 同步一致） |
| 租户解析 | `backend/app/db/models.py:53` `TenantWecomConfig`；`sync_contact_display_names_once.py:74` `_require_tenant_id` 由 `corp_id` 反查 `tenant_id` | 同步 CLI 复用该反查逻辑 |
| 内部员工归属 | 无独立 Employee 表；`AdminUser.wecom_user_id`（`models.py:132`）；`staff` 由 `conversation_membership.py:_is_staff` 推导 | owner 存原始 `owner_wecom_userid`（String），可选 best-effort FK 到 `AdminUser` |
| 现有 `Contact`（内部员工注册表） | `backend/app/db/models.py:761` `class Contact`（`contacts` 表） | 不改动、不复用；新实体是**独立新表** `external_contacts` |
| 同步 worker 范式 | `backend/app/services/`（decrypt_worker/sync_worker/media_worker）+ `backend/scripts/` CLI + systemd timer | **service 模块 + `__main__` CLI**，但见 §5 B 层纪律 |
| 架构边界 allowlist | `backend/tests/test_architecture_boundary.py:70` `_FLAT_SERVICE_MODULES` 含 `app.wecom_contacts`（line 91）；`app.services` 自动归 service 层（line 57） | 扩展 `wecom_contacts.py` → 无需改 allowlist；新建 `app/services/external_contact_sync.py` → 自动 service 层，无需改 allowlist |
| Alembic head | 当前 `0019_audit_log.py`（`revision="0019"`, `down_revision="0018"`） | 新迁移 = `0020_external_contacts.py`, `down_revision="0019"`（**运行时 `alembic heads` 再确认**） |

**设计稿字段对齐**（`design/ui-v1/pages/contacts.html`，表头 line 34）：联系人/所属企业/标签/来源/归属员工/会话数/最后互动 → 对应 `name`/`company`/`tags`/`source`/`owner_wecom_userid`/`message_count`/`last_interaction_at`。cell-id 显示 `wmABC123ext` 即 `external_userid`。

---

## 3. 前置依赖检查（F0）

RND-287 标 `[BLOCKED: F0]`。A4 epic 的 F0 依赖注明为「租户作用域」。

**开工前必须断言以下 F0 基元已存在于工作树**（它们目前都在树上，仅作闸口）：
1. `backend/app/db/models.py` 的 `TenantWecomConfig`（line 53）与 `AdminUser`（line 132，含 `wecom_user_id`）。
2. `backend/app/auth.py` 的 `get_current_user`（line 319）与 `get_wecom_token`（line 265）。
3. 既有 `contacts` 表的租户隔离范式（`UniqueConstraint("tenant_id","wecom_userid")`，`models.py:766`）。

**若上述任一缺失** → 停止并报告 `BLOCKED: F0 未合并`，**不要**自造租户解析逻辑。
若全部存在 → 继续。注意：**生产侧的定时调度（systemd timer 触发本同步）属于 B 层/RND-237 导出范畴，本票不实现**——同步只需「可被 `python -m` 手动调用并填充」即可满足 AC。

---

## 4. 精确落点（改动文件清单）

| 文件 | 动作 | 说明 |
|---|---|---|
| `backend/app/db/models.py` | 新增类 | `class ExternalContact(Base)`（紧接 `Contact` 之后，约 line 800 后） |
| `backend/alembic/versions/0020_external_contacts.py` | 新建 | 手写迁移，`down_revision` 动态取当前 head（现 0019） |
| `backend/app/wecom_contacts.py` | 扩展（追加函数） | 新增 `list_follow_userids` / `list_external_userids_by_user` / `get_external_contact` / `get_corp_tag_list` |
| `backend/app/db/external_contacts.py`（或并入 `app/db/contacts.py` 旁） | 新建 | `upsert_external_contact(session, tenant_id, payload)` 助手 |
| `backend/app/services/external_contact_sync.py` | 新建 | 纯逻辑同步 service + `__main__` CLI 入口 |
| `backend/tests/test_rnd287_external_contacts.py` | 新建 | 模型/隔离/客户端 mock/同步逻辑测试 |

**禁止触碰**：`backend/scripts/`（B 层）、`backend/app/routers/`（无 router）、`backend/app/web/`（无前端）、`.env.example`（B 层）、任何既有迁移文件。

---

## 5. 实现步骤

### 5.1 `ExternalContact` 模型（`backend/app/db/models.py`，约 line 800 后）

```python
class ExternalContact(Base):
    """WeCom 外部联系人（客户/合作方），由外部联系人 API 同步填充。"""

    __tablename__ = "external_contacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "external_userid",
            name="uq_external_contacts_tenant_ext_userid",
        ),
        # 按设计稿：姓名/企业可 ILIKE 搜索 → GIN trgm（同 Contact，见 migration 0013）
        Index("ix_external_contacts_name_trgm", "name",
              postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        Index("ix_external_contacts_company_trgm", "company",
              postgresql_using="gin", postgresql_ops={"company": "gin_trgm_ops"}),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_userid = Column(String(64), nullable=False)
    name = Column(Text, nullable=True)            # remark 优先，回退 external_contact.name
    company = Column(Text, nullable=True)         # corp_name / corp_full_name
    tags = Column(Text, nullable=True)            # JSON 字符串数组，如 '["重点客户","A类"]'
    source = Column(Text, nullable=True)          # follow_user[].state（添加来源）
    owner_wecom_userid = Column(String(64), nullable=True)  # 主归属员工（首个 follow_user.userid）
    last_interaction_at = Column(DateTime(timezone=True), nullable=True)  # 派生，见 5.4
    message_count = Column(Integer, nullable=True)                          # 派生，见 5.4
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        server_default=func.now(), onupdate=func.now())
```

> **决策说明**：`tags` 用 `Text`（存 JSON 字符串）而非 `postgresql.JSONB()`——与 `audit_logs` 的 JSONB 不一致，但可让本实体在 sqlite in-memory 单测中正常建表（项目单测普遍用 sqlite，见 `tests/test_contact_sync.py:38`），避免 JSONB-on-sqlite 崩溃。服务层负责序列化/反序列化。若你判断生产必须 JSONB，请改用 `postgresql.JSONB()` 并仅在 Postgres 测试下验证，但**务必同步改迁移与单测引擎**。

### 5.2 迁移 `0020_external_contacts.py`

- `revision = "0020"`；**`down_revision` 以运行时 `alembic heads` 为准（当前 `0019`）**。
- 复用 `0019_audit_log.py` 风格（sa.Column / ForeignKeyConstraint / PrimaryKeyConstraint / create_index）。
- **必须先** `op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")`（幂等），再建 GIN trgm 索引（migration 0013 惯例）。
- 列定义（名/类型/可空/`server_default`）必须与 §5.1 **逐字一致**，否则 CI 的 `alembic check` 会失败。
- 手写，不依赖 autogenerate。

### 5.3 扩展 `wecom_contacts.py`（追加，不新建模块）

所有函数沿用 `fetch_external_contact_display_name` 的风格：失败返回 `None`/空、绝不记录 token/secret/PII。

```python
def list_follow_userids(token: str) -> Optional[list[str]]:
    """GET externalcontact/get_follow_user_list → 有外部联系人的内部 userid 列表。"""

def list_external_userids_by_user(token: str, userid: str) -> Optional[list[str]]:
    """GET externalcontact/list?userid=<userid> → 该内部员工跟进的外部 userid 列表。"""

def get_external_contact(token: str, external_userid: str) -> Optional[dict]:
    """GET externalcontact/get?external_userid=... → 完整 payload
    （external_contact.{name,corp_name,corp_full_name,type,gender}；
     follow_user[].{userid,remark,state,createtime,tags}）。"""

def get_corp_tag_list(token: str) -> Optional[dict]:
    """GET externalcontact/get_corp_tag_list → {tag_id: tag_name} 映射，用于把 tags id 解析为名称。"""
```

> `tags` 在 WeCom 侧是 id 列表；同步时用 `get_corp_tag_list` 解析为名称再存。
> 注意 WeCom 单企业 tag 上限与分页：若数据量大，`get_corp_tag_list` 可能需处理 `next_cursor`，但首版可假定单页足够（按需加分页，勿过度工程）。

### 5.4 `upsert_external_contact` 助手（`backend/app/db/external_contacts.py`）

- 参照 `backend/app/db/contacts.py:19` `upsert_contact_display_name` 的「tenant-scoped upsert + 不覆盖非空名为空」范式。
- 入参 `(session, tenant_id, external_userid, name, company, tags_json, source, owner_wecom_userid, last_interaction_at, message_count)`。
- 基于 `UniqueConstraint(tenant_id, external_userid)` upsert；`updated_at` 自动推进。
- **派生统计（best-effort，可空）**：
  - `message_count` = 该 `external_userid` 在租户内的消息数：`session.query(func.count(ArchiveMessage.id))` 过滤 `tenant_id` 且 `sender==ext_id` 或 存在 `ArchiveMessageRecipient.receiver_userid==ext_id`（复用 `test_contact_sync.py:288` 的 `ArchiveMessage`/`ArchiveMessageRecipient` 列名与 `conversation_membership.py:_collect_staff_ids` 的参与者枚举思路）。
  - `last_interaction_at` = 上述消息集合的 `max(created_at)`。
  - 若档案消息尚不存在（同步早于消息归档），两列留 `None`，实体与同步仍满足 AC；RND-296 列表 API 将 `None` 渲染为 0 / 「—」。

### 5.5 同步 service + CLI（`backend/app/services/external_contact_sync.py`）

- **纯逻辑模块**，签名 `(session: Session, tenant_id: str, corp_id: str, external_secret: str, ...)`，参考 `backend/app/services/sync_worker.py:13` 的 tenant-required 约定：**绝不扫描「所有租户」**，tenant_id 必传。
- 算法：
  1. `token = get_wecom_token(corp_id, external_secret, cache_key=f"{corp_id}:external_contact")`。
  2. `owners = list_follow_userids(token)`；对每 `userid`：`ext_ids = list_external_userids_by_user(token, userid)`。
  3. 对每 `ext_id`：`detail = get_external_contact(token, ext_id)`；用 `get_corp_tag_list` 解析 tags；计算派生统计（§5.4）；`upsert_external_contact(...)`。
  4. 返回 `@dataclass` `RunSummary`（总数/新增/更新/跳过/失败），参照 `sync_worker.py:43` 风格。
- **不**在此模块里读 env、建 engine、`sys.exit`——这些留给 CLI。
- **CLI 入口**（`if __name__ == "__main__":`）：
  - 读 `WECOM_CORP_ID`（必填，缺失则报错退出）、`WECOM_EXTERNAL_CONTACT_SECRET`（缺失则**跳过同步并 log 提示外部联系人 API 未启用**，与 `sync_contact_display_names_once.py:174` 一致）。
  - `engine = create_engine(DATABASE_URL)`；`with Session(engine) as s: tenant_id = _require_tenant_id(s, corp_id)`（复用 `sync_contact_display_names_once.py:74` 反查逻辑，可小复制或抽到共享处——优先小复制以保 B 层纪律）；调用 service；`s.commit()`。
  - 触发方式：`python -m app.services.external_contact_sync`（**不要用 `backend/scripts/` 新文件**——见 §6）。

### 5.6 测试 `backend/tests/test_rnd287_external_contacts.py`

- **模型/隔离**（sqlite in-memory，参照 `test_contact_sync.py:36`）：`ExternalContact.__table__.create(engine)`；验证 create/update/租户隔离/唯一约束。
- **客户端 mock**（参照 `test_contact_sync.py:132` `_FakeClient`/`_patch_httpx_client`）：`list_follow_userids`/`get_external_contact`/`get_corp_tag_list` 用假响应断言解析正确；请求失败返回 `None` 不抛。
- **同步逻辑**：mock `wecom_contacts` 的各函数，`_FakeClient` 提供 follow→ext→detail 链；断言 `upsert_external_contact` 被按 tenant 正确写入、tag id→name 解析、owner 取首个 `follow_user.userid`、重复运行幂等（updated_at 推进但无重复行）。
- **派生统计**：构造 sqlite 上的 `ArchiveMessage`/`ArchiveMessageRecipient` 假数据，断言 `message_count`/`last_interaction_at` 计算正确（或消息缺失时留 `None`）。
- 运行：`cd backend && pytest tests/test_rnd287_external_contacts.py -v`。

---

## 6. 架构边界 / B 层纪律（硬约束）

1. **禁止改 `backend/scripts/`**：生产 worker 调度属 B 层/RND-237。本票同步入口必须是 `app/services/external_contact_sync.py` 的 `__main__`，经 `python -m app.services.external_contact_sync` 触发。**不要**新建 `backend/scripts/sync_external_contacts_once.py`，也不要改 `run_archive_worker_once.py` / systemd unit。
2. **不新增 router**：列表/筛选 API 是 RND-296；详情时间线是 RND-298。本票零前端、零路由。
3. **架构边界测试零改动**：扩展 `wecom_contacts.py`（已在 `_FLAT_SERVICE_MODULES` line 91）与新建 `app/services/external_contact_sync.py`（自动 service 层）均无需改 `test_architecture_boundary.py`。运行 `pytest tests/test_architecture_boundary.py` 必须绿。
4. **迁移零漂移**：`ExternalContact` 的 ORM 列必须与 `0020` 迁移逐字一致；`make verify`（含 `alembic upgrade head` + `alembic check`）必须绿。
5. **租户隔离**：所有查询/写入带 `tenant_id`；同步 service 的 `tenant_id` 必传。
6. **安全**：`access_token`/`secret` 绝不记录；WeCom 响应体（含外部联系人 PII）不记录（沿用 `wecom_contacts.py:14` 注释约定）。

---

## 7. RED → GREEN 验证（交给 dev agent 自查）

- [ ] `RED`：测试文件就绪、`ExternalContact` 未定义 → `pytest tests/test_rnd287_external_contacts.py` 失败。
- [ ] `GREEN`：
  - `alembic upgrade head` 成功，`external_contacts` 表存在且含全部列 + GIN trgm 索引。
  - `alembic check` 通过（零 schema 漂移）。
  - `pytest tests/test_rnd287_external_contacts.py -v` 全绿（含租户隔离、客户端失败安全、同步幂等、派生统计）。
  - `pytest tests/test_architecture_boundary.py` 全绿。
  - `python -m app.services.external_contact_sync` 在 `WECOM_EXTERNAL_CONTACT_SECRET` 缺失时安全跳过；在 mock/真实凭据下能将 WeCom 数据写入 `external_contacts`。
  - `make verify`（或等价）全绿。

---

## 8. 交付物 / 收尾纪律

- 交付 = 上述代码改动，**不 git commit / 不 push**（Agent 纪律）。
- 改动文件集合（供 QA 核对 diff）：`backend/app/db/models.py`、`backend/alembic/versions/0020_external_contacts.py`、`backend/app/wecom_contacts.py`、`backend/app/db/external_contacts.py`（新建）、`backend/app/services/external_contact_sync.py`（新建）、`backend/tests/test_rnd287_external_contacts.py`（新建）。
- 同步完成后，在 Linear RND-287 标注实现就绪，**等待 QA 验收 + 用户本人决定是否 commit**。
- 未做（明确划出范围，勿擅自扩展）：RND-296 列表/筛选 API、RND-298 详情时间线、生产 systemd 定时调度（RND-237）。
