# RND-321 执行提示词 — 企业微信扫码登录（PC 端管理后台）

> **类型**：feature / 登录方式扩展（与现有 OAuth 静默授权并存，非替换）
> **父 Epic**：RND-263（F0 账号体系重构）
> **依赖**：`[BLOCKED: F0-2]`（RND-276 per-user 鉴权逻辑）。**开工前必须确认 F0-2 已合并**（见 §0）。
> **优先级**：Medium (3) / **状态**：Backlog
> **标签**：backend, type:feature, area:frontend
> **Linear**：RND-321

---

## 0. 开工前硬前置（必查，未满足则停下报告，禁止自行改鉴权）

1. 确认 **F0-2 / RND-276（per-user 密码鉴权）已合并**到工作树：
   - `git log --oneline -10` 应能看到 RND-276 的合并提交；
   - `backend/app/routers/auth.py` 的 `password_login` 应已改为按 `AdminUser.email` 校验 `password_hash`（不再是纯 env 单 hash）；
   - `AdminUser` 已含 `password_hash` + `role`（F0-1=RND-277 已合并，models.py:177/178 已验证存在）。
2. 若 F0-2 **未合并**：**停止开发**，在交付报告中明确「blocked on F0-2，未合并，等待 RND-276 落地后再开工」，不要擅自实现/改写 `wecom_callback` 的鉴权逻辑（那属于 F0-2 职责）。
3. 若 F0-2 **已合并**：继续 §1，并优先复用 F0-2 抽出的 upsert/会话签发 helper（若其已抽出）。

---

## 1. 任务概述（来自 Linear 工单）

PC 端管理后台支持**企业微信扫码登录**：电脑浏览器打开登录页 → 展示二维码 → 用户用手机企业微信「扫一扫」确认 → PC 端自动登录并跳转控制台。

- 与现有 OAuth `snsapi_base` 静默授权是**两种不同场景**：后者仅限**在企业微信 App 内打开**时自动授权（无需扫码）；扫码登录面向**PC 浏览器**。二者**并存而非替换**。
- 扫码最终解析出的身份是 WeCom `UserId`，须走与现有 OAuth **一致的 upsert + 会话签发**逻辑（该逻辑在 F0-2 重构）。
- 数据模型已兼容扫码身份：`AdminUser.wecom_user_id` 早已存在可承载扫码身份；`status`(active/disabled)、`role`、`last_active_at` 也已就位，**本票无需额外模型改动、无需 migration**（除非你选了需要持久化 ticket 的轮询方案——见 §3 备选）。

---

## 2. 现状核查结论（代码侧，已验证）

- **完全未实现**：`grep -rniE "qr|qrcode|扫码|qrconnect"` 在 `backend/app` 下**零命中**。
- 现有登录入口（`backend/app/routers/auth.py`）：
  - `GET /api/auth/wecom/login` → `wecom_login()`（L334）：跳转 `open.weixin.qq.com/connect/oauth2/authorize?scope=snsapi_base`（仅限企微内）。
  - `GET /api/auth/wecom/callback` → `wecom_callback()`（L376-579）：state 校验 → `get_wecom_token` → `getuserinfo` 换 `UserId` → `user/get` 校验 `status==1` → 按 `corp_id` 解析租户 → upsert `AdminUser` → 签发 `AdminSession` + HttpOnly cookie → 302 到 `/admin/conversations`。
  - `POST /api/auth/password/login`（L222，仅 `AUTH_MODE=password`）。
- **关键洞察**：企业微信**网页扫码登录**（`qrConnect`）在手机确认后，同样以 `code`+`state` 重定向回后端，后端用**同一个** `user/getuserinfo` 换 `UserId`。因此本票可**直接复用 `wecom_callback` 的核心交换+upsert+会话签发逻辑**，几乎不写新代码。

---

## 3. 推荐实现方案（主方案：回调重定向，零新增持久化）

### 3.1 主方案 — `qrConnect` 回调重定向（推荐，无新路由表/无 migration）

1. **新路由 `GET /api/auth/wecom/qr/login`**（`routers/auth.py`，仿 `wecom_login` L334）：
   - 校验 `WECOM_CORP_ID` / `WECOM_AGENT_ID` 已配置（缺失 → `RedirectResponse("/admin/login?error=config_error")`），与 `wecom_login` 一致。
   - `state = generate_state()`（复用 `app/auth.py:153` 的单次使用 state）。
   - 构造**网页授权扫码** URL（注意域名是 `open.work.weixin.qq.com`，**不是** `open.weixin.qq.com`）：
     ```
     https://open.work.weixin.qq.com/wwopen/sso/qrConnect
       ?appid={corp_id}
       &agentid={agent_id}
       &redirect_uri={urlencode("https://{admin_domain}/api/auth/wecom/qr/callback")}
       &state={state}
       &self_redirect=true
     ```
   - `return RedirectResponse(qr_connect_url, 302)`。
2. **新路由 `GET /api/auth/wecom/qr/callback`**（`routers/auth.py`，与 `wecom_callback` L376 同签名 `code`+`state`）：
   - **直接复用** `wecom_callback` 的步骤 1-9：`consume_state(state)` → `get_wecom_token` → `getuserinfo` → `user/get` 校验 `status==1` → 按 `corp_id` 解析租户 → upsert `AdminUser`（命中 `wecom_user_id` 即登录，未命中则 upsert）→ 签发 `AdminSession` + 设置**完全相同**的 cookie 标志（`httponly=True, secure=_is_production(), samesite="lax", path="/", max_age=SESSION_TTL_HOURS*3600`）→ 302 到 `/admin/conversations`。
   - 所有失败分支复用 `wecom_callback` 的 `?error=` 码（`invalid_state`/`auth_failed`/`user_inactive`/`config_error`）。
3. **去重（重要）**：把 `wecom_callback` 里「解析 UserId → 校验 → 解析租户 → upsert → 签发会话」抽成一个模块级私有 helper，例如：
   ```python
   def _resolve_and_sign_wecom_session(
       db: Session, *, wecom_user_id: str, corp_id: str, display_name: str | None = None
   ) -> RedirectResponse:
       """复用：OAuth 回调与扫码回调共用。返回已签好 cookie 的 302 响应。"""
   ```
   - 若 F0-2 已抽出等价 helper → **调用它，不要重复实现**。
   - 若 F0-2 未抽 → 本票抽出该 helper，并让 `wecom_callback` 也改调它（同意图、避免后续分叉）。**注意 §0：仅当 F0-2 已合并时才动 `wecom_callback`；否则停在 §0。**
4. **前端（SSR，D1 冻结：原生 JS，不引 React）**：在 `_login_page()` 的 WeCom 模式 `login_body`（L164-170）追加「扫码登录」区：一个 `<iframe>` 其 `src` 指向后端 `GET /api/auth/wecom/qr/login`（或渲染 `<img>` 指向 `qrConnect` 二维码图）。手机扫描确认后 WeCom 以 `self_redirect=true` 把顶层窗口重定向到 `/api/auth/wecom/qr/callback`，后端完成登录并 302 到 `/admin/conversations`——**前端无需轮询**。
   - 风格与现有「使用企业微信登录」按钮一致（复用 `.btn-wecom` 等 design-system 类）。
   - iframe 需处理 CSP：若部署环境不允许外域 iframe，改为主方案的**备选轮询方案**（§3.2）。

### 3.2 备选方案 — 轮询 ticket（仅当 §3.1 iframe/CSP 不可行时）

- 后端 `GET /api/auth/wecom/qr/start`：调用 `https://login.work.weixin.qq.com/wwlogin/qrLogin` 拿 `login_ticket` + 二维码图（base64/SVG），`ticket` 存**进程内** `_qr_ticket_store`（仿 `app/auth.py:149` 的 `_state_store`，TTL 120s，单进程，与项目现状一致）→ **无需 migration**。返回 `{ticket, qr_image}`。
- 新增 `GET /api/auth/wecom/qr/status?ticket=...`：轮询 `checkqr`；扫描确认后拿到 `code`，走与主方案相同的 `getuserinfo`+`user/get`+upsert+会话签发，标记 ticket 已消费；返回登录态。
- 前端 vanilla JS 轮询 `qr/status`，成功后 `window.location.href='/admin/conversations'`。
- **仅在此方案下**需要关心 ticket 表的并发/过期/重放（复用 `_state_store` 的 lock 模式）。

> **决策建议**：优先 §3.1（最小新增面、复用最彻底、无 migration、route-count 仅 +2）。§3.2 仅作 CSP 兜底。两方案都**不得引入 Redis/RabbitMQ/DB 队列表**。

---

## 4. 精确落点（文件:行号:函数）

| 文件 | 位置 | 动作 |
|---|---|---|
| `backend/app/routers/auth.py` | `wecom_login` L334-368 | 仿写新 `qr_login`（§3.1-1） |
| `backend/app/routers/auth.py` | `wecom_callback` L376-579 | 抽 `_resolve_and_sign_wecom_session` helper；新 `qr_callback` 复用（§3.1-2/3） |
| `backend/app/routers/auth.py` | `_login_page` L164-170（WeCom 分支） | 追加扫码区（iframe/img + i18n） |
| `backend/app/auth.py` | `generate_state` L153 / `consume_state` L167 | 复用 state 机制（不改动） |
| `backend/app/auth.py` | `_state_store` L149（如选 §3.2） | 新增 `_qr_ticket_store` 镜像 |
| `backend/app/assets/i18n.js` | L214-228(zh-CN) / ~L577(zh-TW) / ~L940(en) | 新增 `login.qr*` 三语 key（§6） |
| `backend/app/web/templates/login.html` | L49 等 design-system 类 | 仅确认 class 可用；不动 shell |
| `backend/tests/test_http_contract.py` | L325 `assert route_count == 36` | 改为 `== 38`（+2 QR 路由），注释 RND-321 |
| `backend/tests/` | 新增 `test_rnd321_qr_login.py` | 单元/E2E（§7） |

---

## 5. RED → GREEN 实施步骤

1. **前置**：执行 §0 校验；确认 F0-2 合并状态，决定 helper 抽法。
2. **后端-路由**：加 `qr/login` + `qr/callback`；抽出 `_resolve_and_sign_wecom_session` 并接线（OAuth 回调与扫码回调共用）。
3. **后端-安全**：`qr/login` 复用 config_error 校验；state 单次使用；cookie 标志与 `wecom_callback` 完全一致；绝不日志 `code`/`token`/`secret`/`UserId`（沿用 `safe_log_value`）。
4. **前端**：`_login_page` WeCom 分支加扫码区（iframe/src=qr/login 或 img）；i18n key 接入 `data-i18n`。
5. **i18n**：在 `i18n.js` 三语块各加 `login.qrTitle` / `login.qrScanHint`（至少 zh/en，建议含 zh-TW）。
6. **路由基线**：`test_http_contract.py:325` 改 `== 38`，注释 `RND-321: +2 QR routes`。
7. **测试**：新增 `test_rnd321_qr_login.py`（见 §7），mock httpx 调 WeCom（仿 `test_password_auth` / 既有网络 mock 范式）。
8. **回归**：跑 `make verify`（lint-diff / typecheck / build / test）全绿。

---

## 6. i18n 硬要求

- 新增 key（建议，至少含 zh/en，项目惯例三语全加）：
  - `login.qrTitle` — 如「扫码登录」
  - `login.qrScanHint` — 如「打开企业微信，扫一扫登录」
  - `login.qrExpired` / `login.qrRetry`（若用 §3.2 轮询方案）
- **三语块全部补齐**：`app/assets/i18n.js` 的 zh-CN（~L214）、zh-TW（~L577）、en（~L940）。缺失任一语言会导致 i18n 自动扫描/基础测试告警。
- 文案默认中文、需可翻译（与项目约定一致）。

---

## 7. 测试要求（`test_rnd321_qr_login.py`）

- **P1（发起）**：`GET /api/auth/wecom/qr/login` 302 且 `Location` 含 `open.work.weixin.qq.com/wwopen/sso/qrConnect` 与 `state=`（`generate_state` 已存入）。配置缺失 → 302 到 `?error=config_error`。
- **P2（拒绝非法 state）**：`qr/callback?code=X&state=伪造/已用/过期` → 302 到 `/admin/login?error=invalid_state`，**不创建会话、不种 cookie**（用 `client.cookies` 断言无 `session_id`）。
- **P3（成功路径，mock httpx）**：合法 `state` + mock `getuserinfo`/`user/get` 返回 `UserId`+`status=1` → upsert `AdminUser`（命中 `wecom_user_id` 不重复建行）+ 签发 `AdminSession` + `Set-Cookie session_id` + 302 到 `/admin/conversations`。
- **P4（租户绑定）**：`corp_id` 无匹配 `TenantWecomConfig` → 302 `?error=config_error`，不登录。
- **P5（用户停用）**：`user/get` 返回 `status!=1` → 302 `?error=user_inactive`。
- **P6（回归契约）**：现有 RND-110/RND-112 相关测试（`test_auth.py` / `test_password_auth.py` / `test_rnd225_auth_fail_closed.py`）仍全绿；`wecom_callback` 行为不变。
- **P7（i18n）**：登录页 HTML 含 `data-i18n="login.qrTitle"`（仿 `test_i18n_foundation.py:433` 对 `login.wecomButton` 的断言）。
- 网络调用一律 mock（httpx），不依赖真实 WeCom；真实扫码留作手动 QA（见 QA 提示词）。

---

## 8. 硬约束（违反即打回）

1. **绝不 commit/push**：agent 不执行任何 git 提交；交付=本执行提示词 + QA 提示词，用户本人决定提交。
2. **架构边界**：仅改 `routers/auth.py` / `auth.py` / `assets/i18n.js` / `web/templates/login.html` / 测试。这些已在 `_FLAT_SERVICE_MODULES` allowlist；**不新建 service 模块、不新增 DB 表/migration**（主方案无持久化）。
3. **route-count 基线**：必须同步改 `test_http_contract.py:325` 为 `== 38`，否则 CI 红。
4. **D1 冻结**：前端 SSR + 原生 JS，**禁止引入 React/SPA**；二维码区复用 design-system class。
5. **复用优先**：不得重写下载/会话/upsert 逻辑；必须复用 `consume_state`/`get_wecom_token`/现有 upsert+会话签发。
6. **fail-closed**：state 过期/重用/伪造、租户缺失、用户停用、配置缺失 → 一律拒绝登录（302 + error，无 cookie）。
7. **零泄露**：`code`/`access_token`/`secret`/`UserId` 绝不进日志；沿用 `safe_log_value` 与现有脱敏写法。
8. **共存不替换**：不得改动/移除现有 `wecom_login`/`wecom_callback` 的 OAuth 静默授权路径（回归项）。
9. **代码标识符加反引号**：Linear markdown 把 `_` 解析为斜体，所有代码标识符用 `` ` `` 包裹。
10. **认证行为不变**：本票只新增扫码登录方式；登录流程语义（cookie 标志、会话 TTL、租户作用域）必须与 `wecom_callback` 完全一致。

---

## 9. 交付报告格式（交给用户）

完成后产出简短报告，包含：
- 实现摘要（抽出的 helper、新增 2 路由、前端扫码区、i18n key 清单）；
- F0-2 合并状态确认（是否阻塞）；
- `make verify` 结果（lint/typecheck/build/test 全绿）；
- `test_http_contract.py` 基线是否同步更新；
- 回归测试结果（RND-110/RND-112 相关测试）；
- 遗留/风险（如 CSP 是否需 §3.2 备选；真实扫码需手动 QA）。
