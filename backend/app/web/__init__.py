"""RND-216: minimal template + static-asset support for the admin backend.

No template engine is introduced on purpose (see the RND-216 execution
notes) — the admin HTML/CSS/JS is still hand-authored, just moved out of
`app/main.py`'s Python string literals into standalone files under
`templates/` and `static/`. `render_template` does plain `__TOKEN__`
substitution, which is all these pages ever needed (a handful of injection
points: the i18n script tag, one or two dynamic JSON blobs, and — for the
messages/message_detail pages — a few pre-escaped value strings the route
already built with `_e()`).
"""
import hashlib
import re
from pathlib import Path

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

_TOKEN_RE = re.compile(r"__([A-Z0-9_]+)__")


def _compute_static_version() -> str:
    """Hash of every file under static/, sorted by path for determinism.
    Recomputed once at import time — a new process picks up new content
    automatically (this repo has no build step to trigger it otherwise),
    which is what makes the `?v=` cache-busting query string actually bust
    the cache after a deploy."""
    digest = hashlib.md5()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


STATIC_VERSION = _compute_static_version()


def render_template(name: str, **ctx) -> str:
    """Single-pass __TOKEN__ substitution over the original template text.
    Deliberately not sequential str.replace calls: this admin console
    renders message/user-supplied content (msgid, content_text, ...) into
    these pages, and a sequential replace could re-scan and corrupt text
    that a *previous* substitution just inserted if it happened to contain
    another token's literal spelling. re.sub in one pass over the
    untouched template text can't do that. Raises if the template
    references a token no caller-supplied value covers, so a missing
    placeholder fails loudly instead of shipping a literal __TOKEN__ to
    the browser."""
    text = (TEMPLATES_DIR / f"{name}.html").read_text(encoding="utf-8")
    values = {"STATIC_VERSION": STATIC_VERSION}
    values.update({key.upper(): value for key, value in ctx.items()})

    def _sub(match: "re.Match") -> str:
        token = match.group(1)
        if token not in values:
            raise KeyError(f"unresolved template token __{token}__ in {name}.html")
        return values[token]

    text = _TOKEN_RE.sub(_sub, text)
    # GH-101: bootstrap the dark-theme contract on every design-system page
    # before first paint (theme.js sets <html data-theme> from localStorage
    # or prefers-color-scheme). Platform chrome intentionally stays light.
    if not name.startswith("platform") and "design-system.css" in text:
        theme_tag = f'<script src="/web/static/theme.js?v={STATIC_VERSION}"></script>'
        text = text.replace("</head>", theme_tag + "</head>", 1)
    return text
