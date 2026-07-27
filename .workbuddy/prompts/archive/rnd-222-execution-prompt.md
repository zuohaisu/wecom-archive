# RND-222 执行提示词（开发 / 实现）

> 用途：粘贴给实现智能体（按 `DEV_AGENT_RULES.md` 的 Claude Code 实现角色），由其独立完成 RND-222。
> 验收由独立 QA 智能体按 `.workbuddy/prompts/rnd-222-qa-prompt.md` 复验。
> 本任务 = **结构性重构 + 一处安全审计修复**：把 sync / decrypt / media 三个 worker 的 CLI shell 与可测试 application functions 分离（行为保持），并**修复 decrypt worker 的 tenant scope 越界**（唯一的、有意的行为变更，见 §2.3）。

---

## 0. 任务与来源

- **Linear 工单**：RND-222「提取 Worker Application Functions 并审计 Tenant Scope」，P2，父任务 RND-212（渐进式重构为 AI 友好的模块化单体），负责人 Haisu Zuo。
- **在重构链中的位置**：`RND-218 → {RND-219, RND-220, RND-221} → RND-222 → RND-223 → RND-224`。本任务动的是 **worker 层（`backend/scripts/`）**，与 219/220/221 的 router/schema 领地文件不交叉（见 §0.5.3），但共享 `app/services/` 包。
- **目标（工单原文）**：将 sync/decrypt/media worker 的 CLI shell 与可测试 application functions 分离，并明确一次运行的 tenant/corp scope、事务边界与 exit behavior。范围：worker core callable；显式 tenant/corp 参数；fake SDK/session 可注入测试；CLI 输出与 systemd 调用保持兼容；审计 decrypt pending query 的 tenant/corp 边界。
- **非目标（工单原文）**：不在本票中启用完整多 corp 调度，不修改生产 timer cadence。
- **验收标准（工单原文）**：CLI 输出/exit code 不变；worker/recipient/revoke/tenant/SDK mock tests 通过；tenant scope 有明确代码契约和测试；生产 systemd command 可继续使用。

### 0.1 现状架构（精确，基于本地 main `5fa1ee6` post-RND-218；行号为切片指引，实施时以函数体为准）

三个 worker 全在 `backend/scripts/`，均为 one-shot 脚本（`sys.path` 注入 backend 根后 `import app.*`）：

| Worker | 文件 | main() | 参数来源 | 规模 |
|---|---|---|---|---|
| Sync | `scripts/sync_wecom_archive_once.py` | L150–305 | 纯 env（DATABASE_URL / WECOM_SDK_LIB_PATH / WECOM_CORP_ID / WECOM_ARCHIVE_SECRET / WECOM_CHAT_LIMIT） | ~309 行 |
| Decrypt | `scripts/decrypt_wecom_messages_once.py` | L416–695 | 纯 env（另加 WECOM_PRIVATE_KEY_PATH / WECOM_PUBLIC_KEY_VERSION） | ~699 行 |
| Media | `scripts/download_wecom_media_once.py` | main L245–304 + `_run()` L307–464 | argparse：`--count-only` / `--limit` / `--retry` / `--types` / `--since-hours` / `--newest-first` / `--skip-nested` | ~537 行 |

- **编排器**：`scripts/run_archive_worker_once.py`（L135–150）——fcntl 锁（锁被占则 `sys.exit(0)` no-op），然后 `subprocess.run` 依次跑 sync、decrypt；子进程非 0 → `sys.exit(1)`。**本任务不改它**（它只认子进程 exit code）。
- **systemd（`deploy/systemd/`，ExecStart 一字不许改）**：
  - `wecom-archive-worker.service` → `…/.venv/bin/python scripts/run_archive_worker_once.py`（timer 每 5 分钟）
  - `wecom-archive-media-download.service` → `…/.venv/bin/python scripts/download_wecom_media_once.py --since-hours 72 --newest-first --limit 20`
- **输出契约**：全部为 `print("[INFO]…"/"[FAIL]…"/"[PASS]…", flush=True)`；成功 `sys.exit(0)`、失败 `sys.exit(1)`。权威文档：`docs/wecom_archive_worker_runbook.md`、`docs/wecom_archive_media_download_runbook.md`。
- **DB session**：worker 各自 `create_engine(database_url)` + `with Session(engine) as session` 内联创建（sync L161 / decrypt L491 / media L311），不用 web 侧 `app/db/session.py:get_db()`。
- **SDK**：`app/sdk/wecom_sdk.py` 为纯模块级 ctypes 函数（`load_sdk` / `configure_sdk*` / `new_sdk` / `init_sdk` / `decrypt_data` / `iter_media_chunks` / `destroy_sdk`），无类、无 DI；现有测试靠 monkeypatch 模块属性（见 `tests/test_download_wecom_media_once.py` L795–796、L850–863）。
- **`app/services/`**：包已存在；RND-219 正往里放 `listing_service.py`（截至撰写仍未提交）。**worker 与 routers 无 import 交叉**（三个 worker 只依赖 `app.db.models` / `app.sdk` / `app.message_type_registry` / `app.revoke_reconciliation` / `app.structured_message_parser` / `app.media_download` / `app.media_storage` / `app.thumbnail_pipeline`）。

### 0.2 Tenant scope 审计结论（本任务的安全核心，先读懂再动手）

- **Sync ✅ 已隔离**：`_require_tenant_id(session, corp_id)`（`sync_wecom_archive_once.py` L79–111，按 `TenantWecomConfig.corp_id + is_active` 解析）；`tenant_id` 写入 `ArchiveMessage.tenant_id`（L265）与 `SyncState`；seq 游标按 `(tenant_id, corp_id)` 维度。
- **Media ✅ 已隔离**：`_require_tenant_id` L175–191；`tenant_id` 传入 `select_candidates` / `select_nested_media_candidates`（L318/L343），候选查询带 `archive_messages.tenant_id` 过滤。
- **Decrypt ⚠️ 越界（工单点名审计对象）**：
  1. `main()` **从不解析 tenant_id**（无 `_require_tenant_id`）；`corp_id` 只用于 SDK init。
  2. **pending 查询（L509–516）无 tenant 过滤**：`session.query(ArchiveMessage).filter(ArchiveMessage.decrypt_status.in_(["pending","failed"])).order_by(ArchiveMessage.id).all()` —— 多租户库下会扫全部租户的行，却只持有当前 corp 的 RSA 私钥（跨租户行必然 RSA 失败并被标 `failed`，**污染他租户数据**）。
  3. 自修复扫描同样越界：L619 `repair_missing_recipients(session)`、L631 `reconcile_pending_revocations(session)` 均未传 `tenant_id` —— 而这两个函数**已有** `tenant_id: Optional[str] = None` 形参（`decrypt_wecom_messages_once.py` L367、`app/revoke_reconciliation.py` L201–203），传入即可生效。L628–630 注释已自认「Tenant-agnostic」。
  4. 末尾 `pending_remaining` 统计（L641–649）同样无 tenant 过滤。

### 0.3 事务边界现状（提取时逐字保持）

- **Sync**：按次提交——`if records: session.commit()`（L271–278），随后新 session 提交 seq 游标；幂等靠 `(tenant_id, msgid)` 唯一约束。
- **Decrypt**：整轮单次提交——循环内只改内存对象，最后统一 `session.commit()`（L633–638）；commit 失败 → `[FAIL]` + `sys.exit(1)`。recipient upsert / revoke reconcile 失败**不致命**（仅计数）。
- **Media**：逐条提交——`_persist_download_outcome` 每条 `session.commit()`；commit 失败 → rollback + 清理孤儿存储对象（L511–519）。

---

## 0.5 前置条件（硬门，先确认，不满足则停下）

> 任何一条不满足，**停下并在 Linear 评论说明**，不要自行补做前置任务。

1. **工作树必须干净**。截至撰写，工作区存在 RND-219 的未提交改动（`app/routers/conversations.py`、`app/conversation_membership.py`、`app/schemas/listing.py`、`app/services/listing_service.py`、相关测试）。**不要基于这些未提交改动开工，也不要自行 commit 它们**。先请 Haisu 将 219/220/221 的验证改动 commit 到 `main`，`git status` 干净后再开始。
2. **链顺序**：Linear 计划为 `{219,220,221} → 222`。若 Haisu 明确指示提前并行亦可（文件面不交叉），但**默认按链序**：确认 219/220/221 已合入 `main` 后 rebase 开工。
3. **`app/services/` 包共享红线**：本任务只在 `services/` 下**新建** `sync_worker.py` / `decrypt_worker.py` / `media_worker.py` 三个文件；**绝不修改** `services/listing_service.py`、`services/__init__.py` 中他人已有内容（若 `__init__.py` 非空，只做追加式改动或干脆不动它）。
4. **开工方式**：从最新 `origin/main` rebase，在干净 main 上直接实现（`DEV_AGENT_RULES.md`：不建 task branch，除非 Haisu 明确要求）。
5. **绝不触碰 219/220/221 领地**：`app/routers/*`、`app/schemas/*`、`app/main.py`、`app/conversation_membership.py` 本任务一行不改。

---

## 1. 精确范围

### 1.1 新建文件（本任务产出）

- `backend/app/services/sync_worker.py` —— sync 核心循环的 application function。
- `backend/app/services/decrypt_worker.py` —— decrypt 核心循环的 application function（含 tenant scope 修复）。
- `backend/app/services/media_worker.py` —— media 核心循环的 application function。
- `backend/tests/fakes.py`（或 `tests/conftest.py` fixture）—— 可复用 `FakeWecomSdk` / fake session 工厂（满足工单「fake SDK/session 可注入测试」）。
- `backend/tests/test_sync_worker_service.py`、`backend/tests/test_decrypt_worker_service.py`、`backend/tests/test_media_worker_service.py`（命名可微调，但三个 worker 的 service 层都必须有直测）。

### 1.2 修改文件（薄壳化）

- `backend/scripts/sync_wecom_archive_once.py`、`scripts/decrypt_wecom_messages_once.py`、`scripts/download_wecom_media_once.py` —— 变为**薄 CLI shell**：env 解析 / argparse / `_require_tenant_id` / 调 service / 按 summary **打印原格式输出** / `sys.exit`。三个脚本的 `print` 行文案、顺序、条件（如「计数为 0 则不打印该行」）与 exit code **逐字节保持**（decrypt 的 tenant 修复见 §2.3 例外说明）。
- `docs/ARCHITECTURE.md` —— 追加一小节「worker tenant/corp scope 契约」（每个 worker 一次运行 = 一个 `(tenant_id, corp_id)`；事务边界；exit code 语义）。

### 1.3 不改（显式）

- `scripts/run_archive_worker_once.py`（编排器）、`deploy/systemd/*`（ExecStart/timer 一字不改）、`app/sdk/wecom_sdk.py`（函数签名不改，service 通过参数接收「模块或兼容对象」实现注入）、`app/routers/*`、`app/schemas/*`、`app/main.py`、`app/media_download.py` 与 `app/revoke_reconciliation.py` 的函数**实现**（只是开始传入 `tenant_id` 实参）。
- 其它 backfill/migrate 脚本（`backfill_thumbnails_once.py` 等）不在本期范围。

---

## 2. 设计要求

### 2.1 Service 函数契约（三个 worker 统一模式）

每个 service 模块暴露一个核心 callable（命名建议 `run_sync_once` / `run_decrypt_once` / `run_media_download_once`），并遵守：

- **显式参数**：`session`（或 engine/session factory）、`sdk`（默认 `app.sdk.wecom_sdk` 模块本身，测试传 fake 对象——只需鸭子类型实现被用到的函数）、`tenant_id: str`、`corp_id: str`，以及各自业务参数（sync 的 `limit`、decrypt 的 `private_key`/`expected_pubkey_ver`、media 的 argparse 全部选项）。**`tenant_id` 是必填位置参数，绝不允许 `None` / 默认值**——这就是「tenant scope 的代码契约」。
- **返回 summary 对象**（`dataclass`，字段 = 现有 print 的全部计数器，如 decrypt 的 `scanned/success/failed/unsupported/pending_remaining/key_mismatch/rsa_failed/recipient_upsert_failed/recipients_repaired/revoke_event_seen/revoke_reconcile_failed/revocations_reconciled/return_codes`），**绝不 `print`、绝不 `sys.exit`**。可预期失败用返回值或自定义异常表达，由 shell 翻译为 `[FAIL]` + exit code。
- **SDK 生命周期归属**：SDK 的 load/configure/new/init/destroy 可留在 shell（属于「环境准备」）也可进 service，二选一后保持一致；若进 service，必须经 `sdk` 参数调用以便 fake 注入。DB engine 创建（`create_engine`）留在 shell，service 只收 session/factory。
- **事务边界逐字保持**（§0.3）：sync 按次提交、decrypt 整轮单次提交、media 逐条提交 + 失败 rollback/清理。commit 调用发生在 service 内（它是核心循环的一部分），commit 失败向上抛给 shell 翻译成 `[FAIL] Database commit failed: …` + `sys.exit(1)`。
- **循环依赖防控**：service 模块**不得 import `app.main`、任何 router 模块、`scripts.*`**；只依赖 `app.db.*` / `app.sdk.*` / `app.media_download` / `app.revoke_reconciliation` / `app.message_type_registry` / `app.structured_message_parser` / `app.media_storage` / `app.thumbnail_pipeline` 等纯模块。

### 2.2 CLI shell 契约（行为保持）

- env 缺失 / 非法（如 `WECOM_PUBLIC_KEY_VERSION` 非整数）的 `[FAIL]` 文案与 `sys.exit(1)` 保持在 shell，逐字不变。
- 每条现有 `print` 的**文案、顺序、flush、条件打印逻辑**照抄（shell 从 summary 对象读数）。
- media 的 argparse 定义（选项名、默认值、help）不改；`--count-only` 等快捷路径行为不变。
- `if __name__ == "__main__": main()` 结构保留；`run_archive_worker_once.py` 观察到的子进程 exit code 语义不变。

### 2.3 Decrypt tenant scope 修复（唯一的有意行为变更）

1. 给 decrypt shell 增加 `_require_tenant_id(session, corp_id)`（对齐 `sync_wecom_archive_once.py` L79–111 的实现与 `[FAIL]` 文案风格：无活跃 `TenantWecomConfig` 匹配 `corp_id` → `[FAIL]` + `sys.exit(1)`）。
2. pending 查询加 `ArchiveMessage.tenant_id == tenant_id` 过滤（进入 service 后由 `tenant_id` 参数强制）。
3. `repair_missing_recipients(session, tenant_id)`、`reconcile_pending_revocations(session, tenant_id)` 传入实参（两函数已支持，实现不改）。
4. `pending_remaining` 统计同样加 tenant 过滤。
5. 同步删除/更新 L628–630「Tenant-agnostic」注释。
6. **影响声明**（写进 Linear 评论）：生产为单租户部署，输出计数不变；多租户库下 decrypt 从「扫全库」收敛为「只扫本 corp 对应租户」——这是修复而非回归。**新增失败模式**：`TenantWecomConfig` 缺失时 decrypt 现在会 fail-fast（与 sync/media 一致），需在 runbook 提及（在 `docs/wecom_archive_worker_runbook.md` 补一行即可）。

### 2.4 Fake SDK / session 注入 + 新测试

- `FakeWecomSdk`：实现三个 worker 用到的函数面（`load_sdk/configure_sdk*/new_sdk/init_sdk/destroy_sdk/get_chat_data(sync)/decrypt_data(decrypt)/iter_media_chunks(media)`），可编程返回值/异常。放 `tests/fakes.py` 供各测试文件复用；**不要求改造既有测试**（它们的 monkeypatch 手法继续有效），但新测试一律走注入而非 monkeypatch。
- **必须覆盖的新测试**：
  - decrypt service：多租户 fixture（参考 `tests/test_recipient_persistence.py` 的 `_TENANT_A/_TENANT_B` + sqlite `db` fixture）下，tenant A 运行**只**处理 A 的 pending/failed 行，B 的行保持原状（`decrypt_status`、recipients、revocations 全都不动）；`repair_missing_recipients` / `reconcile_pending_revocations` 的 tenant 传参生效；A 的失败不影响 B 已提交数据。
  - sync service：注入 fake SDK 返回构造好的 chatdata，断言按 `(tenant_id, msgid)` 幂等 upsert、seq 游标推进、`chat_ret != 0` 时向 shell 报错路径。
  - media service：至少一条注入式冒烟（候选选择带 tenant 过滤 + 下载成功落库），其余仍由既有 `test_download_wecom_media_once.py` 覆盖。
  - service 纯度：断言三个 service 模块源码不含 `sys.exit` 与 `print(`（简单 grep 断言或 code review 项均可，建议写成测试）。
  - CLI 等价：对每个 shell 至少一条测试，注入 fake 后跑 `main()`（capsys 捕获），断言输出行格式与 exit code（`SystemExit.code`）不变。

---

## 3. 非目标（严禁）

- 不启用多 corp 调度：一次运行仍然只处理**一个** `(tenant_id, corp_id)`（从 env 的 `WECOM_CORP_ID` 解析）；不加「遍历所有租户」逻辑。
- 不修改生产 timer cadence / systemd unit / `run_archive_worker_once.py` 锁机制。
- 不改 CLI 输出格式、参数名、exit code 语义（decrypt 的 §2.3 除外且需声明）。
- 不改 `app/sdk/wecom_sdk.py`、`app/media_download.py`、`app/revoke_reconciliation.py` 的函数实现/签名。
- 不引入 DB migration / schema 变更；不引入新第三方依赖；不碰 CI、`.github/`。
- 不触碰 219/220/221 领地文件（§0.5.5）；不动 `web/` 前端。
- 不自行 `git commit` / `push` / 开 PR（见 §6）。

---

## 4. 执行步骤（严格按顺序）

### 阶段一：锁定行为基线（不改实现）

1. 确认 §0.5 硬门全部满足（工作树干净、链上游已合入）。
2. 跑既有相关测试确认全绿，作为基线：
   ```bash
   cd backend && python -m pytest tests/test_download_wecom_media_once.py tests/test_recipient_persistence.py tests/test_revoke_reconciliation.py tests/test_revoke_concurrency.py tests/test_backfill_revoke_associations.py tests/test_check_message_revocations_integrity.py tests/test_message_revocations_tenant_integrity.py tests/test_tenant_foundation.py tests/test_tenant_isolation.py tests/test_tenant_sync_state.py tests/test_tenant_wecom_config_uniqueness.py tests/test_wecom_sdk_media.py tests/test_qiniu_worker_integration.py tests/test_reachability_audit.py -q
   ```
3. 对三个脚本记录「输出行清单 + 打印条件 + 全部 `sys.exit` 点」（可写成临时清单），作为 §2.2 等价性的对照表。

### 阶段二：建 fake + service（先 decrypt，含审计修复）

4. 新建 `tests/fakes.py`（`FakeWecomSdk` 等）。
5. 新建 `services/decrypt_worker.py`：搬 decrypt 核心循环（L494–649 的循环 + 修复扫描 + commit + pending_remaining 统计），按 §2.1 契约参数化，按 §2.3 加 tenant 过滤。**先写多租户测试再搬代码**（红→绿）。
6. decrypt shell 薄壳化：env 解析 + `_require_tenant_id` + SDK 准备 + 调 service + 原格式打印 + exit。跑 CLI 等价测试。

### 阶段三：sync 与 media 同法炮制

7. `services/sync_worker.py` + shell 薄壳化 + 测试（sync 的 `_require_tenant_id`、`_read_seq`、`_upsert_seq` 可迁入 service 或保留脚本内被 service 调用——选一种，保持单一真源，`sync_contact_display_names_once.py` 等其它脚本对 `_require_tenant_id` 的独立副本不在本期收敛范围）。
8. `services/media_worker.py` + shell 薄壳化 + 测试。media 的 `download_one` / `_persist_download_outcome` 等既有模块级函数如被 `test_download_wecom_media_once.py` 直接 import，**保持可从原路径 import**（在脚本里 re-export 或原地保留），不要破坏既有测试的 import 面。

### 阶段四：文档 + 收口（不达标不收工）

9. `docs/ARCHITECTURE.md` 补「worker tenant/corp scope 契约」小节；`docs/wecom_archive_worker_runbook.md` 补 decrypt 的 `TenantWecomConfig` fail-fast 一行。
10. 跑全量相关回归（阶段一命令 + 全部新增测试文件）。
11. `git diff deploy/` 必须为空；`git diff` 确认未触碰 §0.5.5 领地文件。
12. 收口跑 `make verify`（lint-diff + typecheck + build + 全量 pytest），全绿。

---

## 5. 兼容性契约（不可违反，验收据此判定）

- **systemd 兼容**：`deploy/systemd/*` 零改动；`scripts/run_archive_worker_once.py` 零改动；三个脚本仍可用生产 ExecStart 原命令行调用。
- **CLI 等价**：三个 shell 的输出行文案/顺序/条件与 exit code 与改造前逐字一致（唯一例外：decrypt 新增 `TenantWecomConfig` 缺失 fail-fast，须在 Linear 评论声明）。
- **tenant scope 契约**：三个 service 核心函数的 `tenant_id` 为必填参数；decrypt 的 pending 查询、repair 扫描、revocation 扫描、pending_remaining 统计全部按 `tenant_id` 过滤；有多租户测试证明「A 运行不触 B 数据」。
- **service 纯度**：`services/{sync,decrypt,media}_worker.py` 无 `print` / 无 `sys.exit` / 不 import `app.main`、router、`scripts.*`。
- **事务边界不变**：sync 按次、decrypt 整轮单次、media 逐条 + rollback 清理，模式与改造前一致。
- **既有测试面不破坏**：`test_download_wecom_media_once.py` / `test_recipient_persistence.py` 等对脚本模块的 import（如 `from scripts.decrypt_wecom_messages_once import _upsert_recipients, build_missing_recipient_repair_query, repair_missing_recipients`）继续成立——被搬走的符号在脚本处 re-export。
- **无新依赖 / 无 migration / 无 CI 变更**。

---

## 6. 硬性约束（实现 agent 自身也要守）

- **行为等价优先**：除 §2.3 声明的 tenant 修复外，任何输出、exit code、事务、落库行为的变化都视为回归。
- **绝不 `git commit` / `push`**：所有 git 提交与推送一律由用户（Haisu）本人操作——即便自测全绿也只提示，不代劳。
- 不引入后台进程；命令前台运行。
- 不触碰工作区里任何他人未提交改动（若开工时仍存在则按 §0.5.1 停下）。
- 遇到无法保持等价的点（例如某输出行依赖循环中间状态难以由 summary 复原），如实记录在 Linear 评论，不静默改行为。

---

## 7. 收尾动作

- 在 Linear 把 RND-222 置为 In Progress（如尚未），完成后写一条评论：三个 service 模块的函数签名、shell 保留了什么、decrypt tenant 修复点清单（查询/repair/revocation/pending_remaining 四处）、新增失败模式声明、事务边界确认、新测试清单、`make verify` 结论、`deploy/` 零改动确认。
- 不要自动合入/提交；交还用户（Haisu）决策（本任务阻塞 RND-223）。
