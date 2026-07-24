---
kind: external_dependency
name: WeCom Platform (Enterprise WeChat)
slug: wecom
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
---

### WeCom (企业微信)
- Role: Source of encrypted conversation archive data and OAuth identity provider for admin login.
- Integration points: C SDK shared library loaded via `WECOM_SDK_LIB_PATH`; Conversation Archive API called by sync/decrypt workers; OAuth 2.0 (`snsapi_base`) used for employee login with `WECOM_CORP_ID`, `WECOM_AGENT_ID`, `WECOM_OAUTH_SECRET`, and `ADMIN_DOMAIN`.
- Auth protocol: Two credential domains — archive credentials (server-to-WeCom, RSA+AES decryption) and admin login (WeCom OAuth with CSRF state token, 5-min TTL). Both modes share the same session model and tenant-scoped queries.
- Constraint: Production OAuth requires domain authorization in WeCom Admin; password fallback mode is temporary for dev-only bring-up.
- Verify exact OAuth endpoints and Archive API contracts against the official WeCom developer documentation.