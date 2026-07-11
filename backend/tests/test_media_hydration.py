"""
Tests for RND-187 — client-side media hydration
(loadMediaImage / onMediaImageError / showMediaError / hydrateMediaImages),
the embedded JS that turns a rendered <img data-access-url> placeholder into
an actual image by fetching the unified media access descriptor.

Scope: app/main.py's inline JS only (no backend calls — fetch is mocked).
Backend behavior of the /media/access endpoint itself is covered by
tests/test_media_access_descriptor.py.

Executes the real embedded JS under Node (same technique as
test_admin_auto_load_older.py), with a minimal hand-rolled DOM stub (no
jsdom dependency in this project) sufficient to exercise the hydration
functions: a fake <img> (getAttribute/setAttribute/onerror/src/closest), a
fake wrapping <a> (href), and document.createElement for the error
placeholder.

Run (from backend/):
    pytest tests/test_media_hydration.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.main import _REVIEW_CONSOLE_HTML

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_HTML, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_HTML"
    return match.group(0)


def _bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _extract(r"function handleUnauth\(r\)\{.*?\n\}", "handleUnauth()"),
        _extract(r"function loadMediaImage\(img\)\{.*?\n\}", "loadMediaImage()"),
        _extract(r"function onMediaImageError\(img\)\{.*?\n\}", "onMediaImageError()"),
        _extract(r"function showMediaError\(img\)\{.*?\n\}", "showMediaError()"),
        _extract(r"function hydrateMediaImages\(root\)\{.*?\n\}", "hydrateMediaImages()"),
    ]
    return "\n".join(parts)


_FIXTURE_PREAMBLE = """
window={location:{href:''}};

function makeLink(){
  var l={_href:null, parentNode:null};
  Object.defineProperty(l,'href',{get:function(){return l._href;},set:function(v){l._href=v;}});
  l.parentNode={
    replaceChild:function(newEl,oldEl){this.lastReplacedWith=newEl;this.lastReplaced=oldEl;}
  };
  return l;
}

function makeImg(attrs){
  var attrMap={};
  Object.keys(attrs||{}).forEach(function(k){attrMap[k]=attrs[k];});
  var link=makeLink();
  var img={
    _src:null,
    onerror:null,
    _link:link,
    getAttribute:function(k){return Object.prototype.hasOwnProperty.call(attrMap,k)?attrMap[k]:null;},
    setAttribute:function(k,v){attrMap[k]=String(v);},
    closest:function(sel){return sel==='a.media-link'?link:null;},
    parentNode:{replaceChild:function(newEl,oldEl){this.lastReplacedWith=newEl;this.lastReplaced=oldEl;}}
  };
  Object.defineProperty(img,'src',{get:function(){return img._src;},set:function(v){img._src=v;}});
  return img;
}

document={
  createElement:function(tag){
    return {tagName:tag, className:'', textContent:''};
  }
};

function flush(){ return new Promise(function(res){ setImmediate(res); }); }
"""


def _run(script_body: str) -> dict:
    assert NODE, "node executable not found"
    harness = f"""
{_bundle()}
{_FIXTURE_PREAMBLE}

(async function() {{
{script_body}
}})().catch(function(e) {{
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
}});
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Signed-URL (Qiniu) descriptor rendering
# ---------------------------------------------------------------------------


def test_signed_url_descriptor_sets_img_src_and_link_href() -> None:
    out = _run(
        """
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  return Promise.resolve({ok:true,status:200,json:function(){
    return Promise.resolve({url:'https://media.crowntime.cn/tenants/t1/images/1.jpg?e=1&token=abc',access_type:'signed_url'});
  }});
};
loadMediaImage(img);
await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({src:img.src, href:img._link.href}));
"""
    )
    assert out["src"] == "https://media.crowntime.cn/tenants/t1/images/1.jpg?e=1&token=abc"
    assert out["href"] == out["src"]


def test_proxy_descriptor_sets_img_src() -> None:
    out = _run(
        """
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  return Promise.resolve({ok:true,status:200,json:function(){
    return Promise.resolve({url:'/api/conversations/c1/messages/m1/media',access_type:'proxy'});
  }});
};
loadMediaImage(img);
await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({src:img.src}));
"""
    )
    assert out["src"] == "/api/conversations/c1/messages/m1/media"


# ---------------------------------------------------------------------------
# Fallback (no access URL — defensive path for stale cached payloads)
# ---------------------------------------------------------------------------


def test_missing_access_url_falls_back_to_media_url_without_fetching() -> None:
    out = _run(
        """
var fetchCalls=[];
var img=makeImg({'data-access-url':'','data-fallback-url':'/api/conversations/c1/messages/m1/media','data-retried':'0'});
fetch=function(url){fetchCalls.push(url); return Promise.reject(new Error('should not be called'));};
loadMediaImage(img);
await flush();
process.stdout.write(JSON.stringify({src:img.src, fetchCalls:fetchCalls, retried:img.getAttribute('data-retried')}));
"""
    )
    assert out["src"] == "/api/conversations/c1/messages/m1/media"
    assert out["fetchCalls"] == []
    assert out["retried"] == "1"


def test_missing_access_and_fallback_url_shows_error_immediately() -> None:
    out = _run(
        """
var img=makeImg({'data-access-url':'','data-fallback-url':'','data-retried':'0'});
loadMediaImage(img);
await flush();
process.stdout.write(JSON.stringify({
  replaced: img._link.parentNode.lastReplaced === img._link,
  placeholderText: img._link.parentNode.lastReplacedWith.textContent,
  placeholderClass: img._link.parentNode.lastReplacedWith.className
}));
"""
    )
    assert out["replaced"] is True
    assert out["placeholderText"] == "Image failed to load"
    assert out["placeholderClass"] == "media-placeholder"


# ---------------------------------------------------------------------------
# Retry-once behavior
# ---------------------------------------------------------------------------


def test_fetch_failure_retries_exactly_once_then_succeeds() -> None:
    out = _run(
        """
var fetchCalls=0;
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  fetchCalls++;
  if(fetchCalls===1){return Promise.resolve({ok:false,status:500});}
  return Promise.resolve({ok:true,status:200,json:function(){
    return Promise.resolve({url:'https://media.crowntime.cn/tenants/t1/images/2.jpg?e=2&token=xyz'});
  }});
};
loadMediaImage(img);
await flush(); await flush(); await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls, src:img.src, retried:img.getAttribute('data-retried')}));
"""
    )
    assert out["fetchCalls"] == 2
    assert out["src"] == "https://media.crowntime.cn/tenants/t1/images/2.jpg?e=2&token=xyz"
    assert out["retried"] == "1"


def test_fetch_failure_twice_shows_error_and_never_fetches_a_third_time() -> None:
    out = _run(
        """
var fetchCalls=0;
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  fetchCalls++;
  return Promise.resolve({ok:false,status:500});
};
loadMediaImage(img);
await flush(); await flush(); await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({
  fetchCalls:fetchCalls,
  replaced: img._link.parentNode.lastReplaced === img._link,
  placeholderText: img._link.parentNode.lastReplacedWith.textContent
}));
"""
    )
    assert out["fetchCalls"] == 2  # exactly one retry, never a third attempt
    assert out["replaced"] is True
    assert out["placeholderText"] == "Image failed to load"


def test_img_element_load_error_after_successful_fetch_triggers_one_retry() -> None:
    """Covers the "signed URL expired while scrolled off-screen" case: the
    descriptor fetch succeeds and img.src is set, but the browser's own
    <img> load then fails (401/403 from an expired signed URL) — the
    resulting onerror must re-fetch the descriptor exactly once."""
    out = _run(
        """
var fetchCalls=0;
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  fetchCalls++;
  var freshUrl='https://media.crowntime.cn/tenants/t1/images/'+fetchCalls+'.jpg?e='+fetchCalls+'&token=t'+fetchCalls;
  return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve({url:freshUrl});}});
};
loadMediaImage(img);
await flush(); await flush(); await flush();
var firstSrc=img.src;
// Simulate the real <img> failing to actually load the (now-expired) URL.
img.onerror();
await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls, firstSrc:firstSrc, secondSrc:img.src}));
"""
    )
    assert out["fetchCalls"] == 2
    assert out["firstSrc"] != out["secondSrc"]
    assert out["secondSrc"].endswith("2.jpg?e=2&token=t2")


def test_img_element_load_error_after_two_failures_shows_error_not_infinite_loop() -> None:
    out = _run(
        """
var fetchCalls=0;
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  fetchCalls++;
  return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve({url:'https://media.crowntime.cn/x.jpg?e=1&token=t'});}});
};
loadMediaImage(img);
await flush(); await flush(); await flush();
img.onerror();  // first real <img> load failure -> one retry
await flush(); await flush(); await flush();
img.onerror();  // second real <img> load failure -> must NOT retry again
await flush();
process.stdout.write(JSON.stringify({
  fetchCalls:fetchCalls,
  replaced: img._link.parentNode.lastReplaced === img._link
}));
"""
    )
    assert out["fetchCalls"] == 2  # never a third descriptor fetch
    assert out["replaced"] is True


# ---------------------------------------------------------------------------
# 401 handling (session expired mid-hydration)
# ---------------------------------------------------------------------------


def test_401_response_redirects_and_does_not_retry() -> None:
    out = _run(
        """
var fetchCalls=0;
var img=makeImg({'data-access-url':'/api/conversations/c1/messages/m1/media/access','data-fallback-url':'','data-retried':'0'});
fetch=function(url){fetchCalls++; return Promise.resolve({ok:false,status:401});};
loadMediaImage(img);
await flush(); await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({fetchCalls:fetchCalls, redirected:window.location.href}));
"""
    )
    assert out["fetchCalls"] == 1  # handleUnauth short-circuits before any retry
    assert out["redirected"] == "/admin/login"


# ---------------------------------------------------------------------------
# hydrateMediaImages — iterates every matched element
# ---------------------------------------------------------------------------


def test_hydrate_media_images_processes_every_matched_element() -> None:
    out = _run(
        """
var img1=makeImg({'data-access-url':'/access1','data-fallback-url':'','data-retried':'0'});
var img2=makeImg({'data-access-url':'/access2','data-fallback-url':'','data-retried':'0'});
fetch=function(url){
  return Promise.resolve({ok:true,status:200,json:function(){
    return Promise.resolve({url:'https://media.crowntime.cn/'+url.slice(1)+'.jpg?e=1&token=t'});
  }});
};
var root={querySelectorAll:function(sel){return [img1, img2];}};
hydrateMediaImages(root);
await flush(); await flush(); await flush();
process.stdout.write(JSON.stringify({src1:img1.src, src2:img2.src}));
"""
    )
    assert out["src1"] == "https://media.crowntime.cn/access1.jpg?e=1&token=t"
    assert out["src2"] == "https://media.crowntime.cn/access2.jpg?e=1&token=t"


# ---------------------------------------------------------------------------
# Never persists a signed URL to localStorage / analytics
# ---------------------------------------------------------------------------


def test_hydration_source_never_references_localstorage_or_analytics() -> None:
    """RND-187 explicitly forbids persisting a signed URL client-side. There
    is no analytics layer in this codebase to check against, so this test
    guards the negative: the hydration functions' own source must never
    reference localStorage or any analytics-sounding global."""
    src = "\n".join(
        [
            _extract(r"function loadMediaImage\(img\)\{.*?\n\}", "loadMediaImage()"),
            _extract(r"function onMediaImageError\(img\)\{.*?\n\}", "onMediaImageError()"),
            _extract(r"function showMediaError\(img\)\{.*?\n\}", "showMediaError()"),
            _extract(r"function hydrateMediaImages\(root\)\{.*?\n\}", "hydrateMediaImages()"),
        ]
    )
    assert "localStorage" not in src
    assert "analytics" not in src.lower()
