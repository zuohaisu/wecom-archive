"""RND-216 shared test helper.

app/main.py's former inline HTML/CSS/JS constants (_REVIEW_CONSOLE_HTML,
_SEARCH_PAGE_HTML, _DIAGNOSTICS_HTML) were extracted to standalone files
under app/web/templates/ and app/web/static/, and are now assembled
per-request via app.web.render_template plus a <script src="..."> reference
to the static JS file, instead of one big inlined Python string.

Two distinct things pre-existing tests needed from those old constants, now
served by two distinct functions here — deliberately NOT reassembled into a
single synthetic "page including its embedded JS" string:

- The real rendered HTML page (*_html functions) — for assertions about
  actual DOM markup / template text (element ids, static labels, the
  <script src>/<link> references themselves).
- The real JS source a page loads, in load order (*_js_source functions) —
  i18n.js's raw text followed by the page's own static JS file's raw text,
  read directly off disk — for tests that extract and/or Node-execute a JS
  function body. This must NOT be sourced from the rendered HTML: the page
  only references its JS via <script src>, it doesn't inline it anymore.

Not byte-identical to the pre-RND-216 constants (the diagnostics page now
links external CSS instead of an inline <style> tag, and review-console.js/
search.js read their injected JSON off an `RND216_MTR_ENTRIES` /
`RND216_MSGTYPE_OPTIONS` global instead of a literal JSON blob inlined into
the script body) — those are the intentional structural changes RND-216
made. Every function body and every bit of template markup is otherwise
unchanged.
"""
from __future__ import annotations

from pathlib import Path

from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG
from app.main import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON, _SEARCH_MSGTYPE_OPTIONS_JSON
from app.web import render_template

_STATIC_DIR = Path(__file__).parent.parent / "app" / "web" / "static"


def _static_js(filename: str) -> str:
    return (_STATIC_DIR / filename).read_text(encoding="utf-8")


def review_console_html() -> str:
    return render_template(
        "review_console",
        i18n_script=I18N_SCRIPT_TAG,
        mtr_entries_json=_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON,
    )


def search_page_html() -> str:
    return render_template(
        "search",
        i18n_script=I18N_SCRIPT_TAG,
        msgtype_options_json=_SEARCH_MSGTYPE_OPTIONS_JSON,
    )


def diagnostics_html() -> str:
    return render_template("diagnostics", i18n_script=I18N_SCRIPT_TAG)


def review_console_js_source() -> str:
    return I18N_JS_SOURCE + "\n" + _static_js("review-console.js")


def search_js_source() -> str:
    return I18N_JS_SOURCE + "\n" + _static_js("search.js")


def diagnostics_js_source() -> str:
    return I18N_JS_SOURCE + "\n" + _static_js("diagnostics.js")
