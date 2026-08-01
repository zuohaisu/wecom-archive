# Company Homepage (crowntime.cn)

Minimal static website for 深圳康冠时代科技有限公司, built for ICP beian (备案) review.

This is a plain static site (HTML + CSS, no JS, no build step, no backend
dependency). It is intentionally isolated from the WeCom archive backend
app in this repo — do not link it to `/admin`, `/api`, health endpoints,
or any archive UI.

## Source directory

```
static_site/company_homepage/
├── index.html
├── style.css
├── brand/              # logo SVG、favicon、mask-icon
├── assets/             # console_review.png —— Hero 产品截图
├── site.webmanifest
└── README.md   (this file)
```

## Target deployment directory

`scripts/deploy_server.sh` (step "[9/9] Deploying company homepage static
files") syncs the **entire** contents of this directory (excluding
`README.md`) to a dedicated static directory on the production host:

```
/var/www/$STATIC_SITE_DIR_NAME/
├── index.html
├── style.css
├── brand/
├── assets/
└── site.webmanifest
```

`STATIC_SITE_DIR_NAME` is read from the deploy environment (e.g.
`backend/.env` on the host); it defaults to `site` if unset. **This value
must match whatever `root` the production Nginx config for
`crowntime.cn`/`www.crowntime.cn` actually points at** — if they diverge,
the pipeline will keep syncing files to a directory Nginx never reads,
and the live site will silently stop reflecting new commits with no
error anywhere in the deploy. Confirm the two agree before relying on
automated deploys of this page.

This directory must be separate from wherever the WeCom archive backend
app or its static assets live.

## Domains

- `crowntime.cn`
- `www.crowntime.cn`

Both should serve this static site.

`qwhhcd.crowntime.cn` must continue to route to the existing WeCom archive
backend app and must NOT be affected by this change.

## Nginx routing (proposed, not applied)

This repo does not currently contain a checked-in Nginx config, so no
existing config was modified. The following is a draft for whoever manages
the production Nginx config to review and apply manually:

```nginx
server {
    listen 80;
    server_name crowntime.cn www.crowntime.cn;

    root /var/www/crowntime-site;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }
}

# Existing WeCom archive backend — must remain untouched/unaffected:
# server_name qwhhcd.crowntime.cn;  -> proxied to the archive backend app
```

If HTTPS is configured (e.g. via certbot/Let's Encrypt), add the
corresponding `listen 443 ssl;` server block and redirect port 80 to 443
for `crowntime.cn`/`www.crowntime.cn` only. This does not affect the
`qwhhcd.crowntime.cn` server block.

## CI/CD

This directory **is** wired into the automated deploy pipeline:
`.github/workflows/deploy.yml` → `scripts/deploy_server.sh` step "[9/9]"
runs on every deploy to `main` (see `STATIC_SITE_DIR_NAME` above for the
target-directory caveat). There is no separate deploy path for this page
outside that pipeline step.

## Content notes

`index.html` contains the company's address, phone, email, and ICP beian
number. If any of these change (e.g. beian number is reassigned, office
address changes), update `index.html` directly and push to `main` — the
deploy pipeline will sync the change automatically.
