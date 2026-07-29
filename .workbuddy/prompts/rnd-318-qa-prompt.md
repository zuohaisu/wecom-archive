# RND-318 QA agent 验收提示词
> 面向独立 QA agent，对 RND-318（C3-1 留存配置）的开发交付做独立验收。
> 你**不写业务代码**，只验证开发 agent 的交付是否符合本票 AC 与硬约束。全程不执行 git commit / push。

## 一、验收范围与边界
- **本票 AC（来自 Linear RND-318）**：`策略可配` —— 每租户的「保留天数 / 锁定策略」可读取与更新。
- **明确不在本票范围**：到期锁定/清理任务的执行（属 C3-2 / RND-319）；配置中心的实现（属 F0 / RND-244）；任何前端页面。
- **依赖闸门**：RND-318 为 `[BLOCKED: F0]`，留存配置必须存于 F0 配置中心租户级 KV。当前 F0（RND-244）**未合并** → 开发 agent 应停在 Phase 0 报告 BLOCKED；本验收须覆盖「闸口行为」与「F0 合并后的功能」两种情形。

## 二、前置条件
1. 解释器 `../.venv/bin/python`；跑命令前 `cd backend && set -a && source .env && set +a`（本仓 `settings.py` 的 `DatabaseSettings` 裸 `BaseSettings` 不自动读 `.env`）。
2. 拉取开发 agent 的交付说明（RED/GREEN 数字、改动文件、未提交声明、F0 就绪状态）。
3. 先判定 F0 是否在树：`grep -rn "def get_config\|def set_config\|class ConfigStore\|ConfigService\|routers/config" app/ | grep -viE test`。
   - **无命中** → 走「情形 A：闸口验证」。
   - **有命中** → 走「情形 B：功能验证」。

## 三、情形 A — 当前 F0 未合并（预期主路径）
开发 agent 必须**未写任何实现代码**并输出 BLOCKED 报告。逐项核对：
1. **无实现残留**：`app/routers/retention.py`、`app/schemas/retention.py` **不应存在**；`main.py` 无 `include_router(retention_router...)`；`test_http_contract.py` 未改。
2. **BLOCKED 报告完整**：开发 agent 交付说明须含「F0 未合并」结论 + 证据（无 `config_service` / 无 `routers/config.py` / `models.py` 无通用 config 表）。
3. **未越界**：未自建 `retention_policies` 表 / 迁移 / 配置服务模块；未为绕过 F0 而自造存储。
4. 若上述任一项不满足（例如 agent 擅自建表或伪造 config 存储）→ **判 FAIL**，要求开发 agent 回退并停在 Phase 0。
> 注：情形 A 下「功能验证」暂缓，QA 报告须明确「功能验收待 F0/RND-244 合并后由开发 agent 重跑执行提示词，再走情形 B」。

## 四、情形 B — F0 已合并（功能验收）
开发 agent 已实现。逐项核对：
1. **端点存在**：`GET /api/admin/settings/retention`、`PUT /api/admin/settings/retention` 均可达（200）。
2. **默认 fail-safe**：未配置时 `GET` 返回 `{retention_days:365, lock_strategy:"lock"}`（与执行提示词默认值一致）。
3. **更新生效**：`PUT {retention_days:180, lock_strategy:"delete"}` → `GET` 返回新值；再次 `GET` 幂等一致。
4. **部分更新**：`PUT {retention_days:90}`（不带 `lock_strategy`）→ `GET` 中 `lock_strategy` 保持上次值，不被重置。
5. **持久化跨请求**：两次独立 `GET`（或重启 worker 后）返回一致值（证明写入 F0 KV，非内存）。
6. **租户隔离 fail-closed**：
   - 路由签名中 `tenant_id` **仅**来自 `require_role()` 解包，请求体/query **无** `tenant_id` 参数；
   - 租户 A 写入后，以租户 B 身份 `GET` 返回 B 的默认值（不读 A 数据）；跨租户零泄露。
7. **角色门禁**：`GET` 任意 admin 可读；`PUT` 限 `admin`/`owner`（owner-only 或低权限角色调用 `PUT` 应 403）。

## 五、硬约束审计（两种情形都查）
1. **无越界存储**：无新建 DB 模型、无 Alembic 迁移、无新增 service 模块 → `test_architecture_boundary.py` 不变、`_FLAT_SERVICE_MODULES` 未改、架构边界绿。
2. **路由基线（delta，非硬编码）**：`tests/test_http_contract.py` 的 `assert route_count == N` 应为「读当前值 +2」后的真实数（注释 `# RND-318: +2 retention settings routes.`）；路由路径集合与 snapshot 集合各 +2 条 `/api/admin/settings/retention`（GET/PUT）。**严禁写死具体数字**（如 `== 51`）——以 `make verify` 报错给出的真实 count 回填。
3. **零泄露**：响应体只含 `retention_days` / `lock_strategy`，不泄露 F0 KV 其它键、原始值、租户敏感信息。
4. **不引 React / 不改 i18n / 不动前端**（D1 冻结 SSR + 原生 JS；本票纯后端）。
5. **未提交**：工作树改动 `git status` 可见，但**无 commit / push**（由用户本人操作）。

## 六、回归
- `make verify`（或项目等价命令）全绿：`test_http_contract.py` + `test_architecture_boundary.py` + 相关单测。
- 既有 `users` / `media_library` / `audit` 等 `/api/admin` 路由不受影响（回归冒烟）。

## 七、交付报告（向用户）
给出 PASS / FAIL / BLOCKED-待F0 结论，并附：
- 实际命中的 F0 配置服务模块名与 `get_config`/`set_config` 真实签名（情形 B）；
- 功能核对 1–7 的逐条结果；
- 硬约束审计结果；
- 未提交声明；
- 若为情形 A：明确「待 F0/RND-244 合并后重跑」的后续步骤。
