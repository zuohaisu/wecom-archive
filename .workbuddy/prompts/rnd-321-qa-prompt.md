# RND-321 验收提示词 — 企业微信扫码登录（PC 端管理后台）

> **对应开发提示词**：`rnd-321-execution-prompt.md`
> **类型**：feature / 登录方式扩展（与现有 OAuth 静默授权并存）
> **依赖**：`[BLOCKED: F0-2]`（RND-276）。**若 F0-2 未合并，开发应已停下报告；此处直接判 BLOCKED，不进入验收。**
> **Linear**：RND-321

---

## 0. 验收前置

- 确认开发交付报告声明 **F0-2 / RND-276 已合并**到工作树；否则本 ticket 维持 blocked，不验收。
- 确认 `make verify`（lint-diff / typecheck / build / test）全绿。
- 确认 `.workbuddy/prompts/rnd-321-execution-prompt.md` 的硬约束全部被满足（见 §5）。

---

## 1. 验收清单（映射 Linear 7 项）

| # | 验收项（来自工单） | 验证方式 | 通过标准 |
|---|---|---|---|
| AC1 | PC 浏览器开登录页显示企业微信扫码二维码 | 启动 app，`GET /admin/login`（wecom 模式）查看 HTML；确认存在扫码区 | 页面含二维码（`<iframe src=...qrConnect>` 或 `<img>` 指向 `open.work.weixin.qq.com`），且渲染可见（不被 `overflow:hidden` 裁剪，参考 RND-322 教训） |
| AC2 | 手机企微扫码确认后 PC 自动登录并跳转控制台；会话 cookie 正确签发 | 手动：手机企微扫登录页二维码 → 确认 → PC 自动跳 `/admin/conversations`；检查 `document.cookie` / 响应头 | 302→`/admin/conversations`；`Set-Cookie` 含 `session_id`，标志 `HttpOnly`+`SameSite=Lax`+`Path=/`，production 下 `Secure`；`/api/auth/me` 返回 `authenticated:true` |
| AC3 | 复用现有账号体系：`wecom_user_id` 命中即登录，未命中按现有逻辑 upsert | 用**已存在**的 `wecom_user_id` 扫码 → DB 不新建重复行；用新 `UserId` 扫码 → 新建 `AdminUser`；比对 `wecom_callback` 行为一致 | `admin_users` 无重复 `wecom_user_id`；新用户 `last_login_at` 更新；会话字段（tenant_id/wecom_user_id）正确 |
| AC4 | state/ticket 过期、重复使用、伪造均被拒绝（不登录） | 单元/手动：① 过期 state ② 已消费 state 再次用 ③ 伪造 state ④（若 §3.2）过期/重用 ticket | 三种均 302 到 `/admin/login?error=invalid_state`，**无** `session_id` cookie、**无**新 `admin_sessions` 行 |
| AC5 | 企微内打开仍可用现有 OAuth 静默授权（回归） | `GET /api/auth/wecom/login` → 仍跳 `open.weixin.qq.com/.../oauth2/authorize?scope=snsapi_base`；走完整回调登录 | 现有 OAuth 路径行为不变，登录成功 |
| AC6 | 现有 RND-110/RND-112 登录回归测试全绿 | 跑 `pytest backend/tests/test_auth.py test_password_auth.py test_rnd225_auth_fail_closed.py test_i18n_foundation.py` | 全部 pass |
| AC7 | i18n：新增 key 至少 zh/en 两套 | 查 `app/assets/i18n.js` 三语块；前端切 zh/en 看扫码区文案 | `login.qr*` key 在 zh-CN + en 均存在（建议 zh-TW 也补）；无缺失语言告警 |

---

## 2. 功能验收步骤（建议顺序）

1. **代码评审**：核对 `routers/auth.py` 新增 `qr_login`/`qr_callback`；确认抽出/复用了 `_resolve_and_sign_wecom_session`（或 F0-2 的等价 helper），`wecom_callback` 未改语义。
2. **静态核查**：
   - `test_http_contract.py:325` 已改为 `== 38` 且注释 RND-321。
   - `i18n.js` 三语块均含 `login.qr*`。
   - 无新增 migration（主方案）；新增路由在 `_FLAT_SERVICE_MODULES` allowlist 范围内（仅改既有文件）。
3. **自动化测试**：`pytest backend/tests/test_rnd321_qr_login.py -v` 全绿（P1-P7）；`make verify` 全绿。
4. **手动 E2E（需真实企微环境 + 手机）**：
   - AC1/AC2/AC3：真实扫码 → 登录 → 跳转 → cookie 正确。
   - AC5：企微内打开登录页 → OAuth 静默授权仍可用。
   - AC4：手动构造过期/伪造 state 访问 `qr/callback` → 拒绝。
5. **回归**：AC5/AC6 覆盖现有 OAuth + 密码登录不被破坏。

---

## 3. 安全专项验收（不可妥协）

- **state 单次使用**：消费后即从 `_state_store` 移除；重放被拒（对照 `consume_state` L167）。
- **租户绑定**：`qr/callback` 仍通过 `TenantWecomConfig.corp_id` 解析租户（复用 `wecom_callback` L500-512）；无匹配 → `config_error`，绝不落到任意租户。
- **用户状态校验**：`user/get` 返回 `status!=1` → `user_inactive` 拒绝（对照 L486）。
- **零泄露**：搜索日志确认无 `code`/`access_token`/`secret`/`UserId` 明文（用 `grep -riE "access_token|UserId|code="` 在日志/测试输出中核对，仅 `safe_log_value` 的 errcode 类型名允许）。
- **cookie 标志一致**：`qr/callback` 的 `set_cookie` 与 `wecom_callback` 完全一致（HttpOnly/SameSite/Path/max_age；production Secure）。

---

## 4. 回归测试清单（必须全绿）

- `backend/tests/test_auth.py`
- `backend/tests/test_password_auth.py`（F0-2 契约：404/401/200+cookie/500/租户绑定）
- `backend/tests/test_rnd225_auth_fail_closed.py`
- `backend/tests/test_i18n_foundation.py`（登录页 `data-i18n` 结构，含 `login.wecomButton` 等既有断言不被破坏）
- `backend/tests/test_http_contract.py`（route_count==38）
- `make verify` 全流程

---

## 5. 硬约束合规核对（任一不满足即打回）

- [ ] agent 未执行 git commit/push（交付物仅为两份提示词 + 代码改动，提交由用户本人）
- [ ] 仅改 `routers/auth.py` / `auth.py` / `assets/i18n.js` / `web/templates/login.html` / 测试；无新 service 模块、无新 DB 表/migration（主方案）
- [ ] `test_http_contract.py:325` 已同步为 `== 38`
- [ ] 前端为 SSR + 原生 JS，无 React/SPA
- [ ] 复用 `consume_state`/`get_wecom_token`/既有 upsert+会话签发，无重写
- [ ] fail-closed：过期/重用/伪造 state、租户缺失、用户停用、配置缺失均拒绝登录
- [ ] 无 `code`/`token`/`secret`/`UserId` 进日志
- [ ] 现有 `wecom_login`/`wecom_callback` OAuth 路径未被改动/移除（共存）
- [ ] 代码标识符加反引号

---

## 6. 验收结论模板

- **结论**：PASS / FAIL / BLOCKED（F0-2 未合并）
- **AC1-AC7**：逐条 PASS/FAIL + 证据（HTML 片段 / 测试用例 / 手动步骤结果）
- **回归**：RND-110/RND-112 测试全绿 ✅/❌
- **安全专项**：5 项全部 ✅/❌
- **硬约束**：10 项全部 ✅/❌
- **遗留风险**：如 CSP 是否需 §3.2 备选、真实扫码仅手动 QA 覆盖
- **建议**：是否可交用户 review/合并
