'use strict';
const $ = id => document.getElementById(id);
const labels = {text:'消息',confirmation_request:'请求确认',confirmation_answer:'用户确认',tool_call:'工具调用',tool_result:'工具结果',context:'上下文',context_summary:'压缩摘要',compaction_request:'压缩请求',call_error:'调用失败',attachment:'附件',subagent_context:'子任务过程'};
const warningLabels = {compacted_context:'包含上下文压缩，已保留可用历史',missing_pre_compaction_history:'压缩前的原文缺失',missing_assistant_history:'来源未包含助手历史正文，当前对话不完整',unlinked_snapshot:'部分记录无法确定连续关系，已保留原有顺序和来源',sequence_divergence:'存在不同的上下文分支，请结合消息来源核对',multiple_aliases:'同一会话包含多个身份',unrecognized_input_schema:'部分输入结构未识别，已保留为上下文记录',unresolved_confirmation:'部分确认工具执行失败，尚不能确认用户是否作答',unlinked_subagent_context:'部分子任务缺少父调用记录，已单独保留'};
const stageLabels = {clock_ms:'来源时钟',discovery_ms:'发现会话',lookup_ms:'定位历史记录',history_ms:'读取完整历史',parse_ms:'解析响应',ordering_ms:'排列消息顺序',reconstruct_ms:'还原对话',persist_ms:'保存会话',checkpoint_ms:'读取进度',run_log_ms:'创建采集记录',total_ms:'总耗时'};
const MESSAGE_SIZE=40;
let rows=[], current=null, state={}, listKey='', detailKey='', listController, detailController, runsController;
let listVersion=0, detailVersion=0, runsVersion=0, debounce, toastTimer, composing=false, lastTrigger=null, runsLoaded=false, listScroll=0;
const cache=new Map();
function node(tag,cls,value){const n=document.createElement(tag);if(cls)n.className=cls;if(value!==undefined)n.textContent=value;return n;}
function button(text,action,cls=''){const b=node('button',cls,text);b.type='button';b.onclick=action;return b;}
function date(ms){return new Date(Number(ms)).toLocaleString('zh-CN',{hour12:false});}
function positive(value,fallback=1){const n=Number(value);return Number.isSafeInteger(n)&&n>0?n:fallback;}
function readState(){
  const raw=location.hash.slice(1);
  if(raw&&!raw.includes('=')){let trace=raw;try{trace=decodeURIComponent(raw);}catch{}return {trace,page:1,size:20,mp:1,order:'newest'};}
  const s=Object.fromEntries(new URLSearchParams(raw));
  s.page=positive(s.page);s.size=[20,50,100].includes(Number(s.size))?Number(s.size):20;s.mp=positive(s.mp);
  s.order=s.order==='oldest'?'oldest':'newest';return s;
}
function url(s){const p=new URLSearchParams();for(const [k,v]of Object.entries(s))if(v&&!(k==='page'&&v===1)&&!(k==='size'&&v===20)&&!(k==='mp'&&v===1)&&!(k==='order'&&v==='newest'))p.set(k,v);return '#'+p;}
function navigate(patch,replace=false){clearTimeout(debounce);const next={...state,...patch};history[replace?'replaceState':'pushState'](null,'',url(next));route();}
function listParams(s){return {q:s.q||'',alias:s.alias||'',start:s.start||'',end:s.end||'',warning:s.warning||'',order:s.order,page:s.page,size:s.size};}
function localDate(value,next=false){if(!value)return undefined;const match=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value);if(!match)throw Error('请选择有效日期');const d=new Date(Number(match[1]),Number(match[2])-1,Number(match[3]));if(d.getFullYear()!==Number(match[1])||d.getMonth()!==Number(match[2])-1||d.getDate()!==Number(match[3]))throw Error('请选择有效日期');if(next)d.setDate(d.getDate()+1);return d.getTime();}
async function api(path,params={},signal){
  const controller=new AbortController(),cancel=()=>controller.abort();signal?.addEventListener('abort',cancel,{once:true});if(signal?.aborted)controller.abort();
  const timer=setTimeout(cancel,30000);
  try{const r=await fetch(path+'?'+new URLSearchParams(params),{signal:controller.signal});const data=await r.json();if(!r.ok)throw Error(data.error||'读取失败，请重试');return data;}
  catch(e){if(e.name==='AbortError'&&!signal?.aborted)throw Error('读取超时，请重试');throw e;}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',cancel);}
}
function emptyState(host,title,description,action,label='重试'){
  const box=node('div','empty-state');box.append(node('span','empty-symbol','⌕'),node('h2','',title),node('p','',description));if(action)box.append(button(label,action));host.replaceChildren(box);
}
function skeleton(host,n=5){host.replaceChildren(...Array.from({length:n},()=>{const x=node('div','skeleton');x.setAttribute('aria-hidden','true');return x;}));}
function syncFilters(){
  for(const key of ['q','alias','start','end','order','size']){const el=$(key==='q'?'query':key);if(document.activeElement!==el)el.value=state[key]||'';}
  // A shared link can reference an identity not yet present in the options.
  if(state.alias&&!Array.from($('alias').options).some(o=>o.value===state.alias))$('alias').add(new Option(state.alias,state.alias));
  $('alias').value=state.alias||'';$('warning').checked=state.warning==='1';
  const active=[['q','搜索'],['alias','身份'],['start','从'],['end','至'],['warning','还原提示']].filter(([k])=>state[k]);
  $('active-filters').replaceChildren();$('active-filters').hidden=!active.length;
  for(const [k,label]of active){const b=button('',()=>applyFilters({[k]:null}),'chip');b.append(node('span','',k==='warning'?'含还原提示':`${label}：${state[k]}`),node('span','','×'));b.setAttribute('aria-label',`移除${label}筛选`);$('active-filters').append(b);}
  if(active.length)$('active-filters').append(button('清空筛选',()=>applyFilters({q:null,alias:null,start:null,end:null,warning:null}),'quiet chip'));
  const n=['start','end','warning'].filter(k=>state[k]).length;$('filter-count').textContent=n?`(${n})`:'';
  if(n){$('advanced').hidden=false;$('advanced-toggle').setAttribute('aria-expanded','true');}
}
function applyFilters(patch,replace=false){$('filter-error').hidden=true;navigate({...patch,page:1,trace:null,mode:null,mp:1},replace);}
function submitFilters(){
  try{const start=$('start').value,end=$('end').value;if(start&&end&&localDate(start)>localDate(end))throw Error('结束日期不能早于起始日期');$('filter-error').hidden=true;applyFilters({q:$('query').value.trim(),alias:$('alias').value,start,end,warning:$('warning').checked?'1':null});}
  catch(e){$('filter-error').hidden=false;$('filter-error').textContent=e.message;}
}
function route(){
  const previous=state;state=readState();if((!previous.trace&&state.trace)||(previous.view!=='runs'&&state.view==='runs'))listScroll=window.scrollY;const isRuns=state.view==='runs',full=!!state.trace&&state.mode==='full';
  $('list-view').hidden=isRuns||full;$('runs-view').hidden=!isRuns;$('reader-view').hidden=!full||isRuns;
  $('nav-sessions').setAttribute('aria-current',!isRuns?'page':'false');$('nav-runs').setAttribute('aria-current',isRuns?'page':'false');
  $('nav-sessions').href=url({...state,view:null,trace:null,mode:null,mp:1});$('nav-runs').href=url({...state,view:'runs',trace:null,mode:null,mp:1});$('runs-return').href=$('nav-sessions').href;
  if(previous.view!==state.view){if(isRuns)window.scrollTo({top:0});else requestAnimationFrame(()=>window.scrollTo({top:listScroll}));}
  if(previous.mode==='full'&&!full)requestAnimationFrame(()=>window.scrollTo({top:listScroll}));
  if(isRuns){document.title='采集记录 · ScanIntent';closeDialog();detailController?.abort();detailVersion++;if(!runsLoaded)loadRuns();return;}
  syncFilters();const key=JSON.stringify(listParams(state));if(key!==listKey){listKey=key;loadList();}
  if(!state.trace){detailController?.abort();detailVersion++;detailKey='';current=null;closeDialog();$('conversation').hidden=true;renderSelection();document.title='ScanIntent · 会话记录';if(previous.mode==='full')requestAnimationFrame(()=>window.scrollTo({top:listScroll}));return;}
  const wasOpen=$('preview').open;const article=$('conversation');article.hidden=false;
  if(full){closeDialog();$('reader-view').append(article);$('close-detail').textContent='← 返回会话列表';$('expand-detail').hidden=true;}
  else{$('preview-host').append(article);$('expand-detail').hidden=false;$('close-detail').replaceChildren(document.createTextNode('关闭预览 '),node('kbd','','Esc'));if(!wasOpen){$('preview').showModal();document.body.style.overflow='hidden';}}
  const keyDetail=[state.trace,state.internal||'',state.mp].join('|');
  if(keyDetail!==detailKey){detailKey=keyDetail;loadDetail(state.trace);}

  if(previous.mode!==state.mode&&current){scrollDetail();$('title').focus({preventScroll:true});}
  renderSelection();
}
function closeDialog(){if($('preview').open)$('preview').close();document.body.style.overflow='';}
function closeDetail(){navigate({trace:null,mode:null,mp:1});(lastTrigger?.isConnected?lastTrigger:$('main')).focus({preventScroll:true});}
async function loadList(){
  listController?.abort();const controller=listController=new AbortController(),ticket=++listVersion;
  $('list-status').textContent='正在读取会话…';$('results').setAttribute('aria-busy','true');$('pagination').hidden=true;$('quick-pagination').replaceChildren();skeleton($('sessions'));
  try{
    const params=listParams(state);if(params.start)params.start=localDate(params.start);else delete params.start;if(params.end)params.end=localDate(params.end,true);else delete params.end;
    const data=await api('/api/conversations',params,controller.signal);if(ticket!==listVersion)return;
    if(data.page!==state.page){state.page=data.page;history.replaceState(null,'',url(state));listKey=JSON.stringify(listParams(state));}
    rows=data.conversations;$('total').textContent=data.total.toLocaleString();$('list-status').textContent=data.total?`共 ${data.total.toLocaleString()} 条 · 显示 ${(data.page-1)*data.size+1}–${(data.page-1)*data.size+rows.length} 条`:'没有匹配的会话';
    renderList(data);renderPagination(data);
  }catch(e){if(e.name!=='AbortError'&&ticket===listVersion){$('list-status').textContent='读取失败';$('total').textContent='—';emptyState($('sessions'),'会话暂时未能加载',e.message,loadList);}}
  finally{if(ticket===listVersion)$('results').setAttribute('aria-busy','false');}
}
function renderList(data){
  const host=$('sessions');host.replaceChildren();if(!rows.length){const filtered=['q','alias','start','end','warning'].some(k=>state[k]);emptyState(host,filtered?'没有找到匹配的会话':'还没有会话记录',filtered?'试试缩短关键词，或清空筛选条件。':'完成一次采集后，在这里刷新查看。',filtered?()=>applyFilters({q:null,alias:null,start:null,end:null,warning:null}):loadList,filtered?'清空筛选':'刷新记录');return;}
  for(const row of rows){const b=node('button','session');b.type='button';b.dataset.trace=row.trace_id;
    const main=node('span','session-main'),title=node('strong','session-title',row.title||'未命名会话');title.title=row.title||'';main.append(title,node('span','alias',(row.aliases||[]).join(' · ')||'身份未提供'));
    const status=node('span','session-status');status.append(node('span',(row.warnings||[]).length?'badge warning':'badge',(row.warnings||[]).length?`${row.warnings.length} 项提示`:'无提示'));
    b.append(main,node('span','session-date',date(row.last_ms)),node('span','session-count',`${row.message_count} 条`),status,node('span','row-arrow','›'));
    b.onclick=()=>{lastTrigger=b;navigate({trace:row.trace_id,mode:null,mp:1,internal:null});};host.append(b);
  }renderSelection();
}
function renderSelection(){for(const b of $('sessions').querySelectorAll('.session')){b.classList.toggle('selected',b.dataset.trace===state.trace);b.setAttribute('aria-current',b.dataset.trace===state.trace?'true':'false');}}
function renderPagination(data){
  $('pagination').hidden=!data.total;const pages=Math.max(1,Math.ceil(data.total/data.size));$('jump').max=pages;$('jump').value=data.page;
  const host=$('page-buttons');host.replaceChildren();const move=p=>{navigate({page:p,trace:null,mode:null,mp:1});$('results').scrollIntoView({block:'start'});};
  const prev=button('← 上一页',()=>move(data.page-1));prev.disabled=data.page===1;host.append(prev);
  const numbers=[1,...Array.from({length:5},(_,i)=>data.page+i-2),pages].filter(p=>p>=1&&p<=pages);let last=0;
  for(const p of [...new Set(numbers)].sort((a,b)=>a-b)){if(last&&p-last>1)host.append(node('span','','…'));const b=button(String(p),()=>move(p));b.setAttribute('aria-label',`第 ${p} 页`);if(p===data.page)b.setAttribute('aria-current','page');host.append(b);last=p;}
  const next=button('下一页 →',()=>move(data.page+1));next.disabled=data.page>=pages;host.append(next);
  const quick=$('quick-pagination'),back=button('←',()=>move(data.page-1)),forward=button('→',()=>move(data.page+1));back.setAttribute('aria-label','上一页会话');forward.setAttribute('aria-label','下一页会话');back.disabled=data.page===1;forward.disabled=data.page>=pages;quick.replaceChildren(back,node('span','',`${data.page} / ${pages}`),forward);
}
async function loadAliases(){
  try{const aliases=await api('/api/aliases');const currentAlias=state.alias||'';$('alias').replaceChildren(new Option('全部身份',''));for(const alias of aliases)$('alias').add(new Option(alias,alias));if(currentAlias&&!aliases.includes(currentAlias))$('alias').add(new Option(currentAlias,currentAlias));$('alias').value=currentAlias;}
  catch{notify('身份选项读取失败，可刷新重试');}
}
async function loadDetail(trace,force=false){
  detailController?.abort();const controller=detailController=new AbortController(),ticket=++detailVersion;current=null;
  $('detail-content').hidden=true;$('detail-status').hidden=false;$('detail-status').textContent='正在读取对话…';$('detail-status').className='loading-note';$('conversation').setAttribute('aria-busy','true');
  const key=[trace,state.internal||'',state.mp].join('|');
  try{const row=!force&&cache.has(key)?cache.get(key):await api('/api/conversation',{trace,message_page:state.mp,view:state.internal==='1'?'all':'dialogue'},controller.signal);if(ticket!==detailVersion)return;current=row;cache.delete(key);cache.set(key,row);if(cache.size>3)cache.delete(cache.keys().next().value);renderConversation();scrollDetail();$('title').focus({preventScroll:true});}
  catch(e){if(e.name!=='AbortError'&&ticket===detailVersion){$('detail-status').className='';emptyState($('detail-status'),'对话暂时未能加载',e.message,()=>loadDetail(trace,true));}}
  finally{if(ticket===detailVersion)$('conversation').setAttribute('aria-busy','false');}
}
function partContent(part){
  if(part.kind==='attachment')return `附件：${part.content}`;
  if(part.kind!=='confirmation_request')return part.content||'';
  try{const args=JSON.parse(part.content);return (args.questions||[]).map(q=>(q.question||q.title||'')+'\n'+(q.options||[]).map(o=>typeof o==='string'?o:o.label||o.description||'').join(' / ')).join('\n\n')||args.plan||part.content;}catch{return part.content;}
}
function renderConversation(){
  $('detail-status').hidden=true;$('detail-content').hidden=false;$('identity').textContent=(current.aliases||[]).join(' · ')||'身份未提供';$('title').textContent=current.title||'未命名会话';document.title=`${current.title||'会话'} · ScanIntent`;
  $('meta').textContent=`${date(current.first_ms)} — ${date(current.last_ms)} · ${current.message_count} 条对话消息`;
  $('trace-id').textContent=current.trace_id;$('trace-meta').textContent=`${current.observation_count} 次调用 · ${current.observation_id_count||0} 条来源标识`;
  const warnings=current.warnings||[];$('warnings-box').hidden=!warnings.length;$('warnings-box').open=false;$('warnings-summary').textContent=`${warnings.length} 项还原提示 · 查看完整性说明`;$('warnings').replaceChildren(...warnings.map(k=>node('li','',warningLabels[k]||k)));renderMessages();
}
function renderMessages(){
  if(!current)return;const internal=state.internal==='1';$('dialogue-tab').setAttribute('aria-pressed',String(!internal));$('internal-tab').setAttribute('aria-pressed',String(internal));
  const records=current.records,total=current.record_total;
  const pages=Math.max(1,Math.ceil(total/MESSAGE_SIZE)),page=current.message_page,offset=(page-1)*MESSAGE_SIZE;
  if(state.mp!==page){state.mp=page;history.replaceState(null,'',url(state));detailKey=[state.trace,state.internal||'',state.mp].join('|');}
  $('message-count').textContent=total?`${offset+1}–${Math.min(offset+records.length,total)} / ${total} 条`:'0 条记录';const host=$('messages');host.replaceChildren();
  for(const [index,m]of records.entries()){
    const box=node('section','message '+m.role),parts=m.parts||[m];
    const name=m.kind==='subagent_context'?'子任务':m.role==='user'?'用户':m.role==='assistant'?'Agent':m.role==='tool'?'工具':'上下文';
    const label=node('div','message-label');label.append(node('span','',`${name} · ${labels[m.kind]||m.kind}`),node('span','message-index',`#${offset+index+1}`));box.append(label);
    for(const part of parts){const content=String(partContent(part));if(content.length>1800){box.append(node('div','message-body',content.slice(0,1800)));const more=node('details','long-body');more.append(node('summary','','展开剩余内容'),node('div','message-body',content.slice(1800)));box.append(more);}else box.append(node('div','message-body',content));}
    const source=node('details','source');source.append(node('summary','','查看消息来源'));source.append(node('code','',`observation ${m.observation_id||'未提供'}`));for(const part of parts){let loc=`${part.field||''}${part.path||''}`;if(part.source_span)loc+=` · 字符 ${part.source_span[0]}–${part.source_span[1]}`;if(part.branch_id)loc+=` · 子任务 ${part.branch_id}`;source.append(node('code','',loc));}box.append(source);host.append(box);
  }
  if(!records.length)emptyState(host,'没有可展示的对话','可以切换到全部记录，查看工具与上下文。',()=>navigate({internal:'1',mp:1}),'查看全部记录');
  const pager=$('message-pagination');pager.replaceChildren();const prev=button('← 上一页消息',()=>navigate({mp:page-1}));prev.disabled=page===1;const next=button('下一页消息 →',()=>navigate({mp:page+1}));next.disabled=page>=pages;
  pager.append(prev,node('span','',`第 ${page} / ${pages} 页`),next);
  const quickPrev=button('←',()=>navigate({mp:page-1})),quickNext=button('→',()=>navigate({mp:page+1}));quickPrev.setAttribute('aria-label','上一页记录');quickNext.setAttribute('aria-label','下一页记录');quickPrev.disabled=page===1;quickNext.disabled=page>=pages;$('message-nav').replaceChildren(quickPrev,quickNext);
}
function scrollDetail(){if(state.mode==='full')window.scrollTo({top:0});else $('detail-scroll').scrollTop=0;}
function parseJSON(value,fallback={}){try{return typeof value==='string'?JSON.parse(value):value||fallback;}catch{return fallback;}}
async function loadRuns(){
  runsController?.abort();const controller=runsController=new AbortController(),ticket=++runsVersion;skeleton($('runs'),3);
  try{const runs=await api('/api/runs',{},controller.signal);if(ticket!==runsVersion)return;runsLoaded=true;$('runs').replaceChildren();
    if(!runs.length){emptyState($('runs'),'还没有采集记录','手动完成一次采集后，可在这里查看结果。',loadRuns,'刷新记录');return;}
    for(const r of runs){const box=node('details','run'),summary=node('summary');const status={success:'成功',failed:'失败',running:'未完成',interrupted:'已中断',partial:'部分完成'}[r.status]||r.status;summary.append(node('span',r.status==='success'?'badge':'badge warning',status),node('span','run-title',`${date(r.window_start_ms)} — ${date(r.window_end_ms)}`));box.append(summary,node('p','run-count',`${r.trace_count} 条会话 · ${r.observation_count} 条记录 · 总耗时 ${(Number(parseJSON(r.timings_json).total_ms||0)/1000).toFixed(1)} 秒`));const grid=node('div','timings');for(const [k,v]of Object.entries(parseJSON(r.timings_json))){const line=node('div','timing');line.append(node('span','muted',stageLabels[k]||k),node('strong','',`${(Number(v)/1000).toFixed(3)} 秒`));grid.append(line);}box.append(grid);if(r.error)box.append(node('p','error-text',r.error));const clock=parseJSON(r.source_clock_json);box.append(node('p','run-clock',`来源时区 ${clock.timezone||'未提供'} · 采集依据 event_ts · 时钟偏差 ${clock.clock_offset_ms==null?'未提供':(Number(clock.clock_offset_ms)/1000).toFixed(3)+' 秒'}`));$('runs').append(box);}
  }catch(e){if(e.name!=='AbortError'&&ticket===runsVersion)emptyState($('runs'),'采集记录暂时未能加载',e.message,loadRuns);}
}
function notify(text){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').hidden=false;if($('preview').open)$('conversation').append($('toast'));else document.body.append($('toast'));toastTimer=setTimeout(()=>{$('toast').hidden=true;},3000);}
$('filters').onsubmit=e=>{e.preventDefault();submitFilters();};$('advanced-toggle').onclick=()=>{const expanded=$('advanced').hidden;$('advanced').hidden=!expanded;$('advanced-toggle').setAttribute('aria-expanded',String(expanded));};
$('alias').onchange=()=>submitFilters();$('warning').onchange=()=>submitFilters();$('order').onchange=()=>applyFilters({order:$('order').value});$('size').onchange=()=>applyFilters({size:Number($('size').value)});
$('query').oncompositionstart=()=>{composing=true;clearTimeout(debounce);};$('query').oncompositionend=()=>{composing=false;queueSearch();};
function queueSearch(){clearTimeout(debounce);if(composing)return;debounce=setTimeout(()=>applyFilters({q:$('query').value.trim()},true),350);}
$('query').oninput=queueSearch;
$('jump-form').onsubmit=e=>{e.preventDefault();const page=Math.min(positive($('jump').value),Number($('jump').max));navigate({page,trace:null,mode:null});$('results').scrollIntoView({block:'start'});};
$('close-detail').onclick=closeDetail;$('preview').addEventListener('cancel',e=>{e.preventDefault();closeDetail();});$('preview').onclick=e=>{if(e.target===$('preview')){const rect=$('preview').getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right)closeDetail();}};
$('expand-detail').onclick=()=>navigate({mode:'full'});$('dialogue-tab').onclick=()=>navigate({internal:null,mp:1});$('internal-tab').onclick=()=>navigate({internal:'1',mp:1});
$('to-bottom').onclick=()=>{$('message-pagination').scrollIntoView({block:'end',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});};
$('copy-link').onclick=async()=>{try{await navigator.clipboard.writeText(location.href);notify('会话链接已复制');}catch{notify('复制失败，请从浏览器地址栏复制链接');}};
$('refresh').onclick=async()=>{const b=$('refresh');b.disabled=true;b.textContent='正在刷新…';try{if(state.view==='runs'){runsLoaded=false;await loadRuns();}else{cache.clear();await Promise.allSettled([loadList(),loadAliases(),state.trace?loadDetail(state.trace,true):Promise.resolve()]);}}finally{b.disabled=false;b.textContent='刷新记录';}};
window.addEventListener('popstate',route);window.addEventListener('hashchange',()=>{if(url(readState())!==url(state))route();});
document.addEventListener('keydown',e=>{if(e.key==='/'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)&&!$('preview').open&&state.view!=='runs'&&state.mode!=='full'){e.preventDefault();$('query').focus();}});
document.querySelector('.skip-link').onclick=e=>{e.preventDefault();$('main').focus();};
route();loadAliases();
