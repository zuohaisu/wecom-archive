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
from app.routers.web import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON, _SEARCH_MSGTYPE_OPTIONS_JSON
from app.web import render_template
from app.web.sidenav import render_sidenav

_STATIC_DIR = Path(__file__).parent.parent / "app" / "web" / "static"

# RND-217: review-console.js was split into 8 modules under static/console/,
# loaded by review_console.html as 8 ordered <script src> tags instead of one
# <script src="review-console.js">. This is the same dependency order the
# template uses (function/var hoisting is per-<script>-tag, not global across
# tags, so state must load before anything reads it and console-entry's
# bootstrap sequence must load last).
_CONSOLE_JS_MODULES = [
    "console-state.js",
    "api-client.js",
    "conversation-list.js",
    "timeline.js",
    "delete-timeline.js",
    "timeline-favorites.js",
    "message-renderers.js",
    "media-viewer.js",
    "refresh.js",
    "console-entry.js",
]

# message-renderers.js carries this exact comment at the point where
# media-viewer.js's <script> tag loads in the real page -- see that comment
# for why. A handful of pre-existing Node-harness tests regex-extract the
# RND-206 "var MediaAccessCache=(function(){...function renderCompositeMessage
# (m){...}" span out of the review console JS as a single contiguous chunk
# (it predates RND-217 and was written against the old single-file
# review-console.js, where the Viewer -- including the shared
# timelineViewerItems / registerViewerItem registry those tests rely on --
# lived inline in the middle of that span). RND-217 moved the Viewer (and its
# registry) into media-viewer.js; splicing media-viewer.js's real content
# back in at this marker, for the test-facing bundle only, reproduces that
# exact contiguous text so those tests keep working unmodified -- the real
# browser page never sees this splice, it just loads media-viewer.js as its
# own next <script> tag.
_VIEWER_SPLICE_MARKER = (
    "/* RND-217: the shared Viewer registry (timelineViewerItems /\n"
    "   registerViewerItem) now lives in media-viewer.js alongside the rest of\n"
    "   the Viewer's state and code. renderTimeline() resets timelineViewerItems\n"
    "   and the composite renderers below call registerViewerItem() at runtime;\n"
    "   both are globals defined by media-viewer.js, which loads immediately\n"
    "   after this file, so they resolve before any renderTimeline() pass runs. */\n"
)


def _static_js(filename: str) -> str:
    return (_STATIC_DIR / filename).read_text(encoding="utf-8")


def _console_js_bundle() -> str:
    modules = {name: _static_js(f"console/{name}") for name in _CONSOLE_JS_MODULES}
    assert _VIEWER_SPLICE_MARKER in modules["message-renderers.js"], (
        "media-viewer.js splice marker not found in message-renderers.js -- "
        "did the marker comment text drift out of sync between the two files?"
    )
    modules["message-renderers.js"] = modules["message-renderers.js"].replace(
        _VIEWER_SPLICE_MARKER, _VIEWER_SPLICE_MARKER + modules["media-viewer.js"], 1
    )
    ordered = [name for name in _CONSOLE_JS_MODULES if name != "media-viewer.js"]
    return "\n".join(modules[name] for name in ordered)


def review_console_html() -> str:
    return render_template(
        "review_console",
        i18n_script=I18N_SCRIPT_TAG,
        mtr_entries_json=_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON,
        # Archive Console v2 (design import): review_console.html now also
        # injects the search msgtype catalog (reused, unchanged, from the
        # standalone search page) for its inline "全部类型" filter chip.
        msgtype_options_json=_SEARCH_MSGTYPE_OPTIONS_JSON,
        sidenav=render_sidenav(
            "review",
            {
                "/admin/conversations",
                "/admin/search",
                "/admin/messages",
                "/admin/diagnostics/reachability",
                "/admin/settings",
                "/api/admin/sync-status",
            },
        ),
    )


def search_page_html() -> str:
    return render_template(
        "search",
        i18n_script=I18N_SCRIPT_TAG,
        msgtype_options_json=_SEARCH_MSGTYPE_OPTIONS_JSON,
        sidenav=render_sidenav(
            "search",
            {
                "/admin/conversations",
                "/admin/search",
                "/admin/messages",
                "/admin/diagnostics/reachability",
                "/admin/settings",
            },
        ),
    )


def diagnostics_html() -> str:
    return render_template(
        "diagnostics",
        i18n_script=I18N_SCRIPT_TAG,
        sidenav=render_sidenav(
            "diagnostics",
            {
                "/admin/conversations",
                "/admin/search",
                "/admin/messages",
                "/admin/diagnostics/reachability",
                "/admin/settings",
                "/api/admin/sync-status",
            },
        ),
    )


def review_console_js_source() -> str:
    return I18N_JS_SOURCE + "\n" + _console_js_bundle()


def search_js_source() -> str:
    return I18N_JS_SOURCE + "\n" + _static_js("search.js")


def diagnostics_js_source() -> str:
    return I18N_JS_SOURCE + "\n" + _static_js("diagnostics.js")
