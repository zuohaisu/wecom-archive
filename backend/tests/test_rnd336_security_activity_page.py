"""Executable browser-boundary coverage for RND-336 Security & activity."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "audit_log.html"


def _page_script() -> str:
    source = _TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"<script>\n(.*?)\n</script>", source, re.S)
    assert match, "audit page must contain its executable client script"
    return match.group(1)


def _run_browser_boundary_harness() -> subprocess.CompletedProcess[str]:
    script = json.dumps(_page_script())
    harness = f"""
const pageScript = {script};
const sentinels = [
  'SENTINEL_MESSAGE_BODY', 'SENTINEL_STRUCTURED_CONTENT', 'SENTINEL_PASSWORD',
  'SENTINEL_PASSWORD_HASH', 'SENTINEL_TOKEN', 'SENTINEL_SECRET',
  'SENTINEL_SIGNED_URL', 'SENTINEL_STORAGE_KEY', '/sentinel/fs/path',
  'SENTINEL_SEARCH_TEXT', 'SENTINEL_TRACEBACK', 'SENTINEL_SENDER',
  'SENTINEL_RECIPIENT', 'SENTINEL_ROOM', 'SENTINEL_RAW_MSGID',
  '<img src=x onerror="globalThis.pwned=true">'
];
const originalConsole = globalThis.console;
function assert(condition, message) {{ if (!condition) throw new Error(message); }}
class ClassList {{ constructor() {{ this.values = []; }} add(value) {{ this.values.push(value); }} }}
class Element {{
  constructor(tagName, id) {{
    this.tagName = tagName; this.id = id || ''; this.children = []; this.listeners = {{}};
    this.attributes = {{}}; this.classList = new ClassList(); this.style = {{}};
    this.value = ''; this.checked = false; this.disabled = false; this._text = '';
  }}
  set textContent(value) {{ this._text = String(value); this.children = []; }}
  get textContent() {{ return this._text; }}
  get innerHTML() {{ return allText(this); }}
  appendChild(child) {{ this.children.push(child); return child; }}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  getAttribute(name) {{ return this.attributes[name] || null; }}
  addEventListener(name, callback) {{ this.listeners[name] = callback; }}
  reset() {{ this.value = ''; }}
}}
function allText(element) {{ return element.textContent + element.children.map(allText).join(''); }}
function allAttributes(element) {{ return Object.values(element.attributes).join(' ') + element.children.map(allAttributes).join(' '); }}
function response(status, body) {{ return {{ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }}; }}
function flush() {{ return new Promise((resolve) => setTimeout(resolve, 0)); }}
function install(mode) {{
  const ids = ['audit-filters', 'activity-feed', 'audit-status', 'range-filter',
    'category-filter', 'operator-filter', 'include-system', 'clear-filters', 'result-count',
    'pagination-summary', 'page-number', 'previous-page', 'next-page', 'lang-menu'];
  const elements = Object.fromEntries(ids.map((id) => [id, new Element('div', id)]));
  elements['range-filter'].value = '90';
  elements['category-filter'].value = '';
  elements['operator-filter'].value = '';
  elements['include-system'].checked = false;
  const body = new Element('body');
  Object.values(elements).forEach((element) => body.appendChild(element));
  const consoleCalls = [];
  const localCalls = [], sessionCalls = [];
  const storage = (calls) => ({{ getItem: (key) => {{ calls.push(['get', key]); return null; }}, setItem: (key, value) => calls.push(['set', key, value]) }});
  const requests = [];
  let auditRequest = 0;
  globalThis.document = {{
    documentElement: new Element('html'), body, title: '',
    getElementById: (id) => elements[id],
    createElement: (tag) => new Element(tag),
    querySelectorAll: () => []
  }};
  globalThis.window = globalThis;
  globalThis.localStorage = storage(localCalls);
  globalThis.sessionStorage = storage(sessionCalls);
  globalThis.console = {{ log: (...args) => consoleCalls.push(args), error: (...args) => consoleCalls.push(args), warn: (...args) => consoleCalls.push(args) }};
  globalThis.pwned = false;
  globalThis.I18N = {{
    t: (key) => 'T:' + key, getLocale: () => 'en', availableLocales: () => [], onChange: () => {{}}, setLocale: () => {{}}
  }};
  globalThis.fetch = (url) => {{
    requests.push(url);
    if (url.indexOf('/api/admin/users') === 0) {{
      return mode === 'operator-failure' ? Promise.reject(new Error('network')) : Promise.resolve(response(200, {{ items: [{{ id: 'operator-1', name: 'Safe operator' }}] }}));
    }}
    auditRequest += 1;
    if (mode === '401') return Promise.resolve(response(401, {{}}));
    if (mode === '403') return Promise.resolve(response(403, {{}}));
    if (mode === 'network') return Promise.reject(new Error('network'));
    if (mode === 'empty') return Promise.resolve(response(200, {{ items: [], total: 0, limit: 25, offset: 0, has_more: false }}));
    const hostileDetail = {{
      message_body: sentinels[0], structured_content: sentinels[1], password: sentinels[2], password_hash: sentinels[3], token: sentinels[4], secret: sentinels[5], signed_url: sentinels[6], storage_key: sentinels[7], path: sentinels[8], search_text: sentinels[9], traceback: sentinels[10], sender: sentinels[11], recipient: sentinels[12], room: sentinels[13], raw_msgid: sentinels[14], html: sentinels[15], changed_keys: ['safe.setting', sentinels[4]]
    }};
    const items = mode === 'unknown' ? [{{ id: 'audit-safe', action: 'future.action', category: 'security', object_type: 'tenant', object_id: 'object-safe', actor_name: 'Safe actor', detail: hostileDetail, created_at: '2026-01-01T00:00:00Z' }}] : [
      {{ id: 'audit-safe', action: 'config.changed', category: 'configuration', object_type: 'tenant_config', object_id: 'object-safe', actor_name: 'Safe actor', detail: hostileDetail, created_at: '2026-01-01T00:00:00Z' }},
      {{ id: 'audit-export', action: 'export.executed', category: 'data_access', object_type: 'export', object_id: 'export-safe', actor_name: 'Safe actor', detail: {{ format: 'csv', record_count: 3 }}, created_at: '2026-01-01T00:00:00Z' }}
    ];
    return Promise.resolve(response(200, {{ items, total: 50, limit: 25, offset: auditRequest === 1 ? 0 : 25, has_more: auditRequest === 1 }}));
  }};
  eval(pageScript);
  return {{ document: globalThis.document, elements, consoleCalls, localCalls, sessionCalls, requests }};
}}
async function main() {{
  let state = install('normal');
  assert(allText(state.elements['activity-feed']).includes('T:audit.loading'), 'loading state must render before the audit response');
  await flush(); await flush();
  const feedText = allText(state.elements['activity-feed']);
  const domText = allText(state.document.body);
  const bodyHtml = state.document.body.innerHTML;
  const attributeText = allAttributes(state.document.body);
  for (const sentinel of sentinels) {{
    assert(!feedText.includes(sentinel), 'sentinel leaked into feed text: ' + sentinel);
    assert(!domText.includes(sentinel), 'sentinel leaked into DOM text: ' + sentinel);
    assert(!bodyHtml.includes(sentinel), 'sentinel leaked into document.body.innerHTML: ' + sentinel);
    assert(!attributeText.includes(sentinel), 'sentinel leaked into DOM attributes: ' + sentinel);
  }}
  assert(feedText.includes('safe.setting'), 'allowlisted changed key must remain readable');
  assert(feedText.includes('CSV'), 'allowlisted export format must remain readable');
  assert(feedText.includes('3'), 'allowlisted record count must remain readable');
  assert(!globalThis.pwned, 'hostile HTML must not execute');
  assert(state.consoleCalls.length === 0, 'audit rendering must not write to console');
  assert(state.localCalls.length === 0 && state.sessionCalls.length === 0, 'audit rendering must not use browser storage');
  state.elements['next-page'].listeners.click();
  await flush(); await flush();
  assert(state.requests.some((url) => url.indexOf('offset=25') !== -1), 'next page must use the server offset');

  for (const [mode, key] of [['401', 'audit.unauthorized'], ['403', 'audit.forbidden'], ['network', 'audit.failedToLoad']]) {{
    state = install(mode); await flush(); await flush();
    assert(state.elements['audit-status'].textContent === 'T:' + key, mode + ' must render its localized status');
    assert(allText(state.elements['activity-feed']).includes('T:audit.failedToLoad'), mode + ' must render the failure feed state');
  }}

  state = install('operator-failure'); await flush(); await flush();
  assert(state.elements['audit-status'].textContent === 'T:audit.operatorFallback', 'operator network failure must retain the all-operators fallback');
  state = install('empty'); await flush(); await flush();
  assert(allText(state.elements['activity-feed']).includes('T:audit.empty'), 'empty response must render its localized state');
  state = install('unknown'); await flush(); await flush();
  assert(allText(state.elements['activity-feed']).includes('T:audit.action.unknown'), 'unknown actions must render the localized fallback');
  process.stdout.write('browser boundary harness passed\\n');
}}
main().catch((error) => {{ originalConsole.error(error.stack || error); process.exitCode = 1; }});
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fixture:
        fixture.write(harness)
        path = Path(fixture.name)
    try:
        return subprocess.run(["node", path], capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_audit_feed_executes_hostile_detail_and_state_boundaries() -> None:
    result = _run_browser_boundary_harness()
    assert result.returncode == 0, result.stderr
    assert result.stdout == "browser boundary harness passed\n"
