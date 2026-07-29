# Design Agent Prompt — Crowntime WeCom Archive UI Design (Version 1)

> This document can be handed directly to the design team / design agent for execution. Goal: produce the first version of the complete product UI, spanning from the unauthenticated state, through tenant-level management, to the platform control console.

---

## 1. Your Role / Task

You are a senior B2B product UI designer / design systems engineer. Your task: produce the **first version of the UI** for **Crowntime WeCom Archive (康冠时代 企业微信会话存档)**, covering all pages listed below (see §6). The deliverables must be **directly consumable by frontend engineering** (see §7).

---

## 2. Product Positioning

- **What it is**: An enterprise-facing **compliance archiving and review platform for WeCom (Enterprise WeChat) conversations**. It pulls and decrypts employees' WeCom chats, allowing compliance / HR / legal teams to search, review, and export them within authorized scopes, with management capabilities such as usage analytics, user management, and auditing.
- **Who uses it**: Internal enterprise compliance administrators, HR, and legal staff; plus platform super-admins (who manage multiple tenants).
- **Character**: This is a **compliance / audit-class B2B tool** — **trustworthiness and information density take priority over flashiness**; professional, restrained, and traceable.

---

## 3. Brand & Copy

- Product name: **Crowntime WeCom Archive (康冠时代 企业微信会话存档)** (public-facing slug: `crowntime-wecom-archive`).
- Described as WeCom-related (descriptive usage), but must carry a **"not an official Tencent product" disclaimer** (shown on the login page and in the footer).
- The trademark is a black-and-white registered mark and may be used in any color; use a neutral primary color in the design, and **reserve a logo-upload / white-label slot** (a future capability — use a placeholder for now).
- Language: Chinese / English supported (i18n). Copy defaults to Chinese, but key UI text must be **translatable** (do not bake copy into images / non-editable layers).

---

## 4. ⚠️ Technical Constraints (determine whether the design can be implemented — must be followed)

1. **The frontend is SSR (server-side rendering) + vanilla JS, with no React / Vue or similar frameworks** (project architecture freeze D1). Therefore:
   - All page designs must be implementable as **server-rendered templates + vanilla JS interactions**; do not design complex flows that depend on frontend routing / SPA state.
   - Keep interactions and animations lightweight (within what vanilla JS / CSS transitions can achieve).
2. **Themes / skins are implemented via CSS variables (CSS custom properties)**. The colors and spacing you specify must map to a set of `--color-*` / `--space-*` / `--radius-*` / `--shadow-*` variables; skin switching = swapping variable values (e.g. via light / dark / brand-color variants). **Do not hard-code colors on elements.**
3. **Desktop-first**: This iteration does **not** include mobile adaptation (it has been cut). Designs only need to consider desktop breakpoints (main workspace ≥1280px, with graceful layout convergence down to 1024–1280). "Responsive" here means layout convergence on narrower desktops only — no phones.
4. **Current visual baseline** (from `base.css`, a minimal prototype style): `system-ui` font, link blue `#0070f3`, borders `#ccc`, badges green / red / yellow (`#d1fae5` / `#fee2e2` / `#fef3c7`). Your task is to **build a formal design system on top of this** (preserve semantic class names such as `.badge` / `.badge-success`, extensible), not to start from scratch in a way that breaks reuse.
5. Existing implemented pages (must be visually unified, no conflicts): `review_console.html` (review console), `messages.html`, `search.html` (search), `message_detail.html`, `diagnostics.html` (diagnostics). The new design should be consistent with their style.

---

## 5. Design Principles

- **Trustworthiness first**: Compliance / legal / HR are core users; the interface must feel professional, restrained, and traceable.
- **High-density readability**: Large amounts of tables / logs / chat timelines — high information density but with clear hierarchy (via alignment, whitespace, and status colors).
- **Clear status**: Every action / record must have an explicit status (success / failed / pending / silent), using a unified badge system.
- **Auditable presentation**: "Evidence"-class interfaces such as audit logs and exports should emphasize a **tamper-evident feel** (timestamps, actor, scope).
- **Visible permission layering**: Tenant-internal vs. platform super-admin — the information scope of the UI should show a clear distinction (see §6 Platform Console constraints).

---

## 6. Information Architecture & Page List (v1 Design Scope)

Priority: **P0 = must-have**, **P1 = strongly recommended**, **P2 = can be phase 2**.

### A. Authentication & Account (unauthenticated / account lifecycle)
- **P0 Login page**: WeCom OAuth primary entry + account/password secondary entry; "not an official Tencent product" disclaimer; brand slot; "Forgot password" link.
- **P0 Forgot-password flow**: Enter email → prompt "reset link sent, please check your inbox" → (illustrative) set new password. Email-based recovery is primary (no SMS).
- **P0 Settings page**: Split into "Account settings" (change password, bind phone number [placeholder]) and "Preferences" (skin switching = theme variables, language switching).

### B. Tenant-level Management Panel (Line A, tenant admin)
- **P0 Overview home / Dashboard**: Key metric cards (archive days, total messages, storage usage, monitored employee count, sync health) + recent activity + exception / alert bar. This is the post-login landing page.
- **P0 Usage Analytics page**: Total days, total messages, message-type breakdown (text / image / file / voice / video etc., pie / bar), storage composition, trends (weekly / monthly). Charts must be implementable as **lightweight SVG / Canvas (drawable with vanilla JS)**.
- **P0 User Management page**: User list (name / role / status / **last active time** / actions); actions include enable / disable and reset password. Note: use "last active time + silent-after-N-days marker" to replace the "offboarding inheritance" concept (we cannot actively detect offboarding).
- **P0 External Contacts list + detail**: List (name / company / tags / source / owning employee); detail page shows the full chat timeline related to that contact (wired into search).
- **P1 Advanced search enhancement**: Add filters to the existing search (time / employee / external contact / message type) + result export. Keep the existing search page style.
- **P1 Audit Log page**: Who / when / viewed or exported / which conversation / scope; filter + pagination; emphasize tamper-evidence.
- **P1 Media Library**: Grid browsing of all images / files, filterable by type / time, downloadable.
- **P2 First-run setup wizard**: Guide the user through basic configuration on first entry (multi-step form).

### C. Platform Control Console (Line C, super-admin)
- **P0 Platform Console**: Tenant list (total count + per-tenant name / corp_id / status / created time / message count / storage / active users / archive days / sync health); platform-level aggregate stats; tenant enable / disable.
  - **Privacy constraint (important)**: By default the super-admin **may only see tenant-level metadata and aggregate metrics**, and **must not display any tenant conversation content in the default view**. If content must be viewed, the UI must include an explicit authorization gate (e.g. "request access → logged to audit"). The design must reflect this information layering.
- **P1 Multi-tenant provisioning config**: New-tenant form (name / corp_id / WeCom config entry) + list management.

### D. Compliance Core (Line B, partially covered above)
- Evidence export (PDF / Excel) entry points must appear at the review console / search results / external-contact detail (button-level, P1).
- Data retention policy config page (P2, rules form).

---

## 7. Deliverable Requirements

Produce the following three categories so engineering can consume them directly:

1. **Design language / Design Tokens**: Provided as **CSS variables** (colors, fonts, type-scale, spacing, radius, shadow, status colors). Provide **light plus at least one alternative theme** (for the "skin switching" demo).
2. **Key-page visual comps + wireframes**: **P0 pages first**; P1 / P2 given as layout sketches. Explain information hierarchy, component usage, and **empty / loading / error states**.
3. **Component specs**: Tables, cards, badges (success / failed / pending / silent), buttons (primary / secondary / danger), forms / inputs, modals, side navigation, pagination, filters, chart containers, status bars. For each component, annotate the corresponding CSS variables and accessibility requirements (contrast, focus state).

> If executed by an **AI design agent**: output runnable **HTML + CSS (vanilla, strictly no React)** directly, with components driven by CSS variables, openable for local preview; charts via lightweight SVG / Canvas.

---

## 8. Explicitly Out of Scope

- Mobile (phone) adaptation.
- Sensitive-word / risk-monitoring alert UI (very low priority, deferred).
- A standalone "offboarded employee conversation inheritance" feature (merged into the User Management "last active / silent" marker).

---

## 9. Open Decisions for the Design Side to Propose First

- **How many themes for skin switching?** Suggest starting with light + dark.
- **Side navigation vs. top navigation?** Given the relatively large number of pages (dashboard / usage / users / external contacts / audit / media / settings), recommend a **fixed left sidebar + top breadcrumb**.
- **Charting approach**: Since there is no React, suggest native SVG (hand-drawn) or a lightweight library (e.g. Chart.js via CDN, subject to confirmation that external scripts are acceptable) — please state your choice and rationale.
