"""
RND-157 — shared loader for the admin UI's i18n core JS asset.

The Locale Registry + I18N helpers live in a single JS file
(app/assets/i18n.js) so they can be edited with normal JS tooling. This
module reads that file once at import time and exposes it as a ready-to-embed
<script> tag, so both the login page (routers/auth.py) and the review
console (main.py) share the exact same registry — no locale definitions are
duplicated across Python strings.
"""

from __future__ import annotations

from pathlib import Path

_ASSET_PATH = Path(__file__).parent / "assets" / "i18n.js"
I18N_JS_SOURCE = _ASSET_PATH.read_text(encoding="utf-8")
I18N_SCRIPT_TAG = f"<script>\n{I18N_JS_SOURCE}\n</script>"
