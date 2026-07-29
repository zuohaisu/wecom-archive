# RND-287 验收提示词 — A4-1 ExternalContact 实体 + WeCom 外部联系人同步

> 本文件是交给**专门测试/QA agent** 的验收 brief。请对照开发 agent 的交付物逐项核验。
> 代码标识符均加反引号，便于 grep 落点。架构冻结 D1：SSR + 原生 JS。

---

## 1. 任务与验收矩阵（AC）

**任务**：RND-287（A4-1）新建 `ExternalContact` 实体 + 从企业微信外部联系人 API 同步的 worker/service。
**父 epic**：RND-264（A4）。**本票是 RND-296 / RND-298 的前置基础票**。

| # | 验收点（来自 Linear AC「实体建好；同步填充」+ 设计稿） | 核验方式 |
|---|---|---|
| AC1 | `external_contacts` 表存在，含设计稿全部列：姓名/企业/标签/来源/归属员工/最后互动/会话数（+ `external_userid`/`tenant_id`/时间戳） | `alembic upgrade head` 后查表结构；对照 `models.py` 的 `ExternalContact` |
| AC2 | 同步能从 WeCom 拉取并**填充**实体（name/company/tags/source/owner_wecom_userid） | 运行 `python -m app.services.external_contact_sync`（mock 或真实凭据），断言行数增加/更新 |
| AC3 | **租户隔离**：同步仅写 `tenant_id` 对应的数据；跨租户 `external_userid` 互不串 | 双租户 fixture，断言 A 的数据不带 B 的 `tenant_id`、行 id 不同 |
| AC4 | **失败安全**：WeCom 接口失败/超时/errcode≠0 → 不抛异常、不中断整批、记录 warning | mock 抛错 / 返回 errcode≠0，断言进程退出码 0 且已成功部分仍落库 |
| AC5 | **密钥缺失安全**：`WECOM_EXTERNAL_CONTACT_SECRET` 缺失 → 同步跳过并提示未启用，不报错崩溃 | 不设置该 env 运行 CLI，断言安全退出 + log 提示 |
| AC6 | **幂等**：重复运行不产生重复行（按 `uq_external_contacts_tenant_ext_userid`），`updated_at` 推进 | 跑两次同步，断言行数不变、`updated_at` 改变 |
| AC7 | **派生统计**：`message_count`/`last_interaction_at` 由档案消息正确计算（或消息缺失时 `None`，不报错） | 构造 `ArchiveMessage`/`ArchiveMessageRecipient` 假数据断言计算；及空档案断言 `None` |
| AC8 | **架构边界零改动**：`app/wecom_contacts.py` 仅扩展、`app/services/external_contact_sync.py` 为 service 层，`test_architecture_boundary.py` 无需改且通过 | `pytest tests/test_architecture_boundary.py` 绿；diff 不含该测试文件改动 |
| AC9 | **迁移零漂移**：ORM 与 `0020` 迁移逐字一致，`alembic check` 通过 | `alembic upgrade head` + `alembic check` 绿 |
| AC10 | **B 层纪律**：未改 `backend/scripts/`、未新增 router、未改 `.env.example`、未引入前端 | diff 集合核对（见 §3） |

---

## 2. 决策回顾（开发 agent 应已遵守，QA 需复核）

1. **扩展 `wecom_contacts.py` 而非新建 flat 模块**（该模块已在 `test_architecture_boundary.py:91` 的 `_FLAT_SERVICE_MODULES`）。→ 边界零改动。
2. **新建 `app/services/external_contact_sync.py`**（自动归 service 层，`test_architecture_boundary.py:57`）。→ 边界零改动。
3. **同步入口 = `__main__` + `python -m`**，绝不新建 `backend/scripts/sync_external_contacts_once.py`，绝不改 `run_archive_worker_once.py` / systemd unit（B 层/RND-237）。
4. **`tags` 存 `Text`（JSON 字符串）** 以兼容 sqlite 单测；服务层负责序列化。若开发 agent 改用 `postgresql.JSONB()`，必须确认单测引擎为 Postgres 且 `alembic check` 仍绿。
5. **派生统计 best-effort 可空**：`message_count`/`last_interaction_at` 在档案消息缺失时留 `None`，实体与同步仍满足 AC。
6. **迁移 `down_revision` 动态取当前 head**（现 `0019`）；若树上已有更高 revision，应顺延（如 `0021`）并改 `down_revision`。**绝不硬编码冲突的 revision 号**。
7. **F0 闸口**：开发 agent 应已断言 `TenantWecomConfig`/`AdminUser`/`get_current_user`/`get_wecom_token` 存在于树；若缺失应 `BLOCKED` 停止。QA 应确认这些基元确实在树上（当前在）。

---

## 3. 范围守门（diff 集合核对 — 多改即违规）

**允许改动文件**（应全部出现在 dev 交付的 diff 中）：
- `backend/app/db/models.py`（仅追加 `ExternalContact` 类，紧接 `Contact` 之后）
- `backend/alembic/versions/0020_external_contacts.py`（新建）
- `backend/app/wecom_contacts.py`（仅追加 4 个函数，不删不改既有 2 个）
- `backend/app/db/external_contacts.py`（新建，`upsert_external_contact`）
- `backend/app/services/external_contact_sync.py`（新建，含 `__main__`）
- `backend/tests/test_rnd287_external_contacts.py`（新建）

**严禁改动**（diff 中**不得出现**）：
- `backend/scripts/**`（任何文件）
- `backend/app/routers/**`（本票无 router；列表/筛选 API 属 RND-296）
- `backend/app/web/**`（无前端改动，D1 冻结）
- `.env.example`、任何既有迁移文件、`backend/app/db/contacts.py` 既有逻辑
- `test_architecture_boundary.py`（除非出现新 flat 模块——但本票不应有）

> 若 diff 出现上述任一文件 → 判定**范围违规**，退回开发 agent。

---

## 4. 精确落点核对

| 预期落点 | 核查要点 |
|---|---|
| `models.py` `class ExternalContact`（约 line 800 后） | 列：id/external_userid/name/company/tags(Text)/source/owner_wecom_userid/last_interaction_at/message_count/tenant_id(非空前链 tenants.id, 索引)/created_at/updated_at；`UniqueConstraint("tenant_id","external_userid")`；GIN trgm 索引 name+company |
| `0020_external_contacts.py` | `down_revision`=当前 head；列定义逐字匹配 model；先 `CREATE EXTENSION IF NOT EXISTS pg_trgm` 再建 GIN 索引；`upgrade`/`downgrade` 对称 |
| `wecom_contacts.py` 新增 `list_follow_userids` / `list_external_userids_by_user` / `get_external_contact` / `get_corp_tag_list` | 失败返回 `None`/空；不记录 token/secret/PII；`get_external_contact` 解析 `follow_user[].{userid,remark,state,tags}` 与 `external_contact.{name,corp_name,corp_full_name}` |
| `app/db/external_contacts.py` `upsert_external_contact` | tenant-scoped upsert；空名不覆盖非空名（沿用 `contacts.py:19` 范式）；基于唯一约束；不 commit |
| `app/services/external_contact_sync.py` | 纯逻辑 `(session, tenant_id, corp_id, external_secret, ...)`，`tenant_id` 必传不扫全租户；`__main__` 读 `WECOM_CORP_ID`/`WECOM_EXTERNAL_CONTACT_SECRET`，经 `create_engine`+`Session` 调用 service 并 commit；返回 `RunSummary` dataclass |

---

## 5. 安全 / 契约校验

- `access_token`/`secret` 不得出现在任何 log（含异常栈）。grep 交付代码确认无 `logger.*token` / `print(token)`。
- WeCom 响应体（外部联系人 PII）不得记录。核对 `wecom_contacts.py` 新增函数与 sync service 无 `resp.text`/`data` 全量 log。
- 外部联系人归属员工存原始 `owner_wecom_userid`（String），不依赖 `AdminUser` 必须存在。
- 同步 service 不读 env、不建 engine、不 `sys.exit`（这些只在 `__main__`）。

---

## 6. 架构边界校验

```bash
cd backend && pytest tests/test_architecture_boundary.py -v
```
- 必须全绿。
- 确认 `app/wecom_contacts.py` 仍在 `_FLAT_SERVICE_MODULES`（line 91），`app/services/external_contact_sync.py` 被自动归 service 层（无需出现在 allowlist）。
- 运行 `python -c "import app.services.external_contact_sync"` 与 `import app.db.external_contacts` 成功，无循环依赖。

---

## 7. 测试策略（RED → GREEN → 回归）

### 7.1 回归套件（必须全绿，证明未破坏既有）
```bash
cd backend && make verify            # 或等价：pytest + alembic upgrade head + alembic check
cd backend && pytest tests/test_architecture_boundary.py tests/test_contact_sync.py -v
```
- `test_contact_sync.py` 全绿（证明扩展 `wecom_contacts.py` 未破坏既有 display-name 同步与 `/api/contacts`）。
- `alembic check` 通过（零漂移）。

### 7.2 本票新增测试（GREEN）
```bash
cd backend && pytest tests/test_rnd287_external_contacts.py -v
```
覆盖 §1 的 AC1/AC2/AC3/AC4/AC5/AC6/AC7。重点：
- **sqlite in-memory** 模型 + 租户隔离（参照 `test_contact_sync.py:36` 的 `db_session` fixture）。
- **mock httpx** 客户端（参照 `test_contact_sync.py:132` `_FakeClient`）：`list_follow_userids`/`get_external_contact`/`get_corp_tag_list` 解析正确 + 失败安全。
- **同步幂等**：跑两次断言无重复行。
- **派生统计**：构造 `ArchiveMessage`/`ArchiveMessageRecipient` 假数据断言 `message_count`/`last_interaction_at`；空档案断言 `None`。

### 7.3 端到端（可选，真实凭据）
- 设 `WECOM_CORP_ID` + `WECOM_EXTERNAL_CONTACT_SECRET`，对测试租户跑 `python -m app.services.external_contact_sync`，断言 `external_contacts` 行数 > 0 且字段合理；再跑一次断言幂等。
- 不设 `WECOM_EXTERNAL_CONTACT_SECRET` 重跑，断言安全跳过（退出码 0 + 提示日志）。

---

## 8. 收尾

- 全部 AC 通过 + 回归绿 → 在 Linear RND-287 标注验收通过。
- **不 commit / 不 push**（Agent 纪律）；由用户本人决定是否提交。
- 明确未覆盖项（供用户知会后续票）：RND-296（列表/筛选 API）、RND-298（详情时间线）、生产 systemd 定时调度（RND-237）。这些不在本票验收范围内。
