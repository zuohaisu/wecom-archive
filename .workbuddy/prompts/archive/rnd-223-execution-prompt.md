# RND-223 执行提示词（开发 / 实现）

> 用途：粘贴给实现智能体（按 `DEV_AGENT_RULES.md` 的 Claude Code 实现角色），由其独立完成 RND-223。
> 验收由独立 QA 智能体按 `.workbuddy/prompts/rnd-223-qa-prompt.md` 复验。
> 本任务是**行为保持的结构性重构**：引入 `create_app()` App Factory 与按域分组的 Typed Settings，把散落在各模块的 `os.getenv` / `os.environ.get` 读取收敛为类型化配置对象，同时保持 `uvicorn app.main:app` 启动方式**完全兼容**。**不新增功能、不改任何对外行为、不改任何环境变量名。**

---

## 0. 任务与来源

- **Linear 工单**：RND-223「引入 App Factory 与分域 Typed Settings」，P4，父任务 RND-212，负责人 Haisu Zuo；**blocks RND-224**（架构防退化规则 + Source-of-Truth 文档）。
- **在重构链中的位置**：`RND-218 → {RND-219, RND-220, RND-221} → RND-222 → RND-223 → RND-224`。本任务是链上倒数第二环。
- **目标（工单原文）**：`create_app()` 和按域配置对象，同时保持 `uvicorn app.main:app` 兼容；消灭散落 `os.getenv`；`main.py` 只保留 composition-root 职责。

### 0.1 现状（截至 RND-218 合入后的 `main`）

- `backend/app/main.py` 已收敛到约 136 行，包含：`_RedactOAuthCallbackQueryFilter`（RND-225 日志脱敏）、module-level `app = FastAPI(...)` + `add_middleware(MediaAccessNoStoreMiddleware)` + 7 个 `include_router`、3 个 health 端点（`/health/live`、`/health/ready`、`/health` 别名 + `_readiness_body`）、`_VersionedStaticFiles` + `/web/static` mount（RND-216/217）。
- **`app/` 内环境变量读取点全景**（实现前必须自行 grep 复核一遍，以届时 `main` 为准；下面是编写本提示词时的快照）：

| 模块 | 位置 | 环境变量 | 读取语义 |
|------|------|----------|----------|
| `app/db/session.py` | `_get_engine()` | `DATABASE_URL` | **懒读取 + 进程级 engine 缓存**；缺失时 raise RuntimeError |
| `app/auth.py` | `get_auth_mode()` | `AUTH_MODE` | **每次调用重读**；非法值 warning + 回退 `wecom` |
| `app/auth.py` | `_is_production()` | `APP_ENV` | 每次调用重读；默认 `development` |
| `app/routers/auth.py` | 密码登录 | `ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH` | **请求时重读** |
| `app/routers/auth.py` | OAuth 跳转/回调 | `WECOM_CORP_ID`、`WECOM_AGENT_ID`、`ADMIN_DOMAIN`、`WECOM_OAUTH_SECRET` | **请求时重读** |
| `app/routers/wecom_events.py` | 回调校验 | `WECOM_CALLBACK_TOKEN`、`WECOM_CALLBACK_ENCODING_AES_KEY`、`WECOM_CORP_ID` | **请求时重读** |
| `app/media_storage.py` | provider 工厂 | `MEDIA_STORAGE_PROVIDER`（fallback `STORAGE_BACKEND`）、`STORAGE_LOCAL_PATH`、`QINIU_ACCESS_KEY/SECRET_KEY/BUCKET/DOMAIN/REGION/TIMEOUT_SECONDS`、`MEDIA_SIGNED_URL_TTL_SECONDS`、`MEDIA_SIGNED_URL_WINDOW_SECONDS` | **调用时重读**；Qiniu 缺配置 fail-loud（`QiniuConfigurationError`）；local 缺路径 degrade 到安全 404 |
| `app/media_thumbnails.py` | 缩略图配置 | `MEDIA_THUMBNAIL_ENABLED`、`MEDIA_THUMBNAIL_MAX_EDGE`、`MEDIA_THUMBNAIL_JPEG_QUALITY` | 调用时重读，带默认值与容错解析 |

- **本任务范围仅限 `backend/app/` 内的 web 应用配置**。`backend/scripts/*_once.py`（worker CLI）与 `backend/alembic/env.py`、`backend/tests/` 内的 `os.getenv` **不在本期范围**（worker 配置属 RND-222 领地；测试基建不动）。
- 依赖：`pydantic-settings==2.7.0`、`python-dotenv==1.0.1` **已在 `requirements.txt`**，无需新增任何依赖。

### 0.2 关键行为约束（本任务成败的核心）

**全仓库测试大量使用 `monkeypatch.setenv` 在测试函数内（即 app import 之后、请求发起之前）注入环境变量**——`test_password_auth.py`（28 处）、`test_nested_media_access.py`（23 处）、`test_qiniu_provider_factory.py`（15 处）等。这意味着现有配置语义是「**调用时/请求时懒读取**」。因此：

> **红线：Typed Settings 绝不允许改成「import 时读取并缓存的模块级单例」。** 每个 Settings 对象必须在**被使用的时刻**从环境构造（或提供等价的 per-call 读取语义），否则数十个测试会失败，且生产上「改 env + 重启单 worker」的运维语义也会被破坏。实现方式建议：提供 `get_xxx_settings()` 工厂函数（每次调用重新实例化 `BaseSettings`），调用方在函数体内调用它，**不做 lru_cache、不做模块级常量**。`app/db/session.py` 的 engine 缓存是唯一例外——它缓存的是 engine 而不是配置值，该语义原样保留。

---

## 0.5 前置条件（硬门，先确认，不满足则停下）

> 任何一条不满足，**停下并在 Linear 评论说明依赖未满足**，不要自行补做前置任务。

1. **RND-219、RND-220、RND-221、RND-222 必须已全部合入 `main`**（链前置：`{219,220,221} → 222 → 223`）。用 `git log --oneline -20` 确认对应提交存在。若任何一个未合入，停下。
2. **工作树必须干净**：`git status` 无未提交改动。若存在他人/前序任务的未提交改动，**不要基于其开工、不要自行 commit**，先请 Haisu 处理。
3. **开工方式**：从最新 `main` 直接实现（`DEV_AGENT_RULES.md`：直接在 main 上改，不建 branch，除非 Haisu 明确要求）。
4. **与 RND-224 的边界**：RND-224（架构防退化规则 + 文档）在本任务之后。本任务**不要**预先编写 import-linter/架构规则或大改 `docs/ARCHITECTURE.md`（只做 §4 要求的最小文档更新）。

---

## 1. 精确范围

### 1.1 新建文件

- `backend/app/settings.py`（或 `backend/app/settings/` 包，二选一，单文件优先——最小可审查变更）。内容为**分域** Typed Settings，基于 `pydantic-settings`（已有依赖）：

| Settings 类 | 域 | 字段（env var 名一字不改） |
|-------------|----|---------------------------|
| `DatabaseSettings` | db | `DATABASE_URL` |
| `AuthSettings` | 登录/会话 | `AUTH_MODE`、`APP_ENV`、`ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH` |
| `WecomOAuthSettings` | WeCom OAuth | `WECOM_CORP_ID`、`WECOM_AGENT_ID`、`WECOM_OAUTH_SECRET`、`ADMIN_DOMAIN` |
| `WecomCallbackSettings` | WeCom 回调 | `WECOM_CALLBACK_TOKEN`、`WECOM_CALLBACK_ENCODING_AES_KEY`、`WECOM_CORP_ID` |
| `MediaStorageSettings` | 媒体存储 | `MEDIA_STORAGE_PROVIDER`、`STORAGE_BACKEND`（fallback 别名语义保留）、`STORAGE_LOCAL_PATH`、`QINIU_*` 6 个、`MEDIA_SIGNED_URL_TTL_SECONDS`、`MEDIA_SIGNED_URL_WINDOW_SECONDS` |
| `ThumbnailSettings` | 缩略图 | `MEDIA_THUMBNAIL_ENABLED`、`MEDIA_THUMBNAIL_MAX_EDGE`、`MEDIA_THUMBNAIL_JPEG_QUALITY` |

  - 每个类配 `get_xxx_settings()` 工厂函数（**每次调用重新读环境**，见 §0.2 红线）。
  - **默认值、strip、大小写、容错解析、fail-loud/degrade 语义必须逐字对齐现状**：如 `AUTH_MODE` 非法值 → warning + `wecom`；`APP_ENV` 默认 `development`；`QINIU_TIMEOUT_SECONDS` 空 → 30.0；`STORAGE_LOCAL_PATH` 非法路径 → None（degrade）；Qiniu 必填缺失 → `QiniuConfigurationError`（错误消息文本不变）；`MEDIA_STORAGE_PROVIDER` 缺失 fallback `STORAGE_BACKEND`。凡 pydantic 默认行为与现状不一致处（如空串 vs 缺失、int 解析失败的异常类型），**以现状为准**，必要时用 validator 或普通 `os.environ` 包装保持原语义——「Typed」是手段，行为等价是目的。
  - `settings.py` **不得 import** `app.main`、任何 router、任何 service（只依赖 stdlib + pydantic），防循环依赖。

### 1.2 改造 `backend/app/main.py` → App Factory

```python
def create_app() -> FastAPI:
    app = FastAPI(title="365 WeCom Archive")
    app.add_middleware(MediaAccessNoStoreMiddleware)
    # 7 个 include_router、3 个 health 端点注册、/web/static mount
    return app

app = create_app()   # 保持 uvicorn app.main:app 完全兼容
```

- `_RedactOAuthCallbackQueryFilter` 的 addFilter、health 端点函数体、`_VersionedStaticFiles`、`_readiness_body` 逻辑**逐字保留**（可整体移入 factory 或保持模块级 helper，注册动作收入 factory 内）。
- health 端点可保持现有「装饰器绑定到 factory 内 app」的等价形式；对外路径、响应体、503 语义一字不改。
- `main.py` 完成后**只含 composition-root 职责**：imports、logging filter 安装、`create_app()` 定义与调用。不残留任何业务逻辑。

### 1.3 替换散落的 env 读取点（只换「读取来源」，不改逻辑）

对 §0.1 表中 `app/` 内每个读取点：把 `os.getenv(...)` / `os.environ.get(...)` 替换为对应 `get_xxx_settings().field`。**函数签名、返回值、异常、日志、默认值全部不变。**

- `app/db/session.py`：`_get_engine()` 内改用 `get_database_settings().database_url`，engine 缓存语义、RuntimeError 消息不变。
- `app/auth.py`：`get_auth_mode()` / `_is_production()` 换源；warning 日志文本不变。
- `app/routers/auth.py`：密码登录与 OAuth 各处换源；**空值时的 4xx/5xx 行为、RND-225 fail-closed 行为一字不改**。
- `app/routers/wecom_events.py`：3 处换源；回调验签逻辑不改。
- `app/media_storage.py`：provider 工厂换源；`_require_qiniu_env` 的 fail-loud 语义与错误文本保留（可改为从 Settings 取值后做同样校验）；`get_media_storage_provider(explicit_backend)` 的参数优先级（`MediaFile.storage_backend` 列 > 部署默认，RND-174 约束）不变。
- `app/media_thumbnails.py`：3 处换源；容错解析与默认值不变。

### 1.4 显式不在范围（严禁触碰）

- `backend/scripts/*.py`（worker/backfill CLI 的 env 读取——RND-222 领地及后续）。
- `backend/alembic/env.py`、`backend/tests/` 内的 env 读取。
- 任何路由路径、response schema、状态码、Cache-Control、tenant 隔离逻辑。
- 任何环境变量的**名字**、新增环境变量、`.env.example` 增删条目（如现有文档需要同步措辞可最小更新，见 §4）。
- DB migration、CI/CD 配置、`deploy/` systemd 单元、`requirements.txt`。
- RND-224 的架构规则/防退化工具。

---

## 2. 执行步骤（严格按顺序）

### 阶段一：锁定行为基线（不改实现）

1. `git status` 干净 + §0.5 硬门确认。
2. 全量跑一遍基线并记录：
   ```bash
   cd backend && python -m pytest -q
   ```
   重点确认这些与配置语义强相关的套件全绿：`test_http_contract.py`、`test_password_auth.py`、`test_auth.py`、`test_rnd225_auth_fail_closed.py`、`test_readiness_health_endpoint.py`、`test_qiniu_provider_factory.py`、`test_qiniu_storage.py`、`test_qiniu_https_domain.py`、`test_signed_url_window.py`、`test_generic_media_serving.py`、`test_nested_media_access.py`、`test_media_thumbnails.py`、`test_tenant_foundation.py`。若基线本身有红，先停下报告，不要在红色基线上开工。

### 阶段二：建 `app/settings.py`（纯新增，零行为变化）

3. 按 §1.1 写分域 Settings + 工厂函数。为每个域写**新单测** `tests/test_rnd_223_settings.py`：
   - 每个字段的默认值/strip/容错语义与 §1.1 对齐；
   - **懒读取语义**：`monkeypatch.setenv` 在构造前设置 → 生效；两次调用工厂之间改 env → 第二次反映新值（证明无进程级缓存）；
   - Qiniu 必填缺失 → `QiniuConfigurationError` 文本匹配（或该校验留在 media_storage 时改为集成断言）。

### 阶段三：`main.py` → `create_app()`

4. 按 §1.2 改造。立即跑：
   ```bash
   python -m pytest tests/test_http_contract.py -q
   ```
   `test_router_count`（当前预期 **33**，若 219–222 合入后契约数字有变，以届时 `test_http_contract.py` 断言为准——该文件是唯一真源，**不要改它的期望值**）、`test_route_snapshot_with_real_model_names`、`test_routers_are_registered` 必须全绿。
5. 验证 uvicorn 兼容（前台运行、按 Ctrl-C 即停，不留后台进程）：
   ```bash
   cd backend && DATABASE_URL=sqlite:// python -c "from app.main import app; print(type(app).__name__, len(app.routes))"
   ```
   输出 `FastAPI` 且路由数与契约一致即可（无需真的起 uvicorn 长驻进程）。

### 阶段四：逐域替换 env 读取（一次一个域，替换完立即跑该域测试）

6. 建议顺序（每步跑对应回归，红了立即修复再进下一域）：
   1. db 域 → `test_readiness_health_endpoint.py`；
   2. auth 域 → `test_auth.py test_password_auth.py test_rnd225_auth_fail_closed.py`；
   3. wecom-callback 域 → `test_contact_sync.py`（含回调相关）+ `grep` 确认 `wecom_events` 无遗漏；
   4. media-storage 域 → `test_qiniu_provider_factory.py test_qiniu_storage.py test_qiniu_https_domain.py test_signed_url_window.py test_generic_media_serving.py test_nested_media_access.py test_media_access_descriptor.py test_media_download.py`;
   5. thumbnail 域 → `test_media_thumbnails.py test_thumbnail_pipeline.py test_thumbnail_media_access.py`。
7. 替换完成后 grep 收口：
   ```bash
   grep -rn "os.getenv\|os.environ" backend/app --include="*.py" | grep -v settings.py
   ```
   期望：**零残留**（`app/` 内所有 env 读取都经由 `app/settings.py`；若个别点有充分理由保留——如 settings.py 自身的 validator 包装——在 Linear 评论中逐条说明）。

### 阶段五：收口（不达标不收工）

8. `make verify`（lint-diff + typecheck + build + 全量 pytest）全绿。
9. 按 §4 做最小文档同步。

---

## 3. 兼容性契约（不可违反，验收据此判定）

- **`uvicorn app.main:app` 启动方式不变**：`app.main` 模块级仍暴露名为 `app` 的 FastAPI 实例；`deploy/` 与 `docs/DEPLOYMENT.md` 的启动命令零修改。
- **路由契约不变**：`test_http_contract.py` 三大断言（count / snapshot / registered set）全绿，期望值不修改。
- **懒读取语义不变**（§0.2 红线）：`monkeypatch.setenv` 在请求前注入即生效；全部既有测试**不修改任何一行**即通过（新增 `test_rnd_223_settings.py` 除外）。
- **环境变量名、默认值、容错、fail-loud/degrade、错误消息文本全部不变。**
- **health 端点语义不变**：`/health/live` 不碰 DB；`/health/ready` 与 `/health` 的 200/503 契约、响应体 generic 化（不泄漏异常文本）不变。
- **RND-225 行为不变**：OAuth callback 日志脱敏 filter 继续安装；auth fail-closed 不弱化。
- **无新依赖、无 migration、无 CI/deploy 变更。**
- **循环依赖为零**：`app/settings.py` 不 import 任何 app 内业务模块；`python -c "import app.main"` 无 ImportError/警告。

---

## 4. 文档最小同步（本任务内完成，勿扩大）

- `docs/ARCHITECTURE.md`：在相应小节补一段（≤15 行）说明「配置统一经 `app/settings.py` 分域 Typed Settings；main.py 为 composition root（create_app）」。不重写文档结构（那是 RND-224 的事）。
- 若 `docs/DEPLOYMENT.md` 有「配置读取自环境变量」相关表述与新结构冲突，做最小措辞修正；启动命令不改。

---

## 5. 硬性约束（实现 agent 自身也要守）

- **行为等价优先**：任何状态码、响应字段、日志文本、异常类型、默认值的变化都视为回归。
- **既有测试一行不改**：如果某个既有测试因你的改动变红，那是你的实现错了，不是测试错了。唯一允许新增的测试文件是 `tests/test_rnd_223_settings.py`。
- **一次一个域**：严格按 §2 阶段四的域顺序推进，禁止一把梭全量替换后再调试。
- 不引入后台进程；命令一律前台运行。
- **绝不 `git commit` / `git push` / 开 PR**——全部 git 操作由 Haisu 本人执行（项目硬规则）。完成后只报告，不提交。
- 参照 `DEV_AGENT_RULES.md`；范围疑问停下问 Haisu，不自行扩大。

---

## 6. 收尾动作

- 在 Linear RND-223 写一条评论，包含：
  - `app/settings.py` 的域划分与工厂函数清单；
  - 每个被替换 env 读取点的「文件 + 函数」对照表；
  - grep 收口结果（`app/` 内 os.getenv 残留 = 0 或逐条豁免理由）；
  - `make verify` 结论、路由契约数字、`uvicorn app.main:app` 兼容验证方式；
  - 明确声明「未改任何既有测试、未改任何环境变量名」。
- **不 commit、不 push**；交还 Haisu，由 QA 智能体按 `.workbuddy/prompts/rnd-223-qa-prompt.md` 独立验收后，Haisu 决定提交。
