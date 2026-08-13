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
from pathlib import Path

import pytest

from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_html, review_console_js_source

_REVIEW_CONSOLE_HTML = review_console_html()
_REVIEW_CONSOLE_JS = review_console_js_source()

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
    result = run_node(harness)
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
    src = _extract_from_source(
        _REVIEW_CONSOLE_JS, r"function renderLangMenu\(\)\{.*?\n\}", "renderLangMenu()"
    )
    assert "I18N.availableLocales()" in src
    assert "zh-CN" not in src
    assert "zh-TW" not in src
    assert "'en'" not in src


def test_login_selector_renders_from_available_locales_not_hardcoded() -> None:
    # Design import (2026-07-28): the login page's i18n bootstrap JS moved
    # from the removed _I18N_BOOTSTRAP_JS Python constant into
    # app/web/templates/login.html (rendered via render_template, same as
    # review_console/search/diagnostics already were per RND-216) — extract
    # renderLangMenu() from the actual rendered page instead, so this still
    # exercises the real served source rather than a stale constant.
    from app.routers.auth import _login_page

    html = _login_page(mode="wecom")
    src = _extract_from_source(
        html, r"function renderLangMenu\(\)\{.*?\n\}", "renderLangMenu()"
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
                "sync.now": "立即同步",
                "history.noMore": "没有更多历史消息",
                "media.image": "图片消息",
            },
        ),
        (
            "zh-TW",
            {
                "nav.staff": "員工",
                "nav.logout": "登出",
                "sync.now": "立即同步",
                "history.noMore": "沒有更多歷史訊息",
                "media.image": "圖片訊息",
            },
        ),
        (
            "en",
            {
                "nav.staff": "Staff",
                "nav.logout": "Logout",
                "sync.now": "Sync Now",
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


@pytest.mark.parametrize(
    "code,expected",
    [
        (
            "zh-CN",
            {
                "console.monitoredAccounts": "存档员工",
                "console.pickScope": "请选择员工或外部联系人查看会话",
                "console.scopeLabelStaffPrefix": "存档员工：",
                "console.selectAccountOrContact": "请选择员工或外部联系人查看会话",
            },
        ),
        (
            "zh-TW",
            {
                "console.monitoredAccounts": "存檔員工",
                "console.pickScope": "請選擇員工或外部聯絡人查看會話",
                "console.scopeLabelStaffPrefix": "存檔員工：",
                "console.selectAccountOrContact": "請選擇員工或外部聯絡人查看會話",
            },
        ),
        (
            "en",
            {
                "console.monitoredAccounts": "Archived Employees",
                "console.pickScope": "Select an employee or external contact to view conversations",
                "console.scopeLabelStaffPrefix": "Archived Employee: ",
                "console.selectAccountOrContact": "Select an employee or external contact to view conversations",
            },
        ),
    ],
)
def test_conversation_archive_terminology_translates_per_locale(code: str, expected: dict) -> None:
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


def test_conversation_template_uses_archive_employee_fallback_copy() -> None:
    scope_label = re.search(r'<span class="scope-label" id="scope-label">([^<]+)</span>', _REVIEW_CONSOLE_HTML)
    entity_header = re.search(
        r'<div class="col-header" id="entity-header" data-i18n="console\.monitoredAccounts">([^<]+)</div>',
        _REVIEW_CONSOLE_HTML,
    )
    conversation_empty_state = re.search(
        r'<div class="col-body" id="conv-body"><div class="empty-state" data-i18n="console\.selectAccountOrContact">([^<]+)</div></div>',
        _REVIEW_CONSOLE_HTML,
    )

    assert scope_label and scope_label.group(1) == "请选择员工或外部联系人查看会话"
    assert entity_header and entity_header.group(1) == "存档员工"
    assert conversation_empty_state and conversation_empty_state.group(1) == "请选择员工或外部联系人查看会话"


# ---------------------------------------------------------------------------
# UI coverage — settings entry exists on both pages and console labels render
# through i18n instead of hardcoded strings.
# ---------------------------------------------------------------------------


def test_console_html_has_i18n_core_and_language_selector() -> None:
    assert "I18N_CORE_START" in _REVIEW_CONSOLE_JS
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


# ---------------------------------------------------------------------------
# QA round (2026-07-28) — login.subtitle independence, brand-icon removal,
# and the inert forgot-password entry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("zh-CN", "对话审阅控制台"),
        ("zh-TW", "對話審閱控制台"),
        ("en", "Conversation Review Console"),
    ],
)
def test_login_subtitle_translates_per_locale(code: str, expected: str) -> None:
    out = _run(
        f"""
I18N.setLocale({json.dumps(code)});
process.stdout.write(JSON.stringify(I18N.t('login.subtitle')));
"""
    )
    assert out == expected


@pytest.mark.parametrize("mode", ["password", "wecom"])
def test_login_subtitle_is_an_independent_key_not_app_subtitle(mode: str) -> None:
    """The login page's hero subtitle must be its own login.subtitle key, not
    a reuse of app.subtitle (which review_console.html also renders) — so the
    two copy points can diverge later without one edit silently changing the
    other page."""
    from app.routers.auth import _login_page

    html = _login_page(mode=mode)
    assert 'data-i18n="login.subtitle"' in html
    assert 'data-i18n="app.subtitle"' not in html


@pytest.mark.parametrize("mode", ["password", "wecom"])
def test_login_page_has_no_hand_drawn_wecom_icon(mode: str) -> None:
    """The WeCom button previously carried a hand-drawn SVG approximation of
    a WeCom-style glyph, which risked reading as an official WeCom/Tencent
    mark next to the page's own "not an official Tencent product" disclaimer.
    Removed in favor of a plain text button — no icon is safer than a
    look-alike one here."""
    from app.routers.auth import _login_page

    html = _login_page(mode=mode)
    assert "<svg" not in html


def test_wecom_button_still_links_to_the_real_oauth_endpoint() -> None:
    from app.routers.auth import _login_page

    html = _login_page(mode="wecom")
    assert 'href="/api/auth/wecom/login"' in html
    assert 'data-i18n="login.wecomButton"' in html


def test_login_password_mode_links_to_password_recovery() -> None:
    from app.routers.auth import _login_page

    html = _login_page(mode="password")
    assert 'href="/admin/forgot-password"' in html
    assert 'data-i18n="login.forgotPassword"' in html
    assert 'forgot-password-disabled' not in html
    assert 'login.forgotPasswordDisabled' not in html


def test_password_mode_still_has_no_wecom_link_and_wecom_mode_has_no_password_field() -> (
    None
):
    """AUTH_MODE-driven single-entry behavior is unchanged by the redesign:
    the page still renders exactly one of the two login methods, never both
    side by side — the mockup's combined "WeCom button + divider + password
    form" layout is explicitly deferred pending an auth product decision."""
    from app.routers.auth import _login_page

    pw_html = _login_page(mode="password")
    assert "wecom/login" not in pw_html
    assert 'data-i18n="login.wecomButton"' not in pw_html

    wc_html = _login_page(mode="wecom")
    assert 'type="password"' not in wc_html
    assert 'id="pwd-form"' not in wc_html


# ---------------------------------------------------------------------------
# QA follow-up (2026-07-28) — the login page's marketing/legal copy (the 3
# compliance selling points, both disclaimers, the hero title, the timezone
# note, and <title>) previously rendered as hardcoded
# zh-CN text baked into templates/login.html, so switching locale left the
# form translated but the rest of the page stuck in Chinese. All of it now
# goes through data-i18n, same as the rest of the page.
# ---------------------------------------------------------------------------

_LOGIN_MARKETING_I18N_KEYS = [
    "login.pageTitle",
    "login.heroTitle",
    "login.timezoneNote",
    "login.point1Title",
    "login.point1Body",
    "login.point2Title",
    "login.point2Body",
    "login.point3Title",
    "login.point3Body",
    "login.legalDisclaimerPrefix",
    "login.legalDisclaimerEmphasis",
    "login.legalDisclaimerSuffix",
    "login.legalDisclaimerShort",
]


@pytest.mark.parametrize("mode", ["password", "wecom"])
def test_login_marketing_and_legal_copy_all_use_i18n_keys(mode: str) -> None:
    """Every marketing/legal string must carry data-i18n."""
    from app.routers.auth import _login_page

    html = _login_page(mode=mode)
    for key in _LOGIN_MARKETING_I18N_KEYS:
        assert f'data-i18n="{key}"' in html, f"missing data-i18n for {key}"


@pytest.mark.parametrize(
    "code,expected",
    [
        (
            "zh-CN",
            {
                "login.pageTitle": "登录 — Crowntime WeCom Archive",
                "login.heroTitle": "登录会话存档控制台",
                "login.timezoneNote": "时间均为北京时间 (UTC+8)",
                "login.point1Title": "合规留存",
                "login.point2Title": "可追溯审阅",
                "login.point3Title": "最小授权",
                "login.legalDisclaimerShort": "非腾讯官方产品 · © 2026 康冠时代",
            },
        ),
        (
            "zh-TW",
            {
                "login.pageTitle": "登入 — Crowntime WeCom Archive",
                "login.heroTitle": "登入會話存檔控制台",
                "login.timezoneNote": "時間均為北京時間 (UTC+8)",
                "login.point1Title": "合規留存",
                "login.point2Title": "可追溯審閱",
                "login.point3Title": "最小授權",
                "login.legalDisclaimerShort": "非騰訊官方產品 · © 2026 康冠時代",
            },
        ),
        (
            "en",
            {
                "login.pageTitle": "Login — Crowntime WeCom Archive",
                "login.heroTitle": "Log in to the Conversation Archive Console",
                "login.timezoneNote": "All times shown in Beijing Time (UTC+8)",
                "login.point1Title": "Compliance retention",
                "login.point2Title": "Traceable review",
                "login.point3Title": "Least privilege",
                "login.legalDisclaimerShort": "Not an official Tencent product · © 2026 Crown Time",
            },
        ),
    ],
)
def test_login_marketing_copy_translates_per_locale(code: str, expected: dict) -> None:
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


@pytest.mark.parametrize("code", ["zh-CN", "zh-TW", "en"])
def test_login_legal_disclaimer_segments_concatenate_cleanly(code: str) -> None:
    """The long disclaimer is split into prefix/emphasis/suffix keys so the
    <strong> emphasis survives translation (I18N only ever sets
    textContent, never innerHTML — see applyI18n() in login.html). Every
    locale's three segments must still read as one coherent sentence when
    concatenated with no separator, exactly how the DOM assembles them."""
    out = _run(
        f"""
I18N.setLocale({json.dumps(code)});
var full = I18N.t('login.legalDisclaimerPrefix') + I18N.t('login.legalDisclaimerEmphasis') + I18N.t('login.legalDisclaimerSuffix');
process.stdout.write(JSON.stringify(full));
"""
    )
    if code == "zh-CN":
        assert out == (
            "本产品为康冠时代自主开发的企业微信会话存档合规工具，"
            "与腾讯公司无隶属、赞助或认可关系，非腾讯官方产品"
            "。「企业微信」为腾讯公司注册商标，此处仅作功能描述性使用。"
        )
    elif code == "zh-TW":
        assert out == (
            "本產品為康冠時代自主開發的企業微信會話存檔合規工具，"
            "與騰訊公司無隸屬、贊助或認可關係，非騰訊官方產品"
            "。「企業微信」為騰訊公司註冊商標，此處僅作功能描述性使用。"
        )
    else:
        assert "Tencent" in out
        assert "WeCom" in out
        assert out.count(".") >= 2


def test_rnd152_history_labels_render_through_i18n_in_all_locales() -> None:
    """RND-152's loading/end-of-history/retry banners must still work — now
    sourced from I18N.t() instead of hardcoded English."""
    i18n_core = _extract_from_source(
        _REVIEW_CONSOLE_JS, r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"
    )
    history_status_el = _extract_from_source(
        _REVIEW_CONSOLE_JS, r"function historyStatusEl\(\)\{.*?\}", "historyStatusEl()"
    )
    show_loading = _extract_from_source(
        _REVIEW_CONSOLE_JS, r"function showLoadingOlder\(\)\{.*?\n\}", "showLoadingOlder()"
    )
    show_end = _extract_from_source(
        _REVIEW_CONSOLE_JS, r"function showEndOfHistory\(\)\{.*?\n\}", "showEndOfHistory()"
    )
    retry_html = _extract_from_source(
        _REVIEW_CONSOLE_JS, r"function historyRetryHtml\(\)\{.*?\n\}", "historyRetryHtml()"
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
        result = run_node(harness)
        assert result.returncode == 0, f"[{code}] node harness failed: {result.stderr}"
        out = json.loads(result.stdout)
        assert loading_sub in out["loading"], f"[{code}] {out['loading']!r}"
        assert end_sub in out["end"], f"[{code}] {out['end']!r}"
        assert retry_sub in out["retry"], f"[{code}] {out['retry']!r}"
