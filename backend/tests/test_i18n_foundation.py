"""
Focused tests for RND-157 — Admin UI Internationalization (i18n) Foundation.

Scope: the Locale Registry + I18N helpers (app/assets/i18n.js), and their
wiring into the login page (routers/auth.py) and review console
(main.py's _REVIEW_CONSOLE_HTML). No backend, schema, auth, or API changes.

These tests execute the real i18n.js source under Node (same technique as
test_admin_auto_load_older.py / test_admin_group_participant_overflow.py),
plus structural checks against the rendered HTML, so assertions exercise
real behavior rather than only pattern-matching source text.

Run (from backend/):
    pytest tests/test_i18n_foundation.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")

_I18N_JS_PATH = Path(__file__).resolve().parent.parent / "app" / "assets" / "i18n.js"
_I18N_JS_SOURCE = _I18N_JS_PATH.read_text(encoding="utf-8")


def _run(js_body: str) -> object:
    """Run `js_body` after loading the real i18n.js source (with a stubbed
    localStorage backed by an in-memory object so persistence is testable
    without a browser), and return the JSON value it writes to stdout.

    `I18N_SOURCE` and `__store` stay in scope for the rest of `js_body`, so
    a test can call `eval(I18N_SOURCE)` again mid-script to simulate a page
    reload against the same (persisted) storage backing.
    """
    assert NODE, "node executable not found"
    harness = f"""
var __store={{}};
var localStorage={{
  getItem:function(k){{return Object.prototype.hasOwnProperty.call(__store,k)?__store[k]:null;}},
  setItem:function(k,v){{__store[k]=String(v);}},
  removeItem:function(k){{delete __store[k];}}
}};
var I18N_SOURCE={json.dumps(_I18N_JS_SOURCE)};
eval(I18N_SOURCE);
{js_body}
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Locale Registry — single source of truth, extensible without touching
# selector/lookup/persistence logic
# ---------------------------------------------------------------------------


def test_registry_contains_zh_cn_zh_tw_and_en() -> None:
    out = _run("process.stdout.write(JSON.stringify(Object.keys(LocaleRegistry).sort()));")
    assert out == ["en", "zh-CN", "zh-TW"]


def test_available_locales_is_generated_from_registry() -> None:
    out = _run(
        "process.stdout.write(JSON.stringify(I18N.availableLocales().map(function(l){return l.code;}).sort()));"
    )
    assert out == ["en", "zh-CN", "zh-TW"]


def test_get_locale_info_and_has_locale() -> None:
    out = _run(
        """
process.stdout.write(JSON.stringify({
  hasZhCn: I18N.hasLocale('zh-CN'),
  hasBogus: I18N.hasLocale('xx-XX'),
  zhCnNative: I18N.getLocaleInfo('zh-CN').nativeName,
  bogusInfo: I18N.getLocaleInfo('xx-XX')
}));
"""
    )
    assert out["hasZhCn"] is True
    assert out["hasBogus"] is False
    assert out["zhCnNative"] == "简体中文"
    assert out["bogusInfo"] is None


def test_registering_a_new_locale_changes_available_locales_with_no_other_code_change() -> None:
    """
    Adding a locale means adding one entry to LocaleRegistry — availableLocales(),
    hasLocale(), and getLocaleInfo() must all reflect it immediately since they
    derive from the registry object rather than a hardcoded list.
    """
    out = _run(
        """
var before = I18N.availableLocales().length;
LocaleRegistry['fr'] = {
  code: 'fr', name: 'French', nativeName: 'Français', direction: 'ltr',
  translations: { 'nav.staff': 'Personnel' }
};
process.stdout.write(JSON.stringify({
  before: before,
  after: I18N.availableLocales().length,
  hasFr: I18N.hasLocale('fr'),
  nativeName: I18N.getLocaleInfo('fr').nativeName,
  translated: I18N.t('nav.staff') // still zh-CN default locale; unaffected by the new entry
}));
"""
    )
    assert out["before"] == 3
    assert out["after"] == 4
    assert out["hasFr"] is True
    assert out["nativeName"] == "Français"
    assert out["translated"] == "员工"


# ---------------------------------------------------------------------------
# Selector logic must read from the registry, not a hardcoded list
# ---------------------------------------------------------------------------


def _extract_from_source(source: str, pattern: str, label: str) -> str:
    match = re.search(pattern, source, re.S)
    assert match is not None, f"{label} not found"
    return match.group(0)


def test_console_selector_renders_from_available_locales_not_hardcoded() -> None:
    from app.main import _REVIEW_CONSOLE_HTML

    src = _extract_from_source(
        _REVIEW_CONSOLE_HTML, r"function renderLangMenu\(\)\{.*?\n\}", "renderLangMenu()"
    )
    assert "I18N.availableLocales()" in src
    assert "zh-CN" not in src
    assert "zh-TW" not in src
    assert "'en'" not in src


def test_login_selector_renders_from_available_locales_not_hardcoded() -> None:
    from app.routers.auth import _I18N_BOOTSTRAP_JS

    src = _extract_from_source(
        _I18N_BOOTSTRAP_JS, r"function renderLangMenu\(\)\{.*?\n\}", "renderLangMenu()"
    )
    assert "I18N.availableLocales()" in src
    assert "zh-CN" not in src
    assert "zh-TW" not in src
    assert "'en'" not in src


# ---------------------------------------------------------------------------
# Default locale / persistence / fallback contract
# ---------------------------------------------------------------------------


def test_default_locale_is_zh_cn() -> None:
    out = _run("process.stdout.write(JSON.stringify(I18N.getLocale()));")
    assert out == "zh-CN"
    out2 = _run("process.stdout.write(JSON.stringify(I18N.defaultLocale));")
    assert out2 == "zh-CN"


def test_localstorage_restore_works_across_a_simulated_reload() -> None:
    out = _run(
        """
I18N.setLocale('zh-TW');
eval(I18N_SOURCE); // simulate a fresh page load reading the same localStorage
process.stdout.write(JSON.stringify(I18N.getLocale()));
"""
    )
    assert out == "zh-TW"


def test_invalid_locale_passed_to_set_locale_falls_back_to_zh_cn() -> None:
    out = _run(
        """
var applied = I18N.setLocale('xx-not-real');
process.stdout.write(JSON.stringify({applied: applied, current: I18N.getLocale()}));
"""
    )
    assert out["applied"] == "zh-CN"
    assert out["current"] == "zh-CN"


def test_invalid_stored_locale_falls_back_to_zh_cn_on_load() -> None:
    out = _run(
        """
__store['wecom_admin_locale'] = 'not-a-real-locale';
eval(I18N_SOURCE);
process.stdout.write(JSON.stringify(I18N.getLocale()));
"""
    )
    assert out == "zh-CN"


def test_missing_key_in_current_locale_falls_back_to_zh_cn_value() -> None:
    out = _run(
        """
I18N.setLocale('en');
delete LocaleRegistry['en'].translations['nav.staff'];
process.stdout.write(JSON.stringify(I18N.t('nav.staff')));
"""
    )
    assert out == "员工"


def test_key_missing_everywhere_falls_back_to_the_key_name_itself() -> None:
    out = _run("process.stdout.write(JSON.stringify(I18N.t('totally.unknown.key')));")
    assert out == "totally.unknown.key"


def test_t_never_throws_for_odd_input() -> None:
    out = _run(
        """
var results = [];
[null, undefined, 123, '', 'a.b.c.d.e'].forEach(function(k){
  try { I18N.t(k); results.push('ok'); } catch (e) { results.push('ERROR:' + e.message); }
});
process.stdout.write(JSON.stringify(results));
"""
    )
    assert out == ["ok", "ok", "ok", "ok", "ok"]


# ---------------------------------------------------------------------------
# Language switching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["zh-CN", "zh-TW", "en"])
def test_switch_to_each_supported_locale(code: str) -> None:
    out = _run(
        f"""
I18N.setLocale({json.dumps(code)});
process.stdout.write(JSON.stringify(I18N.getLocale()));
"""
    )
    assert out == code


def test_refresh_preserves_selected_locale() -> None:
    out = _run(
        """
I18N.setLocale('en');
eval(I18N_SOURCE); // simulated refresh: fresh module instance, same localStorage
process.stdout.write(JSON.stringify(I18N.getLocale()));
"""
    )
    assert out == "en"


def test_on_change_listener_fires_with_the_newly_selected_locale() -> None:
    out = _run(
        """
var seen = [];
I18N.onChange(function(code){ seen.push(code); });
I18N.setLocale('zh-TW');
I18N.setLocale('en');
process.stdout.write(JSON.stringify(seen));
"""
    )
    assert out == ["zh-TW", "en"]


# ---------------------------------------------------------------------------
# Sample admin labels translate correctly in all three locales
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        (
            "zh-CN",
            {
                "nav.staff": "员工",
                "nav.logout": "退出登录",
                "refresh.manual": "刷新",
                "history.noMore": "没有更多历史消息",
                "media.image": "图片消息",
            },
        ),
        (
            "zh-TW",
            {
                "nav.staff": "員工",
                "nav.logout": "登出",
                "refresh.manual": "重新整理",
                "history.noMore": "沒有更多歷史訊息",
                "media.image": "圖片訊息",
            },
        ),
        (
            "en",
            {
                "nav.staff": "Staff",
                "nav.logout": "Logout",
                "refresh.manual": "Refresh",
                "history.noMore": "No more history",
                "media.image": "Image message",
            },
        ),
    ],
)
def test_sample_admin_labels_translate_per_locale(code: str, expected: dict) -> None:
    lookups = "".join(f"result[{json.dumps(k)}]=I18N.t({json.dumps(k)});" for k in expected)
    out = _run(
        f"""
I18N.setLocale({json.dumps(code)});
var result = {{}};
{lookups}
process.stdout.write(JSON.stringify(result));
"""
    )
    assert out == expected


# ---------------------------------------------------------------------------
# UI coverage — settings entry exists on both pages, and RND-152/RND-153
# labels still render, now going through i18n instead of hardcoded strings
# ---------------------------------------------------------------------------


def test_console_html_has_i18n_core_and_language_selector() -> None:
    from app.main import _REVIEW_CONSOLE_HTML

    assert "I18N_CORE_START" in _REVIEW_CONSOLE_HTML
    assert 'id="lang-switch"' in _REVIEW_CONSOLE_HTML
    assert 'id="lang-menu"' in _REVIEW_CONSOLE_HTML
    assert 'data-i18n="nav.language"' in _REVIEW_CONSOLE_HTML


@pytest.mark.parametrize("mode", ["password", "wecom"])
def test_login_html_has_i18n_core_and_language_selector(mode: str) -> None:
    from app.routers.auth import _login_page

    html = _login_page(mode=mode)
    assert "I18N_CORE_START" in html
    assert 'id="lang-switch"' in html
    assert 'id="lang-menu"' in html
    assert 'data-i18n="nav.language"' in html


def test_login_password_mode_has_i18n_placeholders_and_submit_key() -> None:
    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    assert 'data-i18n-placeholder="login.username"' in html
    assert 'data-i18n-placeholder="login.password"' in html
    assert 'data-i18n="login.submit"' in html


def test_login_error_banner_uses_i18n_key_with_zh_cn_fallback_text() -> None:
    from app.routers.auth import _login_page

    html = _login_page(mode="wecom", error="user_inactive")
    assert 'data-i18n="login.error.userInactive"' in html
    assert "您的企业微信账号已停用" in html


def test_rnd152_history_labels_render_through_i18n_in_all_locales() -> None:
    """RND-152's loading/end-of-history/retry banners must still work — now
    sourced from I18N.t() instead of hardcoded English."""
    from app.main import _REVIEW_CONSOLE_HTML

    i18n_core = _extract_from_source(
        _REVIEW_CONSOLE_HTML, r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"
    )
    history_status_el = _extract_from_source(
        _REVIEW_CONSOLE_HTML, r"function historyStatusEl\(\)\{.*?\}", "historyStatusEl()"
    )
    show_loading = _extract_from_source(
        _REVIEW_CONSOLE_HTML, r"function showLoadingOlder\(\)\{.*?\n\}", "showLoadingOlder()"
    )
    show_end = _extract_from_source(
        _REVIEW_CONSOLE_HTML, r"function showEndOfHistory\(\)\{.*?\n\}", "showEndOfHistory()"
    )
    retry_html = _extract_from_source(
        _REVIEW_CONSOLE_HTML, r"function historyRetryHtml\(\)\{.*?\n\}", "historyRetryHtml()"
    )

    expectations = {
        "zh-CN": ("正在加载更多历史消息", "没有更多历史消息", "历史消息加载失败"),
        "zh-TW": ("正在載入更多歷史訊息", "沒有更多歷史訊息", "歷史訊息載入失敗"),
        "en": ("Loading older messages", "No more history", "Failed to load history"),
    }
    for code, (loading_sub, end_sub, retry_sub) in expectations.items():
        harness = f"""
{i18n_core}
I18N.setLocale({json.dumps(code)});
var historyStatusHtml='';
var historyStatusElObj={{
  get innerHTML(){{return historyStatusHtml;}},
  set innerHTML(v){{historyStatusHtml=v;}}
}};
var document={{getElementById:function(id){{
  if(id==='timeline-history-status')return historyStatusElObj;
  throw new Error('unexpected getElementById('+id+')');
}}}};
{history_status_el}
{show_loading}
{show_end}
{retry_html}
showLoadingOlder();
var loadingHtml=historyStatusHtml;
showEndOfHistory();
var endHtml=historyStatusHtml;
var retryHtmlStr=historyRetryHtml();
process.stdout.write(JSON.stringify({{loading:loadingHtml,end:endHtml,retry:retryHtmlStr}}));
"""
        result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"[{code}] node harness failed: {result.stderr}"
        out = json.loads(result.stdout)
        assert loading_sub in out["loading"], f"[{code}] {out['loading']!r}"
        assert end_sub in out["end"], f"[{code}] {out['end']!r}"
        assert retry_sub in out["retry"], f"[{code}] {out['retry']!r}"
