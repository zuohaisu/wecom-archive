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

## Target deployment directory (proposed)

On the production host, deploy the **entire** contents of
`static_site/company_homepage/` (including `brand/` and `assets/`, not just
`index.html` and `style.css`) to a dedicated static directory, e.g.:

```
/var/www/crowntime-site/
├── index.html
├── style.css
├── brand/
└── assets/
```

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

No CI/CD deploy script in this repo currently references a static site
target. No pipeline changes were made as part of this task. If an
automated deploy is desired later, propose a minimal, reviewed change
(e.g. an rsync/copy step to `/var/www/crowntime-site`) rather than wiring
this into the backend deploy pipeline.

## Content notes

`index.html` contains the company's address, phone, email, and ICP beian
number. If any of these change (e.g. beian number is reassigned, office
address changes), update `index.html` directly and redeploy the two
static files.
