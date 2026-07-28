# Crowntime WeCom Archive — Design System & UI v1

The **shipped UI baseline** for Crowntime WeCom Archive (康冠时代 企业微信会话存档), synced from the `wecom-archive-365` repo, plus the design system extracted from it and the v1 page set built on top.

**Start at `index.html`** — the delivery index linking every token sheet, spec card, and page.

## What was added (2026-07-28)

- `styles.css` — the design system. Tokens (`--color-*`, `--space-*`, `--radius-*`, `--shadow-*`) extracted from the shipped `review_console.html` / `search.html` / `diagnostics.css` chrome, plus a full component layer (buttons, forms, tables, badges, chips, tabs, pagination, alerts, modals, charts, app shell). Ships light + dark; theme switch = `document.documentElement.dataset.theme`. Legacy `base.css` class names (`.badge`, `.badge-success`, `.badge-failed`, `.badge-pending`, table/form/link styles) are preserved and re-pointed at variables, so existing SSR templates keep working.
- `ds/*.html` — the spec cards (color, type/space/elevation, controls, data display, shell & permission planes). Also visible in the Design System tab.
- `pages/*.html` — the v1 page set: login, forgot-password, settings, dashboard, analytics, users, contacts, contact-detail, search-advanced, audit-log, media, platform. Vanilla HTML + vanilla JS only; `pages/shell.js` renders the side nav from one config (becomes a Jinja include in production).
- Charts are hand-written SVG driven by small vanilla-JS functions — no chart library, no CDN. SVG inherits the CSS variables, so dark mode and white-label re-skins cost nothing.

## How to use this project

- `brief/design-agent-prompt-2026-07-28.md` — the actual design brief. Start here for scope, constraints, and the page list (v1).
- `tokens/base.css` — the pre-existing live CSS in production today (superseded by `styles.css`, kept for diffing). It is intentionally minimal (a prototype baseline). Preserve its semantic class names (`.badge`, `.badge-success`, `.badge-failed`, `.badge-pending`, table/form/link styles) and **extend them with CSS variables** rather than replacing or renaming them — engineering wires these classes into server-rendered templates already.
- `tokens/diagnostics.css` — supplementary styles for the diagnostics page.
- `reference/*.html` — the 5 pages already implemented and live (`review_console.html`, `messages.html`, `search.html`, `message_detail.html`, `diagnostics.html`, plus the `message_detail_404.html` empty state). These are Jinja/SSR templates with vanilla-JS hooks, not static mockups — treat them as ground truth for existing information density, layout, and interaction patterns. New pages must feel like siblings of these, not a redesign of them.
- `brand/*` — existing logo marks (horizontal, reversed, icon-only) and favicon set. The brief calls for a black-and-white mark usable in any color plus a reserved white-label/logo-upload slot — these are the actual current assets, not placeholders to invent.

## Hard constraints from the brief (do not violate)

- SSR + vanilla JS only — no React/Vue, no SPA routing assumptions.
- Theming = CSS custom properties (`--color-*`, `--space-*`, `--radius-*`, `--shadow-*`); never hard-code colour.
- Desktop-first, ≥1280px, graceful convergence down to 1024–1280. No phone layouts.
- Must stay visually consistent with `reference/*.html` — no conflicting new patterns for things those pages already solve (tables, badges, forms).

## Status in this repo (as of this sync)

This directory is a **design reference mirror**, not live application code. Of the pages above, only the login page's *visual design* has been carried into the running app so far (`backend/app/web/templates/login.html` + `backend/app/routers/auth.py`, restyled in place with no behavior change — WeCom OAuth and password login both still work exactly as before). `styles.css` was added to `backend/app/web/static/` as an additive stylesheet; `base.css`/`diagnostics.css` and the 5 reference pages were left untouched.

The other 15 pages (dashboard, analytics, users, contacts, contact-detail, search-advanced, audit-log, media, settings, platform, tenant-provisioning, onboarding, onboarding-invite, onboarding-done, forgot-password) have no backend behind them yet — see `deliverables/feature-roadmap-2026-07-28.md` for effort estimates per feature. They stay here as a locally-openable, self-contained reference until each feature is built, at which point that page gets its own route + real template rather than being wired up with placeholder data.

`reference/*.html` from the original design-tool sync was intentionally **not** mirrored into this directory — those 5 pages already exist live in `backend/app/web/templates/` and are the actual source of truth; keeping a second static copy here would just drift.
