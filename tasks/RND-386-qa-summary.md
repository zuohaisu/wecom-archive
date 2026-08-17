# RND-386 QA Summary — Tenant-scoped 配置向导与凭据安全保存 (T1)

## Files changed

- `backend/alembic/versions/0055_rnd386_tenant_archive_config.py` — add `callback_token_encrypted`, `callback_encoding_aes_key_encrypted`, `publickey_version` to `tenant_wecom_configs` (nullable; downgrade drops them).
- `backend/app/db/models.py` — `TenantWecomConfig`: 3 new columns + `set_private_key` / `set_callback_credentials` / `decrypted_callback_token` / `decrypted_callback_encoding_aes_key` / `has_callback_credentials`.
- `backend/app/services/tenant_config_service.py` (new) — masked `config_snapshot`, validated/audited `apply_config_updates` (blank values never overwrite; corp_id/agent_id always server-side from the binding), `test_config` (local decryptability/RSA/AES-key checks + real `get_wecom_token` probe with coarse safe error codes).
- `backend/app/routers/provisioning.py` — `GET/PUT /api/provisioning/config`, `POST /api/provisioning/config/test`; body model `extra="forbid"`; `/admin/provisioning/settings` now renders the wizard page.
- `backend/app/web/sidenav.py` — `render_provisioning_sidenav(active_id)` extracted from the duplicated HTML in `routers/billing.py` (billing.py now imports it; no new UI strings).
- `backend/app/web/templates/provisioning_settings.html` (new) + `backend/app/web/static/provisioning.js` (new) — wizard page: read-only org info, callback URL with copy, 5 field cards with per-field official instructions, masked secret inputs (GET never returns plaintext), save + test-connection, missing-items banner.
- `backend/app/assets/i18n.js` — `provisioning.*` keys added to all 3 locales (zh-CN/zh-TW/en).
- `backend/tests/fakes.py`, `backend/tests/test_dashboard_api.py`, `backend/tests/test_http_contract.py` — hand-written sqlite DDL for `tenant_wecom_configs` kept in sync with the 3 new columns (worker/dashboard suites failed without this).
- `backend/tests/test_http_contract.py` — route count 148→151; 3 new expected paths; 3 new snapshot rows.
- `backend/tests/test_rnd386_tenant_config_wizard.py` (new) — 10 tests.

## Acceptance criteria

- [x] 平台人员无需录入客户凭据或直接修改数据库 — wizard API + UI replaces manual fills for provisioning tenants.
- [x] 不信任客户端提交的 tenant_id、CorpID 或授权主体 — body `extra="forbid"`; corp_id/agent_id copied server-side from `ThirdPartyOrganizationBinding`; reserved keys rejected (tested 422).
- [x] secret、私钥、code、token 不出现在 URL、HTML、公开 JSON、日志、审计详情或 Linear — GET returns `mask()` only; tests assert raw secret substrings absent from every response; audit detail carries field names only.
- [x] 跨租户、重复提交、部分保存、加密 key 缺失和并发更新失败关闭 — session-scoped tenant_id; unique tenant config row; blank no-overwrite; decrypt failures surface as `unreadable` status; single-commit persistence.
- [x] 现有自建部署配置中心与默认租户行为零回归 — no changes to config-center or env callback paths in T1; full suite green.

## Commands run

- `ruff check` on all changed .py files — clean.
- `compileall` + `from app.main import app` import check — OK.
- `node --check` on `assets/i18n.js` and `web/static/provisioning.js` — OK.
- Full suite: `pytest backend/tests -q` — **3077 passed, 93 skipped, 0 failed**.
- New suite: `pytest tests/test_rnd386_tenant_config_wizard.py -q` — 10 passed.

## Manual verification

- `render_template("provisioning_settings", ...)` — no unresolved tokens; provisioning sidenav renders with the settings item active; i18n script tag present.

## Risks or gaps

- `test_config` connectivity probe shares the `get_wecom_token` cache keyed per tenant (`provisioning-config-test:{tenant_id}`); a real WeCom probe is exercised only in the non-prod E2E (T4).
- Callback credentials stored here are not yet consumed by the runtime — that lands in T2 (RND-387); until then env-scoped callback creds remain authoritative for the default tenant (by design, zero-regression).
- `app_secret` remains NOT NULL: a config row can only be created once the archive Secret is provided (validated as `required_first`).
- Local dev venv: this worktree has no standalone `.venv` (cryptography 49 has no py3.9 wheels); tests ran with the main checkout's venv symlinked at `.venv` (gitignored) + `qrcode` installed into it to match `requirements.txt`.

## No secrets introduced: confirmed
## Only intentional files changed: confirmed

## Post-rebase alignment (2026-08-17)

- Delivery branch rebased onto `origin/main` (bc55370, after RND-392/396/398 PRs).
  Conflicts resolved in `i18n.js` (billing.* + provisioning.* keys kept in all 3
  locales), `billing.py` (inline `_provisioning_sidenav` removed — shared
  `render_provisioning_sidenav` used by both sides), `provisioning.py` (wizard and
  status pages win over RND-396's static guidance cards, which pointed at this flow).
- Sidenav billing label aligned to RND-398's trial-first wording
  ("开始 15 天免费试用").
- `test_rnd395_billing_experience.py` policy scan moved from router source text to
  the merged sources of truth (`web/sidenav.py` + `i18n.js`); the payment-not-first
  policy itself is unchanged and still enforced.
