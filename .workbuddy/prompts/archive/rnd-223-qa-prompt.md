# RND-223 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-223 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-223-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。
> 权威依据：Linear 工单 **RND-223**（`create_app()` + 分域配置对象；保持 `uvicorn app.main:app` 兼容；消灭散落 `os.getenv`；`main.py` 只保留 composition-root 职责）+ 下述代码硬约束。

---

## 0. 验收依据

- **Linear RND-223 目标（必须全部达成）**：
  1. 存在 `create_app()` App Factory，`main.py` 只保留 composition-root 职责；
  2. 配置收敛为**按域分组的 Typed Settings**（`app/settings.py`）；
  3. `app/` 内散落 `os.getenv` / `os.environ` 读取被消灭（或逐条豁免且理由成立）；
  4. `uvicorn app.main:app` 启动方式完全兼容。
- **非目标不可被破坏**：环境变量名不改、默认值/容错/fail-loud 语义不改、路由契约不改、health 语义不改、RND-225 fail-closed 不弱化、无新依赖/migration/CI 变更、不触碰 `backend/scripts/`、`alembic/`、`tests/` 的 env 读取（RND-222 领地及测试基建）。

---

## 1. 前置检查（先确认环境，再验收）

1. **链前置已合入 `main`**：`git log --oneline -30` 确认 RND-219、RND-220、RND-221、RND-222 的提交存在。任一缺失 → **BLOCKED**，在 Linear 评论说明，不验收。
2. 开发 agent 声称已通过 `make verify`；无论如何，本 agent **必须自己复跑**一遍作为基线。
3. 拿到开发 agent 的 Linear 评论（域划分清单、替换对照表、grep 收口结果、豁免清单）。缺失 → 自行推导，并在报告标注「开发交付物不完整」。
4. 确认工作区为待验收改动（未 commit 属正常——本项目 git 操作一律由 Haisu 执行；QA 不 commit/push）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### A. 工单目标达成

- **C1 App Factory 存在且 main.py 是纯 composition root（代码审查）**：`app/main.py` 定义 `create_app() -> FastAPI` 并在模块级 `app = create_app()`；文件内只有 imports、logging filter 安装、factory 定义/调用与其直接 helper（health 函数体、`_VersionedStaticFiles`、`_readiness_body`、`_RedactOAuthCallbackQueryFilter` 属 composition-root 职责，允许保留）；**无任何业务逻辑/DB 查询/schema 定义**。
- **C2 uvicorn 兼容（行为）**：
  ```bash
  cd backend && DATABASE_URL=sqlite:// python -c "from app.main import app; from fastapi import FastAPI; assert isinstance(app, FastAPI); print(len(app.routes))"
  ```
  import 成功、类型正确；`deploy/`、`docs/DEPLOYMENT.md`、`scripts/` 中 `app.main:app` 启动命令**零修改**（`git diff` 证明）。
- **C3 分域 Typed Settings（代码审查）**：`app/settings.py` 按域分组（至少覆盖 db / auth / WeCom OAuth / WeCom callback / media-storage / thumbnail），每域一个类 + 工厂函数；`settings.py` 不 import `app.main`、router、service（头部 import 逐行核对）。
- **C4 散落 env 读取已消灭（grep 铁证）**：
  ```bash
  grep -rn "os\.getenv\|os\.environ" backend/app --include="*.py" | grep -v "settings.py"
  ```
  期望**零输出**；每条残留必须能在开发评论的豁免清单中找到且理由成立（如 settings.py 自身包装），否则 FAIL。
- **C5 范围红线（grep + git diff）**：`backend/scripts/`、`backend/alembic/`、`backend/tests/`（除新增 `test_rnd_223_settings.py`）的 env 读取**未被触碰**；`git diff --stat` 中不出现这些路径的相关改动。

### B. 行为等价（核心风险区）

- **C6 懒读取语义保持（本任务最高风险，行为 + 代码双查）**：
  - 代码审查：`app/settings.py` 无模块级 Settings 单例、无 `lru_cache` / `functools.cache` 包裹工厂函数；调用方在**函数体内**调用 `get_xxx_settings()`（而非模块级常量）。
  - 行为铁证：**全部使用 `monkeypatch.setenv` 的既有测试不改一行仍全绿**——重点抽查 `test_password_auth.py`（28 处 setenv）、`test_nested_media_access.py`（23 处）、`test_qiniu_provider_factory.py`（15 处）、`test_generic_media_serving.py`、`test_media_download.py`。
  - 补充验证（可自写临时脚本，跑完删除）：同进程内两次调用某工厂之间修改 `os.environ`，第二次读到新值。
- **C7 既有测试零修改（git diff 铁证）**：`git diff --stat backend/tests/` 仅允许出现**新增**的 `test_rnd_223_settings.py`；任何既有测试文件被修改 → 直接 FAIL（这是「行为等价」的最强证据链）。
- **C8 路由契约不变**：`python -m pytest tests/test_http_contract.py -q` 全绿；`test_router_count` 期望值未被修改（`git diff` 确认该文件无改动）。
- **C9 health 语义不变**：`test_readiness_health_endpoint.py` 全绿；`/health/live` 不碰 DB、`/health(/ready)` 200/503 契约与 generic 响应体不变。
- **C10 auth / RND-225 行为不变**：`test_auth.py`、`test_password_auth.py`、`test_rnd225_auth_fail_closed.py` 全绿；OAuth callback 日志脱敏 filter 仍被安装（代码审查 `main.py`）；`AUTH_MODE` 非法值仍 warning + 回退 `wecom`（对照 settings/auth 代码）。
- **C11 media-storage 语义不变**：`test_qiniu_provider_factory.py`、`test_qiniu_storage.py`、`test_qiniu_https_domain.py`、`test_signed_url_window.py`、`test_generic_media_serving.py` 全绿；抽查代码确认：Qiniu 必填缺失仍抛 `QiniuConfigurationError` 且消息文本不变、`STORAGE_LOCAL_PATH` 非法仍 degrade None、`MEDIA_STORAGE_PROVIDER` 缺失仍 fallback `STORAGE_BACKEND`、`QINIU_TIMEOUT_SECONDS` 默认 30.0。
- **C12 环境变量名与默认值零变化（代码审查）**：对照开发评论的替换对照表，逐个 env var 名在 `git diff` 前后一致；未新增/未删除/未重命名任何环境变量。
- **C13 新增 settings 单测质量**：`tests/test_rnd_223_settings.py` 存在且全绿；至少覆盖每域的默认值语义 + 懒读取语义（两次构造间改 env 生效）。若缺失懒读取断言，标注为缺口（不阻塞但记录）。

### C. 回归与收口

- **C14 全量回归**：`cd backend && python -m pytest -q` 全绿。
- **C15 `make verify` 全绿**（lint-diff + typecheck + build + 全量 pytest）。
- **C16 无新依赖 / 无 migration / 无 CI・deploy 变更（git diff）**：`requirements.txt`、`pyproject.toml`、`alembic/versions/`、`.github/`、`deploy/` 全部无改动。
- **C17 无循环依赖 / import 副作用**：`DATABASE_URL=sqlite:// python -c "import app.settings; import app.main"` 无异常；确认 `import app.settings` 本身**不读取**任何必填 env（缺 `DATABASE_URL` 时 import 不炸，只有调用工厂才炸——与现状 `_get_engine()` 懒失败语义一致）。

---

## 3. 测试方法

- **主命令**（`cd backend` 后）：
  ```bash
  python -m pytest -q                                   # C14 全量
  python -m pytest tests/test_http_contract.py tests/test_readiness_health_endpoint.py tests/test_auth.py tests/test_password_auth.py tests/test_rnd225_auth_fail_closed.py tests/test_qiniu_provider_factory.py tests/test_qiniu_storage.py tests/test_qiniu_https_domain.py tests/test_signed_url_window.py tests/test_generic_media_serving.py tests/test_nested_media_access.py tests/test_media_access_descriptor.py tests/test_media_download.py tests/test_media_thumbnails.py tests/test_thumbnail_pipeline.py tests/test_rnd_223_settings.py -q   # 重点域
  make verify                                           # C15（仓库根目录）
  ```
- **代码审查项（C1/C3/C4/C5/C7/C8/C12/C16/C17）**：直接读 `git diff`、`app/settings.py`、`app/main.py` 与各替换点头部/函数体，逐项核对。
- **grep 铁证（C4/C5）**：命令见清单内，输出原样贴入报告。

---

## 4. 硬性约束（验收 agent 自身也要守）

- **不修改任何实现代码与任何测试**；只读、断言、跑测试。若必须改代码才能验证，那是「待测代码缺口」，判 FAIL/缺口而非自己补。
- 临时验证脚本（如 C6 补充验证）跑完必须删除，不留在工作区。
- **绝不 `git commit` / `git push`**（项目硬规则：git 操作一律由 Haisu 本人执行）；只输出验收结论与证据。
- 不引入后台进程；命令一律前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-223 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：HEAD <sha> · 219/220/221/222 已合入：是/否 · make verify：通过/失败
grep 收口：app/ 内 os.getenv|os.environ 残留 <n> 条（豁免 <m> 条，理由成立/不成立）

| 编号 | 验收点 | 结果 | 证据（实测/命令/代码位置） |
|------|--------|------|---------------------------|
| C1   | App Factory + composition root | PASS | main.py 审查 |
| C2   | uvicorn app.main:app 兼容 | PASS | import 验证 + deploy 零改动 |
| C3   | 分域 Typed Settings | PASS | settings.py 审查 |
| C4   | 散落 env 读取消灭 | PASS | grep 零输出 |
| C5   | scripts/alembic/tests 未触碰 | PASS | git diff |
| C6   | 懒读取语义保持 | PASS | setenv 测试全绿 + 无缓存审查 |
| C7   | 既有测试零修改 | PASS | git diff --stat tests/ |
| C8   | 路由契约不变 | PASS | test_http_contract 全绿 |
| C9   | health 语义不变 | PASS | ... |
| C10  | auth/RND-225 不变 | PASS | ... |
| C11  | media-storage 语义不变 | PASS | ... |
| C12  | env var 名/默认值零变化 | PASS | 对照表核对 |
| C13  | settings 单测质量 | PASS/缺口 | ... |
| C14  | 全量回归 | PASS | pytest -q |
| C15  | make verify | PASS | ... |
| C16  | 无新依赖/migration/CI 变更 | PASS | git diff |
| C17  | 无循环依赖/import 副作用 | PASS | import 验证 |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 非目标（env 名/worker scripts/RND-224 规则）确认未触碰 → 视为 PASS 非缺陷。
- 其他观察到的限制：<…>

### 结论与建议
- 可交 Haisu 提交 / 需返工（列出必须修的项）/ 阻塞（缺 219–222）。
```

- 若某条无法复现（环境原因），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；实现确有问题则判 FAIL 并给可复现证据。
- 最终把报告作为 Linear RND-223 评论贴出；**状态与提交交还 Haisu 决策**（QA 不 commit/push，本项目所有 git 操作由 Haisu 本人执行）。
