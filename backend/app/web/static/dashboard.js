(function(){
  'use strict';

  var ROOT_ID='dashboard-root';
  var RANGE_DAYS=14;
  var requestInFlight=false;
  var lastData=null;
  var MAX_COUNT=1000000000000000;
  var COLORS=['var(--color-viz-1)','var(--color-viz-2)','var(--color-viz-3)','var(--color-viz-5)','var(--color-viz-6)','var(--color-info-fg)','var(--color-text-4)'];
  var FALLBACK={
    'dashboard.pageTitle':'会话存档总览','dashboard.description':'查看会话归档状态、数据规模、存储用量和近期变化。','dashboard.loading':'正在加载会话存档总览…','dashboard.failed':'总览数据加载失败，请稍后重试。','dashboard.retry':'重试','dashboard.archiveCoverage':'归档覆盖','dashboard.totalMessages':'累计消息','dashboard.storageUsage':'存储用量','dashboard.archiveMembers':'归档成员','dashboard.archiveStatus':'归档状态','dashboard.notStarted':'尚未开始归档','dashboard.firstArchived':'首次归档：{date}','dashboard.lastArchived':'最近归档：{date}','dashboard.storageUsed':'已使用 {value}','dashboard.memberCount':'当前归档范围内的内部成员','dashboard.status.normal':'正常','dashboard.status.processing':'处理中','dashboard.status.needsAttention':'需要关注','dashboard.status.configurationError':'配置异常','dashboard.status.notConfigured':'尚未配置','dashboard.status.notStarted':'尚未开始','dashboard.notice.notConfigured.title':'尚未完成企业微信会话存档配置','dashboard.notice.notConfigured.copy':'完成配置后，系统将在这里展示归档状态和数据。','dashboard.notice.configure':'前往配置','dashboard.notice.noData.title':'尚未收到会话存档数据','dashboard.notice.noData.copy':'归档开始后，数据规模和趋势将在这里展示。','dashboard.notice.processing.title':'正在处理首批会话数据','dashboard.notice.processing.copy':'页面会展示已经完成归档的数据；其余数据处理完成后会自动纳入统计。','dashboard.notice.attention.title':'归档状态需要关注','dashboard.notice.attention.copy':'系统检测到归档任务异常或缺少可确认的任务状态，请检查配置与归档健康。','dashboard.notice.configurationError.title':'企业微信会话存档配置异常','dashboard.notice.configurationError.copy':'当前配置未启用，归档任务无法正常运行。','dashboard.notice.viewDiagnostics':'查看归档健康','dashboard.trendTitle':'近 {days} 天归档量','dashboard.textStructured':'文本与结构化消息','dashboard.mediaMessages':'媒体（图片、文件、音视频）','dashboard.noData':'暂无可展示的数据','dashboard.capacityTitle':'容量与费用','dashboard.capacityUnavailable':'套餐容量和费用规则尚未配置；当前仅展示已使用的媒体文件容量。','dashboard.dataInsights':'数据洞察','dashboard.typeComposition':'消息类型构成','dashboard.storageComposition':'存储构成','dashboard.hourlyDistribution':'沟通活跃时段','dashboard.estimated':'估算','dashboard.type.text':'文本','dashboard.type.image':'图片','dashboard.type.voice':'语音','dashboard.type.video':'视频','dashboard.type.file':'文件','dashboard.type.structured':'结构化消息','dashboard.type.other':'其他','dashboard.storage.image':'图片','dashboard.storage.voice':'语音','dashboard.storage.video':'视频','dashboard.storage.file':'文件','dashboard.storage.textAndIndex':'文本与索引','dashboard.storage.other':'其他','dashboard.quickEntries':'快捷入口','dashboard.quick.review':'会话审阅','dashboard.quick.review.copy':'查看已归档会话','dashboard.quick.search':'全局搜索','dashboard.quick.search.copy':'检索会话存档','dashboard.quick.contacts':'外部联系人','dashboard.quick.contacts.copy':'查看关联沟通','dashboard.quick.media':'媒体附件','dashboard.quick.media.copy':'查看已下载媒体','dashboard.chartSummary':'共 {value} 条消息','dashboard.hour':'{hour}:00','dashboard.date':'日期','dashboard.messages':'消息数','dashboard.planExpiry':'年费套餐到期'
  };
  var TYPE_KEYS={text:'dashboard.type.text',image:'dashboard.type.image',voice:'dashboard.type.voice',video:'dashboard.type.video',file:'dashboard.type.file',structured:'dashboard.type.structured',other:'dashboard.type.other'};
  var STORAGE_KEYS={image:'dashboard.storage.image',voice:'dashboard.storage.voice',video:'dashboard.storage.video',file:'dashboard.storage.file',text_and_index_estimate:'dashboard.storage.textAndIndex',other:'dashboard.storage.other'};
  var STATUS_KEYS={normal:'dashboard.status.normal',processing:'dashboard.status.processing',needs_attention:'dashboard.status.needsAttention',configuration_error:'dashboard.status.configurationError',not_configured:'dashboard.status.notConfigured',not_started:'dashboard.status.notStarted'};

  function t(key, values){
    var value=I18N.t(key);
    if(value===key)value=FALLBACK[key]||key;
    Object.keys(values||{}).forEach(function(name){value=value.replace('{'+name+'}',String(values[name]));});
    return value;
  }
  function applyStaticI18n(){
    document.querySelectorAll('[data-i18n]').forEach(function(node){
      node.textContent=t(node.getAttribute('data-i18n'));
    });
  }
  function el(tag, className, value){var node=document.createElement(tag);if(className)node.className=className;if(value!==undefined)node.textContent=value;return node;}
  function append(parent, tag, className, value){var node=el(tag,className,value);parent.appendChild(node);return node;}
  function clear(node){node.replaceChildren();}
  function number(value){return typeof value==='number'&&isFinite(value)&&value>=0&&value<=MAX_COUNT&&Math.floor(value)===value?value:0;}
  function validDate(value){if(typeof value!=='string'||value.length>64)return null;var date=new Date(value);return isNaN(date.getTime())?null:date;}
  function formatDate(value){var date=validDate(value);if(!date)return null;try{return new Intl.DateTimeFormat(I18N.getLocale(),{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Shanghai'}).format(date);}catch(_error){return date.toISOString().replace('T',' ').replace('.000Z',' UTC');}}
  function formatDay(value){var date=validDate(value);if(!date)return '';try{return new Intl.DateTimeFormat(I18N.getLocale(),{month:'numeric',day:'numeric',timeZone:'Asia/Shanghai'}).format(date);}catch(_error){return value.slice(5);}}
  function formatNumber(value){return number(value).toLocaleString(I18N.getLocale());}
  function formatBytes(value){var bytes=number(value);var units=['B','KB','MB','GB','TB'];var unit=0;while(bytes>=1024&&unit<units.length-1){bytes/=1024;unit+=1;}var digits=unit===0?0:bytes>=100?0:bytes>=10?1:2;return bytes.toLocaleString(I18N.getLocale(),{maximumFractionDigits:digits})+' '+units[unit];}
  function setSvg(node, name, value){node.setAttribute(name,String(value));}
  function svgElement(tag, className){var node=document.createElementNS('http://www.w3.org/2000/svg',tag);if(className)node.setAttribute('class',className);return node;}
  function svgText(svg, x, y, value, className, anchor){var node=svgElement('text',className);setSvg(node,'x',x);setSvg(node,'y',y);if(anchor)setSvg(node,'text-anchor',anchor);node.textContent=value;svg.appendChild(node);return node;}
  function addSvgTitle(parent, value){var title=svgElement('title');title.textContent=value;parent.appendChild(title);}
  function badge(status){var className=status==='normal'?'badge-success':status==='processing'?'badge-info':status==='not_started'||status==='not_configured'?'badge-silent':'badge-pending';return el('span','badge badge-dot '+className,t(STATUS_KEYS[status]||'dashboard.status.needsAttention'));}
  function empty(parent, message){append(parent,'div','dashboard-empty',message||t('dashboard.noData'));}
  function retry(parent){var button=append(parent,'button','btn btn-sm',t('dashboard.retry'));button.type='button';button.addEventListener('click',load);return button;}
  function moduleError(parent){var box=append(parent,'div','dashboard-module-error');append(box,'span','',t('dashboard.failed'));retry(box);}

  function normalise(data){
    data=data&&typeof data==='object'?data:{};
    var allowedStatus=Object.prototype.hasOwnProperty.call(STATUS_KEYS,data.archive_status)?data.archive_status:'needs_attention';
    var series=Array.isArray(data.daily_series)?data.daily_series.map(function(item){return {date:typeof item.date==='string'?item.date:'',text_count:number(item.text_count),media_count:number(item.media_count)};}).filter(function(item){return validDate(item.date);}):null;
    var types=Array.isArray(data.type_composition)?data.type_composition.map(function(item){return {category:typeof item.category==='string'&&TYPE_KEYS[item.category]?item.category:'other',count:number(item.count)};}):null;
    var storage=Array.isArray(data.storage_composition)?data.storage_composition.map(function(item){return {category:typeof item.category==='string'&&STORAGE_KEYS[item.category]?item.category:'other',bytes:number(item.bytes),estimated:item.estimated===true};}):null;
    var hourly=Array.isArray(data.hourly_distribution)&&data.hourly_distribution.length===24?data.hourly_distribution.map(function(item,index){return {hour:typeof item.hour==='number'&&item.hour===index?index:index,count:number(item.count)};}):null;
    var insightErrors=data.insight_errors&&typeof data.insight_errors==='object'?data.insight_errors:{};
    return {range_days:[14,30,90].indexOf(data.range_days)>=0?data.range_days:14,total_archived_messages:number(data.total_archived_messages),archive_coverage_days:number(data.archive_coverage_days),first_archived_at:validDate(data.first_archived_at)?data.first_archived_at:null,last_archived_at:validDate(data.last_archived_at)?data.last_archived_at:null,annual_plan_expires_at:validDate(data.annual_plan_expires_at)?data.annual_plan_expires_at:null,storage_bytes:number(data.storage_bytes),staff_count:number(data.staff_count),archive_status:allowedStatus,archive_configured:data.archive_configured===true,can_manage_settings:data.can_manage_settings===true,daily_series:series,type_composition:types,storage_composition:storage,hourly_distribution:hourly,insight_errors:{type_composition:insightErrors.type_composition==='unavailable',storage_composition:insightErrors.storage_composition==='unavailable',hourly_distribution:insightErrors.hourly_distribution==='unavailable'}};
  }

  function appendNotice(root, data){
    var state=data.archive_status;
    var title=null,copy=null,style='alert-neutral',action=null;
    if(state==='not_configured'){title='dashboard.notice.notConfigured.title';copy='dashboard.notice.notConfigured.copy';style='alert-warning';action=data.can_manage_settings?{href:'/admin/settings',key:'dashboard.notice.configure'}:null;}
    else if(state==='configuration_error'){title='dashboard.notice.configurationError.title';copy='dashboard.notice.configurationError.copy';style='alert-danger';action=data.can_manage_settings?{href:'/admin/settings',key:'dashboard.notice.configure'}:null;}
    else if(state==='processing'){title='dashboard.notice.processing.title';copy='dashboard.notice.processing.copy';style='alert';}
    else if(state==='needs_attention'){title='dashboard.notice.attention.title';copy='dashboard.notice.attention.copy';style='alert-warning';action={href:'/admin/diagnostics/reachability',key:'dashboard.notice.viewDiagnostics'};}
    else if(data.total_archived_messages===0){title='dashboard.notice.noData.title';copy='dashboard.notice.noData.copy';style='alert-neutral';}
    if(!title)return;
    var notice=append(root,'section','alert dashboard-notice '+style);notice.setAttribute('role','status');append(notice,'span','alert-ico',state==='configuration_error'||state==='needs_attention'?'!':state==='processing'?'…':'○').setAttribute('aria-hidden','true');
    var content=append(notice,'div');append(content,'strong','',t(title));append(content,'div','',t(copy));
    if(action){var right=append(notice,'div','right');var link=append(right,'a','btn btn-sm',t(action.key));link.href=action.href;}
  }

  function metric(parent, label, value, sub, status){
    var card=append(parent,'section','stat dashboard-metric');append(card,'span','stat-k',label);
    if(status){var statusValue=append(card,'div','dashboard-status-value');statusValue.appendChild(badge(status));}else append(card,'span','stat-v tnum',value);
    append(card,'span','stat-sub',sub||'');
  }
  function renderMetrics(root, data){
    var grid=append(root,'section','dashboard-metrics');
    var coverage=data.total_archived_messages?formatNumber(data.archive_coverage_days)+' '+(I18N.getLocale()==='en'?'days':'天'):t('dashboard.notStarted');
    var first=data.first_archived_at?formatDate(data.first_archived_at):'';
    var last=data.last_archived_at?formatDate(data.last_archived_at):'';
    metric(grid,t('dashboard.archiveCoverage'),coverage,first?t('dashboard.firstArchived',{date:first}):'');
    metric(grid,t('dashboard.totalMessages'),formatNumber(data.total_archived_messages),last?t('dashboard.lastArchived',{date:last}):'');
    metric(grid,t('dashboard.storageUsage'),formatBytes(data.storage_bytes),t('dashboard.storageUsed',{value:formatBytes(data.storage_bytes)}));
    metric(grid,t('dashboard.archiveMembers'),formatNumber(data.staff_count),t('dashboard.memberCount'));
    // Annual-plan expiry card: rendered only for annual-billing subscriptions,
    // so a self-deployed instance (no subscription) keeps the 5-card layout.
    if(data.annual_plan_expires_at){
      grid.classList.add('has-plan');
      metric(grid,t('dashboard.planExpiry'),formatDate(data.annual_plan_expires_at),'');
    }
    metric(grid,t('dashboard.archiveStatus'),'','',data.archive_status);
  }

  function renderTrend(parent, data){
    if(!data.daily_series){moduleError(parent);return;}
    var series=data.daily_series;if(!series.length||series.every(function(item){return item.text_count+item.media_count===0;})){empty(parent);return;}
    var summary=series.reduce(function(total,item){return total+item.text_count+item.media_count;},0);
    var wrap=append(parent,'div','dashboard-chart-wrap');var accessible=append(wrap,'p','dashboard-sr-only',t('dashboard.chartSummary',{value:formatNumber(summary)}));accessible.id='dashboard-trend-summary';
    var svg=svgElement('svg','dashboard-chart');setSvg(svg,'viewBox','0 0 720 220');setSvg(svg,'preserveAspectRatio','none');setSvg(svg,'role','img');setSvg(svg,'aria-labelledby','dashboard-trend-summary');
    var W=720,H=220,L=40,R=8,T=10,B=32,plotH=H-T-B,max=Math.max.apply(null,series.map(function(item){return item.text_count+item.media_count;}))||1;
    [0,.5,1].forEach(function(ratio){var y=T+plotH*(1-ratio);var line=svgElement('line','grid');setSvg(line,'x1',L);setSvg(line,'x2',W-R);setSvg(line,'y1',y);setSvg(line,'y2',y);svg.appendChild(line);svgText(svg,L-6,y+3,formatNumber(Math.round(max*ratio)),'axis','end');});
    var step=(W-L-R)/series.length,bw=Math.max(2,step*.62);series.forEach(function(item,index){var total=item.text_count+item.media_count;var totalH=total/max*plotH,mediaH=item.media_count/max*plotH,x=L+index*step+(step-bw)/2;var group=svgElement('g','dashboard-chart-bar');var base=svgElement('rect');setSvg(base,'x',x);setSvg(base,'y',H-B-totalH);setSvg(base,'width',bw);setSvg(base,'height',Math.max(0,totalH-mediaH));setSvg(base,'rx',2);setSvg(base,'fill','var(--color-viz-1)');group.appendChild(base);var media=svgElement('rect');setSvg(media,'x',x);setSvg(media,'y',H-B-mediaH);setSvg(media,'width',bw);setSvg(media,'height',mediaH);setSvg(media,'rx',2);setSvg(media,'fill','var(--color-viz-3)');group.appendChild(media);addSvgTitle(group,item.date+' · '+t('dashboard.messages')+' '+formatNumber(total));svg.appendChild(group);if(index===0||index===series.length-1||index===Math.floor(series.length/2))svgText(svg,x+bw/2,H-12,formatDay(item.date),'axis','middle');});
    var baseline=svgElement('line','baseline');setSvg(baseline,'x1',L);setSvg(baseline,'x2',W-R);setSvg(baseline,'y1',H-B);setSvg(baseline,'y2',H-B);svg.appendChild(baseline);wrap.appendChild(svg);
    var legend=append(parent,'div','chart-legend');legendItem(legend,'var(--color-viz-1)',t('dashboard.textStructured'));legendItem(legend,'var(--color-viz-3)',t('dashboard.mediaMessages'));
  }
  function legendItem(parent,color,label){var item=append(parent,'span');var icon=append(item,'i');icon.style.background=color;append(item,'span','',label);}
  function renderCapacity(parent, data){
    var value=append(parent,'div','dashboard-capacity-value',formatBytes(data.storage_bytes));value.setAttribute('aria-label',t('dashboard.storageUsed',{value:formatBytes(data.storage_bytes)}));append(parent,'p','dashboard-capacity-meta',t('dashboard.storageUsed',{value:formatBytes(data.storage_bytes)}));
    append(parent,'div','dashboard-capacity-unavailable',t('dashboard.capacityUnavailable'));
  }

  function renderDonut(parent, data){
    if(data.insight_errors.type_composition||!data.type_composition){moduleError(parent);return;}
    var items=data.type_composition.filter(function(item){return item.count>0;});if(!items.length){empty(parent);return;}
    var total=items.reduce(function(sum,item){return sum+item.count;},0),layout=append(parent,'div','dashboard-donut-layout'),svg=svgElement('svg','dashboard-donut');setSvg(svg,'viewBox','0 0 180 180');setSvg(svg,'role','img');setSvg(svg,'aria-label',t('dashboard.typeComposition')+' · '+t('dashboard.chartSummary',{value:formatNumber(total)}));var offset=0;
    items.forEach(function(item,index){var portion=item.count/total,circle=svgElement('circle');setSvg(circle,'cx',90);setSvg(circle,'cy',90);setSvg(circle,'r',53);setSvg(circle,'fill','none');setSvg(circle,'stroke',COLORS[index%COLORS.length]);setSvg(circle,'stroke-width',22);setSvg(circle,'stroke-dasharray',(portion*333.01)+' '+(333.01-portion*333.01));setSvg(circle,'stroke-dashoffset',-offset);setSvg(circle,'transform','rotate(-90 90 90)');addSvgTitle(circle,t(TYPE_KEYS[item.category])+': '+formatNumber(item.count));svg.appendChild(circle);offset+=portion*333.01;});
    svgText(svg,90,87,formatNumber(total),'dashboard-donut-total','middle');svgText(svg,90,105,t('dashboard.messages'),'dashboard-donut-caption','middle');layout.appendChild(svg);var legend=append(layout,'div','dashboard-legend');items.forEach(function(item,index){var row=append(legend,'div','dashboard-legend-row');var swatch=append(row,'span','dashboard-swatch');swatch.style.background=COLORS[index%COLORS.length];append(row,'b','',t(TYPE_KEYS[item.category]));append(row,'span','',formatNumber(item.count)+' · '+Math.round(item.count/total*100)+'%');});
  }
  function renderStorage(parent, data){
    if(data.insight_errors.storage_composition||!data.storage_composition){moduleError(parent);return;}
    var items=data.storage_composition.filter(function(item){return item.bytes>0;});if(!items.length){empty(parent);return;}
    var total=items.reduce(function(sum,item){return sum+item.bytes;},0);var list=append(parent,'div','dashboard-storage-list');items.forEach(function(item,index){var row=append(list,'div','dashboard-storage-row');var swatch=append(row,'span','dashboard-swatch');swatch.style.background=COLORS[index%COLORS.length];append(row,'b','',t(STORAGE_KEYS[item.category]));append(row,'span','',formatBytes(item.bytes)+' · '+Math.round(item.bytes/total*100)+'%');});
    var estimate=items.some(function(item){return item.estimated;});if(estimate)append(parent,'p','dashboard-storage-note',t('dashboard.estimated')+'：'+t('dashboard.storage.textAndIndex'));
  }
  function renderHourly(parent, data){
    if(data.insight_errors.hourly_distribution||!data.hourly_distribution){moduleError(parent);return;}
    var items=data.hourly_distribution;if(!items.some(function(item){return item.count>0;})){empty(parent);return;}
    var summary=items.reduce(function(sum,item){return sum+item.count;},0),svg=svgElement('svg','dashboard-hourly');setSvg(svg,'viewBox','0 0 480 210');setSvg(svg,'preserveAspectRatio','none');setSvg(svg,'role','img');setSvg(svg,'aria-label',t('dashboard.hourlyDistribution')+' · '+t('dashboard.chartSummary',{value:formatNumber(summary)}));var W=480,H=210,L=30,R=6,T=10,B=30,plotH=H-T-B,max=Math.max.apply(null,items.map(function(item){return item.count;}))||1;
    [0,.5,1].forEach(function(ratio){var y=T+plotH*(1-ratio),line=svgElement('line','grid');setSvg(line,'x1',L);setSvg(line,'x2',W-R);setSvg(line,'y1',y);setSvg(line,'y2',y);svg.appendChild(line);});
    var step=(W-L-R)/24,bw=Math.max(2,step*.7);items.forEach(function(item,index){var h=item.count/max*plotH,x=L+index*step+(step-bw)/2,bar=svgElement('rect','dashboard-chart-bar');setSvg(bar,'x',x);setSvg(bar,'y',H-B-h);setSvg(bar,'width',bw);setSvg(bar,'height',h);setSvg(bar,'rx',1.5);setSvg(bar,'fill','var(--color-viz-2)');addSvgTitle(bar,t('dashboard.hour',{hour:String(item.hour).padStart(2,'0')})+' · '+formatNumber(item.count));svg.appendChild(bar);if(index%4===0)svgText(svg,x+bw/2,H-11,String(item.hour).padStart(2,'0'),'axis','middle');});var baseline=svgElement('line','baseline');setSvg(baseline,'x1',L);setSvg(baseline,'x2',W-R);setSvg(baseline,'y1',H-B);setSvg(baseline,'y2',H-B);svg.appendChild(baseline);parent.appendChild(svg);
  }
  function insightCard(parent, title, renderer, data){var card=append(parent,'section','card dashboard-insight-card');var header=append(card,'div','card-hd');append(header,'h2','',title);var body=append(card,'div','card-bd');renderer(body,data);}
  function renderQuick(root){var section=append(root,'section','dashboard-quick-section');var header=append(section,'div','section-hd');append(header,'h2','',t('dashboard.quickEntries'));var grid=append(section,'div','dashboard-quick');[{href:'/admin/conversations',icon:'阅',title:'dashboard.quick.review',copy:'dashboard.quick.review.copy'},{href:'/admin/search',icon:'搜',title:'dashboard.quick.search',copy:'dashboard.quick.search.copy'},{href:'/admin/contacts',icon:'外',title:'dashboard.quick.contacts',copy:'dashboard.quick.contacts.copy'},{href:'/admin/media',icon:'媒',title:'dashboard.quick.media',copy:'dashboard.quick.media.copy'}].forEach(function(item){var link=append(grid,'a');link.href=item.href;append(link,'span','dashboard-quick-icon',item.icon).setAttribute('aria-hidden','true');var copy=append(link,'span','dashboard-quick-copy');append(copy,'strong','',t(item.title));append(copy,'span','',t(item.copy));});}

  function render(data){
    lastData=data;RANGE_DAYS=data.range_days;document.title=t('dashboard.pageTitle');var root=document.getElementById(ROOT_ID);if(!root)return;clear(root);root.setAttribute('aria-busy','false');appendNotice(root,data);renderMetrics(root,data);
    var main=append(root,'section','dashboard-main-grid');var trendCard=append(main,'section','card dashboard-card');var trendHeader=append(trendCard,'div','card-hd');append(trendHeader,'h2','',t('dashboard.trendTitle',{days:data.range_days}));var right=append(trendHeader,'div','right');var selector=append(right,'div','segmented');[14,30,90].forEach(function(days){var button=append(selector,'button',days===data.range_days?'active':'',String(days));button.type='button';button.setAttribute('aria-pressed',days===data.range_days?'true':'false');button.addEventListener('click',function(){if(days!==RANGE_DAYS){RANGE_DAYS=days;load();}});});var trendBody=append(trendCard,'div','card-bd');renderTrend(trendBody,data);
    // Haisu request: 沟通活跃时段 swaps places with 容量与费用 — the hourly
    // chart now fills the side column beside the trend, and the capacity
    // card closes the 数据洞察 row.
    var hourlyCard=append(main,'section','card dashboard-card');var hourlyHeader=append(hourlyCard,'div','card-hd');append(hourlyHeader,'h2','',t('dashboard.hourlyDistribution'));var hourlyBody=append(hourlyCard,'div','card-bd');renderHourly(hourlyBody,data);
    var insightsHeader=append(root,'div','dashboard-insights-hd');insightsHeader.id='data-insights';append(insightsHeader,'h2','',t('dashboard.dataInsights'));var insights=append(root,'section','dashboard-insights');insightCard(insights,t('dashboard.typeComposition'),renderDonut,data);insightCard(insights,t('dashboard.storageComposition'),renderStorage,data);insightCard(insights,t('dashboard.capacityTitle'),renderCapacity,data);renderQuick(root);
  }
  function renderFailure(){var root=document.getElementById(ROOT_ID);if(!root)return;clear(root);root.setAttribute('aria-busy','false');var box=append(root,'section','dashboard-module-error');append(box,'p','',t('dashboard.failed'));retry(box);}
  function load(){if(requestInFlight)return;requestInFlight=true;var root=document.getElementById(ROOT_ID);if(root){root.setAttribute('aria-busy','true');if(!lastData){clear(root);append(root,'p','dashboard-loading',t('dashboard.loading'));}}
    fetch('/api/admin/dashboard?range='+encodeURIComponent(String(RANGE_DAYS)),{credentials:'include'}).then(function(response){if(response.status===401){window.location='/admin/login';return null;}if(!response.ok)throw new Error('dashboard_load_failed');return response.json();}).then(function(data){if(data)render(normalise(data));}).catch(function(){renderFailure();}).finally(function(){requestInFlight=false;});
  }
  I18N.onChange(function(){applyStaticI18n();if(lastData)render(lastData);});applyStaticI18n();load();
}());
