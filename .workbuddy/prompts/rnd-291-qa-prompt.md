# RND-291 QA / 验收 agent 提示词
> 面向独立测试/QA agent。只读言、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-291-execution-prompt.md` 产出的改动。
> 标识符加反引号；D1 冻结（本票纯后端，不涉及前端改动验收）。

## 一、验收目标
确认 `GET /api/admin/media` 已落地：租户内 `media_files` 列表 + `file_type`/时间筛选 + 分页 + 安全元数据响应；类型/时间筛选正确；租户隔离成立；零泄露（无存储引用/sdkfileid）；`routers/media.py` 既有媒体服务路由、WeCom、登录、审计、契约全部零回归。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 后端（列表/筛选）
- [ ] B1 端点存在且门禁正确 —— 证据：`curl -b <admin cookie> /api/admin/media` → 200；无 cookie → 401；低权角色 → 403；`readonlyaudit` 角色 → 200。grep 确认 `Depends(require_role())`（`routers/media_library.py`）。
- [ ] B2 类型筛选正确 —— 证据：`?file_type=image` 返回项 `file_type` 全为 `image`；`?file_type=image,voice` 仅两类；非法值（如 `?file_type=foo`）→ 400 且 detail 含 `unknown file_type`。
- [ ] B3 时间筛选正确 —— 证据：`?days=30` 返回项 `created_at` 均在近 30 天；`?since=/?until=` ISO 区间生效；默认（无参）返回全部时间。抽查 DB：`select count(*) from media_files where tenant_id=<T> and created_at >= now()-interval '30 day'` 与响应 `total` 一致。
- [ ] B4 分页正确 —— 证据：`limit=10&offset=0` 返回 ≤10 项；`has_more` 为 `len(items)==limit`；`offset` 翻页不重复/不漏（比对 `id` 序列）。
- [ ] B5 派生字段合理 —— 证据：`items[].name` 为关联 `archive_messages.structured_content` 的 `title`/`filename`/`items[0].title` 之一（或 None）；`msgtime`/`room_id`/`message_id` 来自 JOIN 的父消息；`has_thumbnail` 与 `thumbnail_ref` 非空一致。

### 安全 / 零泄露（SF-1，硬门槛）
- [ ] L1 响应无敏感列 —— 证据：用 `jq '.. | keys?'` 或路径扫描，确认**不存在** `sdkfileid` / `local_path` / `storage_ref` / `oss_key` / `bucket` / `migration_error` / `checksum_sha256` / `thumbnail_ref`(原值) / `playback_ref` / `migration_status` / `migration_attempted_at`。仅允许 `storage_backend`（类型串）与 `has_thumbnail`（布尔）。
- [ ] L2 租户隔离 fail-closed —— 证据：tenant B 的 cookie 请求，响应所有项对应 `media_files.tenant_id == B`；尝试用 tenant B cookie + 猜测的其它租户 `id` 不会返回跨租户数据（不存在接受 tenant_id 的请求参数）。grep 确认 `tenant_id` 仅来自 `require_role()` 返回值，WHERE 带 `MediaFile.tenant_id == tenant_id`。
- [ ] L3 无审计写入（scope 守门）—— 证据：grep `routers/media_library.py` 与 `schemas/media_library.py` 不含 `write_audit` / `import app.audit` / `AuditLog`；列表查询不 INSERT audit_logs（可在测试 DB 前后 `count(audit_logs)` 对比，应不变）。

### 全局契约（回归守门）
- [ ] C1 路由数基线同步 —— 证据：`test_http_contract.py:325` `route_count == <N+1>`（N 为实现时真实值，A6-1 加 1 条）；`test_routers_are_registered` 的 expected 含 `/api/admin/media`；`test_route_snapshot_with_real_model_names` 含 `("/api/admin/media", frozenset({"GET"}), "MediaLibraryPage", "None")`。
- [ ] C2 既有媒体路由不变 —— 证据：`routers/media.py` 未改动（git diff 无该文件）；`/api/conversations/.../media/access` 仍 200 且 `Cache-Control: no-store`（`MediaAccessNoStoreMiddleware` 路径正则未受影响）。
- [ ] C3 租户隔离 / i18n —— 证据：无 i18n 改动；既有端点可达性不变。
- [ ] C4 `make verify` 全绿 —— 证据：lint-diff + typecheck + build + test 全绿（含 `test_architecture_boundary.py` —— 本票**未**新建 flat service 模块，故该测试不受影响）。
- [ ] C5 `alembic check` 绿 —— 证据：无新 migration；`AuditLog` 表存在性仅作前置校验，不引入 schema 变更。

## 三、回归套件（必须全绿）
`make verify` + 重点：`test_http_contract.py`、`test_architecture_boundary.py`、`test_password_auth.py`、`test_auth.py`、`test_media*.py`（若存在）。

## 四、智能路由判定（每轮必给）
- 源码有 Bug（筛选/隔离/泄露）→ 反馈开发 agent 修复，附错误 + 失败测试 + 期望；不自行改实现。
- 测试代码有 Bug（断言旧路径，如 snapshot 未同步）→ 可自行修正测试（仅当断言旧路径，须标注）。
- 全部通过 → 报告 SUCCESS，附 RED→GREEN 对比。
最多 2 轮：第1轮修复，第2轮回归；仍不过则标注遗留。

## 五、交付报告格式
```
RND-291 验收结论：PASS / FAIL
RED 基线：端点不存在(404) / route_count=46
GREEN：200 + 过滤/隔离证据
回归：make verify ___（绿/红）
契约：___ 不变
安全：L1/L2/L3 ___（通过/失败）
遗留：___
```
