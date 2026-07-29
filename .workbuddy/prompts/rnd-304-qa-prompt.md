# RND-304 QA / 验收 agent 提示词
> 面向独立测试 / QA agent。只读言、不改实现、不 commit / push。
> 验收对象：开发 agent 按 `rnd-304-execution-prompt.md` 产出的改动。

## 一、验收目标
确认 `first_run` 标志的**存储 + 读写端点**正确：默认未完成 → 完成后置位 → 幂等 → 租户隔离；且零回归、契约不变、不越界自建配置中心。

## 二、逐条验收清单（PASS/FAIL，附证据）
### 依赖前置
- [ ] P0 **F0 配置中心（RND-244）已合并**：能 `import app.config_service`（或实际模块名）且 `alembic check` 绿。若未合并，开发 agent 应已停下报告 —— 本项 FAIL 且整体判 FAIL，附「依赖未就绪」说明。

### 端点行为（B）
- [ ] B1 `GET /api/onboarding/status` 默认返回 `{"first_run": true}`（无配置 → 视为未完成）—— 证据：curl + jq。
- [ ] B2 `POST /api/onboarding/complete`（含合法 admin session）后 `status` 返回 `{"first_run": false}` —— 证据：curl。
- [ ] B3 **幂等**：重复 `POST /api/onboarding/complete` 仍 `{"first_run": false}`，无 4xx/5xx、无副作用报错 —— 证据：连发两次对比。
- [ ] B4 完成后 `GET /api/onboarding/status` 仍 `{"first_run": false}`（持久化生效） —— 证据：POST 后再 GET。
- [ ] B5 **fail-safe**：F0 配置读取抛异常时 `status` 仍返回 `first_run=true`（不 500） —— 证据：可注入故障或审查源码 try/except。

### 租户隔离（T）
- [ ] T1 租户 A 完成向导后，租户 B 的 `GET /api/onboarding/status` 仍为 `{"first_run": true}`（不串） —— 证据：双租户 session 对比。
- [ ] T2 端点**不**接受请求级 `tenant_id` 参数（tenant 仅来自 `require_role()` 解包） —— 证据：grep 源码确认无 `tenant_id: str = Query(...)` / `Body(...)`。

### 授权（A）
- [ ] A1 `GET /api/onboarding/status` 需登录（匿名 401/302） —— 证据：无 session 直连。
- [ ] A2 `POST /api/onboarding/complete` 需 admin/owner 角色（`require_role("admin","owner")`）；readonlyaudit 等被拒（403） —— 证据：多角色 session 对比。

### 范围守门（S，越界即 FAIL）
- [ ] S1 **无新建模型 / 迁移**：`git diff --stat` 不应含 `db/models.py` 新增表、`alembic/versions/*` 新文件 —— 证据：`git diff --stat` + `alembic check` 绿。
- [ ] S2 **无自建配置服务**：不应新增 `app/config_service.py` / `app/services/config_*` 等（复用 F0） —— 证据：grep 改动文件。
- [ ] S3 **未触碰向导页面 / i18n / 前端框架**：`design/`、`app/assets/i18n.js`、React 不引入 —— 证据：`git diff --stat` 路径审计。
- [ ] S4 不改 `_FLAT_SERVICE_MODULES`（`test_architecture_boundary.py:70`） —— 证据：git diff 该文件为空。

### 全局契约（C）
- [ ] C1 路由数：按「读当前 N → N+2」同步；`test_http_contract.py` route_count / path 集合 / snapshot 三处一致 —— 证据：`make verify`（test_http_contract 段）绿。
- [ ] C2 `make verify` 全绿（lint / type / build / test） —— 证据：完整日志。
- [ ] C3 `test_architecture_boundary.py` 绿（无反向依赖 / 组合根纯净） —— 证据：pytest 输出。

## 三、回归套件（必须全绿）
`make verify` + 重点：`backend/tests/test_http_contract.py`、`backend/tests/test_architecture_boundary.py`、以及 onboarding 相关新增测试（`test_onboarding.py` 或并入既有 auth 套件）。

## 四、智能路由判定（每轮必给）
- 源码有 Bug → 反馈开发 agent 修复，附错误 + 失败测试 + 期望；不自行改实现。
- 测试代码有 Bug → 可自行修正测试（仅当断言旧路径，须标注）。
- 全部通过 → 报告 SUCCESS，附清单结果。
最多 2 轮：第1轮修复，第2轮回归；仍不过则标注遗留。

## 五、交付报告格式
>RND-304 验收结论：PASS / FAIL
>依赖 F0：已合并 / 未合并（未合并→整体 FAIL）
>RED 基线：___（默认 first_run=true）
>GREEN：___（complete 后 first_run=false，幂等，租户隔离）
>回归：make verify ___（绿/红）
>契约：路由数 +2、_FLAT_SERVICE_MODULES 未改、无新建模型/迁移
>遗留：___
