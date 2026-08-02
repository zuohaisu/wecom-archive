"""Executable hostile-data and request-lifecycle boundary tests for RND-338."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_JS = (_BACKEND / "app" / "web" / "static" / "diagnostics.js").read_text(encoding="utf-8")


def _run_harness() -> subprocess.CompletedProcess[str]:
    script = json.dumps(_JS)
    harness = f"""
const pageScript = {script};
const sentinels = ['SENTINEL_MESSAGE_BODY','SENTINEL_STRUCTURED_CONTENT','SENTINEL_PASSWORD','SENTINEL_PASSWORD_HASH','SENTINEL_TOKEN','SENTINEL_SECRET','SENTINEL_SIGNED_URL','SENTINEL_STORAGE_KEY','/sentinel/fs/path','SENTINEL_SEARCH_TEXT','SENTINEL_TRACEBACK','SENTINEL_SENDER','SENTINEL_RECIPIENT','SENTINEL_ROOM','SENTINEL_RAW_MSGID','<img src=x onerror="globalThis.pwned=true">'];
const originalConsole = console;
const nativeSetTimeout = globalThis.setTimeout;
function assert(ok, message) {{ if (!ok) throw new Error(message); }}
class Element {{
  constructor(tag, id) {{ this.tagName=tag; this.id=id||''; this.children=[]; this.attributes={{}}; this.listeners={{}}; this.className=''; this.disabled=false; this.type=''; this._text=''; }}
  set textContent(value) {{ this._text=String(value); this.children=[]; }}
  get textContent() {{ return this._text; }}
  appendChild(child) {{ this.children.push(child); return child; }}
  replaceChildren() {{ this.children=[]; this._text=''; }}
  setAttribute(key, value) {{ this.attributes[key]=String(value); }}
  getAttribute(key) {{ return this.attributes[key] || null; }}
  addEventListener(name, callback) {{ this.listeners[name]=callback; }}
}}
function allText(el) {{ return el.textContent + el.children.map(allText).join(''); }}
function allAttributes(el) {{ return Object.values(el.attributes).join(' ') + el.children.map(allAttributes).join(' '); }}
function find(el, predicate) {{ if (predicate(el)) return el; for (const child of el.children) {{ const hit=find(child,predicate); if(hit)return hit; }} return null; }}
function response(status, body) {{ return {{status,ok:status>=200&&status<300,json:()=>Promise.resolve(body)}}; }}
function flush() {{ return new Promise(resolve => nativeSetTimeout(resolve, 0)); }}
function snapshot(state, overrides={{}}) {{ return Object.assign({{public_run_id:'public-safe',state,complete:state==='healthy'||state==='attention'||state==='no_data',scope:{{from_at:'2026-01-01T00:00:00Z',to_at:'2026-01-08T00:00:00Z'}},counts:{{matching:4,checked:4,reachable:3,unreachable:1}},reason_counts:{{unreachable_missing_recipient:1}},last_checked_at:'2026-01-08T00:01:00Z',algorithm_version:'reachability-v1',safe_error_code:null}},overrides); }}
function install(mode, state) {{
  const root=new Element('section','diag-root'); const body=new Element('body'); body.appendChild(root);
  const timers=[]; const windowListeners={{}}; const requests=[]; const consoleCalls=[]; const storageCalls=[];
  globalThis.document={{documentElement:new Element('html'),title:'',body,getElementById:id=>id==='diag-root'?root:null,createElement:tag=>new Element(tag),querySelectorAll:()=>[]}};
  globalThis.window=globalThis; globalThis.addEventListener=(name, callback)=>{{windowListeners[name]=callback;}};
  globalThis.setTimeout=(callback, delay)=>{{timers.push({{callback,delay,cleared:false}});return timers.length-1;}};
  globalThis.clearTimeout=id=>{{if(timers[id])timers[id].cleared=true;}};
  globalThis.I18N={{t:key=>'T:'+key,getLocale:()=> 'en',onChange:()=>{{}}}};
  globalThis.console={{log:(...args)=>consoleCalls.push(args),warn:(...args)=>consoleCalls.push(args),error:(...args)=>consoleCalls.push(args)}};
  globalThis.localStorage={{getItem:()=>storageCalls.push('local-get'),setItem:()=>storageCalls.push('local-set')}};
  globalThis.sessionStorage={{getItem:()=>storageCalls.push('session-get'),setItem:()=>storageCalls.push('session-set')}};
  globalThis.pwned=false;
  let latestCalls=0;
  globalThis.fetch=(url, options={{}})=>{{
    requests.push([url,options]);
    if(url.endsWith('/latest')) {{
      latestCalls+=1;
      if(mode==='401')return Promise.resolve(response(401,{{}}));
      if(mode==='403')return Promise.resolve(response(403,{{}}));
      if(mode==='network')return Promise.reject(new Error('network'));
      return Promise.resolve(response(200,snapshot(state)));
    }}
    if(mode==='post401')return Promise.resolve(response(401,{{}}));
    if(mode==='post409')return Promise.resolve(response(409,{{}}));
    if(mode==='postNetwork')return Promise.reject(new Error('network')); 
    if(mode==='post500')return Promise.resolve(response(500,{{}}));
    return Promise.resolve(response(202,snapshot('checking')));
  }};
  eval(pageScript);
  return {{root,body,timers,windowListeners,requests,consoleCalls,storageCalls}};
}}
async function boot(mode, state) {{ const env=install(mode,state); await flush(); await flush(); return env; }}
async function main() {{
  for(const state of ['healthy','attention','checking','no_data','incomplete','error']) {{
    const env=await boot('normal',state);
    assert(allText(env.root).includes('T:diagnostics.state.' + (state==='no_data'?'noData':state) + '.title'), 'missing state title '+state);
    assert(find(env.root,e=>e.tagName==='details') !== null, 'details missing '+state);
  }}
  let env=await boot('normal','healthy');
  const hostile=snapshot('attention',{{algorithm_version:sentinels[5],safe_error_code:sentinels[10],reason_counts:{{[sentinels[4]]:9,unreachable_missing_room:1}},counts:{{matching:sentinels[0],checked:4,reachable:3,unreachable:1}}}});
  // Reboot with a latest response containing hostile fields, all of which must be discarded or mapped.
  globalThis.fetch=()=>Promise.resolve(response(200,hostile)); eval(pageScript); await flush(); await flush();
  const domText=allText(document.body), attrs=allAttributes(document.body);
  for(const sentinel of sentinels) {{ assert(!domText.includes(sentinel),'sentinel in DOM '+sentinel); assert(!attrs.includes(sentinel),'sentinel in attribute '+sentinel); }}
  assert(!globalThis.pwned,'hostile HTML executed');
  assert(!String(document.title).includes('SENTINEL'),'hostile data affected title');
  assert(env.consoleCalls.length===0,'rendering must not write to console');
  assert(env.storageCalls.length===0,'rendering must not use browser storage');

  env=await boot('normal','healthy');
  const check=find(env.root,e=>e.tagName==='button'&&e.listeners.click);
  assert(check && check.type==='button','check action must be a native button');
  check.listeners.click(); check.listeners.click(); await flush(); await flush();
  assert(env.requests.filter(r=>r[0].endsWith('/reachability-checks')).length===1,'duplicate POST');
  assert(env.timers.some(timer=>timer.delay===1000),'checking must schedule bounded polling');
  assert(env.windowListeners.pagehide,'pagehide cleanup missing'); env.windowListeners.pagehide();
  assert(env.timers.some(timer=>timer.cleared),'pagehide must clear polling timer');

  for(const [mode,key] of [['401','diagnostics.authFailed'],['403','diagnostics.forbidden'],['network','diagnostics.failedToLoad']]) {{
    env=await boot(mode,'healthy'); assert(allText(env.root).includes('T:'+key),mode+' recoverable state missing');
  }}
  env=await boot('post401','healthy'); find(env.root,e=>e.tagName==='button'&&e.listeners.click).listeners.click(); await flush(); await flush();
  assert(allText(env.root).includes('T:diagnostics.authFailed'),'POST 401 state missing');
  env=await boot('postNetwork','healthy'); find(env.root,e=>e.tagName==='button'&&e.listeners.click).listeners.click(); await flush(); await flush();
  assert(allText(env.root).includes('T:diagnostics.startRequestFailed'),'manual trigger failure state missing');
  env=await boot('post500','healthy'); find(env.root,e=>e.tagName==='button'&&e.listeners.click).listeners.click(); await flush(); await flush();
  assert(allText(env.root).includes('T:diagnostics.startRequestFailed'),'POST server error state missing');
  env=await boot('post409','healthy'); find(env.root,e=>e.tagName==='button'&&e.listeners.click).listeners.click(); await flush(); await flush();
  assert(env.requests.filter(r=>r[0].endsWith('/reachability-checks')).length===1,'409 must reuse instead of duplicate POST');
  assert(env.requests.filter(r=>r[0].endsWith('/latest')).length>=2,'409 must read the active run');
  process.stdout.write('archive health harness passed\\n');
}}
main().catch(error=>{{originalConsole.error(error.stack||error);process.exitCode=1;}});
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fixture:
        fixture.write(harness)
        path = Path(fixture.name)
    try:
        return subprocess.run(["node", str(path)], capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_archive_health_executes_state_and_hostile_data_boundaries() -> None:
    result = _run_harness()
    assert result.returncode == 0, result.stderr
    assert result.stdout == "archive health harness passed\n"
