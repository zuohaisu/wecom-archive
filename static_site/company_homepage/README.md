# Company Homepage and Static Product Demo

This directory is the public, standalone static website for **康冠时代企业微信会话存档**. It is intentionally isolated from the archive backend: it must not link to `/admin`, `/api`, health endpoints, or an authenticated archive UI.

It contains two public experiences:

- `/` — marketing homepage: customer-asset positioning, delivered capabilities, boundaries, FAQ, legal footer, and real consultation links.
- `/pricing.html` — the frozen annual plan, capacity behavior, official-fee boundary, and pricing FAQ.
- `/demo/` — a static, local-only product tour. It shows fictional examples of the review console, search, contacts, media, usage analysis, and audit logs. It never sends API requests, authenticates a user, or includes production chat data.

## Source tree

```text
static_site/company_homepage/
├── index.html
├── pricing.html
├── style.css
├── site.webmanifest
├── brand/                    # logo and favicon assets
├── assets/                   # homepage visual assets
├── demo/
│   ├── index.html            # static demo overview
│   ├── conversations.html
│   ├── search.html
│   ├── contacts.html
│   ├── media.html
│   ├── analytics.html
│   ├── audit-log.html
│   └── assets/               # standalone demo CSS and local-only JS/data
└── README.md                 # contributor documentation; never served
```

## Content guardrails

- The currently published package is **99 yuan/year including 5 GiB storage**, with unlimited seats. No overage unit price or larger plan is currently approved: do not invent free tiers, overage prices, annual discounts, or additional plans.
- State clearly that WeCom Conversation Archive API enablement and related official fees are not included in the package and follow WeCom's rules.
- The public homepage retains the legal entity, ICP record, public security record, and public contact email. Do **not** publish a detailed street address or telephone number.
- Keep the clear independent-product disclaimer: this is not a Tencent or WeCom official product.
- Do not market the service as an official Qiniu reseller or partner without documented authorization. Capacity-based pricing is the public product message; the underlying storage provider is not a marketing claim.
- Present only shipped capabilities. Risk-signal automation, business-record export, recycle-bin cleanup, and larger storage plans must remain explicitly planned/unavailable until their own delivery tickets pass acceptance.
- Never promise 100% flying-order prevention, personal-WeChat monitoring, automatic employee-violation decisions, or absolute legal validity.
- Every record in `demo/` must be synthetic. Never copy production conversations, customer details, media, exports, identifiers, or credentials into a static asset.
- The demo must remain static and read-only: no backend dependency, authentication, API calls, tracking pixels, download, export, or write operation.

## Target deployment directory

`scripts/deploy_server.sh` step `[9/9]` syncs the **entire** contents of this directory (except `README.md`) to the public static directory on the production host:

```text
/var/www/$STATIC_SITE_DIR_NAME/
├── index.html
├── style.css
├── brand/
├── assets/
├── demo/
└── site.webmanifest
```

`STATIC_SITE_DIR_NAME` is read from the deploy environment and defaults to `site`. It must match the `root` in the operator-managed Nginx configuration for `crowntime.cn` / `www.crowntime.cn`; otherwise the deployment can copy successfully to a directory Nginx does not serve.

This static directory must remain separate from the WeCom archive backend app and its static assets. `archive.crowntime.cn` continues to route to the backend and must not be affected by homepage changes.

## Local preview

```bash
cd static_site/company_homepage
python3 -m http.server 8080
```

Open:

- `http://127.0.0.1:8080/`
- `http://127.0.0.1:8080/demo/`

Verify all demo links locally before publishing. A static update does not itself deploy production; commit/push and production deployment remain human-controlled.
