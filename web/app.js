const el = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge = value => '<span class="badge '+esc(value.toLowerCase())+'">'+esc(value)+'</span>';
let operatorToken = '';
let db, view='live', selected, submitAction;
const decisions = () => db.events.filter(e => e.kind==='decision');
const state = s => s.status==='confirmed' && db.services.find(v=>v.name===s.service)?.version!==s.version ? 'Retest' : s.status;
const rows = () => view==='feed' ? decisions() : view==='ledger' ? db.scars : db.services;
const id = item => item.id || item.name;
async function api(path,data) {
  const options=data===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json', ...(operatorToken?{Authorization:'Bearer '+operatorToken}:{})},body:JSON.stringify(data)};
  const response=await fetch('/api/'+path, options);
  if(!response.ok){const body=await response.json();throw Error(typeof body.detail==='string'?body.detail:'Check the required fields.');}
  return response.json();
}
async function refresh(){db=await api('workspace');render();startFeed();}
function render(){
  el('metrics').innerHTML=[[decisions().length,'Traced decisions'],[decisions().filter(x=>x.verdict==='Blocked').length,'Blocked actions'],[db.scars.length,'Recorded scars'],[db.services.length,'Tracked services']].map(([n,t])=>'<div class="metric"><b>'+n+'</b><span>'+t+'</span></div>').join('');
  document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
  el('workspace-layout').hidden=['live','meko','benchmark'].includes(view);
  el('extra').hidden=!['live','meko','benchmark'].includes(view);
  el('unlock').hidden=!db.operator_required;
  if(!el('extra').hidden){renderExtra();return;}
  el('title').textContent={feed:'Decision feed',ledger:'Scar ledger',services:'Services'}[view];
  el('incident').hidden=view!=='feed';
  el('unlock').hidden=!db.operator_required;
  el('create').hidden=view==='services';
  el('create').textContent=view==='feed'?'Run pre-flight':'Record failure';
  const term=el('search').value.toLowerCase().trim();
  const items=rows().filter(x=>[x.service,x.name,x.action,x.symptom,x.version,x.verdict,x.reason].join(' ').toLowerCase().includes(term));
  const current=items.find(x=>id(x)===selected)||items[0]; selected=current && id(current);
  el('list').innerHTML=items.map(x=>'<button class="row '+(id(x)===selected?'selected':'')+'" data-id="'+esc(id(x))+'"><span><strong>'+esc(x.action||x.name)+'</strong><em>'+esc(x.service||'Deployment')+' · v'+esc(x.version)+'</em></span>'+badge(x.verdict||(view==='services'?'Current':state(x)))+'</button>').join('')||'<p class="empty">No matches for this filter.</p>';
  details(current);
}
const action=(name,label)=>'<button class="quiet" data-act="'+name+'">'+label+'</button>';
const evidence=s=>'<div class="evidence"><small>SCAR '+esc(s.id.slice(0,8))+'</small><p>'+esc(s.evidence)+'</p><span class="sub">'+esc(s.author)+' · v'+esc(s.version)+'</span></div>';
function details(x){
  const box=el('detail');
  if(!x){box.innerHTML='<h2>No record selected</h2>';return;}
  let content='';
  if(view==='feed'){
    const exec=x.execution;
    const cloud=(db.cloud_jobs||[]).filter(job=>job.trace_id===x.trace_id);
    const workflow=(x.steps||[]).map(s=>'<li><b>'+esc(s.agent)+'</b> '+esc(s.detail)+'</li>').join('');
    const control=x.verdict==='Caution'?action('approve','Approve supervised run'):x.verdict==='Blocked'&&!x.risk?.prohibited?action('override','Override in simulator'):'';
    content=badge(x.verdict)+'<h2>'+esc(x.action)+'</h2><div class="sub">'+esc(x.service)+' · v'+esc(x.version)+'</div><p>'+esc(x.symptom)+'</p><section class="explain"><h3>Explain this decision</h3><p>'+esc(x.reason)+'</p><strong>'+esc(exec?.detail||'Historical pre-flight decision.')+'</strong></section>'
      +(x.risk?'<p>Risk: '+esc(x.risk.level)+'. '+esc(x.risk.detail)+'</p>'+(x.risk.evidence_score!==null?'<p class="sub">Evidence score '+esc(x.risk.evidence_score)+'/100 · rule score, not probability · '+esc(x.risk.factors.join(', '))+'</p>':''):'')+x.scars.map(evidence).join('')+(x.approval?'<p>Human '+esc(x.approval.mode)+' by '+esc(x.approval.reviewer)+'. '+esc(x.approval.note)+'</p>':'')
      +(exec?.ran?'<details><summary>Simulated execution evidence</summary><p>Health check: '+esc(exec.health_check)+'. Rollback: '+(exec.rollback?'applied in simulation':'not needed')+'.</p><p>Before: '+esc(exec.snapshot_before?.health)+'. After: '+esc(exec.snapshot_after?.health)+'.</p></details>':'')+(workflow?'<details><summary>Six-stage trace</summary><ol>'+workflow+'</ol></details>':'')
      +'<div class="sub trace">Trace '+esc((x.trace_id||x.id).slice(0,12))+' · '+esc(x.origin||'historical')+' · '+esc(new Date(x.created).toLocaleString())+(x.parent?' · Replay of '+esc(x.parent.slice(0,8)):'')+'</div>'
      +(x.recall?.runbooks||[]).map(r=>'<details><summary>Runbook · '+esc(r.source)+'</summary><p>'+esc(r.text)+'</p></details>').join('')+'<div class="sub">Recall · '+esc(x.recall?.status||'historical')+'</div>'
      +(x.proposal?.usage?'<div class="sub">Model use · '+esc(x.proposal.usage.total_tokens)+' tokens</div>':'')
      +cloud.map(j=>'<div class="sub">'+esc(j.kind)+' · '+esc(j.state)+'</div>').join('')
      +'<div class="actions">'+action('replay','Replay on current version')+(!x.approval&&(!exec||!exec.ran)?control:'')+(db.meko?action('recall','Search Meko context'):'')+'</div>';
  }else if(view==='ledger'){
    content=badge(state(x))+'<h2>'+esc(x.action)+'</h2><div class="sub">'+esc(x.service)+' · failed on v'+esc(x.version)+'</div><p>'+esc(x.reason)+'</p>'+evidence(x)+(x.reviewer?'<p>Last reviewed by '+esc(x.reviewer)+'</p>':'')+'<div class="actions">'+action('review','Review evidence')+'</div>';
  }else{
    const scars=db.scars.filter(s=>s.service===x.name); const health=db.health[x.name];
    content='<small>SERVICE MEMORY MAP</small><h2>'+esc(x.name)+'</h2><p>Current deployment · v'+esc(x.version)+'</p><p>'+esc(health.state)+' · '+esc(health.detail)+'</p>'+health.evidence.map(e=>'<p class="sub">'+esc(e)+'</p>').join('')+scars.map(s=>'<div class="evidence">'+badge(state(s))+' <strong>'+esc(s.action)+'</strong><p>'+esc(s.reason)+'</p></div>').join('')+'<div class="actions">'+action('deploy','Update version')+'</div>';
  }box.innerHTML=content;
}
function switchView(name){view=name;selected=null;el('search').value='';el('notice').textContent='';render();}
document.querySelector('nav').onclick=e=>{if(e.target.dataset.view)switchView(e.target.dataset.view);};
el('list').onclick=e=>{const row=e.target.closest('[data-id]');if(row){selected=row.dataset.id;el('notice').textContent='';render();}};
el('search').oninput=render;
const field=(name,label,value='',long=false)=>'<label>'+label+(long?'<textarea required maxlength="1200" name="'+name+'">'+esc(value)+'</textarea>':'<input required maxlength="120" name="'+name+'" value="'+esc(value)+'">')+'</label>';
const select=(name,label,options)=>'<label>'+label+'<select name="'+name+'">'+options.map(x=>'<option>'+esc(x)+'</option>').join('')+'</select></label>';
function modal(title,fields,label,fn){
  el('formtitle').textContent=title;el('fields').innerHTML=fields;el('submit').textContent=label;el('formerror').textContent='';submitAction=fn;document.querySelector('dialog').showModal();
}
el('close').onclick=()=>document.querySelector('dialog').close();
el('form').onsubmit=async e=>{e.preventDefault();el('submit').disabled=true;try{await submitAction(Object.fromEntries(new FormData(e.target)));document.querySelector('dialog').close();el('search').value='';await refresh();}catch(err){el('formerror').textContent=err.message;el('notice').textContent=err.message;}finally{el('submit').disabled=false;}};
el('create').onclick=()=>{
  const common=select('service','Service',db.services.map(x=>x.name))+field('action','Proposed action');
  if(view==='feed')modal('Run pre-flight',common+field('symptom','Incident symptom','',true),'Check prior failures',async d=>{selected=(await api('decisions',d)).id;});
  else modal('Record failure',common+field('reason','Why did it fail?','',true)+field('evidence','Observed evidence','',true)+field('author','Recorded by','Responder')+select('severity','Severity',['medium','low','high','critical']),'Save scar',async d=>{selected=(await api('scars',d)).id;});
};
el('detail').onclick=async e=>{
 const btn=e.target.closest('[data-act]');if(!btn)return;
 const x=rows().find(i=>id(i)===selected);btn.disabled=true;
 try{switch(btn.dataset.act){
 case 'approve':case 'override':{
   const mode=btn.dataset.act;
   modal(mode==='approve'?'Approve supervised run':'Override block in simulator',field('reviewer','On-call reviewer')+field('note','Reason for this decision','',true),'Run supervised simulation',async d=>{selected=(await api('decisions/'+x.id+'/'+mode,d)).id;});
   break;}
 case 'review':modal('Review evidence',field('reviewer','Reviewed by','On-call')+select('status','Finding',['confirmed','disputed'])+field('note','Review note','',true),'Save review',d=>api('scars/'+x.id+'/review',d));break;
 case 'deploy':modal('Update '+x.name,field('version','New deployment version'),'Update version',d=>api('services/'+encodeURIComponent(x.name)+'/deploy',d));break;
 case 'replay':selected=(await api('decisions/'+x.id+'/replay',{})).id;await refresh();break;
 case 'recall':{const r=await api('meko/search',x);const out=document.createElement('pre');out.textContent=[...r.context,...(r.runbooks||[]).map(x=>x.source+': '+x.text),'Sources: '+JSON.stringify(r.sources)].join('\n');el('detail').append(out);break;}
 }}catch(err){el('notice').textContent=err.message;}finally{btn.disabled=false;}
};
el('incident').onclick=()=>modal('New incident',select('service','Service',db.services.map(x=>x.name))+field('symptom','What is happening?','',true),'Investigate incident',async d=>{selected=(await api('incidents',d)).trace_id;view='live';});
el('unlock').onclick=()=>modal('Workspace access','<label>Access token<input type="password" required name="token" autocomplete="off"></label>','Sign in',async d=>{await api('session',d);operatorToken='';});
let live;
function startFeed(){
  if(live)return;
  live=new EventSource('/api/events');
  live.onopen=()=>el('connection').textContent='Live';
  live.onerror=()=>el('connection').textContent='Reconnecting';
  live.addEventListener('workspace',()=>refresh().catch(err=>el('notice').textContent=err.message));
}
function renderExtra(){
  let content='';
  if(view==='live'){
    const items=db.inbox||[];
    selected=items.some(x=>x.trace_id===selected)?selected:items[0]?.trace_id;
    const events=db.events.filter(e=>e.trace_id===selected).reverse();
    content='<div class="panel"><div class="panelhead"><h2>Live incident queue</h2><button data-extra="demo">'+(db.demo.running?'Stop demo':'Start 10-incident demo')+'</button></div><p>Monitoring alerts and telemetry flow into one trace per incident. Target execution is simulated.</p><p class="sub">Demo '+(db.demo.running?'running · '+db.demo.remaining+' remaining':'stopped')+' · '+items.filter(x=>x.state==='queued').length+' queued · '+items.filter(x=>x.state==='running').length+' running</p></div>'
      +'<div class="health-grid">'+Object.entries(db.health).map(([name,h])=>'<article class="panel"><strong>'+esc(name)+'</strong><p>'+esc(h.state)+'</p><p class="sub">'+esc(h.detail)+'</p>'+h.evidence.map(e=>'<p class="sub">'+esc(e)+'</p>').join('')+'</article>').join('')+'</div>'
      +'<div class="layout"><section class="panel"><h2>Received incidents</h2>'+items.map(x=>'<button class="row '+(selected===x.trace_id?'selected':'')+'" data-trace="'+esc(x.trace_id)+'"><span><strong>'+esc(x.service)+'</strong><em>'+esc(x.symptom)+'</em></span>'+badge(x.state)+'</button>').join('')+(items.length?'':'<p>Start the demo or send an authenticated alert.</p>')+'</section><aside class="panel"><h2>Agent activity</h2><p class="sub">'+esc(selected||'Awaiting incident')+'</p>'
      +events.map(e=>'<div class="evidence"><strong>'+esc(e.agent||e.kind)+'</strong><p>'+esc(e.detail||e.reason||e.symptom||e.state||'Trace saved')+'</p><span class="sub">'+esc(new Date(e.created).toLocaleTimeString())+' · '+esc(e.state||e.verdict||e.kind)+'</span></div>').join('')
      +(items.find(x=>x.trace_id===selected)?.state==='failed'?'<button data-extra="retry">Retry failed incident</button>':'')
      +(decisions().some(x=>x.trace_id===selected)?'<button data-extra="decision">Open decision</button>':'')+'</aside></div>';
  }else if(view==='meko'){
    const packs=Object.entries(db.datapacks).filter(([,id])=>id);
    content='<div class="panel"><h2>Meko connection</h2><p>'+ (db.meko?'Server credentials configured. Check acknowledgements below to verify individual operations.':'Local mode. Set MEKO_API_KEY and MEKO_DATAPACK_ID on the server to connect.')+'</p><div class="actions"><button data-extra="runbook" '+(!db.meko?'disabled':'')+'>Upload and verify runbook</button><button data-extra="audit" '+(!db.meko?'disabled':'')+'>Save verified audit artifact</button></div><div class="actions">'+packs.map(([name,id])=>'<a class="outline" target="_blank" rel="noopener" href="https://cloud.mekodata.ai/datapacks/'+encodeURIComponent(id)+'">'+esc(name)+' datapack</a>').join('')+'</div></div>'
      +'<div class="health-grid"><section class="panel"><h2>Knowledge files</h2>'+(db.kb_files.map(x=>'<p>'+esc(x.filename)+' · '+esc(x.state)+'</p>').join('')||'<p>No runbook upload recorded here yet.</p>')+'</section><section class="panel"><h2>WorkbenchLM</h2><p>Open the Knowledge datapack, then its gear menu → Open Workbench. Select the indexed runbook and ask:</p><blockquote>Why can restarting payments-api v2.14 exhaust its connection pool, and what should an on-call reviewer verify before a retest?</blockquote><p class="sub">Workbench usage belongs to the Meko portal. Provider token reports from Scarline are separate.</p></section></div>'
      +'<section class="panel"><h2>Observed calls from this installation</h2><p>These counts describe local attempts and responses. They are not your account quota balance.</p><table><thead><tr><th>Tool</th><th>Status</th><th>Calls</th></tr></thead><tbody>'+db.meko_calls.map(x=>'<tr><td>'+esc(x.tool)+'</td><td>'+esc(x.state)+'</td><td>'+esc(x.count)+'</td></tr>').join('')+'</tbody></table><h3>Webhook sources</h3>'+Object.entries(db.alert_sources).map(([name,on])=>'<p>'+esc(name)+' · '+(on?'secret configured':'disabled')+'</p>').join('')+'</section>';
  }else{
    const b=db.benchmark;
    content='<section class="panel"><h2>Paired simulator benchmark</h2><p>Cold-start ledgers, identical incident schedule and deterministic target outcomes. These results do not measure production reliability or cloud retrieval quality.</p>'+(b?'<p>'+esc(b.repetitions||1)+' paired repetitions · '+esc(b.baseline.incidents)+' incidents per arm per repetition</p><table><thead><tr><th>Measure (first pair)</th><th>Without Scarline</th><th>With Scarline</th></tr></thead><tbody>'+[['Repeated failures','repeated_failures'],['Failed executions','failed_executions'],['Target calls','target_calls'],['Passed health checks','passed_health_checks'],['Mean elapsed ms','mean_local_ms']].map(([label,key])=>'<tr><td>'+label+'</td><td>'+esc(b.baseline[key])+'</td><td>'+esc(b.scarline[key])+'</td></tr>').join('')+'</tbody></table><p>Provider tokens: '+(b.scarline.reported_tokens===null?'unmeasured':esc(b.scarline.reported_tokens))+'. Run benchmark.py to reproduce the report.</p>':'<p>Run benchmark.py and save its output to benchmark-results.json.</p>')+'</section>';
  }
  el('extra').innerHTML=content;
}
el('extra').onclick=async e=>{
  const trace=e.target.closest('[data-trace]');
  if(trace){selected=trace.dataset.trace;renderExtra();return;}
  const button=e.target.closest('[data-extra]');if(!button)return;
  const kind=button.dataset.extra;button.disabled=true;
  try{
    if(kind==='demo')await api('demo',{enabled:!db.demo.running,count:10});
    if(kind==='retry')await api('incidents/'+selected+'/retry',{});
    if(kind==='decision'){const x=decisions().find(x=>x.trace_id===selected);view='feed';selected=x.id;render();return;}
    if(kind==='runbook'||kind==='audit'){
      el('notice').textContent='Waiting for Meko verification…';
      const r=await api('meko/'+kind,{});el('notice').textContent=kind==='runbook'?'Runbook indexed and retrieved.':'Audit bytes verified in Meko.';
    }
    await refresh();
  }catch(err){el('notice').textContent=err.message;}finally{button.disabled=false;}
};
refresh().catch(err=>{el('notice').textContent=err.message;el('connection').textContent='Access required or unavailable';el('unlock').hidden=false;});
