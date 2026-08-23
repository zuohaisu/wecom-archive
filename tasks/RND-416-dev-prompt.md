[Goal check] This work advances the self-service annual-plan checkout by delivering an Alipay PC cashier flow whose verified server callback or query is the only entitlement authority.

# RND-416 开发提示词（Developer Prompt）

## 任务身份
- 工单：RND-416「优先实现支付宝电脑网站扫码支付」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-416/优先实现支付宝电脑网站扫码支付
- 风险等级：**R2**（支付渠道、回调验签、公共路由与运行时配置）
- 交付分支：`zuohaisu/rnd-416-优先实现支付宝电脑网站扫码支付`（本票专用；非 `main`）

## 背景与目标
当前支付订单、验签结果、幂等激活和期限授予已通过 `payment_provider.py`、`payment_orders.py` 和 `subscription_activation.py` 分层；现有 WeChat Native adapter 仅支持二维码。交付支付宝 `alipay.trade.page.pay` 的 PC 收银台跳转（收银台负责显示支付宝扫码），保留服务端订单、验签通知/签名查单、重复事件和权益激活的现有权威链路。浏览器返回页不是开通依据。

官方资料（2026-08-23 调研）：支付宝电脑网站支付会将 PC 浏览器跳转到收银台，收银台支持支付宝 App 扫码；异步通知和查单结果必须验签。开放平台页面抓取对正文不完整，实施以协议字段与模拟传输测试锁定。

## 范围
**In scope：**
1. 添加关闭默认的支付宝环境设置、RSA2 PEM/证书验证、固定 HTTPS `notify_url` 与 `return_url` 校验，并在应用启动时 fail closed。
2. 实现 `alipay.trade.page.pay` 签名收银台跳转、`alipay.trade.query`/`alipay.trade.close` 签名查单关单、支付宝 RSA2 回调验签与严格订单/金额/商户核对。
3. 将创建订单优先选择已启用的支付宝；按持久化 provider 正确分派旧微信和新支付宝订单的查询/关闭，避免切换配置后误用渠道。
4. 新增受鉴权的支付宝收银台跳转端点；不向 JSON 或页面暴露签名 checkout URL。账单页对微信保留 QR，对支付宝提供收银台入口。
5. 添加支付宝公开通知路由，只有验签的 `TRADE_SUCCESS` / `TRADE_FINISHED` 或签名查单成功可调用既有幂等激活；返回页不授予权益。合法回调使用支付宝要求的 `success` 确认文本。
6. 为不支持支付宝退款的既有微信退款操作增加 fail-closed provider guard，且补充配置、API、部署和架构文档。
7. 为 provider、路由/生命周期、前端 DOM/JS 与 HTTP 路由契约添加测试。

**Out of scope：**
- 真实商户配置、真实付款、生产密钥/证书、生产部署与退款接入。
- 微信支付的新功能、支付宝退款、自动扣款、移动网站/小程序/服务商模式。
- 数据库迁移（已有 provider、订单引用和 checkout 字段足够承载本票）。

## 必须满足的安全契约
- 价格、币种、tenant 与期限只由现有服务端订单和订阅服务决定。
- 私钥、签名、签名 URL、通知内容和配置值不写入日志、API JSON、页面或测试 fixture；测试临时生成 RSA key。
- 回调采用原始表单字节的 SHA-256 作为回放内容证据，排除 `sign`/`sign_type` 后按支付宝 RSA2 规则验签；校验 AppID、seller ID、订单号、交易号、支付状态和总金额。
- 支付宝 `return_url` 仅回到已登录的账单页，不能触发激活。回调和查单只复用 `apply_trusted_payment`。
- 新增路由同步更新 `backend/tests/test_http_contract.py`。
- 不在 `app/main.py` 注册业务路由；service 不反向 import router；所有 UI 新文案覆盖 zh-CN、zh-TW、en。

## 允许写入的文件
- `backend/app/settings.py`
- `backend/app/services/alipay.py`（新增）
- `backend/app/services/payment_orders.py`
- `backend/app/services/refunds.py`（仅支付宝订单退款 fail-closed guard）
- `backend/app/routers/billing.py`
- `backend/app/routers/refunds.py`（仅支付宝订单退款 fail-closed guard）
- `backend/app/routers/platform_operations.py`（仅按持久化 provider 分派付款查询）
- `backend/app/main.py`（仅支付宝配置启动校验 import/call）
- `backend/app/schemas/billing.py`
- `backend/app/web/templates/billing.html`
- `backend/app/web/static/billing.js`
- `backend/app/assets/i18n.js`
- `.env.example`
- `docs/API.md`
- `docs/DEPLOYMENT.md`
- `docs/ARCHITECTURE.md`
- `docs/adr/0004-annual-plan-wechat-pay-gates.md`（仅标注支付宝边界已由 RND-416 替代）
- `docs/adr/0006-alipay-pc-page-pay.md`（新增）
- `backend/tests/test_rnd416_alipay_provider.py`（新增）
- `backend/tests/test_rnd416_alipay_billing.py`（新增）
- `backend/tests/test_rnd416_alipay_billing_ui.py`（新增）
- `backend/tests/test_http_contract.py`（仅新增路径、snapshot 与 route count）
- `backend/tests/test_rnd380_billing_api.py`、`backend/tests/test_rnd406_billing_e2e.py`（仅将既有微信 callback fake 绑定到独立微信 callback dependency）

## 验收标准
- 已配置支付宝时，账单页创建订单并通过受鉴权的 checkout 端点跳到签名的 `alipay.trade.page.pay` 收银台；JSON/page 不含 URL 或私钥。
- 未配置或不完整的启用配置不能启动应用/创建付款，且不会泄露配置值。
- 支付宝回调和主动查单均严格验签并核对订单、AppID、seller、金额和 `CNY`；无效签名、未知状态、错误金额或身份无写入。
- `TRADE_SUCCESS`/`TRADE_FINISHED` 经验证只激活一次；重复回调/查单不重复授予权益；返回页不激活。
- 支付失败、交易关闭、超时与用户关闭订单都产生现有可重试终态；旧微信订单在支付宝启用后仍由微信 adapter 查询/关闭。
- 微信退款控制面不会尝试用微信渠道退款支付宝订单。
- `make verify`、架构边界、RND-416 新增测试与现有支付/退款回归测试通过。

## 验证命令
```bash
.venv/bin/python -m pytest backend/tests/test_rnd416_alipay_provider.py backend/tests/test_rnd416_alipay_billing.py backend/tests/test_rnd416_alipay_billing_ui.py -q
.venv/bin/python -m pytest backend/tests/test_rnd380_wechat_pay_provider.py backend/tests/test_rnd380_billing_api.py backend/tests/test_rnd380_payment_orders.py backend/tests/test_rnd403_wechat_refund_provider.py backend/tests/test_architecture_boundary.py backend/tests/test_http_contract.py -q
make verify
```

## 风险与人工点位
- 风险：支付协议字段、签名范围或商户配置与实际开放平台账号不匹配。缓解：默认关闭、临时 RSA keys 的协议测试、启动校验与上线前独立非生产实测。
- 不提交、不推送、不移动 Linear 状态；待 Haisu 审阅 QA 后批准本票唯一 commit/push/PR。
- 两轮修复仍无法通过或需要真实商户资料时，停止并报告 `BLOCKED_NEEDS_HUMAN`。

## QA Summary

**Files changed:**
- `backend/app/services/alipay.py`, `settings.py`, `main.py` — closed-by-default RSA2 adapter, startup validation and fixed HTTPS configuration contract.
- `payment_orders.py`, billing/platform/refund routers and billing schema — Alipay-first new orders, stored-provider dispatch, protected cashier redirect, signed callback path and refund fail-closed guard.
- Billing template/JS/i18n — retained WeChat QR and added an Alipay cashier link without exposing checkout material.
- `.env.example`, API/deployment/architecture docs and ADR-0006 — configuration, proxy and security/runbook contract.
- `test_rnd416_alipay_*.py`, payment callback fixtures and HTTP contract — protocol, UI, lifecycle/idempotency, regression and route coverage.

**Acceptance criteria:**
- [x] PC Alipay cashier redirect and no checkout URL in order JSON/page.
- [x] RSA2 signed page-pay/query/close, callback verification, merchant/order/CNY checks, invalid-signature and tamper rejection.
- [x] Callback/query-only idempotent activation; return page and bad total cannot activate.
- [x] Pending/closed/missing trade handling, timeout close and stored-provider WeChat regression.
- [x] No Alipay refund is sent to the WeChat refund control surface.
- [x] Environment/deployment/API documentation and three locales are present.

**Commands run:**
- `.venv/bin/python -m pytest backend/tests/test_rnd416_alipay_provider.py backend/tests/test_rnd416_alipay_billing.py backend/tests/test_rnd416_alipay_billing_ui.py backend/tests/test_rnd380_wechat_pay_provider.py backend/tests/test_rnd380_billing_api.py backend/tests/test_rnd380_payment_orders.py backend/tests/test_rnd403_wechat_refund_provider.py backend/tests/test_rnd406_billing_e2e.py backend/tests/test_platform_operations.py backend/tests/test_architecture_boundary.py backend/tests/test_http_contract.py -q` → `165 passed, 2 skipped`.
- `make verify` → PASS: `3406 passed, 158 skipped`; lint-diff, import/syntax typecheck and JS build all passed.
- `git diff --check` → PASS.

**Manual verification:** TestClient exercised the authenticated redirect (including anonymous rejection), order JSON redaction, callback acknowledgement and existing-WeChat dispatch. No real merchant credentials, payment, callback or production system was accessed.

**Risks/gaps:** Actual merchant application registration, callback reachability and non-production real-payment acceptance remain an explicit operational gate. Alipay refunds are deliberately unsupported and rejected rather than routed through WeChat.

**No secrets introduced:** confirmed; environment template has only empty placeholders and generated test keys remain in memory.
**Only intentional files changed:** confirmed; working tree contains only RND-416 implementation, documentation, tests and this artifact.
