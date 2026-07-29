# RND-318 开发 agent 执行提示词
> 面向开发 agent（单人端到端实现 RND-318 / C3-1 留存配置）。
> 本文件即你的完整 brief。全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）
为「数据留存策略」提供**留存配置（保留天数 / 锁定策略）的读写端点**，配置以**租户级**粒度存储于 F0 配置中心的 KV。
后端-only（标签 `backend`，Epic RND-272 C3 数据留存策略 的 C3-1 子票）；**不**实现到期锁定/清理逻辑（那是 C3-2 / RND-319 的职责）。**不**自建配置中心（属 F0 / RND-244）。

## 二、精确落点 / 依赖
### 依赖 F0（RND-244）—— 配置中心 KV 存储（硬前置闸门）
- RND-318 在 Linear 为 `[BLOCKED: F0]`，描述明确 `Dependencies F0`，且 scope 为「留存配置表/**租户设置**」、non-goals 为「不做跨租户统一策略（每租户独立）」、被 A8/A9 引用 → 设计意图是**每租户一份留存策略，存于 F0 配置中心租户级 KV**（与 RND-304 的 `onboarding_completed` 同属 F0 租户 KV 范畴）。
- **已核实 F0 配置中心（RND-244）当前未合并**（grep 全仓确认）：
  - `backend/app/db/models.py` 无通用 config 表（仅有 `TenantWecomConfig`，是企微连接配置，非 F0 配置中心）；
  - `backend/app/services/` 无 `config_service`；
  - `_FLAT_SERVICE_MODULES`（`tests/test_architecture_boundary.py`）无 config 模块；
  - 无 `backend/app/routers/config.py` / `routers/settings.py`（后者 RND-302 尚未合并）；
  - `app/audit.py:50` 的 `TENANT_CONFIG = "tenant_config"` 仅是审计动作类型常量，非配置存储；
  - `app/settings.py` 的 `Settings` 类是 env 级 `pydantic-settings` 封装，非租户 KV。
- **禁止**：自建 `retention_policies` 表 / 迁移 / 配置服务模块（越界即判失败，且会撞 RND-287/297/306 的 `0020` 迁移定序雷）。
- **因此 F0 是硬前置**：开工必须校验 F0 配置服务已合并，否则 **STOP 并报告 BLOCKED**（见第三节 Phase 0）。

### 落点（F0 已合并的前提下）
- **发现 F0 配置服务 API（Phase 0，必做）**：在 `app/` 下 grep `def get_config` / `def set_config` / `class ConfigStore` / `ConfigService` / `routers/config`；常见落点 `app/config_service.py` 或 `app/services/config_service.py`。读取其公开签名（期望形如 `get_config(tenant_id, key, default=...)` / `set_config(tenant_id, key, value)`，或同语义的 `config_get`/`config_set`）。**以实际模块为准**，下文用 `get_config`/`set_config` 作占位名。
- `backend/app/routers/retention.py`（**新建**，distinct 文件名，避免与 RND-302 未来的 `settings.py` 撞名）：`retention_router = APIRouter()` + 2 端点。
- `backend/app/main.py`：在现有 `app.include_router(...)` 列表（L97–108 区域）追加 `app.include_router(retention_router, prefix="/api/admin/settings")`。
- `backend/app/schemas/retention.py`（**新建**）：`LockStrategy` / `RetentionConfigOut` / `RetentionConfigUpdate`。
- `backend/tests/test_http_contract.py`：三处同步（见第五节）。
- **不**新建 DB 模型 / **不**写迁移 / **不**改 `_FLAT_SERVICE_MODULES`（存储归 F0，RND-318 零架构边界改动）。

## 三、阶段一：复现 + 测量（RED + Phase 0 硬闸门）
1. 启动环境；解释器用 `../.venv/bin/python`，跑命令前 `cd backend && set -a && source .env && set +a`（本仓 `app/settings.py` 的 `DatabaseSettings` 是裸 `BaseSettings`，**不自动读 `.env`**，必须 source 才能拿到带密码的 `DATABASE_URL`；无 DB 时涉及 DB 的测试按 `if not DATABASE_URL: skip` 处理）。
2. **Phase 0 — F0 合并校验（最关键，先做）**：
   ```bash
   grep -rn "def get_config\|def set_config\|class ConfigStore\|ConfigService\|routers/config" app/ | grep -viE "test"
   ```
   - **若 grep 无命中** → F0 未合并。**STOP，不写任何实现代码**，输出 BLOCKED 报告（列证据：无 config_service / 无 routers/config.py / models.py 无通用 config 表），并向用户说明「RND-318 须等 F0/RND-244 合并后由开发 agent 重跑本提示词」。结束。
   - **若 grep 命中** → 读取实际模块与 `get_config`/`set_config` 真实签名，后续以真实符号替换下方占位名。
3. （F0 合并后）基线：`GET /api/admin/settings/retention` 应返回 fail-safe 默认值（`retention_days=365`、`lock_strategy="lock"`）；`PUT` 写入后 `GET` 返回新值。

## 四、阶段二：实现（GREEN，最小变更）
### 路 A — schema（新建 `schemas/retention.py`）
```python
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

class LockStrategy(str, Enum):
    LOCK = "lock"          # 到期后锁定（保留数据，禁止访问/导出）—— 默认，最安全
    DELETE = "delete"      # 到期后删除
    ANONYMIZE = "anonymize"  # 到期后匿名化（去标识）

class RetentionConfigOut(BaseModel):
    retention_days: int
    lock_strategy: LockStrategy

class RetentionConfigUpdate(BaseModel):
    # 部分更新：任一字段可省略
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    lock_strategy: Optional[LockStrategy] = None
```

### 路 B — retention 路由（新建 `routers/retention.py`）
```python
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from app.auth import require_role
from app.schemas.retention import (
    LockStrategy, RetentionConfigOut, RetentionConfigUpdate,
)
# F0 配置服务（RND-244 合并后存在；以实际模块名为准）：
from app.config_service import get_config, set_config  # 或 app.services.config_service

retention_router = APIRouter()

_KEY_DAYS = "retention_days"
_KEY_STRATEGY = "retention_lock_strategy"
_DEFAULT_DAYS = 365
_DEFAULT_STRATEGY = LockStrategy.LOCK


def _read_config(tenant_id: str) -> RetentionConfigOut:
    """读取租户留存配置；缺失时返回 fail-safe 默认值，绝不抛 500。"""
    try:
        days_raw = get_config(tenant_id, _KEY_DAYS, default=None)
        days = int(days_raw) if days_raw not in (None, "") else _DEFAULT_DAYS
    except Exception:
        days = _DEFAULT_DAYS
    try:
        strat_raw = get_config(tenant_id, _KEY_STRATEGY, default=None)
        strat = LockStrategy(strat_raw) if strat_raw else _DEFAULT_STRATEGY
    except Exception:
        strat = _DEFAULT_STRATEGY
    return RetentionConfigOut(retention_days=days, lock_strategy=strat)


@retention_router.get("/retention", response_model=RetentionConfigOut)
def get_retention(auth=Depends(require_role())):
    """读取当前租户留存配置（任意 admin 可读）。"""
    _, tenant_id = auth
    return _read_config(tenant_id)


@retention_router.put("/retention", response_model=RetentionConfigOut)
def update_retention(
    payload: RetentionConfigUpdate,
    auth=Depends(require_role("admin", "owner")),
):
    """更新留存配置（敏感操作，限 admin/owner）。部分更新：只写提供的字段。"""
    _, tenant_id = auth
    if payload.retention_days is not None:
        set_config(tenant_id, _KEY_DAYS, str(payload.retention_days))
    if payload.lock_strategy is not None:
        set_config(tenant_id, _KEY_STRATEGY, payload.lock_strategy.value)
    return _read_config(tenant_id)
```
- **租户隔离（fail-closed）**：`tenant_id` 仅来自 `require_role()` 解包，**绝不**接受请求参数。
- **fail-safe 读取**：F0 KV 缺键 / 解析异常一律回落默认值，不抛 500。
- **部分更新**：`PUT` 只写提供的字段，未提供的保留原值（先读后写可由 F0 层保证；若 F0 `set_config` 为全量覆盖，则先 `_read_config` 取当前再合并再写）。
- **最小暴露**：响应只含 `retention_days` / `lock_strategy`，不泄露 F0 KV 其它键。

### 路 C — main.py 注册
在 `app.include_router(...)` 列表（现有 `users_router` / `media_library_router` 之后，L104–105 附近）追加：
```python
from app.routers.retention import retention_router
app.include_router(retention_router, prefix="/api/admin/settings")
```
> 注：用 `prefix="/api/admin/settings"` + 路由 `/retention` → 完整 URL `GET/PUT /api/admin/settings/retention`。**不要**复用 RND-302 未来的 `settings.py` 文件名（避免并行未合并冲突），本票独立 `retention.py`。

## 五、阶段三：验证（GREEN + 回归 + make verify）
1. 功能验证（F0 合并后）：
   - `GET /api/admin/settings/retention` 默认返回 `{retention_days:365, lock_strategy:"lock"}`；
   - `PUT` 改 `retention_days=180`、`lock_strategy="delete"` 后 `GET` 返回新值；
   - 部分更新：`PUT {retention_days:90}` 后 `lock_strategy` 保持原值；
   - 跨租户不串：租户 B 仍为默认值（不读 A 的写入）。
2. 契约测试三处同步（`backend/tests/test_http_contract.py`，**用 grep 定位，勿死磕行号**）：
   - `assert route_count == 49` → 改为「读当前 N，+2」实际值（**勿硬编码**：今天基线 49，实现时若已有兄弟票合并会更高，用 `make verify` 报错给出的真实 count 回填）；注释 `# RND-318: +2 retention settings routes.`
   - 路由路径集合（搜包含 `/api/admin/users` 的 list）追加 `"/api/admin/settings/retention"`；
   - snapshot 集合（搜 `("/api/admin/users", ...)` 附近）追加
     `("/api/admin/settings/retention", frozenset({"GET"}), "RetentionConfigOut", "None")`、
     `("/api/admin/settings/retention", frozenset({"PUT"}), "RetentionConfigOut", "None")`。
3. 回归：`make verify` 全绿；重点 `test_http_contract.py` + `test_architecture_boundary.py`（无新建模型 / 迁移 / 服务模块 → **不改** `_FLAT_SERVICE_MODULES`，边界仍绿）。
4. 失败先修实现，不迁就测试（除非测试断言旧路径，需标注）。

## 六、硬约束（违反即判失败）
- **不**自建留存表 / 迁移 / 配置服务模块（属 F0，越界即失败）。
- **F0 未合并 → STOP 报告 BLOCKED**，不自行补配置中心、不自行建表。
- 不改既有 URL / status / body 形状（新增端点除外）；租户隔离 fail-closed（`tenant_id` 仅来自会话）。
- `response_model` 之外不暴露字段；不泄露 F0 KV 其它键。
- 不引 React / 不改 i18n / 不动前端（D1 冻结 SSR + 原生 JS；本票纯后端）。
- 不 commit / push。

## 七、收尾（交付物）
向用户交付：RED 基线数字、GREEN 数字、改动文件清单、`make verify` 日志、未提交声明、**F0 就绪状态说明**（含实际使用的 F0 配置服务模块名与 `get_config`/`set_config` 签名），以及「若当前 F0 未合并则本票停在 Phase 0 BLOCKED」的结论。
