"""RND-323: staff auto-selection is isolated by tenant and admin user."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

_SOURCE = review_console_js_source()


def _function(name: str) -> str:
    match = re.search(r"function " + name + r"\([^)]*\)\{.*?\n\}", _SOURCE, re.S)
    assert match is not None, f"{name} not found"
    return match.group(0)


def test_auth_me_exposes_admin_user_id() -> None:
    """The browser key must use stable `AdminUser.id`, not WeCom identity."""
    auth_source = (Path(__file__).parent.parent / "app" / "routers" / "auth.py").read_text(
        encoding="utf-8"
    )
    assert '"id": user.id,' in auth_source


def test_staff_auto_select_and_storage_isolation() -> None:
    """Execute the real selection functions against a minimal in-memory DOM."""
    conversation = (
        Path(__file__).parent.parent
        / "app" / "web" / "static" / "console" / "conversation-list.js"
    ).read_text(encoding="utf-8")
    select = _function("selectEntityIfPresent")
    harness = r'''
var mode='staff',selEntityId=null,selConvId=null,selEntityName=null,selConvName=null;
var timelineConvId=null,timelineMsgs=[],timelineHasOlder=false,timelineNextBefore=null;
var lastEntityItems=null,lastEntitySig=null,currentTenantId='T1',currentUserId='U1',searchSelectionInProgress=false;
var calls=[];
var I18N={t:function(k){return k;}};
function esc(s){return s==null?'':String(s);}
function entityListSignature(){return '';}
function updateScopeButton(){} function closeScopePopover(){} function resetPanelForNoConversation(){}
function loadConversations(id){calls.push(id);}
var storage={};
var localStorage={setItem:function(k,v){storage[k]=String(v);},getItem:function(k){return storage[k]||null;}};
function El(id,name){this.dataset={id:id,name:name||id};this._classes={};var self=this;this.classList={add:function(c){self._classes[c]=true;},remove:function(c){delete self._classes[c];},toggle:function(c,on){if(on)self._classes[c]=true;else delete self._classes[c];}};}
function Body(){this.items=[];this._html='';}
Body.prototype.querySelectorAll=function(sel){return sel==='.entity-item'?this.items:[];};
Object.defineProperty(Body.prototype,'innerHTML',{get:function(){return this._html;},set:function(v){this._html=String(v);this.items=[];var re=/data-id="([^"]*)" data-name="([^"]*)"/g,m;while((m=re.exec(this._html)))this.items.push(new El(m[1],m[2]));}});
var entityBody=new Body(); var elements={'entity-body':entityBody};
['conv-header','timeline-header','timeline-body','conv-body'].forEach(function(id){elements[id]=new Body();});
var document={getElementById:function(id){return elements[id]||null;},querySelectorAll:function(sel){return sel==='.entity-item'?entityBody.items:[];}};
''' + conversation + '\n' + select + r'''
function renderPanelAuditEmpty(){}
function reset(){ selEntityId=null;selEntityName=null;selConvId=null;calls=[];entityBody.items=[];}
var many=[{staff_id:'A',display_name:'A'},{staff_id:'B',display_name:'B'}];
renderEntityList([{staff_id:'only',display_name:'Only'}]);
var one={id:selEntityId,calls:calls.slice(),key:storage['rnd.lastEntity.T1.U1']};
reset(); renderEntityList(many); var emptyMulti=selEntityId;
onEntityClick(entityBody.items[0]);
var persisted=storage['rnd.lastEntity.T1.U1'];
reset(); renderEntityList(many); var restored=selEntityId;
currentTenantId='T2'; reset(); renderEntityList(many); var tenantIsolated=selEntityId;
currentTenantId='T1';currentUserId='U2';reset();renderEntityList(many);onEntityClick(entityBody.items[1]);
var userTwo=storage['rnd.lastEntity.T1.U2'];
currentUserId='U1';reset();renderEntityList(many);var userOneRestored=selEntityId;
currentUserId='U2';reset();renderEntityList(many);var userTwoRestored=selEntityId;
currentTenantId=null; currentUserId=null; reset(); renderEntityList(many); onEntityClick(entityBody.items[0]);
var noIdentityWrite=storage['rnd.lastEntity']===undefined;
currentTenantId='T1';currentUserId='U1';storage['rnd.lastEntity.T1.U1']='missing';reset();renderEntityList(many);var stale=selEntityId;
localStorage={setItem:function(){throw new Error('disabled');},getItem:function(){throw new Error('disabled');}};reset();renderEntityList(many);var storageDisabled=selEntityId;
mode='contact';reset();renderEntityList([{contact_id:'contact',display_name:'Contact'}]);var contact=selEntityId;
process.stdout.write(JSON.stringify({one:one,emptyMulti:emptyMulti,persisted:persisted,restored:restored,tenantIsolated:tenantIsolated,userTwo:userTwo,userOneRestored:userOneRestored,userTwoRestored:userTwoRestored,noIdentityWrite:noIdentityWrite,stale:stale,storageDisabled:storageDisabled,contact:contact}));
'''
    result = run_node(harness)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["one"] == {"id": "only", "calls": ["only"], "key": "only"}
    assert out["emptyMulti"] is None
    assert out["persisted"] == "A"
    assert out["restored"] == "A"
    assert out["tenantIsolated"] is None
    assert out["userTwo"] == "B"
    assert out["userOneRestored"] == "A"
    assert out["userTwoRestored"] == "B"
    assert out["noIdentityWrite"] is True
    assert out["stale"] is None
    assert out["storageDisabled"] is None
    assert out["contact"] is None
