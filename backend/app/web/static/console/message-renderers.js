var MEDIA_LABELS={image:I18N.t('media.image'),video:I18N.t('media.video'),voice:I18N.t('media.voice'),file:I18N.t('media.file')};
var MEDIA_STATUS_LABELS={not_downloaded:I18N.t('media.status.notDownloaded'),unsupported:I18N.t('media.status.unsupported'),unknown:I18N.t('media.status.unknown'),failed:I18N.t('media.status.failed')};
function rebuildMediaLabels(){
  MEDIA_LABELS={image:I18N.t('media.image'),video:I18N.t('media.video'),voice:I18N.t('media.voice'),file:I18N.t('media.file')};
  MEDIA_STATUS_LABELS={not_downloaded:I18N.t('media.status.notDownloaded'),unsupported:I18N.t('media.status.unsupported'),unknown:I18N.t('media.status.unknown'),failed:I18N.t('media.status.failed')};
}
// Archive Console v2 (Message Types spec, section 七 · 媒体状态): grade the
// STATIC media_status placeholder (a backend classifier signal, checked
// BEFORE any descriptor fetch is even attempted) by recoverability instead
// of one uniform grey line -- not_downloaded/failed/unsupported each get
// distinct explanatory copy. This is separate from, and does not change,
// the live descriptor-fetch error path (classifyMediaError/buildErrorBox
// below), which already has its own correct 401/403/404/network handling
// and retry-once behavior. No "retry download"/"view failure detail"
// click actions are wired here -- there is no backend endpoint to trigger
// either of those yet, and this file's own convention is to never render a
// non-functional action rather than fabricate one.
var MEDIA_STATUS_DOT={not_downloaded:['#c2703a','#fdf0e6','…'],failed:['#d4436b','#fdeaef','!'],unsupported:['#7a828f','#f1f3f7','—'],unknown:['#7a828f','#f1f3f7','?']};
var MEDIA_STATUS_REASON_KEYS={not_downloaded:'media.status.notDownloaded.reason',failed:'media.status.failed.reason',unsupported:'media.status.unsupported.reason'};
function renderGradedMediaPlaceholder(typeLabel,status){
  var dot=MEDIA_STATUS_DOT[status]||MEDIA_STATUS_DOT.unsupported;
  var statusLabel=MEDIA_STATUS_LABELS[status]||I18N.t('media.status.unsupported');
  var reasonKey=MEDIA_STATUS_REASON_KEYS[status];
  var html='<div class="media-placeholder">'
    +'<div class="media-placeholder-title"><span class="media-placeholder-dot" style="color:'+dot[0]+';background:'+dot[1]+'">'+dot[2]+'</span>'
    +esc(typeLabel)+' · '+esc(statusLabel)+'</div>';
  if(reasonKey)html+='<div class="media-placeholder-reason">'+esc(I18N.t(reasonKey))+'</div>';
  html+='</div>';
  return html;
}
var MessageTypeRegistry=(function(){
  var entries=RND216_MTR_ENTRIES;
  var FALLBACK={category:'placeholder',placeholderKey:'placeholder.unsupported'};
  function resolve(msgtype){
    return (msgtype&&Object.prototype.hasOwnProperty.call(entries,msgtype))?entries[msgtype]:null;
  }
  function resolvePlaceholder(msgtype){
    var entry=resolve(msgtype);
    return (entry&&entry.category==='placeholder')?entry:null;
  }
  // RND-198: resolveSystem() returns the entry if it is a "system" category
  // entry (used by the system card renderer dispatch). Returns null otherwise.
  function resolveSystem(msgtype){
    var entry=resolve(msgtype);
    return (entry&&entry.category==='system')?entry:null;
  }
  return {entries:entries,resolve:resolve,resolvePlaceholder:resolvePlaceholder,resolveSystem:resolveSystem,fallback:FALLBACK};
})();
// RND-197 — structured card rendering. isSafeUrl mirrors the backend's
// app.structured_message_parser.safe_url (http/https absolute URLs only)
// as defense-in-depth: structured_content.fields is already filtered
// server-side, but nothing here should trust that without re-checking at
// the point a value becomes an href/src.
function isSafeUrl(u){
  if(!u||typeof u!=='string')return false;
  try{
    var parsed=new URL(u);
    return (parsed.protocol==='http:'||parsed.protocol==='https:')&&!!parsed.host;
  }catch(e){return false;}
}
function hostnameOf(u){
  try{return new URL(u).hostname;}catch(e){return null;}
}
function fmtCoord(n){return (typeof n==='number'&&!isNaN(n))?n.toFixed(6):'';}
// Shared "known type, content unavailable" card — used for card/docmsg/
// audio_doc (no field extraction attempted at all — see
// app.structured_message_parser module docstring) and as the terminal
// fallback for any structured type whose fields failed to parse
// (malformed/historical dirty data).
function renderStructuredFallback(m){
  var extra=(m.normalized_type==='audio_doc')
    ?I18N.t('card.audioDoc.playbackUnavailable')
    :I18N.t('card.generic.unavailable');
  return '<div class="structured-card structured-card-fallback">'
    +'<div class="structured-card-type-label">'+esc(I18N.t(m.display_label_key))+'</div>'
    +'<div class="structured-card-degraded">'+esc(extra)+'</div>'
    +'</div>';
}
function renderLinkCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var url=isSafeUrl(f.url)?f.url:null;
  var host=url?hostnameOf(url):null;
  var title=f.title||host||I18N.t('messageType.link');
  var html='<div class="structured-card structured-card-link">';
  if(f.image_url&&isSafeUrl(f.image_url)){
    html+='<img class="structured-card-img" src="'+esc(f.image_url)+'" alt="" loading="lazy" onerror="this.remove()">';
  }
  html+='<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.description)html+='<div class="structured-card-desc">'+esc(f.description)+'</div>';
  // RND-206 QA fix #14: hostname shown as its own line (distinct from the
  // title, which may equal it as a fallback above) and a localized
  // "open link" action instead of duplicating the full raw URL as the
  // link's visible text. href/target/rel and the http(s)-only safety
  // check (isSafeUrl) are unchanged; an unsafe/missing URL renders a
  // disabled, non-navigable action instead of ever falling back to an
  // unvalidated href.
  if(host)html+='<div class="structured-card-hostname">'+esc(host)+'</div>';
  html+=url
    ?'<a class="structured-card-link-action" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(I18N.t('link.openLink'))+'</a>'
    :'<span class="structured-card-link-action structured-card-link-disabled" aria-disabled="true">'+esc(I18N.t('card.link.unavailable'))+'</span>';
  html+='</div>';
  return html;
}
function renderLocationCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var primary=f.name||f.address||null;
  var hasCoords=typeof f.latitude==='number'&&typeof f.longitude==='number';
  var html='<div class="structured-card structured-card-location">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.location'))+'</div>';
  if(primary){
    html+='<div class="structured-card-title">'+esc(primary)+'</div>';
    if(f.name&&f.address&&f.address!==f.name)html+='<div class="structured-card-desc">'+esc(f.address)+'</div>';
    if(hasCoords)html+='<div class="structured-card-meta">'+fmtCoord(f.latitude)+', '+fmtCoord(f.longitude)+'</div>';
  }else if(hasCoords){
    html+='<div class="structured-card-title">'+fmtCoord(f.latitude)+', '+fmtCoord(f.longitude)+'</div>';
  }else{
    html+='<div class="structured-card-degraded">'+esc(I18N.t('card.location.unknown'))+'</div>';
  }
  html+='</div>';
  return html;
}
// Escape-first, fixed-subset sanitizer — no markdown dependency. Link
// URLs are validated against the raw (pre-escape) value with isSafeUrl
// and substituted back in after the rest of the text is escaped, so an
// unsafe/malformed link degrades to its escaped literal text rather than
// ever reaching innerHTML unescaped.
function renderSanitizedMarkdown(raw){
  var text=String(raw);
  var links=[];
  text=text.replace(/\[([^\]\n]*)\]\(([^)\n]*)\)/g,function(whole,label,url){
    var token=' LINK'+links.length+' ';
    links.push({label:label||url,url:isSafeUrl(url)?url:null});
    return token;
  });
  text=esc(text);
  text=text.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  var out=[];var inList=false;
  text.split(/\n/).forEach(function(line){
    var bullet=line.match(/^\s*[-*]\s+(.*)$/);
    if(bullet){
      if(!inList){out.push('<ul>');inList=true;}
      out.push('<li>'+bullet[1]+'</li>');
    }else{
      if(inList){out.push('</ul>');inList=false;}
      out.push(line+'<br>');
    }
  });
  if(inList)out.push('</ul>');
  var html=out.join('').replace(/<br>$/,'');
  links.forEach(function(link,i){
    var token=' LINK'+i+' ';
    var replacement=link.url
      ?'<a href="'+esc(link.url)+'" target="_blank" rel="noopener noreferrer">'+esc(link.label)+'</a>'
      :esc('['+link.label+']');
    html=html.split(token).join(replacement);
  });
  return html;
}
function renderMarkdownCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f||!f.content){
    return '<div class="structured-card structured-card-markdown"><div class="structured-card-degraded">'+esc(I18N.t('card.markdown.empty'))+'</div></div>';
  }
  return '<div class="structured-card structured-card-markdown">'+renderSanitizedMarkdown(f.content)+'</div>';
}
function renderNewsCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  var articles=(f&&f.articles)||[];
  if(!Array.isArray(articles))articles=[];
  if(!articles.length){
    return '<div class="structured-card structured-card-news"><div class="structured-card-degraded">'+esc(I18N.t('card.news.empty'))+'</div></div>';
  }
  var html='<div class="structured-card structured-card-news">';
  articles.forEach(function(a){
    var url=isSafeUrl(a.url)?a.url:null;
    var title=a.title||I18N.t('card.news.noTitle');
    html+='<div class="structured-card-news-item">';
    if(a.image_url&&isSafeUrl(a.image_url)){
      html+='<img class="structured-card-img" src="'+esc(a.image_url)+'" alt="" loading="lazy" onerror="this.remove()">';
    }
    html+=url
      ?'<a class="structured-card-title" style="color:inherit;text-decoration:none;display:block" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(title)+'</a>'
      :'<div class="structured-card-title">'+esc(title)+'</div>';
    if(a.description)html+='<div class="structured-card-desc">'+esc(a.description)+'</div>';
    html+='</div>';
  });
  html+='</div>';
  return html;
}
function renderMiniprogramCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||f.display_name||I18N.t('messageType.miniprogram');
  var html='<div class="structured-card structured-card-miniprogram">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.miniprogram'))+'</div>';
  if(f.icon_url&&isSafeUrl(f.icon_url)){
    html+='<img class="structured-card-img" style="max-height:60px;max-width:60px" src="'+esc(f.icon_url)+'" alt="" loading="lazy" onerror="this.remove()">';
  }
  html+='<div class="structured-card-title">'+esc(title)+'</div>';
  // pagepath is an internal mini-program route, not a browser URL — never
  // rendered as a clickable link (ticket requirement).
  if(f.username)html+='<div class="structured-card-meta">'+esc(f.username)+'</div>';
  html+='</div>';
  return html;
}
function sphfeedTypeLabel(feedType){
  if(feedType===2)return I18N.t('card.sphfeed.image');
  if(feedType===4)return I18N.t('card.sphfeed.video');
  if(feedType===9)return I18N.t('card.sphfeed.live');
  return I18N.t('card.sphfeed.unknown');
}
// WeCom's sphfeed payload has no playback URL or SDK media id.  This is a
// faithful archive card (type, account and description), never a fake video
// player or a link reconstructed from untrusted content.
function renderSphfeedCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.sph_name||I18N.t('messageType.sphfeed');
  var html='<div class="structured-card structured-card-sphfeed">'
    +structuredCardHeader('messageType.sphfeed',m.msgtype,CARD_DOT_COLORS.sphfeed)
    +'<div class="structured-card-title">'+esc(title)+'</div>'
    +'<div class="structured-card-meta">'+esc(sphfeedTypeLabel(f.feed_type))+'</div>';
  if(f.feed_desc){
    html+='<div class="structured-card-desc">'+esc(f.feed_desc)+'</div>';
  }else{
    html+='<div class="structured-card-degraded">'+esc(I18N.t('card.sphfeed.empty'))+'</div>';
  }
  html+='</div>';
  return html;
}
// Archive Console v2 (Message Types spec, section 四 · 互动业务类): a
// single shared card header (color dot + type label + raw msgtype badge)
// for every interactive/business card type below plus audio_archive/
// audio_doc -- "share one card system," not a bespoke header per type.
// redpacket is the one deliberate exception (keeps its own warm-gradient
// header, see renderRedpacketCard) -- the spec calls that out explicitly
// as mirroring WeCom's native orange bubble rather than the plain-dot card.
var CARD_DOT_COLORS={todo:'#e5844d',vote:'#0891b2',collect:'#8b5cf6',meeting:'#1677ff',schedule:'#8b5cf6',switch_corp:'#98a0ab',audio_archive:'#0891b2',audio_doc:'#0891b2',sphfeed:'#e5844d'};
function structuredCardHeader(labelKey,rawType,dotColor){
  return '<div class="sc-hd"><div class="sc-hd-dot" style="background:'+esc(dotColor)+'"></div>'
    +'<span class="sc-hd-label">'+esc(I18N.t(labelKey))+'</span>'
    +'<span class="sc-hd-raw">'+esc(rawType||'')+'</span></div>';
}
// RND-198: business card renderers for interactive message types.
function renderVoteCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.vote');
  var html='<div class="structured-card structured-card-vote">'
    +structuredCardHeader('messageType.vote',m.msgtype,CARD_DOT_COLORS.vote)
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.type)html+='<div class="structured-card-meta">'+esc(I18N.t('card.vote.type'))+esc(f.type)+'</div>';
  if(Array.isArray(f.items)&&f.items.length){
    html+='<ul style="margin:.2rem 0 .2rem 1.1rem">';
    f.items.forEach(function(item){
      var name=item.name||I18N.t('card.vote.unnamed');
      var count=(typeof item.count==='number')?' ('+item.count+')':'';
      html+='<li>'+esc(name)+count+'</li>';
    });
    html+='</ul>';
  }else{
    html+='<div class="structured-card-degraded">'+esc(I18N.t('card.vote.noItems'))+'</div>';
  }
  html+='</div>';
  return html;
}
function renderTodoCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.todo');
  var html='<div class="structured-card structured-card-todo">'
    +structuredCardHeader('messageType.todo',m.msgtype,CARD_DOT_COLORS.todo)
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.content)html+='<div class="structured-card-desc">'+esc(f.content)+'</div>';
  html+='</div>';
  return html;
}
function renderCollectCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.collect');
  var html='<div class="structured-card structured-card-collect">'
    +structuredCardHeader('messageType.collect',m.msgtype,CARD_DOT_COLORS.collect)
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(Array.isArray(f.details)&&f.details.length){
    html+='<div class="structured-card-meta">'+esc(f.details.length+' '+I18N.t('card.collect.entries'))+'</div>';
  }
  html+='</div>';
  return html;
}
function renderMeetingCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.meeting');
  var html='<div class="structured-card structured-card-meeting">'
    +structuredCardHeader('messageType.meeting',m.msgtype,CARD_DOT_COLORS.meeting)
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.time)html+='<div class="structured-card-meta">'+esc(I18N.t('card.meeting.time'))+fmtTime(f.time)+'</div>';
  if(f.place)html+='<div class="structured-card-meta">'+esc(I18N.t('card.meeting.place'))+esc(f.place)+'</div>';
  if(f.agenda)html+='<div class="structured-card-desc">'+esc(f.agenda)+'</div>';
  html+='</div>';
  return html;
}
function renderScheduleCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.schedule');
  var html='<div class="structured-card structured-card-schedule">'
    +structuredCardHeader('messageType.schedule',m.msgtype,CARD_DOT_COLORS.schedule)
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.starttime)html+='<div class="structured-card-meta">'+esc(I18N.t('card.schedule.start'))+fmtTime(f.starttime)+'</div>';
  if(f.endtime)html+='<div class="structured-card-meta">'+esc(I18N.t('card.schedule.end'))+fmtTime(f.endtime)+'</div>';
  if(f.place)html+='<div class="structured-card-meta">'+esc(I18N.t('card.schedule.place'))+esc(f.place)+'</div>';
  if(f.description)html+='<div class="structured-card-desc">'+esc(f.description)+'</div>';
  html+='</div>';
  return html;
}
// Redpacket keeps its own warm-gradient header (spec: "归档台保留可辨识的
// 暖色卡头" -- mirrors WeCom's native orange bubble) instead of the shared
// plain-dot structuredCardHeader every other business card above uses.
// Still never shows monetary amounts (security) -- unchanged from before.
function renderRedpacketCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  var label=I18N.t('card.redpacket.label');
  var wishing=f&&f.wishing||null;
  var html='<div class="structured-card structured-card-redpacket">'
    +'<div class="redpacket-hd"><div class="redpacket-hd-icon"></div>'
    +'<span class="redpacket-hd-label">'+esc(label)+'</span>'
    +'<span class="redpacket-hd-raw">'+esc(m.msgtype||'')+'</span></div>'
    +'<div class="redpacket-body">';
  if(wishing)html+='<div class="structured-card-desc">'+esc(wishing)+'</div>';
  if(f&&typeof f.totalnum==='number')html+='<div class="structured-card-meta">'+esc(f.totalnum+' '+I18N.t('card.redpacket.nPackets'))+'</div>';
  if(!wishing&&!(f&&typeof f.totalnum==='number'))html+='<div class="structured-card-degraded">'+esc(I18N.t('card.generic.unavailable'))+'</div>';
  html+='</div></div>';
  return html;
}
function renderSwitchCorpCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var corpName=f.corp_name||'';
  var html='<div class="structured-card structured-card-switchcorp">'
    +structuredCardHeader('messageType.switchCorp',m.msgtype,CARD_DOT_COLORS.switch_corp);
  if(corpName)html+='<div class="structured-card-title">'+esc(I18N.t('card.switchCorp.switchedTo'))+esc(corpName)+'</div>';
  html+='</div>';
  return html;
}
// RND-210 (+ QA FAIL remediation): business-card (名片) renderer. Surfaces
// the company name + contact identifier extracted by parse_card_message.
// When the backend resolved a tenant-scoped Contact display name
// (f.contact_name), it is shown as the primary contact label so the
// timeline reads "张三" instead of the raw WeCom userid "contact_zhangsan";
// the raw userid is shown as a secondary line for traceability. The WeCom
// protocol does NOT provide a display name or avatar itself, so when no
// Contact match exists we fall back to the raw userid and never fabricate
// an avatar/name (card.businessCard.noAvatar).
function renderCardMessage(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var corpName=f.corpname||'';
  var contactName=f.contact_name||f.userid||'';
  var contactId=f.userid||'';
  var html='<div class="structured-card structured-card-businesscard">'
    +'<div class="structured-card-type-label">'+esc(I18N.t(m.display_label_key))+'</div>';
  if(corpName)html+='<div class="structured-card-title">'+esc(I18N.t('card.businessCard.corpName'))+esc(corpName)+'</div>';
  if(contactName)html+='<div class="structured-card-desc">'+esc(I18N.t('card.businessCard.contactName'))+esc(contactName)+'</div>';
  // Show the raw userid only when it differs from the resolved name (i.e.
  // a name was actually found) — otherwise it would merely repeat the label.
  if(contactId&&contactName!==contactId)html+='<div class="structured-card-meta structured-card-userid">'+esc(I18N.t('card.businessCard.contactId'))+esc(contactId)+'</div>';
  // Protocol never provides an avatar/snapshot — state that rather than
  // inventing one (RND-210 privacy boundary).
  html+='<div class="structured-card-meta">'+esc(I18N.t('card.businessCard.noAvatar'))+'</div>';
  html+='</div>';
  return html;
}
// RND-210 (+ QA FAIL remediation) / RND-202: audio-archive
// (meeting_voice_call / audio_archive) renderer. Previously audio_archive
// was a PLACEHOLDER type excluded from the frontend registry, so it
// collapsed into the generic "unknown message type"; RND-210 added the
// type label + end time + an explicit "not playable" status. RND-202 adds
// real playback: when this row's own media_status/media_access_url are
// "available" (set generically by the timeline API once a recording has
// been downloaded through the unified media pipeline — see
// app.media_download._SIGNATURE_CATEGORY_BY_MSGTYPE), the card renders a
// real lazily-hydrated <audio> element via the EXACT same richMediaPlaceholder
// ('voice', ...) -> hydrateRichMedia -> swapRichMediaPlaceholder path
// RND-206 already built for ordinary voice messages — no new hydration
// code. Call participants are NOT duplicated here: the caller is already
// shown by the standard per-message sender header every row gets; the
// callee (m.recipient_display_names) is not otherwise shown for a 1:1
// row, so it is surfaced here. starttime/duration are shown only when the
// (unconfirmed-schema, defensively-parsed — see
// parse_meetingvoicecall_message) fields are actually present; nothing is
// fabricated when absent, matching every other structured card in this
// file. Missing enterprise call-recording permission (no sdkfileid, or a
// tenant without archive-call access) degrades to the existing
// "not playable" placeholder — never a crash, never a fabricated player.
function renderAudioArchiveMessage(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var st=f.starttime,et=f.endtime;
  // WeCom endtime/starttime are epoch-seconds; fmtTime expects epoch-ms —
  // convert only when the value looks like seconds (defensive for either
  // unit).
  if(st&&st<1e12)st=st*1000;
  if(et&&et<1e12)et=et*1000;
  var html='<div class="structured-card structured-card-audioarchive">'
    +structuredCardHeader('messageType.audioArchive',m.msgtype,CARD_DOT_COLORS.audio_archive);
  if(st)html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.startedAt'))+esc(fmtTime(st))+'</div>';
  if(et)html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.endedAt'))+esc(fmtTime(et))+'</div>';
  if(f.duration_seconds!=null)html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.duration'))+esc(fmtDurationSeconds(f.duration_seconds))+'</div>';
  if(m.recipient_display_names&&m.recipient_display_names.length)html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.callee'))+esc(m.recipient_display_names.join('、'))+'</div>';
  if(m.media_status==='available'&&m.media_access_url){
    html+=richMediaPlaceholder('voice',m.media_access_url,'voice.loading');
  }else{
    html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.playbackUnavailable'))+'</div>';
  }
  html+='</div>';
  return html;
}
function fmtDurationSeconds(totalSeconds){
  var s=Math.max(0,Math.round(totalSeconds));
  var m=Math.floor(s/60);
  var r=s%60;
  return m+':'+String(r).padStart(2,'0');
}
// RND-210 (+ QA FAIL remediation): audio-shared-doc (voip_doc_share /
// audio_doc) renderer. Shows the shared document title (when the parser
// captured one) and an explicit "not playable" status. The previous
// renderStructuredFallback copy omitted the title, so surface it here.
function renderAudioDocMessage(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||f.docid||I18N.t('messageType.audioDoc');
  var html='<div class="structured-card structured-card-audiodoc">'
    +structuredCardHeader('messageType.audioDoc',m.msgtype,CARD_DOT_COLORS.audio_doc)
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.url&&isSafeUrl(f.url))html+='<a class="structured-card-link-action" href="'+esc(f.url)+'" target="_blank" rel="noopener noreferrer">'+esc(I18N.t('link.openLink'))+'</a>';
  html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.playbackUnavailable'))+'</div>';
  html+='</div>';
  return html;
}
// RND-198: system event card renderer — dispatches by action subtype,
// renders a distinct (non-bubble) card visually separate from chat messages.
// QA fix: unknown subtypes must never render raw i18n keys (e.g.
// "system.event.future_action"). If I18N.t() returns the key itself
// (no translation exists), fall back to the generic localized label.
function renderSystemCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  var subtype=f&&f.subtype||null;
  var displayText=f&&f.display_text||null;
  var html='<div class="system-card" style="text-align:center;font-size:.78rem;color:#999;padding:.25rem .5rem;">';
  if(displayText){
    html+=esc(displayText);
  }else if(subtype){
    var key='system.event.'+subtype;
    var translated=I18N.t(key);
    // I18N.t() returns the key itself when no translation exists —
    // detect this and fall back to the generic system event label.
    if(translated===key){
      html+=esc(I18N.t('system.event.unknown'));
    }else{
      html+=esc(translated);
    }
  }else{
    html+=esc(I18N.t('system.event.unknown'));
  }
  html+='</div>';
  return html;
}
var STRUCTURED_CARD_RENDERERS={
  link:renderLinkCard,
  location:renderLocationCard,
  markdown:renderMarkdownCard,
  news:renderNewsCard,
  miniprogram:renderMiniprogramCard,
  sphfeed:renderSphfeedCard,
  card:renderCardMessage,
  docmsg:renderStructuredFallback,
  // RND-210 (+ QA FAIL remediation): audio_archive now renders a dedicated
  // card (type label + end time + explicit "not playable") instead of the
  // generic unknown placeholder. audio_doc shows the shared-doc title.
  audio_archive:renderAudioArchiveMessage,
  audio_doc:renderAudioDocMessage,
  // RND-198 interactive business types
  vote:renderVoteCard,
  todo:renderTodoCard,
  collect:renderCollectCard,
  meeting:renderMeetingCard,
  schedule:renderScheduleCard,
  redpacket:renderRedpacketCard,
  switch_corp:renderSwitchCorpCard
};
function renderStructuredCard(m){
  var fn=STRUCTURED_CARD_RENDERERS[m.normalized_type];
  return fn?fn(m):renderStructuredFallback(m);
}
// ---------------------------------------------------------------------------
// RND-206 — unified rich-media rendering: MediaAccessCache, the shared
// Viewer, and video/voice/file/emotion/composite (mixed/chatrecord)
// renderers. Every renderer here consumes already-normalized data (a
// TimelineMessageOut row, or a composite node's {type,text,fields,media,
// children} shape from structured_content.fields) -- never a raw backend
// payload, storage path, media_id, or sdkfileid.
// ---------------------------------------------------------------------------

// In-memory only (no localStorage/sessionStorage) media access descriptor
// cache, keyed by access-url. Reused by every lazy media renderer below
// (video/voice/file/emotion + nested/composite media + the Viewer) so a
// re-render (composite re-render, refresh poll) does not re-mint a Qiniu
// signed URL unnecessarily. Refreshes automatically once the cached
// descriptor's expires_at has passed (or is within 5s of expiring) --
// access_type="proxy" descriptors have no expiry and are cached forever
// for the page lifetime.
// RND-206 QA fix: malformed/unparseable expires_at must never be treated as
// "cache forever" -- it is treated as already-expired (non-cacheable), the
// opposite of the pre-fix behavior. Descriptor fetch failures carry a
// numeric `.status` (HTTP status) or `.network=true` so callers can
// classify the failure (401/403/404/network) instead of a single generic
// error bucket.
var MediaAccessCache=(function(){
  var store={};
  function isFresh(entry){
    if(!entry)return false;
    if(!entry.expires_at)return true;
    var expiresMs=Date.parse(entry.expires_at);
    if(isNaN(expiresMs))return false;
    return expiresMs-Date.now()>5000;
  }
  function get(accessUrl){
    if(!accessUrl){var e0=new Error('no access url');e0.status=0;return Promise.reject(e0);}
    var cached=store[accessUrl];
    if(isFresh(cached))return Promise.resolve(cached);
    return fetch(accessUrl,{credentials:'same-origin'}).then(function(r){
      if(!r.ok){var e=new Error('HTTP '+r.status);e.status=r.status;throw e;}
      return r.json();
    }).then(function(desc){
      store[accessUrl]=desc;
      return desc;
    }).catch(function(err){
      if(typeof err.status!=='number')err.network=true;
      throw err;
    });
  }
  function invalidate(accessUrl){delete store[accessUrl];}
  return {get:get,invalidate:invalidate};
})();

// Classifies a MediaAccessCache error into one of the required buckets
// (RND-206 QA fix #8): auth (401) / forbidden-or-expired (403) / missing
// (404) / network (no HTTP status at all, e.g. offline) / generic error.
function classifyMediaError(e){
  if(e&&e.status===401)return 'auth';
  if(e&&e.status===403)return 'forbidden';
  if(e&&e.status===404)return 'missing';
  if(e&&e.network)return 'network';
  return 'error';
}
function redirectToLogin(){
  if(typeof window!=='undefined'&&window.location)window.location.href='/admin/login';
}
// Controlled, bounded refresh: on a 403 (permission denied or an access
// grant that expired between mint and use) invalidate the cached
// descriptor and request exactly one fresh one. `retried` prevents any
// possibility of an infinite retry loop -- a second failure of any kind is
// surfaced to the caller as-is.
function fetchDescriptorWithRecovery(accessUrl,retried){
  return MediaAccessCache.get(accessUrl).catch(function(e){
    if(!retried&&e&&e.status===403){
      MediaAccessCache.invalidate(accessUrl);
      return fetchDescriptorWithRecovery(accessUrl,true);
    }
    if(e&&e.status===401)redirectToLogin();
    throw e;
  });
}

function fmtBytes(n){
  if(typeof n!=='number'||isNaN(n))return I18N.t('file.sizeUnknown');
  var units=['B','KB','MB','GB'];var i=0;var v=n;
  while(v>=1024&&i<units.length-1){v/=1024;i++;}
  return (i===0?String(v):v.toFixed(1))+' '+units[i];
}
// RND-206 QA fix #12: display the authorized descriptor's filename when the
// backend provides one (currently always null -- see NestedMediaAccessOut's
// docstring, a documented backend-contract gap, not fabricated client-side)
// with a localized fallback. Never derived from local_path/object_key/URL.
function fmtFileName(desc){
  if(desc&&typeof desc.filename==='string'&&desc.filename.trim())return desc.filename;
  return I18N.t('file.fallbackName');
}

/* RND-217: the shared Viewer registry (timelineViewerItems /
   registerViewerItem) now lives in media-viewer.js alongside the rest of
   the Viewer's state and code. renderTimeline() resets timelineViewerItems
   and the composite renderers below call registerViewerItem() at runtime;
   both are globals defined by media-viewer.js, which loads immediately
   after this file, so they resolve before any renderTimeline() pass runs. */


// ---------------------------------------------------------------------------
// Lazy rich-media hydration -- ONE shared engine (MediaAccessCache +
// data-rnd206-kind/data-rnd206-access-url) for every media kind
// (image/video/voice/file/emotion) in BOTH the top-level timeline and every
// nested mixed/chatrecord node (RND-206 QA fix #2/#3: no separate
// access-fetch logic duplicated inside the composite renderers -- they
// only ever emit a placeholder built by richMediaPlaceholder() below and
// this same hydrateRichMedia() call resolves it, wherever it was mounted).
// Called after every DOM insertion point that can contain one of these
// placeholders: renderTimeline() (top-level + inline mixed children) and
// viewerShow()'s chatrecord branch (nested chatrecord content).
// ---------------------------------------------------------------------------
function hydrateRichMedia(root){
  root.querySelectorAll('[data-rnd206-access-url]').forEach(function(el){loadRichMedia(el);});
}
function loadRichMedia(el){
  var accessUrl=el.getAttribute('data-rnd206-access-url');
  var kind=el.getAttribute('data-rnd206-kind');
  if(!accessUrl){showRichMediaError(el,kind,'missing');return;}
  fetchDescriptorWithRecovery(accessUrl).then(function(desc){
    swapRichMediaPlaceholder(el,kind,desc);
  }).catch(function(e){
    showRichMediaError(el,kind,classifyMediaError(e));
  });
}
function buildErrorBox(kind,errKind){
  var box=document.createElement('div');
  box.className='media-placeholder';
  box.setAttribute('role','alert');
  var key;
  if(errKind==='auth')key='viewer.unauthorized';
  else if(errKind==='forbidden')key='media.error.forbidden';
  else if(errKind==='missing')key='viewer.missingMedia';
  else if(errKind==='network')key='media.error.network';
  else key=kind==='video'?'video.playbackError':kind==='voice'?'voice.playbackError':kind==='file'?'file.unavailable':'media.loadFailed';
  box.textContent=I18N.t(key);
  return box;
}
function showRichMediaError(el,kind,errKind){
  var box=buildErrorBox(kind,errKind||'error');
  if(el.parentNode)el.parentNode.replaceChild(box,el);
  else if(el.tagName)el.textContent=box.textContent;
}
// RND-206 QA fix #8: a REAL playback/load failure of the mounted element
// (distinct from a descriptor-fetch failure, already handled by
// loadRichMedia/showRichMediaError above) invalidates the cached
// descriptor and retries exactly once, updating the SAME element in place
// (no DOM replacement, so an unaffected sibling video/audio never
// reloads). A second failure replaces `outerEl` (the element actually
// mounted in the timeline/viewer DOM -- may wrap `mediaEl`, e.g. an <img>
// inside a <button>) with a classified error box.
function handleRichMediaPlaybackFailure(kind,accessUrl,mediaEl,outerEl){
  if(mediaEl.getAttribute&&mediaEl.getAttribute('data-rnd206-retried')==='1'){
    var box=buildErrorBox(kind,'error');
    if(outerEl.parentNode)outerEl.parentNode.replaceChild(box,outerEl);
    return;
  }
  if(mediaEl.setAttribute)mediaEl.setAttribute('data-rnd206-retried','1');
  MediaAccessCache.invalidate(accessUrl);
  fetchDescriptorWithRecovery(accessUrl,true).then(function(desc){
    if(kind==='file'){mediaEl.href=desc.url;}else{mediaEl.src=desc.url;}
  }).catch(function(e){
    var box2=buildErrorBox(kind,classifyMediaError(e));
    if(outerEl.parentNode)outerEl.parentNode.replaceChild(box2,outerEl);
  });
}
function swapRichMediaPlaceholder(el,kind,desc){
  var accessUrl=el.getAttribute('data-rnd206-access-url');
  var viewerIdxAttr=el.getAttribute('data-rnd206-viewer-idx');
  var replacement;
  if(kind==='video'){
    var video=document.createElement('video');
    video.className='media-video';
    video.controls=true;
    video.preload='metadata';
    video.src=desc.url;
    if(viewerIdxAttr!==null){
      var vwrap=document.createElement('div');
      vwrap.className='media-video-wrap';
      video.onerror=function(){handleRichMediaPlaybackFailure('video',accessUrl,video,vwrap);};
      var expandBtn=document.createElement('button');
      expandBtn.type='button';
      expandBtn.className='media-video-expand';
      expandBtn.setAttribute('aria-label',I18N.t('viewer.expand'));
      expandBtn.textContent='⤢';
      expandBtn.onclick=(function(idx){return function(){openViewer(timelineViewerItems,idx);};})(parseInt(viewerIdxAttr,10));
      vwrap.appendChild(video);
      vwrap.appendChild(expandBtn);
      replacement=vwrap;
    }else{
      video.onerror=function(){handleRichMediaPlaybackFailure('video',accessUrl,video,video);};
      replacement=video;
    }
  }else if(kind==='voice'){
    var audio=document.createElement('audio');
    audio.className='media-audio';
    audio.controls=true;
    audio.preload='metadata';
    audio.type=desc.content_type||'audio/mpeg';
    audio.src=desc.url;
    audio.onerror=function(){handleRichMediaPlaybackFailure('voice',accessUrl,audio,audio);};
    replacement=audio;
  }else if(kind==='file'){
    var link=document.createElement('a');
    link.className='file-card';
    link.href=desc.url;
    link.target='_blank';
    link.rel='noopener noreferrer';
    var icon=document.createElement('span');
    icon.className='file-card-icon';
    icon.setAttribute('aria-hidden','true');
    icon.textContent='📄';
    var info=document.createElement('span');
    info.className='file-card-info';
    var nameLine=document.createElement('div');
    nameLine.className='file-card-name';
    nameLine.textContent=fmtFileName(desc);
    var typeLine=document.createElement('div');
    typeLine.className='file-card-type';
    typeLine.textContent=desc.content_type||I18N.t('media.file');
    var metaLine=document.createElement('div');
    metaLine.className='file-card-meta';
    metaLine.textContent=fmtBytes(desc.size_bytes);
    info.appendChild(nameLine);
    info.appendChild(typeLine);
    info.appendChild(metaLine);
    var dl=document.createElement('span');
    dl.className='file-card-download';
    dl.textContent=I18N.t('file.download');
    link.appendChild(icon);
    link.appendChild(info);
    link.appendChild(dl);
    replacement=link;
  }else if(kind==='image'||kind==='emotion'){
    var img=document.createElement('img');
    img.className=kind==='emotion'?'emotion-preview':'media-preview';
    img.src=desc.url;
    img.alt=kind==='emotion'?I18N.t('emotion.alt'):I18N.t('media.image');
    img.loading='lazy';
    // RND-207: async decode keeps a large-ish thumbnail off the main thread;
    // the reserved w/h (from the placeholder, computed from intrinsic dims)
    // are carried onto the <img> so the swap-in causes no layout shift.
    img.decoding='async';
    var rw=el.getAttribute('data-rnd207-w'),rh=el.getAttribute('data-rnd207-h');
    if(rw&&rh){img.width=parseInt(rw,10);img.height=parseInt(rh,10);}
    if(viewerIdxAttr!==null){
      var btn=document.createElement('button');
      btn.type='button';
      btn.className='media-preview-btn';
      btn.setAttribute('aria-label',img.alt);
      img.onerror=function(){handleRichMediaPlaybackFailure(kind,accessUrl,img,btn);};
      btn.appendChild(img);
      btn.onclick=(function(idx){return function(){openViewer(timelineViewerItems,idx);};})(parseInt(viewerIdxAttr,10));
      replacement=btn;
    }else{
      img.onerror=function(){handleRichMediaPlaybackFailure(kind,accessUrl,img,img);};
      replacement=img;
    }
  }else{
    return;
  }
  if(el.parentNode)el.parentNode.replaceChild(replacement,el);
}
// One placeholder builder for every media kind (image/video/voice/file/
// emotion), top-level or nested -- the single "hydration contract": every
// consumer emits exactly this markup and nothing else, and hydrateRichMedia
// is the only code that ever resolves it into a live element.
function richMediaPlaceholder(kind,accessUrl,loadingKey,extraAttrs){
  var attrs='';
  if(extraAttrs){
    for(var k in extraAttrs){
      if(Object.prototype.hasOwnProperty.call(extraAttrs,k))attrs+=' '+k+'="'+esc(String(extraAttrs[k]))+'"';
    }
  }
  return '<div class="media-rich-loading" role="status" data-rnd206-kind="'+esc(kind)+'" data-rnd206-access-url="'+esc(accessUrl||'')+'"'+attrs+'>'
    +esc(I18N.t(loadingKey))+'</div>';
}
// Eagerly registers a Viewer slot at render time (not after the descriptor
// resolves) so top-level and nested image/emotion/video items behave
// identically and register in stable document order.
// opts (RND-207, optional): {thumbUrl,width,height} for image/emotion.
//  - The VIEWER always registers the ORIGINAL accessUrl (opened full-res).
//  - The LIST placeholder hydrates from thumbUrl when present (small
//    thumbnail), falling back to the original when there is no thumbnail.
//  - width/height (the original's intrinsic pixels) reserve an exact display
//    box on the placeholder so swapping the <img> in causes no layout shift.
function renderViewableMediaSlot(kind,accessUrl,label,loadingKey,opts){
  var idx=registerViewerItem({kind:kind==='emotion'?'image':kind,accessUrl:accessUrl,label:label});
  var extra={'data-rnd206-viewer-idx':idx};
  var hydrateUrl=accessUrl;
  if(opts){
    if(opts.thumbUrl)hydrateUrl=opts.thumbUrl;
    var w=parseInt(opts.width,10),h=parseInt(opts.height,10);
    if(w>0&&h>0){
      var cap=kind==='emotion'?150:280; // mirrors .emotion-preview / .media-preview CSS caps
      var scale=Math.min(cap/w,cap/h,1);
      var dw=Math.max(1,Math.round(w*scale)),dh=Math.max(1,Math.round(h*scale));
      extra['data-rnd207-w']=dw;extra['data-rnd207-h']=dh;
      extra['style']='width:'+dw+'px;height:'+dh+'px';
    }
  }
  return richMediaPlaceholder(kind,hydrateUrl,loadingKey,extra);
}
function renderVideoPreview(accessUrl){
  // RND-206 QA fix #7: video is registered with the same shared-viewer
  // dispatch as image/emotion (an explicit expand action alongside the
  // inline native-controls player -- see swapRichMediaPlaceholder's video
  // branch), not a separate independent video modal.
  return renderViewableMediaSlot('video',accessUrl,I18N.t('media.video'),'video.loading');
}
function renderVoicePreview(accessUrl){
  return richMediaPlaceholder('voice',accessUrl,'voice.loading');
}
function renderFilePreview(accessUrl){
  return richMediaPlaceholder('file',accessUrl,'console.loading');
}
function renderEmotionPreview(accessUrl,opts){
  return renderViewableMediaSlot('emotion',accessUrl,I18N.t('emotion.alt'),'viewer.loading',opts);
}
function renderNestedImageSlot(accessUrl,opts){
  return renderViewableMediaSlot('image',accessUrl,I18N.t('media.image'),'viewer.loading',opts);
}
// RND-207: pack the thumbnail/dimension hints a timeline message (m) or a
// nested media descriptor (media) carries into the opts renderViewableMediaSlot
// expects. Both shapes name the fields identically (thumbnail_access_url/
// image_width/image_height), so one helper serves both.
function thumbSlotOpts(src){
  return {thumbUrl:src&&src.thumbnail_access_url,width:src&&src.image_width,height:src&&src.image_height};
}
// RND-206 QA fix #2: registry-gated top-level dispatch for the three
// media-preview kinds whose element choice still needs a small kind->
// renderer map (same accepted pattern as STRUCTURED_CARD_RENDERERS) --
// used by renderMessageBody once app.message_type_registry reports
// renderer_strategy=="media_preview" for the message's type (see
// message_type_registry.py; the registry, not this list, is what marks a
// type as preview-capable).
var MEDIA_PREVIEW_KINDS={video:1,voice:1,file:1};
function renderMediaPreviewByKind(kind,accessUrl){
  if(kind==='video')return renderVideoPreview(accessUrl);
  if(kind==='voice')return renderVoicePreview(accessUrl);
  if(kind==='file')return renderFilePreview(accessUrl);
  return '';
}

// ---------------------------------------------------------------------------
// Composite (mixed/chatrecord) node rendering — RND-200's recursive
// structured_content.fields.items[...]/.children[...] shape, rendered
// in-order via the SAME renderer registry used for top-level messages
// (STRUCTURED_CARD_RENDERERS, renderVideoPreview/etc, nested media
// descriptors already enriched server-side to {status,media_type,
// mime_type,size_bytes,access_url} — see _enrich_nested_media_fields).
// Never displays raw JSON; an unrecognized child degrades to a safe,
// labeled fallback instead of breaking the whole message. Every node is
// rendered inside a try/catch (RND-206 QA fix #10) so one malformed child
// can never take down its siblings.
// ---------------------------------------------------------------------------
var COMPOSITE_MAX_DEPTH=8; // ONE documented maximum -- mirrors backend _MIXED_MAX_DEPTH, defense in depth only
var COMPOSITE_MEDIA_KINDS={image:1,video:1,voice:1,file:1,emotion:1};

function renderCompositeNodeMedia(node){
  var media=node.media;
  if(!media||typeof media!=='object')return '<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
  if(media.status!=='available'||!media.access_url){
    var label=MEDIA_LABELS[media.media_type]||I18N.t('media.generic');
    return renderGradedMediaPlaceholder(label,media.status);
  }
  var kind=node.type;
  if(kind==='image')return renderNestedImageSlot(media.access_url,thumbSlotOpts(media));
  if(kind==='emotion')return renderEmotionPreview(media.access_url,thumbSlotOpts(media));
  if(kind==='video')return renderVideoPreview(media.access_url);
  if(kind==='voice')return renderVoicePreview(media.access_url);
  if(kind==='file')return renderFilePreview(media.access_url);
  return '<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
}
function renderCompositeStructured(node){
  var fn=STRUCTURED_CARD_RENDERERS[node.type];
  var fake={structured_content:{fields:node.fields||null},normalized_type:node.type,display_label_key:'messageType.'+node.type};
  return fn?fn(fake):renderStructuredFallback(fake);
}
// Shared by BOTH the top-level chatrecord card (renderChatrecordMessage)
// and a chatrecord/mixed node nested inside another composite message
// (renderCompositeNode) -- ONE dispatch path, not two parallel
// implementations (RND-206 QA fix #2). data-depth carries the REAL
// recursion depth this card sits at so opening it in the Viewer resumes
// counting from there instead of silently restarting at 0 (QA fix #9). A
// semantic <button> (not a clickable <div>) so it is natively keyboard
// operable (QA fix #11) -- Enter/Space activation is free.
function renderChatrecordCard(node,depth,title,summaryText,count){
  return '<button type="button" class="chatrecord-card" data-depth="'+(depth||0)+'" data-node="'+esc(JSON.stringify(node))+'" '
    +'onclick="openChatrecordViewer(JSON.parse(this.getAttribute(&quot;data-node&quot;)),parseInt(this.getAttribute(&quot;data-depth&quot;),10))" '
    +'aria-haspopup="dialog">'
    +'<div class="chatrecord-card-title">'+esc(title)+'</div>'
    +(summaryText?'<div class="chatrecord-card-summary">'+esc(summaryText)+'</div>':'')
    +'<div class="chatrecord-card-count">'+esc(count+' '+I18N.t('chatrecord.itemsSuffix'))+'</div>'
    +'</button>';
}
function renderCompositeNode(node,depth){
  if(!node||typeof node!=='object')return '';
  if(depth>COMPOSITE_MAX_DEPTH)return '<div class="composite-unknown">'+esc(I18N.t('composite.depthLimitReached'))+'</div>';
  try{
    var metaBits=[];
    if(node.sender_name||node.sender)metaBits.push(esc(node.sender_name||node.sender));
    if(node.timestamp)metaBits.push(esc(fmtTime(node.timestamp)));
    var meta=metaBits.length?'<div class="composite-node-meta">'+metaBits.join(' · ')+'</div>':'';
    var body;
    if(node.type==='text'){
      body=node.text?'<div class="composite-node-text">'+esc(node.text)+'</div>'
        :'<div class="media-placeholder">'+esc(I18N.t('timeline.emptyText'))+'</div>';
    }else if(COMPOSITE_MEDIA_KINDS[node.type]){
      body=renderCompositeNodeMedia(node);
    }else if(STRUCTURED_CARD_RENDERERS[node.type]){
      body=renderCompositeStructured(node);
    }else if(node.type==='chatrecord'||node.type==='mixed'){
      body=renderChatrecordCard(node,depth,(node.fields&&node.fields.title)||I18N.t('chatrecord.title'),null,
        Array.isArray(node.children)?node.children.length:0);
    }else if(node.text){
      // Unknown/unsupported node type, but the parser still extracted a
      // best-effort text rendition (see structured_message_parser's
      // _extract_nested_text) -- show it rather than a bare "unsupported"
      // label, same spirit as the top-level unknown-type handling.
      body='<div class="composite-node-text">'+esc(node.text)+'</div>';
    }else{
      // Archive Console v2 (Message Types spec, mixed section): a segment
      // whose type is genuinely unsupported (not just "no media object at
      // all", handled above) must say so explicitly and note the original
      // type is preserved -- distinct copy from the generic
      // composite.unknownChild used by the no-media/empty-items cases.
      body='<div class="composite-unknown">'+esc(I18N.t('composite.unsupportedSegment'))+'</div>';
    }
    return '<div class="composite-node">'+meta+body+'</div>';
  }catch(e){
    // RND-206 QA fix #10: this node's renderer threw -- isolate the
    // failure to this one node; siblings (already rendered, or rendered
    // next in the same forEach loop) are unaffected. Never surfaces the
    // exception message/stack or any node content.
    return '<div class="composite-node"><div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div></div>';
  }
}
function renderCompositeChildren(node,depth){
  var children=node&&(node.children||(node.fields&&node.fields.items));
  if(!Array.isArray(children)||!children.length){
    return '<div class="composite-unknown">'+esc(I18N.t('chatrecord.empty'))+'</div>';
  }
  var html='';
  children.forEach(function(child){
    html+='<div class="v-chatrecord-node">'+renderCompositeNode(child,(depth||0)+1)+'</div>';
  });
  return html;
}
// mixed: rendered inline, in order, recursively -- reusing the exact same
// per-node renderer as chatrecord's viewer (renderCompositeNode). Each
// item is independently failure-isolated by renderCompositeNode itself, so
// this loop never needs its own try/catch.
function renderMixedMessage(m){
  var fields=m.structured_content&&m.structured_content.fields;
  var items=fields&&fields.items;
  if(!Array.isArray(items)||!items.length){
    return '<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
  }
  var html='<div class="composite-wrap">';
  items.forEach(function(item){html+=renderCompositeNode(item,1);});
  html+='</div>';
  return html;
}
// Archive Console v2 (Message Types spec, chatrecord section): top-level
// chatrecord messages have an in-place transcript card. Its initial,
// collapsed form deliberately contains summary rows only: no rich-media
// placeholder is emitted until the user expands it. Nested chatrecord/mixed
// nodes retain the overlay-only renderChatrecordCard() path above.
function chatrecordChildKindLabel(item){
  var t=item&&item.type;
  if(t==='text')return null;
  var entry=MessageTypeRegistry.resolve(t);
  if(entry&&entry.placeholderKey)return I18N.t(entry.placeholderKey);
  if(t&&entry)return I18N.t('messageType.'+t);
  return I18N.t('placeholder.unsupported');
}
function renderChatrecordRowPlaceholder(item){
  item=item||{};
  var sender=item.sender_name||item.sender||'';
  var time=fmtTime(item.timestamp);
  var kind=chatrecordChildKindLabel(item);
  var text=item.type==='text'&&item.text?String(item.text).slice(0,60):'';
  return '<div class="chatrecord-row"><div class="chatrecord-row-body">'
    +'<div class="chatrecord-row-meta">'
    +'<span class="chatrecord-row-sender">'+esc(sender)+'</span>'
    +(kind?'<span class="chatrecord-row-kind">['+esc(kind)+']</span>':'')
    +'<span class="chatrecord-row-time">'+esc(time)+'</span>'
    +'</div>'
    +(text?'<div class="chatrecord-row-text">'+esc(text)+'</div>':'')
    +'</div></div>';
}
function renderChatrecordMessage(m){
  var fields=m.structured_content&&m.structured_content.fields;
  var items=(fields&&fields.items)||[];
  var title=(fields&&fields.title)||I18N.t('chatrecord.title');
  var node={fields:fields,children:items};
  var rowsHtml=items.map(renderChatrecordRowPlaceholder).join('');
  var itemsData=esc(JSON.stringify(node));
  var toggleBtn='<button type="button" class="chatrecord-card-toggle" onclick="toggleChatrecordRows(&quot;'+esc(m.msgid)+'&quot;,this)">'+esc(I18N.t('chatrecord.expandAll'))+'</button>';
  return '<div class="chatrecord-card">'
    +'<div class="chatrecord-card-hd"><div class="chatrecord-card-icon"></div>'
    +'<span class="chatrecord-card-title">'+esc(title)+'</span>'
    +'<span class="chatrecord-card-count">'+esc(items.length+' '+I18N.t('chatrecord.itemsSuffix'))+'</span>'
    +toggleBtn+'</div>'
    +'<div class="chatrecord-card-rows" id="cr-rows-'+esc(m.msgid)+'" data-items="'+itemsData+'">'+rowsHtml+'</div>'
    +'<button type="button" class="chatrecord-viewer-link" data-node="'+esc(JSON.stringify(node))+'" '
    +'onclick="openChatrecordViewer(JSON.parse(this.getAttribute(&quot;data-node&quot;)),0)">'+esc(I18N.t('chatrecord.viewInViewer'))+'</button>'
    +'</div>';
}
function toggleChatrecordRows(msgid,btn){
  var el=document.getElementById('cr-rows-'+msgid);
  if(!el)return;
  var node;
  try{node=JSON.parse(el.getAttribute('data-items'));}catch(e){return;}
  var items=Array.isArray(node&&node.children)?node.children:[];
  var expanded=el.getAttribute('data-expanded')==='true';
  if(expanded){
    el.innerHTML=items.map(renderChatrecordRowPlaceholder).join('');
    el.removeAttribute('data-expanded');
    if(btn)btn.textContent=I18N.t('chatrecord.expandAll');
    return;
  }
  el.innerHTML=items.map(function(item){
    return '<div class="v-chatrecord-node">'+renderCompositeNode(item,1)+'</div>';
  }).join('');
  el.setAttribute('data-expanded','true');
  hydrateRichMedia(el);
  if(btn)btn.textContent=I18N.t('chatrecord.collapse');
}
function renderCompositeMessage(m){
  if(m.normalized_type==='chatrecord')return renderChatrecordMessage(m);
  if(m.normalized_type==='mixed')return renderMixedMessage(m);
  var typeEntry=MessageTypeRegistry.resolvePlaceholder(m.normalized_type)||MessageTypeRegistry.fallback;
  return '<div class="media-placeholder">'+I18N.t(typeEntry.placeholderKey)+'</div>';
}

function renderRevokePlaceholder(m){
  // RND-201: a standalone "revoke" event row that could not be linked to
  // its original (target not archived yet, or the event's own payload
  // was malformed) — the only content ever shown here is a stable,
  // i18n-driven status label; the original message's content is never
  // fabricated or guessed. A LINKED revoke event never reaches this
  // function — it is folded into the original message, which renders
  // through its own normal path below with an "already revoked" badge
  // instead (see renderTimeline's revokedBadge).
  var key='revoke.pending';
  if(m.revoke_association_status==='original_missing')key='revoke.originalMissing';
  else if(m.revoke_association_status==='malformed')key='revoke.malformed';
  // RND-206 QA fix #13: consumes the existing RND-201 revoked_at field
  // (already present on TimelineMessageOut for a standalone revoke-event
  // row -- see app.routers.conversations) when available; never fabricates
  // a time when it is absent, and never exposes revoke_event_msgid or any
  // other internal association id here.
  var timeLine=m.revoked_at?'<div class="revoke-time">'+esc(I18N.t('revoke.time'))+esc(fmtTime(m.revoked_at))+'</div>':'';
  return '<div class="media-placeholder">'+esc(I18N.t(key))+'</div>'+timeLine;
}
function renderMessageBody(m){
  if(m.revoke_association_status&&m.revoke_association_status!=='linked'){
    return renderRevokePlaceholder(m);
  }
  var mediaType=m.media_type||'text';
  if(mediaType==='text'){
    return m.content_text?esc(m.content_text):'<div class="media-placeholder">'+I18N.t('timeline.emptyText')+'</div>';
  }
  if(mediaType==='image'&&m.media_status==='available'&&m.media_access_url){
    // RND-206 QA fix (narrow remediation pass): top-level image now shares
    // the exact same hydration path as nested/video/voice/file/emotion
    // (renderViewableMediaSlot -> richMediaPlaceholder -> hydrateRichMedia
    // -> loadRichMedia -> fetchDescriptorWithRecovery -> MediaAccessCache
    // -> swapRichMediaPlaceholder) instead of the now-removed standalone
    // RND-187 loadMediaImage chain. No src/href is set here — the actual
    // URL (a short-lived Qiniu signed URL, or the local proxy path) is
    // only known once the descriptor is fetched, on demand, post-render.
    // Gated on media_access_url alone (no media_url fallback): the
    // backend always sets both together whenever media_status=="available"
    // (see app.routers.conversations.get_conversation_messages), the same
    // invariant video/voice/file already rely on.
    return renderViewableMediaSlot('image', m.media_access_url, I18N.t('media.image'), 'viewer.loading', thumbSlotOpts(m));
  }
  if(mediaType==='image'){
    return renderGradedMediaPlaceholder(MEDIA_LABELS.image||I18N.t('media.generic'),m.media_status);
  }
  // RND-206 QA fix #2: video/voice/file are gated by renderer_strategy==
  // "media_preview" -- the authoritative Message Type Registry's own
  // capability signal (message_type_registry.py), not a hardcoded
  // mediaType literal list maintained independently of it. media_status/
  // media_access_url remain the separate, per-message "is THIS message's
  // media actually available right now" check. Lazily hydrated the same
  // way image is (see hydrateRichMedia/loadRichMedia below); falls through
  // to the shared "known type, not available" placeholder when
  // media_status isn't "available" so a not-yet-downloaded / failed video
  // still shows a clear, type-specific status instead of a broken player.
  if(m.renderer_strategy==='media_preview'&&MEDIA_PREVIEW_KINDS[mediaType]){
    if(m.media_status==='available'&&m.media_access_url)return renderMediaPreviewByKind(mediaType,m.media_access_url);
    return renderGradedMediaPlaceholder(MEDIA_LABELS[mediaType]||I18N.t('media.generic'),m.media_status);
  }
  // RND-206 QA fix #2: emotion (sticker/GIF) preview, gated the same way --
  // renderer_strategy=="media_preview" is the registry's capability signal
  // for emotion too (message_type_registry.py). classify_media()
  // intentionally still reports media_type/media_status "unsupported" for
  // emotion (see app.media_classification), so this cannot be gated by
  // mediaType/media_status like video/voice/file above -- media_access_url
  // presence is the per-message availability signal instead. When no
  // access URL is available this falls through unchanged to the normal
  // registry-driven "unsupported" placeholder path below (same as every
  // other still-unsupported type), rather than a separate hand-written
  // fallback string.
  if(m.normalized_type==='emotion'&&m.renderer_strategy==='media_preview'&&m.media_access_url){
    return renderEmotionPreview(m.media_access_url,thumbSlotOpts(m));
  }
  if(mediaType==='unknown'){
    return '<div class="media-placeholder">'+I18N.t('media.unknownType')+'</div>';
  }
  // RND-206: mixed/chatrecord composite viewer.
  if(m.renderer_strategy==='composite_view'){
    return renderCompositeMessage(m);
  }
  if(m.renderer_strategy==='structured_card'){
    return renderStructuredCard(m);
  }
  // RND-198: system events rendered as centered non-bubble cards.
  if(m.renderer_strategy==='system_card'){
    return renderSystemCard(m);
  }
  // RND-197 fix: keyed by normalized_type, not raw msgtype —
  // MessageTypeRegistry.entries is keyed by normalized_type (e.g.
  // "miniprogram"), but msgtype is the raw wire value (e.g. "weapp").
  // Looking this up by m.msgtype silently missed every aliased type and
  // fell through to the generic fallback (see
  // test_message_type_registry_core.py's documented-divergence test, now
  // fixed). normalized_type is available on every message row, not just
  // structured ones, so this is safe unconditionally.
  //
  // RND-206 QA fix: this terminal fallback now reads MessageTypeRegistry.
  // resolve() (every registered entry) instead of the narrower
  // resolvePlaceholder() (only category=="placeholder" entries) so a
  // message that reaches here in an unexpected shape (e.g. a stale/missing
  // renderer_strategy on old cached data) still gets its own type's
  // specific label via .placeholderKey when the registry carries one
  // (video/voice/file/emotion still do, even though their normal dispatch
  // above never reaches this line) instead of collapsing into the generic
  // "unknown message type" text.
  var typeEntry=MessageTypeRegistry.resolve(m.normalized_type);
  var placeholderKey=(typeEntry&&typeEntry.placeholderKey)||MessageTypeRegistry.fallback.placeholderKey;
  return '<div class="media-placeholder">'+I18N.t(placeholderKey)+'</div>';
}
// RND-206 QA fix: per-message failure isolation -- a single message whose
// renderer throws (malformed structured_content, unexpected field shape,
// etc.) must never blank the rest of the timeline. Never logs message
// content/signed URLs, even in the caught-error path (dev diagnostics
// requirement) -- the error itself is discarded.
function safeRenderMessageBody(m){
  try{
    return renderMessageBody(m);
  }catch(e){
    return '<div class="media-placeholder">'+esc(I18N.t('render.messageFailed'))+'</div>';
  }
}
