"""
Regression tests for the QA pass on the Archive Console v2 redesign (no
Linear ticket number — see test_archive_console_v2.py). Samuel's review
found 3 high-severity issues and an i18n gap; this file proves each fix
with real, executed behavior (not just source-text presence).

  1. A non-'all' 全部/群聊/单聊 filter silently hid the target conversation's
     card from a search-locate/locator-bar jump, so the hit count updated
     but the timeline never loaded. Fix: focusMessage() resets the filter
     to 'all' before locating.
  2. Clicking a contact search result raced setMode('staff')'s ASYNC
     entity-list fetch against a SYNCHRONOUS read of lastEntityItems
     (still whatever the previous mode last loaded) taken on the very
     next line — so the contact branch fired almost every time regardless
     of where the id actually resolved. Fix: setMode()/loadEntityList()
     now return their fetch promise; onSearchContactItemClick() awaits it.
  3. Selecting an entity (but no conversation yet) left the detail panel
     stuck on "加载中" forever; switching staff<->contact left a
     previously-selected message's audit info visible even though nothing
     is selected anymore. Fix: resetPanelForNoConversation() (a real empty
     state, not a perpetual spinner) replaces that call in onEntityClick(),
     and setMode() now calls it too.
  4. Several pieces of dynamically-rendered text (search input placeholder,
     the compact "↵ 结果页" hint, the selected scope label, the open
     locator bar's count, and the detail panel's audit/info content) were
     only ever set once, at render time, and never refreshed by
     applyLocale() -- so they stayed in the old language after a locale
     switch. The scope label additionally had a `data-i18n` attribute
     fighting its own dynamically-set text, so it reverted to the generic
     placeholder on every locale switch even with an entity selected.

Run (from backend/):
    pytest tests/test_archive_console_v2_qa_fixes.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests._rnd216_web_shims import review_console_html, review_console_js_source

_REVIEW_CONSOLE_JS = review_console_js_source()
_REVIEW_CONSOLE_HTML = review_console_html()
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


# ---------------------------------------------------------------------------
# Shared minimal DOM stub -- id-keyed fake elements, enough for setMode/
# onEntityClick/focusMessage/applyLocale to run for real without a browser.
# ---------------------------------------------------------------------------

_DOM_STUB = r"""
function FakeEl(id){
  this.id=id; this._text=''; this._html=''; this.style={}; this.dataset={};
  this._classes={};
  var self=this;
  this.classList={
    toggle:function(c,on){
      var next = (on===undefined) ? !self._classes[c] : !!on;
      if(next)self._classes[c]=true; else delete self._classes[c];
    },
    add:function(c){ self._classes[c]=true; },
    remove:function(c){ delete self._classes[c]; }
  };
}
Object.defineProperty(FakeEl.prototype,'textContent',{
  get:function(){return this._text;}, set:function(v){this._text=String(v);}
});
Object.defineProperty(FakeEl.prototype,'innerHTML',{
  get:function(){return this._html;}, set:function(v){this._html=String(v);}
});
// '.entity-item' is special-cased: a stubbed renderEntityList() (see
// _search_contact_race_bundle) populates _entityItems on the entity-body
// element so selectEntityIfPresent()'s real querySelectorAll('.entity-item')
// call has something real to find -- this stub has no HTML parser, so it
// can't discover elements from an innerHTML string the way a browser would.
FakeEl.prototype.querySelectorAll=function(sel){
  if(sel==='.entity-item'&&this._entityItems)return this._entityItems;
  return [];
};
FakeEl.prototype.addEventListener=function(){};
FakeEl.prototype.removeEventListener=function(){};
FakeEl.prototype.contains=function(){return false;};

var __elements={};
function makeStubDocument(ids){
  ids.forEach(function(id){__elements[id]=new FakeEl(id);});
  return {
    documentElement:{lang:''},
    getElementById:function(id){ return __elements[id]||null; },
    querySelectorAll:function(){ return []; },
    addEventListener:function(){}
  };
}
"""

_STUB_IDS = [
    "entity-body", "entity-header", "conv-header", "timeline-header",
    "conv-body", "timeline-body", "tab-staff", "tab-contact",
    "panel-info-body", "panel-audit-body", "search-input", "search-filters",
    "search-results", "conv-type-tabs", "scope-label", "scope-avatar",
    "locator-text", "locator-bar", "lang-menu", "refresh-status",
    "current-user", "new-msg-indicator", "panel-tab-info", "panel-tab-audit",
    "panel-col", "btn-panel-toggle", "btn-audit-mode",
]


def _console_state_bundle() -> str:
    return _extract(
        r"var mode=.*?\nvar lastRenderedTimelineSignature=null;",
        "console state (mode..lastRenderedTimelineSignature)",
    ) + (
        "\nvar auditMode=false,panelOpen=true,panelTab='info',selectedMsgId=null,"
        "scopePopoverOpen=false,convTypeFilter='all',searchHits=[],locatorIndex=0,"
        "convDetailCache={},searchDateRange=null,searchAllTypes=false;"
        "\nvar searchTimer=null,searchLastQ='';"
    )


# ---------------------------------------------------------------------------
# QA fix #1 -- focusMessage() resets the conv-type filter
# ---------------------------------------------------------------------------


def test_focus_message_resets_nonall_conv_type_filter_before_locating() -> None:
    """Reproduces Samuel's exact repro: filter set to 'group', then locate a
    message whose conversation is 'direct'. Before the fix, the conv-card
    was never rendered (filtered out), so onConvClick was never reached and
    the timeline never loaded even though the locator bar armed. After the
    fix, convTypeFilter must be back to 'all' by the time focusMessage's
    entity/conv search runs, and setConvTypeFilter must actually have been
    invoked (not just the bare variable poked)."""
    parts = [
        _console_state_bundle(),
        "var setConvTypeFilterCalls=[];",
        "function setConvTypeFilter(t){ setConvTypeFilterCalls.push(t); convTypeFilter=t; }",
        "function setMode(m){ mode=m; }",
        "function onEntityClick(el){ selEntityId=el.dataset.id; }",
        "function onConvClick(el){ selConvId=el.dataset.id; }",
        _extract(r"function focusMessage\(msgid, convId, convType, entityId, entityType\)\{.*?\n\}", "focusMessage()"),
    ]
    harness = "\n".join(parts) + """
convTypeFilter='group';
var document={
  getElementById:function(id){
    if(id==='entity-body')return {querySelectorAll:function(){return [];}};
    if(id==='conv-body')return {querySelectorAll:function(){return [];}};
    return null;
  }
};
focusMessage('m-1','conv-1','direct','staff_alice','staff');
setTimeout(function(){
  process.stdout.write(JSON.stringify({
    filterAfter: convTypeFilter,
    setConvTypeFilterCalls: setConvTypeFilterCalls
  }));
}, 10);
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["filterAfter"] == "all"
    assert out["setConvTypeFilterCalls"] == ["all"]


def test_focus_message_does_not_touch_an_already_all_filter() -> None:
    """No-op when the filter is already 'all' -- don't fire a spurious
    re-render on every single locate."""
    parts = [
        _console_state_bundle(),
        "var setConvTypeFilterCalls=[];",
        "function setConvTypeFilter(t){ setConvTypeFilterCalls.push(t); convTypeFilter=t; }",
        "function setMode(m){ mode=m; }",
        "function onEntityClick(el){ selEntityId=el.dataset.id; }",
        "function onConvClick(el){ selConvId=el.dataset.id; }",
        _extract(r"function focusMessage\(msgid, convId, convType, entityId, entityType\)\{.*?\n\}", "focusMessage()"),
    ]
    harness = "\n".join(parts) + """
convTypeFilter='all';
var document={getElementById:function(){return {querySelectorAll:function(){return [];}};}};
focusMessage('m-1','conv-1','direct','staff_alice','staff');
setTimeout(function(){ process.stdout.write(JSON.stringify(setConvTypeFilterCalls)); }, 10);
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# QA fix #2 -- setMode()/loadEntityList() return a promise; the
# staff-then-contact fallback actually waits for the fresh list.
# ---------------------------------------------------------------------------


def _search_contact_race_bundle() -> str:
    parts = [
        _DOM_STUB,
        _console_state_bundle(),
        "function esc(s){return s==null?'':String(s);}",
        "function handleUnauth(r){return false;}",
        "var I18N={setLocale:function(){},t:function(k){return k;},getLocale:function(){return 'zh-CN';},availableLocales:function(){return [];}};",
        # renderEntityList()/onEntityClick() stand-ins: the FakeEl DOM stub
        # has no HTML parser, so the real HTML-generating renderEntityList()
        # can't be queried back out via querySelectorAll -- these mirror its
        # real, meaningful contract (mode-dependent id field selection;
        # lastEntityItems bookkeeping; selEntityId assignment) without
        # needing full markup fidelity. The race-condition fix under test
        # here is entirely about setMode()/loadEntityList()/
        # onSearchContactItemClick() ordering, not rendering -- that's
        # already covered by test_archive_console_v2.py and the RND-204/
        # RND-206 suites.
        "function renderEntityList(items){ lastEntityItems=items; __elements['entity-body']._entityItems=(items||[]).map(function(it){ var id=mode==='staff'?it.staff_id:it.contact_id; return {dataset:{id:id,name:it.display_name}}; }); }",
        "function onEntityClick(el){ selEntityId=el.dataset.id; selEntityName=el.dataset.name; }",
        "function updateScopeButton(){} function closeScopePopover(){} function resetPanelForNoConversation(){} function loadConversations(){} function setSearchActive(){}",
        _extract(r"function loadEntityList\(\)\{.*?\n\}", "loadEntityList()"),
        "function hideNewMessageIndicator(){}",
    ]
    # setMode(), trimmed of the DOM writes this harness's stub can't satisfy
    # (tab active-class toggling etc. are exercised elsewhere) -- keep the
    # ordering-critical parts: state reset, resetPanelForNoConversation(),
    # and returning loadEntityList()'s promise.
    parts.append(
        _extract(r"function setMode\(m\)\{.*?\n\}", "setMode()")
    )
    parts.append(
        _extract(r"function selectEntityIfPresent\(wecomUserId\)\{.*?\n\}", "selectEntityIfPresent()")
    )
    parts.append(
        _extract(
            r"function onSearchContactItemClick\(\)\{.*?\n\}",
            "onSearchContactItemClick()",
        )
    )
    return "\n".join(parts)


def test_contact_search_click_waits_for_fresh_staff_list_before_deciding() -> None:
    """The exact race Samuel found: a DELAYED staff-list response must be
    awaited before checking membership -- not a synchronous read of
    whatever lastEntityItems held before the click. A staff match found
    only in the fresh (delayed) response must still be selected."""
    harness = f"""
{_search_contact_race_bundle()}
var fetchLog=[];
global.fetch=function(url){{
  fetchLog.push(url);
  return new Promise(function(resolve){{
    setTimeout(function(){{
      var items = url.indexOf('monitored-accounts')>=0
        ? [{{staff_id:'staff_target',display_name:'Target Staff',raw_id:'staff_target'}}]
        : [];
      resolve({{ok:true,status:200,json:function(){{return Promise.resolve(items);}}}});
    }}, 40);
  }});
}};
document = makeStubDocument({json.dumps(_STUB_IDS)});
// Simulate a STALE lastEntityItems from a previous contact-mode load,
// containing a DIFFERENT id -- if the race exists, the code would read
// this instead of waiting for the fetch above.
mode='contact';
lastEntityItems=[{{contact_id:'someone_else',display_name:'Someone Else',raw_id:'someone_else'}}];

var fakeEl={{dataset:{{searchContact:'staff_target'}}}};
onSearchContactItemClick.call(fakeEl);

setTimeout(function(){{
  process.stdout.write(JSON.stringify({{
    finalMode: mode,
    finalEntity: selEntityId,
    fetchCount: fetchLog.length
  }}));
}}, 200);
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["finalMode"] == "staff"
    assert out["finalEntity"] == "staff_target"


def test_contact_search_click_falls_back_to_contact_when_absent_from_staff() -> None:
    """When the wecom_userid is genuinely a contact (absent from the FRESH
    staff list), it must fall through to contact mode and select it there
    -- also only after THAT fetch resolves."""
    harness = f"""
{_search_contact_race_bundle()}
global.fetch=function(url){{
  return new Promise(function(resolve){{
    setTimeout(function(){{
      if(url.indexOf('monitored-accounts')>=0){{
        resolve({{ok:true,status:200,json:function(){{return Promise.resolve([]);}}}});
      }}else{{
        resolve({{ok:true,status:200,json:function(){{return Promise.resolve(
          [{{contact_id:'contact_target',display_name:'Target Contact',raw_id:'contact_target'}}]
        );}}}});
      }}
    }}, 30);
  }});
}};
document = makeStubDocument({json.dumps(_STUB_IDS)});
mode='staff';
lastEntityItems=null;

var fakeEl={{dataset:{{searchContact:'contact_target'}}}};
onSearchContactItemClick.call(fakeEl);

setTimeout(function(){{
  process.stdout.write(JSON.stringify({{finalMode: mode, finalEntity: selEntityId}}));
}}, 300);
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["finalMode"] == "contact"
    assert out["finalEntity"] == "contact_target"


def test_load_entity_list_returns_a_promise() -> None:
    load_entity_list_src = _extract(r"function loadEntityList\(\)\{.*?\n\}", "loadEntityList()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
function handleUnauth(r){{return false;}}
var I18N={{t:function(k){{return k;}}}};
function renderEntityList(){{}}
{load_entity_list_src}
global.fetch=function(){{return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve([]);}}}});}};
document = makeStubDocument({json.dumps(_STUB_IDS)});
var ret = loadEntityList();
process.stdout.write(JSON.stringify(typeof ret==='object' && typeof ret.then==='function'));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    assert json.loads(result.stdout) is True


# ---------------------------------------------------------------------------
# QA fix #3 -- panel empty/reset states
# ---------------------------------------------------------------------------


def test_on_entity_click_shows_select_conversation_not_stuck_loading() -> None:
    """Selecting an entity with no conversation chosen yet must show the
    'select a conversation' empty state in the info tab, never a
    perpetual loading spinner (nothing is actually loading yet)."""
    reset_panel_src = _extract(r"function resetPanelForNoConversation\(\)\{.*?\n\}", "resetPanelForNoConversation()")
    on_entity_click_src = _extract(r"function onEntityClick\(el\)\{.*?\n\}", "onEntityClick()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
function esc(s){{return s==null?'':String(s);}}
var I18N={{t:function(k){{return k;}}}};
function updateScopeButton(){{}} function closeScopePopover(){{}} function loadConversations(){{}}
function renderPanelAuditEmpty(){{ __elements['panel-audit-body'].innerHTML='<div class="empty-state">panel.noSelection</div>'; }}
{reset_panel_src}
{on_entity_click_src}
document = makeStubDocument({json.dumps(_STUB_IDS)});
selectedMsgId='stale-msg-from-before';
onEntityClick({{dataset:{{id:'staff_alice',name:'Alice'}},classList:{{add:function(){{}}}}}});
process.stdout.write(JSON.stringify({{
  infoBody: __elements['panel-info-body'].innerHTML,
  selectedMsgId: selectedMsgId
}}));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert "console.loading" not in out["infoBody"]
    assert "console.selectConversation" in out["infoBody"]
    assert out["selectedMsgId"] is None


def test_set_mode_clears_stale_selected_message_and_panel() -> None:
    """Switching staff<->contact must not leave a previous entity/
    conversation's selected-message audit info visible -- a real reviewer
    misattribution risk (auditing the wrong scope's message)."""
    reset_panel_src = _extract(r"function resetPanelForNoConversation\(\)\{.*?\n\}", "resetPanelForNoConversation()")
    set_mode_src = _extract(r"function setMode\(m\)\{.*?\n\}", "setMode()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
var I18N={{t:function(k){{return k;}},getLocale:function(){{return 'zh-CN';}}}};
function hideNewMessageIndicator(){{}}
var loadEntityListCalls=0;
function loadEntityList(){{ loadEntityListCalls++; return Promise.resolve(); }}
function renderPanelAuditEmpty(){{ __elements['panel-audit-body'].innerHTML='<div class="empty-state">panel.noSelection</div>'; }}
{reset_panel_src}
{set_mode_src}
document = makeStubDocument({json.dumps(_STUB_IDS)});
selectedMsgId='msg-from-staff-mode';
__elements['panel-audit-body'].innerHTML='<div class="panel-audit-preview">some stale audit info</div>';
setMode('contact');
process.stdout.write(JSON.stringify({{
  selectedMsgId: selectedMsgId,
  auditBody: __elements['panel-audit-body'].innerHTML,
  loadEntityListCalls: loadEntityListCalls
}}));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["selectedMsgId"] is None
    assert "stale audit info" not in out["auditBody"]
    assert out["loadEntityListCalls"] == 1


# ---------------------------------------------------------------------------
# QA fix #4 -- i18n: dynamic content actually refreshes on locale switch
# ---------------------------------------------------------------------------


def test_scope_label_has_no_data_i18n_attribute() -> None:
    """A data-i18n attribute on #scope-label would make every
    applyStaticI18n() call (i.e. every locale switch) stomp the
    dynamically-set "监控账号：Alice" text back to the generic
    placeholder -- this element must be JS-managed only."""
    match = re.search(r'<span class="scope-label"[^>]*>', _REVIEW_CONSOLE_HTML)
    assert match is not None, "expected #scope-label span in review_console.html"
    assert "data-i18n" not in match.group(0)


def test_enter_hint_has_data_i18n_for_locale_refresh() -> None:
    match = re.search(r'<span class="enter-hint"[^>]*>', _REVIEW_CONSOLE_HTML)
    assert match is not None
    assert 'data-i18n="search.enterHintShort"' in match.group(0)


def test_search_enter_hint_short_key_exists_in_all_locales() -> None:
    from app.i18n_assets import I18N_JS_SOURCE

    harness = f"""
{I18N_JS_SOURCE}
var out={{}};
['zh-CN','zh-TW','en'].forEach(function(loc){{
  I18N.setLocale(loc);
  out[loc]=I18N.t('search.enterHintShort');
}});
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["zh-CN"] != "search.enterHintShort"
    assert out["zh-TW"] != "search.enterHintShort"
    assert out["en"] != "search.enterHintShort"
    assert len({out["zh-CN"], out["zh-TW"], out["en"]}) == 3  # genuinely distinct per-locale copy


def test_apply_locale_refreshes_search_placeholder_and_selected_scope_label() -> None:
    update_scope_button_src = _extract(r"function updateScopeButton\(name\)\{.*?\n\}", "updateScopeButton()")
    apply_locale_src = _extract(r"function applyLocale\(\)\{.*?\n\}", "applyLocale()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
function esc(s){{return s==null?'':String(s);}}
var localeCode='zh-CN';
var I18N={{
  setLocale:function(c){{localeCode=c;}},
  getLocale:function(){{return localeCode;}},
  t:function(k){{
    var table={{
      'search.placeholder': localeCode==='en' ? 'Search contacts or messages...' : '搜索联系人或聊天内容…',
      'console.monitoredAccounts':'x','console.contactsHeader':'x','nav.conversations':'x',
      'console.timelineHeader':'x','console.selectAccountOrContact':'x','console.selectConversation':'x'
    }};
    return table.hasOwnProperty(k) ? table[k] : k;
  }},
  availableLocales:function(){{return [];}}
}};
function applyStaticI18n(){{}}
function renderLangMenu(){{}}
function rebuildMediaLabels(){{}}
function updateRefreshStatus(){{}}
function renderEntityList(){{}}
function renderConvList(){{}}
function renderTimeline(){{}}
function renderSearchFilters(){{}}
function updateLocatorText(){{}}
function renderPanelAudit(){{}}
function renderPanelAuditEmpty(){{}}
function renderPanelInfo(){{}}
function findTimelineMessage(){{return null;}}
{update_scope_button_src}
document = makeStubDocument({json.dumps(_STUB_IDS)});
selEntityId='staff_alice'; selEntityName='Alice'; mode='staff';
{apply_locale_src}
I18N.setLocale('en');
applyLocale();
process.stdout.write(JSON.stringify({{
  placeholder: __elements['search-input'].placeholder,
  scopeLabel: __elements['scope-label'].textContent
}}));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["placeholder"] == "Search contacts or messages..."
    assert "Alice" in out["scopeLabel"]


# ---------------------------------------------------------------------------
# QA fix #5 -- 1024px layout: the timeline column was squeezed to ~184px
# because side-nav (212px) + col-conv (328px) + col-panel (300px) are all
# fixed-width with no breakpoint; the locator bar's text also wrapped
# character-by-character once its allocated width got tight.
# ---------------------------------------------------------------------------


def _extract_style_block() -> str:
    match = re.search(r"<style>(.*?)</style>", _REVIEW_CONSOLE_HTML, re.S)
    assert match is not None
    return match.group(1)


def test_narrow_viewport_breakpoint_shrinks_fixed_width_columns() -> None:
    css = _extract_style_block()
    match = re.search(r"@media \(max-width:\s*1300px\)\{(.*?)\n\}", css, re.S)
    assert match is not None, "expected an @media (max-width: 1300px) breakpoint"
    block = match.group(1)
    # Each shrunk width must be strictly less than its full-width value
    # (212/328/300) so the timeline actually gains room back.
    side_nav = re.search(r"\.side-nav\{width:(\d+)px\}", block)
    col_conv = re.search(r"\.col-conv\{width:(\d+)px\}", block)
    col_panel = re.search(r"\.col-panel\{width:(\d+)px\}", block)
    assert side_nav and int(side_nav.group(1)) < 212
    assert col_conv and int(col_conv.group(1)) < 328
    assert col_panel and int(col_panel.group(1)) < 300


def test_locator_text_truncates_instead_of_wrapping_per_character() -> None:
    css = _extract_style_block()
    match = re.search(r"(?:^|\n)\.locator-text\{([^}]*)\}", css)
    assert match is not None, "expected a .locator-text CSS rule"
    rule = match.group(1)
    assert "white-space:nowrap" in rule
    assert "text-overflow:ellipsis" in rule
    actions_match = re.search(r"(?:^|\n)\.locator-actions\{([^}]*)\}", css)
    assert actions_match is not None
    assert "flex-shrink:0" in actions_match.group(1)


# ---------------------------------------------------------------------------
# QA fix round 2 -- #1 scope-label not reset on mode switch
# ---------------------------------------------------------------------------


def test_set_mode_resets_scope_label_and_avatar_to_placeholder() -> None:
    """setMode() clears selEntityId/selEntityName, but the "监控账号：X"
    scope-label/avatar updateScopeButton() set on selection is JS-managed
    (see the template comment on #scope-label) and was never reset here --
    switching staff<->contact left the OLD entity's name showing at the
    top even though the conversation list underneath had already gone
    back to "select an account or contact"."""
    reset_panel_src = _extract(r"function resetPanelForNoConversation\(\)\{.*?\n\}", "resetPanelForNoConversation()")
    set_mode_src = _extract(r"function setMode\(m\)\{.*?\n\}", "setMode()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
var I18N={{t:function(k){{return k;}},getLocale:function(){{return 'zh-CN';}}}};
function hideNewMessageIndicator(){{}}
function loadEntityList(){{ return Promise.resolve(); }}
function renderPanelAuditEmpty(){{}}
{reset_panel_src}
{set_mode_src}
document = makeStubDocument({json.dumps(_STUB_IDS)});
__elements['scope-label'].textContent='监控账号：365客服英子';
__elements['scope-avatar'].textContent='3';
setMode('contact');
process.stdout.write(JSON.stringify({{
  scopeLabel: __elements['scope-label'].textContent,
  scopeAvatar: __elements['scope-avatar'].textContent
}}));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["scopeLabel"] == "console.pickScope"
    assert "365客服英子" not in out["scopeLabel"]
    assert out["scopeAvatar"] == "?"


# ---------------------------------------------------------------------------
# QA fix round 2 -- #2 i18n: bootstrap only ran applyStaticI18n(), and
# applyLocale() didn't refresh the panel's "nothing selected" empty states
# ---------------------------------------------------------------------------


def test_bootstrap_calls_apply_locale_not_just_apply_static_i18n() -> None:
    """A persisted non-default locale only got applyStaticI18n()'s
    data-i18n pass at page load -- dynamic content (search placeholder,
    scope label, panel empty states) stayed in the default locale's
    baked-in text until the user manually reopened the language menu.
    applyLocale() is a strict superset (it calls applyStaticI18n() first),
    so bootstrap must call that instead."""
    bootstrap_match = re.search(
        r"// RND-217 C5: bootstrap runs LAST.*?\n(?:.*\n)*?applyLocale\(\);\nloadCurrentUser\(\);",
        _REVIEW_CONSOLE_JS,
    )
    assert bootstrap_match is not None, "expected bootstrap to call applyLocale() before loadCurrentUser()"
    # applyStaticI18n() must still exist as a function (applyLocale calls it
    # internally) -- this only checks bootstrap no longer calls the NARROWER
    # one directly as its own top-level statement.
    assert "function applyStaticI18n(){" in _REVIEW_CONSOLE_JS


def test_apply_locale_refreshes_panel_empty_states_when_nothing_selected() -> None:
    apply_locale_src = _extract(r"function applyLocale\(\)\{.*?\n\}", "applyLocale()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
function esc(s){{return s==null?'':String(s);}}
var localeCode='zh-CN';
var I18N={{
  setLocale:function(c){{localeCode=c;}},
  getLocale:function(){{return localeCode;}},
  t:function(k){{
    var zh={{'console.selectConversation':'请选择会话','panel.noSelection':'点击任意消息查看审计元数据'}};
    var en={{'console.selectConversation':'Select a conversation','panel.noSelection':'Click any message to view its audit metadata'}};
    var table=localeCode==='en'?en:zh;
    return table.hasOwnProperty(k)?table[k]:k;
  }},
  availableLocales:function(){{return [];}}
}};
function applyStaticI18n(){{}} function renderLangMenu(){{}} function rebuildMediaLabels(){{}}
function updateRefreshStatus(){{}} function renderEntityList(){{}} function renderConvList(){{}}
function renderTimeline(){{}} function renderSearchFilters(){{}} function updateLocatorText(){{}}
function renderPanelInfo(){{}} function updateScopeButton(){{}}
function findTimelineMessage(){{return null;}}
function renderPanelAuditEmpty(){{ __elements['panel-audit-body'].innerHTML='<div class="empty-state">'+I18N.t('panel.noSelection')+'</div>'; }}
{apply_locale_src}
document = makeStubDocument({json.dumps(_STUB_IDS)});
// Nothing selected: no entity, no conversation, no message -- exactly the
// state right after setMode() or at initial page load.
selEntityId=null; selConvId=null; selectedMsgId=null;
I18N.setLocale('en');
applyLocale();
process.stdout.write(JSON.stringify({{
  infoBody: __elements['panel-info-body'].innerHTML,
  auditBody: __elements['panel-audit-body'].innerHTML
}}));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert "Select a conversation" in out["infoBody"]
    assert "请选择会话" not in out["infoBody"]
    assert "Click any message to view its audit metadata" in out["auditBody"]


# ---------------------------------------------------------------------------
# QA fix round 2 -- #2 i18n: the "← 返回搜索结果" jump-back banner was
# hardcoded Chinese, never run through I18N.t()
# ---------------------------------------------------------------------------


def test_focus_banner_uses_i18n_not_hardcoded_chinese() -> None:
    show_focus_banner_src = _extract(r"function showFocusBanner\(\)\{.*?\n\}", "showFocusBanner()")
    assert "← 返回搜索结果" not in show_focus_banner_src
    assert "I18N.t('search.jumpBack')" in show_focus_banner_src


# ---------------------------------------------------------------------------
# QA fix round 3 -- #1 an already-visible #focus-banner never refreshed on
# a subsequent locale switch (showFocusBanner() only sets its text once, at
# creation time, and is a no-op if the banner already exists)
# ---------------------------------------------------------------------------


def test_apply_locale_refreshes_an_already_visible_focus_banner() -> None:
    apply_locale_src = _extract(r"function applyLocale\(\)\{.*?\n\}", "applyLocale()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
function esc(s){{return s==null?'':String(s);}}
var localeCode='zh-CN';
var I18N={{
  setLocale:function(c){{localeCode=c;}},
  getLocale:function(){{return localeCode;}},
  t:function(k){{
    var table={{'search.jumpBack': localeCode==='en' ? 'Back to search results' : '\\u8fd4\\u56de\\u641c\\u7d22\\u7ed3\\u679c'}};
    return table.hasOwnProperty(k)?table[k]:k;
  }},
  availableLocales:function(){{return [];}}
}};
function applyStaticI18n(){{}} function renderLangMenu(){{}} function rebuildMediaLabels(){{}}
function updateRefreshStatus(){{}} function renderEntityList(){{}} function renderConvList(){{}}
function renderTimeline(){{}} function renderSearchFilters(){{}} function updateLocatorText(){{}}
function renderPanelInfo(){{}} function updateScopeButton(){{}} function findTimelineMessage(){{return null;}}
function renderPanelAuditEmpty(){{}}
{apply_locale_src}
document = makeStubDocument({json.dumps(_STUB_IDS + ["focus-banner"])});
// showFocusBanner() would have set this at creation time, in zh-CN.
__elements['focus-banner'].textContent = I18N.t('search.jumpBack');
var before = __elements['focus-banner'].textContent;
I18N.setLocale('en');
applyLocale();
process.stdout.write(JSON.stringify({{before: before, after: __elements['focus-banner'].textContent}}));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["before"] == "返回搜索结果" or "返回" in out["before"]
    assert out["after"] == "Back to search results"


def test_apply_locale_leaves_no_focus_banner_untouched_when_absent() -> None:
    """No banner currently shown -- applyLocale() must not create one."""
    apply_locale_src = _extract(r"function applyLocale\(\)\{.*?\n\}", "applyLocale()")
    harness = f"""
{_DOM_STUB}
{_console_state_bundle()}
function esc(s){{return s==null?'':String(s);}}
var I18N={{setLocale:function(){{}},getLocale:function(){{return 'zh-CN';}},t:function(k){{return k;}},availableLocales:function(){{return [];}}}};
function applyStaticI18n(){{}} function renderLangMenu(){{}} function rebuildMediaLabels(){{}}
function updateRefreshStatus(){{}} function renderEntityList(){{}} function renderConvList(){{}}
function renderTimeline(){{}} function renderSearchFilters(){{}} function updateLocatorText(){{}}
function renderPanelInfo(){{}} function updateScopeButton(){{}} function findTimelineMessage(){{return null;}}
function renderPanelAuditEmpty(){{}}
{apply_locale_src}
var docStub = makeStubDocument({json.dumps(_STUB_IDS)});
document = {{
  documentElement: docStub.documentElement,
  getElementById: function(id){{ if(id==='focus-banner') return null; return docStub.getElementById(id); }},
  querySelectorAll: docStub.querySelectorAll,
  addEventListener: docStub.addEventListener
}};
applyLocale();
process.stdout.write('ok');
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    assert result.stdout == "ok"


# ---------------------------------------------------------------------------
# QA fix round 3 -- #2 (visual) scope label hardcoded a full-width Chinese
# colon regardless of locale
# ---------------------------------------------------------------------------


def test_scope_button_prefix_has_no_hardcoded_fullwidth_colon() -> None:
    src = _extract(r"function updateScopeButton\(name\)\{.*?\n\}", "updateScopeButton()")
    # Check the actual assignment statement, not the whole function body --
    # its own explanatory comment legitimately mentions the OLD literal for
    # documentation purposes, which a blanket substring check would
    # (wrongly) also flag.
    assignment_line = next(line for line in src.split("\n") if "label.textContent=" in line)
    assert "'：'" not in assignment_line, "expected no hardcoded full-width colon concatenation"
    assert "console.scopeLabelStaffPrefix" in assignment_line
    assert "console.scopeLabelContactPrefix" in assignment_line


def test_scope_button_prefix_localizes_correctly_for_english() -> None:
    update_scope_button_src = _extract(r"function updateScopeButton\(name\)\{.*?\n\}", "updateScopeButton()")
    harness = f"""
{_DOM_STUB}
var mode='staff';
var I18N={{t:function(k){{
  var table={{
    'console.scopeLabelStaffPrefix':'Monitored account: ',
    'console.scopeLabelContactPrefix':'Contact: '
  }};
  return table.hasOwnProperty(k)?table[k]:k;
}}}};
document = makeStubDocument({json.dumps(["scope-label", "scope-avatar"])});
{update_scope_button_src}
updateScopeButton('Alice');
process.stdout.write(JSON.stringify(__elements['scope-label'].textContent));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    label = json.loads(result.stdout)
    assert label == "Monitored account: Alice"
    assert "：" not in label  # no stray full-width colon in the English label


def test_scope_label_prefix_keys_exist_in_all_locales() -> None:
    from app.i18n_assets import I18N_JS_SOURCE

    harness = f"""
{I18N_JS_SOURCE}
var out={{}};
['zh-CN','zh-TW','en'].forEach(function(loc){{
  I18N.setLocale(loc);
  out[loc]={{staff:I18N.t('console.scopeLabelStaffPrefix'),contact:I18N.t('console.scopeLabelContactPrefix')}};
}});
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    out = json.loads(result.stdout)
    for loc in ("zh-CN", "zh-TW", "en"):
        assert out[loc]["staff"] != "console.scopeLabelStaffPrefix"
        assert out[loc]["contact"] != "console.scopeLabelContactPrefix"
    assert "：" not in out["en"]["staff"] and "：" not in out["en"]["contact"]
