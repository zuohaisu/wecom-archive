---
kind: frontend_style
name: Plain CSS + Vanilla JS Console with Design Tokens
category: frontend_style
scope:
    - '**'
source_files:
    - backend/app/web/templates/review_console.html
    - backend/app/web/static/base.css
    - backend/app/web/static/diagnostics.css
    - backend/app/web/static/console/console-entry.js
    - static_site/company_homepage/style.css
---

The frontend styling in this monorepo is a lightweight, server-rendered approach with no build step, component framework, or CSS preprocessor. It consists of two distinct UI surfaces:

1. **Admin Web Console** (`backend/app/web/templates/review_console.html` and `backend/app/web/static/console/`)
   - A single HTML template with an embedded `<style>` block (~170 lines) that defines the console's layout (three-column: staff/contact list, conversations, timeline), bubble styles for messages, search bar, language switcher, media viewer overlay, and structured message cards.
   - Global base styles live in `backend/app/web/static/base.css` — minimal resets for body, tables, forms, badges, and grid definitions.
   - Diagnostics page has its own scoped stylesheet `diagnostics.css`.
   - JavaScript is vanilla ES5-style modules loaded via separate `<script src=...>` tags: `console-entry.js`, `api-client.js`, `console-state.js`, `conversation-list.js`, `timeline.js`, `message-renderers.js`, `media-viewer.js`, `refresh.js`. No bundler, no imports — files are referenced directly by URL with cache-busting `?v=__STATIC_VERSION__`.
   - Styling uses CSS custom properties (`--bubble-self-bg`, `--link-color`, etc.) defined inline in the template's `:root` for theming within the page.

2. **Company Homepage Static Site** (`static_site/company_homepage/`)
   - A standalone static site built with pure CSS and a checkbox-hack dark mode toggle — no JavaScript for theme switching.
   - Centralized design tokens in `style.css` using CSS custom properties: color palette (`--color-primary`, `--color-ink*`, `--color-surface-*`, `--color-hairline*`), spacing (`--space-section`, `--space-section-mobile`), radii (`--radius-md`, `--radius-lg`, `--radius-pill`), typography (`--font-body`), and max-widths.
   - Dark mode is implemented purely via CSS sibling selectors (`#theme-toggle:checked ~ ...`) overriding the same token names.
   - Responsive breakpoints at 900px, 700px, and 480px using standard `@media` queries.

**Architecture & Conventions:**
- No CSS frameworks (no Tailwind, Bootstrap, etc.), no preprocessors (Sass/Less/PostCSS), no module bundlers (Webpack/Vite).
- Styles are co-located with their templates/pages rather than shared across components — each page owns its CSS.
- Design tokens are expressed as CSS custom properties (`var(--name)`), not SCSS variables or a centralized config file.
- The console uses a flat class naming scheme (BEM-like but without strict methodology): `.top-bar`, `.col-left`, `.tl-bubble`, `.structured-card`, `.search-bar`, etc.
- All assets are served directly from Flask's `static/` directory; versioning is done via query string parameters injected into templates.
- Internationalization is handled via a small `I18N` object and `data-i18n` attributes on elements, not through CSS.

**Rules developers should follow:**
- Keep CSS scoped to the page it belongs to; avoid global style bleed between templates.
- Use CSS custom properties for any new colors, spacing, or radii to maintain consistency with existing tokens.
- Prefer vanilla DOM manipulation over introducing a JS framework — the existing codebase uses direct `document.querySelector`/`addEventListener` patterns.
- Do not introduce build steps; all assets must be plain `.css` and `.js` files served statically.
- Follow the existing class naming convention: descriptive, lowercase-with-dashes, no utility classes.