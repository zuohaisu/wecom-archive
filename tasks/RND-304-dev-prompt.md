# RND-304 开发 agent 执行提示词
> 面向开发 agent（单人端到端实现 RND-304 / A9-3 标记首次完成）。
> 本文件即你的完整 brief。全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）
为「首次配置向导」提供 `first_run` 标志的**存储 + 读写端点**：向导完成后置位，使下次跳过向导。
仅动后端；不实现向导页面本身（属 A9-1/A9-2）；**不**自建配置中心（属 F0 / RND-244）。

## 二、精确落点 / 依赖
### 依赖 F0（RND-244）—— 配置中心 KV 存储
- RND-304 在 Linear 为 `[BLOCKED: F0]`。**已核实 F0 配置中心（RND-244）当前未合并**：
  - `backend/app/db/models.py` 无通用 config 表（仅有 `TenantWecomConfig`，是企微连接配置，非 F0 配置中心）；
  - `backend/app/services/` 无 `config_service`（仅有 decrypt_worker / media_access / timeline_service / usageservice 等）；
  - `backend/tests/test_architecture_boundary.py:70` 的 `_FLAT_SERVICE_MODULES` 无 config 模块；
  - 无 `backend/app/routers/config.py` / `routers/settings.py`。
- `first_run` 标志**必须**存于 F0 配置中心的**租户级 KV**：键 `onboarding_completed`，值 `"true"`/`"false"`，默认 `"false"`。
- **禁止**：自建表 / 迁移 / 配置服务模块（越界即判失败）。

### 落点（F0 已合并的前提下）
- **发现 F0 配置服务 API（Phase 0，必做）**：在 `app/` 下 grep `def get_config` / `def set_config` / `class ConfigStore` / `ConfigService`；常见落点 `app/config_service.py` 或 `app/services/config_service.py`。读取其公开签名（期望形如 `get_config(tenant_id, key, default=...)` / `set_config(tenant_id, key, value)`，或同语义的 `config_get`/`config_set`）。**以实际模块为准**，下文用 `get_config`/`set_config` 作占位名。
- `backend/app/routers/onboarding.py`（**新建**）：两个端点 + `APIRouter()`。
- `backend/app/main.py`：在现有 `app.include_router(...)` 列表中追加 onboarding router（仿 RND-286 / RND-295 的注册形态）。
- `backend/app/schemas/onboarding.py`（**新建**）：`OnboardingStatusOut` / `OnboardingCompleteOut`。
- `backend/tests/test_http_contract.py`：三处同步（见第五节）。

## 三、阶段一：复现 + 测量（RED）
1. **环境前置（必读，避免 `python`/`alembic` exit 127）**：本仓无系统级 `python`/`alembic`，必须用 venv；且 `.env` 不会被自动加载，须手动 `source`：
   ```bash
   cd backend && set -a && source .env && set +a
   PY=../.venv/bin/python   # 或 .venv/bin/python 绝对路径
   ```
   之后所有 `python` / `alembic` / `pytest` / `make verify` 均在该 shell 内执行（`source .env` 才能拿到带密码的 `DATABASE_URL`，否则 `alembic check` 连不上 DB）。
2. 校验依赖（用上面的 `$PY`）：
   - `$PY -c "import app.config_service" 2>/dev/null && echo F0_READY || echo F0_MISSING`
   - 或 `$PY -m alembic check`（缺配置表会报错）。
   若 `F0_MISSING` / import 失败 → 记录「依赖未就绪」，按下文硬约束停下报告，**不自行补配置中心、不自行建表**。
3. （F0 合并后）基线：`GET /api/onboarding/status` 应返回 `first_run=true`（默认未配置 → 视为未完成）。

## 四、阶段二：实现（GREEN，最小变更）
### 路 A — onboarding 路由（新建 `routers/onboarding.py`）
```python
from fastapi import APIRouter, Depends
from app.auth import require_role
from app.schemas.onboarding import OnboardingStatusOut, OnboardingCompleteOut
# F0 配置服务（RND-244 合并后存在；以实际模块名为准）：
from app.config_service import get_config, set_config  # 或 app.services.config_service

router = APIRouter()
_KEY = "onboarding_completed"


@router.get("/api/onboarding/status", response_model=OnboardingStatusOut)
def onboarding_status(auth=Depends(require_role())):
    """租户级首次运行标志读取。默认未完成 → first_run=true。"""
    _, tenant_id = auth
    try:
        done = (get_config(tenant_id, _KEY, default="false") or "false").strip().lower() == "true"
    except Exception:
        # fail-safe：读取异常一律按「未配置」处理，绝不抛 500
        done = False
    return OnboardingStatusOut(first_run=not done)


@router.post("/api/onboarding/complete", response_model=OnboardingCompleteOut)
def onboarding_complete(auth=Depends(require_role("admin", "owner"))):
    """向导完成后置位标志。幂等：重复调用仍返回 first_run=false。"""
    _, tenant_id = auth
    set_config(tenant_id, _KEY, "true")
    return OnboardingCompleteOut(first_run=False)
```
- **零枚举 / fail-safe**：`get_config` 默认 `"false"` → `first_run=true`；任何读取异常都按「未配置」处理（不抛 500）。
- **幂等**：`complete` 重复调用仍返回 `first_run=false`，不报错、不重复副作用。
- **租户隔离（fail-closed）**：`tenant_id` 仅来自 `require_role()` 解包，**绝不**接受请求参数。
- **最小暴露**：状态端点只返回 `first_run` 布尔，不泄露配置中心其它键 / 原始值。

### 路 B — schema（新建 `schemas/onboarding.py`）
```python
from pydantic import BaseModel

class OnboardingStatusOut(BaseModel):
    first_run: bool

class OnboardingCompleteOut(BaseModel):
    first_run: bool
```

### 路 C — main.py 注册
- 在 `app.include_router(...)` 列表中追加（仿既有 router 注册）：
  ```python
  from app.routers.onboarding import router as onboarding_router
  app.include_router(onboarding_router)
  ```

### 「下次跳过」消费端（协调，非严格 scope）
- 机制由 `GET /api/onboarding/status` 提供；控制台 shell / 向导入口读取 `first_run` 决定跳转（未完成→向导，已完成→总览）。
- RND-304 **不**新建向导 SSR 页面（属 A9-1/A9-2）。如需最小入口，可在 `routers/web.py` 追加 `GET /admin/onboarding` 重定向（已完成→`/admin/conversations`，未完成→`/admin/onboarding/start`），但向导页模板由兄弟票提供；**此重定向列为 optional，且必须幂等、不破坏既有路由、不引入新依赖**。本票 AC 不依赖该重定向。

## 五、阶段三：验证（GREEN + 回归 + make verify）
1. 功能验证（F0 合并后）：
   - `GET /api/onboarding/status` 默认 `first_run=true`；
   - `POST /api/onboarding/complete` 后 `first_run=false`；
   - 再次 GET 仍 `false`（幂等）；
   - 跨租户不串：租户 B 仍 `first_run=true`。
2. 契约测试三处同步（`backend/tests/test_http_contract.py`）：
   - L325 `assert route_count == 49` → 改为「读当前 N，+2」实际值（**勿硬编码死值**：今天基线 49，实现时若已有兄弟票合并会更高，用 `make verify` 报错给出的真实 count 回填）；
   - path 集合（L336-377）追加 `"/api/onboarding/status"`、`"/api/onboarding/complete"`；
   - snapshot（L410-432）追加
     `("/api/onboarding/status", frozenset({"GET"}), "OnboardingStatusOut", "None")`、`("/api/onboarding/complete", frozenset({"POST"}), "OnboardingCompleteOut", "None")`。
3. 回归：在 §三 的环境 shell 内（已 `source .env` + venv）跑 `make verify` 全绿；重点 `test_http_contract.py` + `test_architecture_boundary.py`（无新建模型 / 迁移 / 服务模块 → **不改** `_FLAT_SERVICE_MODULES`，边界仍绿）。
4. 失败先修实现，不迁就测试（除非测试断言旧路径，需标注）。

## 六、硬约束（违反即判失败）
- **不**自建 `first_run` 表 / 迁移 / 配置服务模块（属 F0，越界即失败）。
- F0 未合并 → **停下报告**，不自行补配置中心、不自行建表。
- 不改既有 URL / status / body 形状（新增端点除外）；租户隔离 fail-closed。
- `response_model` 之外不暴露字段；不泄露配置中心其它键。
- 不引 React / 不改 i18n / 不动向导页面（D1 冻结 SSR + 原生 JS）。
- 不 commit / push。

## 七、收尾（交付物）
向用户交付：RED 基线数字、GREEN 数字、改动文件清单、`make verify` 日志、未提交声明、F0 就绪状态说明（含实际使用的 F0 配置服务模块名与 `get_config`/`set_config` 签名）。
