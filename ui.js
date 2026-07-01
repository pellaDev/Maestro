'use strict';
// Path: ui.js

const MAESTRO_UID  = '__maestro__';
const EXPLORER_UID = '__explorer__';
const MAX_PANELS   = 32;

const TERM_COLORS = {
  default:{ bg:'var(--bg2)', border:'var(--brd)',  title:'var(--txt2)', label:'Default' },
  navy:   { bg:'#0a0e1a',    border:'#1a2a4a',     title:'#5dade2',     label:'Navy'   },
  forest: { bg:'#0a150a',    border:'#1a3a1a',     title:'#2ecc71',     label:'Forest' },
  plum:   { bg:'#160a1a',    border:'#3a1a4a',     title:'#c39bd3',     label:'Plum'   },
  teal:   { bg:'#091518',    border:'#0e3340',     title:'#1abc9c',     label:'Teal'   },
  ember:  { bg:'#160a00',    border:'#3a1a00',     title:'#e67e22',     label:'Ember'  },
};

// ── State ─────────────────────────────────────────────────────────────────────
let entries=[],selIdx=-1,panels=[],activeTab=null,sseConns={},dragSrc=null,_dragTabUid=null;
let _prevHash='',_loggingEnabled=true;
let _settings={};
let _treeCurrentPath=null,_fbPath=null,_explorerPath=null;
let _pageVisible=!document.hidden;
let _sysCpu=0,_sysRam=0,_sysRamTotal=0;
let _gpu={gpu:0,vramUsed:0,vramTotal:0};
const _tabReady=new Set();

// ── Stale connection watchdog state ─────────────────────────────────────────────
// Chromium (and Chromium-based browsers like Brave) throttles or pauses timers
// on background tabs regardless of Memory Saver settings, since it targets any
// non-focused tab. This silently stalls poll() and can drop SSE connections,
// making the UI look "frozen" (clicks on Launch/Kill appear to do nothing) until
// the page is manually refreshed. This watchdog detects the stall and recovers.
const STALE_MS = 6000;           // ms without a successful poll before we consider the UI stale
let _lastPollOk = Date.now();    // timestamp of the last successful /api/state response
let _lastPollNetErr = false;     // true = last poll failed with a real fetch error; false = just stale (throttled timer)
let _staleActive = false;        // guards against overlapping reconnect attempts

let _widePanels={};
try{ _widePanels=JSON.parse(localStorage.getItem('tpw')||'{}'); }catch{}
function _saveWide(){ try{localStorage.setItem('tpw',JSON.stringify(_widePanels));}catch{} }

let _tabOrder=[];
try{ _tabOrder=JSON.parse(localStorage.getItem('mto')||'[]'); }catch{}
function _saveTabOrder(){ try{localStorage.setItem('mto',JSON.stringify(_tabOrder));}catch{} }

function _savePanels(){ try{sessionStorage.setItem('mp',JSON.stringify(panels));}catch{} }
function _loadPanels(){ try{const p=sessionStorage.getItem('mp');if(p)panels=JSON.parse(p);}catch{} }
_loadPanels();

document.addEventListener('visibilitychange',()=>{
  _pageVisible=!document.hidden;
  if(_pageVisible){ poll(); _checkStale(); }
});

// DOM refs needed by poll callbacks — must be initialized before any async poll result
const fQ=document.getElementById('ftr-quote');
const fUrl=document.getElementById('ftr-url');
const fT=document.getElementById('ftr-tip');

// ── Utilities ─────────────────────────────────────────────────────────────────
function g(fn){ let t=0; return function(...a){const n=Date.now();if(n-t<500)return;t=n;fn(...a);}; }
function el(tag,cls){ const e=document.createElement(tag); if(cls)e.className=cls; return e; }
function dotCls(e){ return e.crash?'crash':e.pid?'run':'stop'; }
function uidIdx(uid){ return entries.findIndex(e=>e.uid===uid); }
function isAlwaysValid(uid){ return uid===MAESTRO_UID||uid===EXPLORER_UID; }
const isWebapp   =uid=>/_w\d+$/.test(uid);
const isFvPanel  =uid=>uid.startsWith('fv_');
const isInfoPanel=uid=>uid.startsWith('info_');

// ── Load bar ──────────────────────────────────────────────────────────────────
let _loadN=0;
const _loadFill=document.getElementById('loadfill');
function showLoad(){ _loadN++;_loadFill.classList.add('act'); }
function hideLoad(){ if(--_loadN<=0){_loadN=0;_loadFill.classList.remove('act');} }
// Safety: if loadbar gets stuck, reset after 8s
setInterval(()=>{ if(_loadN>0){_loadN=0;_loadFill.classList.remove('act');} },8000);

// ── API ───────────────────────────────────────────────────────────────────────
async function api(method,path,body){
  showLoad();
  try{
    const o={method,headers:{'Content-Type':'application/json'}};
    if(body!==undefined) o.body=JSON.stringify(body);
    return await (await fetch(path,o)).json();
  }catch(e){ return{error:String(e)}; }
  finally{ hideLoad(); }
}

// ── Formatting ────────────────────────────────────────────────────────────────
function _toGB(mb){ return (mb/1024).toFixed(1); }
function _resColor(pct){ return pct>=70?'#e74c3c':pct>=35?'#e67e22':'#ffffff'; }
function _resNum(val,pct){ return `<span style="color:${_resColor(pct)}">${val}</span>`; }
function _fmtCPU(cpu){
  const v=typeof cpu==='number'?cpu:0;
  return `CPU ${_resNum(v.toFixed(1)+'%',v)}`;
}
function _fmtRAM(ram){
  const v=typeof ram==='number'?ram:0;
  const pct=_sysRamTotal>0?v/_sysRamTotal*100:Math.min(v/4096*100,100);
  return `RAM ${_resNum(_toGB(v),pct)}`;
}
function _fmtStats(cpu,ram){ return `${_fmtCPU(cpu)} | ${_fmtRAM(ram)}`; }

// ── Poll ──────────────────────────────────────────────────────────────────────
function _mkHash(es){
  return es.map(e=>
    `${e.uid}|${e.status}|${e.name||''}|${e.delay}|${+!!e.autoRestart}|`+
    `${+(e.keepOpen!==false)}|${+!!e.launchHidden}|`+
    `${JSON.stringify(e.webapps||[])}|${+!!e.crash}|${+!!e.pid}|`+
    `${e.sepFile||''}|${+!!e.hasUpdate}|${e.infoFile||''}|${+!!e.openOutside}|${e.termColor||'default'}|${+!!e.unreachable}`
  ).join(';');
}

async function poll(){
  const d=await api('GET','/api/state');
  if(d&&d.entries){ _lastPollOk=Date.now(); _lastPollNetErr=false; } else { _lastPollNetErr=true; }
  if(!d||!d.entries) return;
  entries=d.entries;
  if(d.loggingEnabled!==undefined) _loggingEnabled=d.loggingEnabled;
  if(d.gpu)             _gpu=d.gpu;
  if(d.sysCpu!==undefined)     _sysCpu=d.sysCpu;
  if(d.sysRam!==undefined)     _sysRam=d.sysRam;
  if(d.sysRamTotal!==undefined) _sysRamTotal=d.sysRamTotal;

  const h=_mkHash(entries);
  if(h!==_prevHash){ _prevHash=h; renderSidebar(); }
  else { patchMeta(); }

  renderPills();
  updatePanelMeta();
  patchPanelTitles();
  _patchPanelColors();
  if(_pageVisible) updateMaestroBar();
  updateFooterForActiveTab();
  _updateMaestroEye();

  // Blank iframes whose process stopped
  entries.forEach(e=>{
    (e.webapps||[]).forEach((_,wi)=>{
      const uid=`${e.uid}_w${wi}`;
      if(!e.pid&&_tabReady.has(uid)){
        _tabReady.delete(uid);
        const ifr=tabIframes[uid];
        if(ifr){ifr.style.opacity='0';ifr.src='about:blank';}
        const ph=document.getElementById('ph-'+uid);
        if(ph) ph.style.display='flex';
      }
    });
  });

  // Remove panels whose entry no longer exists in config at all
  const validUIDs=new Set([
    ...entries.map(e=>e.uid),
    ...entries.flatMap(e=>(e.webapps||[]).map((_,wi)=>`${e.uid}_w${wi}`)),
    MAESTRO_UID,EXPLORER_UID,
    ...entries.filter(e=>e.sepFile).map(e=>'fv_'+e.uid),
    ...entries.filter(e=>e.infoFile).map(e=>'info_'+e.uid),
  ]);
  // Use a soft remove that doesn't close SSE — just clean panels array
  // hidePanel would close SSE prematurely on first poll after browser refresh
  const stalePanels=panels.filter(uid=>!isAlwaysValid(uid)&&!validUIDs.has(uid));
  if(stalePanels.length){
    stalePanels.forEach(uid=>{
      panels=panels.filter(u=>u!==uid);
      if(sseConns[uid]){sseConns[uid].close();delete sseConns[uid];}
      document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.remove();
    });
    _savePanels();
    renderGrid();
    renderSidebar();
  }

  // Connect SSE for open panels that lost connection.
  // renderGrid first so panel DOM nodes exist before log events arrive.
  const needSSE=panels.filter(uid=>!isWebapp(uid)&&!isFvPanel(uid)&&!isInfoPanel(uid)&&!sseConns[uid]);
  if(needSSE.length) renderGrid();
  needSSE.forEach(uid=>connectSSE(uid));
  if(activeTab&&!sseConns[activeTab]&&!isWebapp(activeTab)) connectSSE(activeTab);
}

function _pollSoon(){
  setTimeout(poll,400);
  setTimeout(poll,1200);
  setTimeout(poll,3000);
}

// Like poll() but always forces a full sidebar re-render regardless of hash.
// Use after batch operations where daemon state may lag behind.
async function forcePoll(){
  const d=await api('GET','/api/state');
  if(d&&d.entries){ _lastPollOk=Date.now(); _lastPollNetErr=false; } else { _lastPollNetErr=true; }
  if(!d||!d.entries) return;
  entries=d.entries;
  if(d.loggingEnabled!==undefined) _loggingEnabled=d.loggingEnabled;
  if(d.gpu)             _gpu=d.gpu;
  if(d.sysCpu!==undefined)     _sysCpu=d.sysCpu;
  if(d.sysRam!==undefined)     _sysRam=d.sysRam;
  if(d.sysRamTotal!==undefined) _sysRamTotal=d.sysRamTotal;
  // Force full re-render ignoring hash
  _prevHash='';
  renderSidebar();
  renderPills();
  updatePanelMeta();
  patchPanelTitles();
  _patchPanelColors();
  if(_pageVisible) updateMaestroBar();
  updateFooterForActiveTab();
  _updateMaestroEye();
}

setInterval(()=>{ if(_pageVisible)poll(); },2000);
poll();

// ── Maestro bar ───────────────────────────────────────────────────────────────
function updateMaestroBar(){
  if(!_pageVisible) return;
  const mb=document.getElementById('mb-resources'); if(!mb) return;
  const ramPct=_sysRamTotal>0?_sysRam/_sysRamTotal*100:0;
  const vramPct=_gpu.vramTotal>0?_gpu.vramUsed/_gpu.vramTotal*100:0;
  const ramStr=_sysRamTotal>0?`${_toGB(_sysRam)}/${_toGB(_sysRamTotal)}`:`${_toGB(_sysRam)}`;
  let html=`CPU ${_resNum(_sysCpu.toFixed(1)+'%',_sysCpu)} | RAM ${_resNum(ramStr,ramPct)}`;
  if(_gpu.vramTotal>0) html+=` | GPU ${_resNum(_gpu.gpu+'%',_gpu.gpu)} | VRAM ${_resNum(_toGB(_gpu.vramUsed)+'/'+_toGB(_gpu.vramTotal),vramPct)}`;
  mb.innerHTML=html;
}

// ── Footer ────────────────────────────────────────────────────────────────────

function ftrDefault(){ fQ.classList.remove('hide');fT.classList.remove('show');if(!activeTab||!isWebapp(activeTab))fUrl.classList.remove('show'); }
function ftrTip(html){ fQ.classList.add('hide');fUrl.classList.remove('show');fT.innerHTML=html;fT.classList.add('show'); }
function setStatus(m){
  const s=document.getElementById('ftr-status');
  s.textContent=m;s._tmp=true;clearTimeout(s._t);
  s._t=setTimeout(()=>{s._tmp=false;s.textContent='';updateFooterForActiveTab();},3500);
}

// ── Footer alert (bold/colored, used for the stale-connection watchdog) ──────
// Shares the #ftr-status element with setStatus() — sets s._tmp so
// updateFooterForActiveTab() won't clobber it while active, exactly like
// setStatus() already does for regular short-lived status messages.
function _footerAlert(msg,opts={}){
  const s=document.getElementById('ftr-status');
  clearTimeout(s._t);
  s._tmp=true;
  s.style.color=opts.color||'';
  s.style.fontWeight=opts.bold?'700':'';
  s.textContent=msg;
  if(opts.autoClear!==false){
    s._t=setTimeout(()=>{
      s._tmp=false;s.style.color='';s.style.fontWeight='';s.textContent='';
      updateFooterForActiveTab();
    },opts.duration||3500);
  }
}

// ── Stale connection watchdog ─────────────────────────────────────────────────
// Called on tab visibility return and before Launch/Kill actions. Checks the
// age of the last successful poll; if too old, alerts the user in the footer
// and attempts an automatic soft-reconnect (forcePoll + reopen dead SSE).
async function _checkStale(){
  if(_staleActive) return;
  if(Date.now()-_lastPollOk<STALE_MS) return;
  _staleActive=true;
  const msg=_lastPollNetErr
    ?'⚠ SERVER NON RAGGIUNGIBILE — riconnessione…'
    :'⚠ TAB SOSPESA DAL BROWSER — riconnessione…';
  _footerAlert(msg,{color:'var(--red)',bold:true,autoClear:false});
  await _attemptReconnect();
}

async function _attemptReconnect(){
  await forcePoll();
  const recovered=(Date.now()-_lastPollOk)<STALE_MS;
  if(recovered){
    // Reopen any panel SSE connection that is missing or actually closed
    panels.forEach(uid=>{
      if(!isWebapp(uid)&&!isFvPanel(uid)&&!isInfoPanel(uid)){
        if(!sseConns[uid]||sseConns[uid].readyState===EventSource.CLOSED) connectSSE(uid);
      }
    });
    if(activeTab&&!isWebapp(activeTab)&&(!sseConns[activeTab]||sseConns[activeTab].readyState===EventSource.CLOSED)){
      connectSSE(activeTab);
    }
    _footerAlert('✓ RICONNESSO!',{color:'var(--grn)',bold:true,duration:3000});
  } else {
    _footerAlert('✕ RICONNESSIONE FALLITA — ricarica la pagina (F5)',{color:'var(--red)',bold:true,autoClear:false});
  }
  _staleActive=false;
}

function updateFooterForActiveTab(){
  const s=document.getElementById('ftr-status');
  if(!activeTab||!isWebapp(activeTab)){
    fUrl.classList.remove('show');fQ.classList.remove('hide');fT.classList.remove('show');
    if(!s._tmp)s.textContent=''; return;
  }
  const m=activeTab.match(/^(.+)_w(\d+)$/); if(!m) return;
  const e=entries.find(x=>x.uid===m[1])||{};
  const wb=(e.webapps||[])[parseInt(m[2])]||{};
  fUrl.textContent=_suspendedTabs.has(activeTab)?`⏸ SUSPENDED — ${wb.url||''}`:(wb.url||'');
  fUrl.classList.add('show');fQ.classList.add('hide');fT.classList.remove('show');
  if(!s._tmp) s.textContent=(e.pid&&e.metrics)?`${_fmtCPU(e.metrics.cpu)} | ${_fmtRAM(e.metrics.ram)}`:'';
}

// ── Tooltips ──────────────────────────────────────────────────────────────────
document.addEventListener('mouseover',ev=>{ const t=ev.target.closest('[data-tip]');if(t)ftrTip(t.dataset.tip); });
document.addEventListener('mouseout',ev=>{
  const t=ev.target.closest('[data-tip]');
  if(t&&!t.contains(ev.relatedTarget)) setTimeout(()=>{
    fT.classList.remove('show');
    if(activeTab&&isWebapp(activeTab)){fQ.classList.add('hide');fUrl.classList.add('show');}
    else ftrDefault();
  },60);
});
document.getElementById('main').addEventListener('click',ev=>{
  if(ev.target.id==='main'||ev.target.id==='tab-view'){selIdx=-1;closeEdit();ftrDefault();}
});

// ── Cross-hover: sidebar ↔ panel ──────────────────────────────────────────────
(function(){
  const list=document.getElementById('lsb-scripts');
  const main=document.getElementById('main');
  function clear(){ document.querySelectorAll('.tp.phover,.erow.phover').forEach(x=>x.classList.remove('phover')); }
  list.addEventListener('mouseover',ev=>{
    const r=ev.target.closest('.erow[data-uid]');clear();if(!r)return;
    r.classList.add('phover');
    document.querySelector(`.tp[data-uid="${CSS.escape(r.dataset.uid)}"]`)?.classList.add('phover');
  });
  list.addEventListener('mouseleave',clear);
  main.addEventListener('mouseover',ev=>{
    const p=ev.target.closest('.tp[data-uid]');clear();if(!p)return;
    p.classList.add('phover');
    document.querySelector(`.erow[data-uid="${CSS.escape(p.dataset.uid)}"]`)?.classList.add('phover');
  });
  main.addEventListener('mouseleave',clear);
})();

// ── Tab pills ─────────────────────────────────────────────────────────────────
function renderPills(){
  const bar=document.getElementById('tab-bar');
  bar.innerHTML='';

  let pills=[];
  entries.forEach(e=>{(e.webapps||[]).forEach((wb,wi)=>pills.push({uid:`${e.uid}_w${wi}`,e,wi,wb}));});
  pills=pills.filter(p=>!!p.e.pid);   // only show when process running

  const om=Object.fromEntries(_tabOrder.map((uid,i)=>[uid,i]));
  pills.sort((a,b)=>(om[a.uid]??Infinity)-(om[b.uid]??Infinity));
  const cur=new Set(pills.map(p=>p.uid));
  _tabOrder=_tabOrder.filter(u=>cur.has(u));
  pills.forEach(p=>{if(!_tabOrder.includes(p.uid))_tabOrder.push(p.uid);});
  pills.forEach(({uid,e,wi,wb})=>bar.appendChild(makePill(e,wi,wb)));

  if(activeTab&&!cur.has(activeTab)){
    activeTab=null;
    document.getElementById('tab-view').classList.remove('active');
    try{sessionStorage.removeItem('mat');}catch{}
  }
}

function makePill(e,wi=0,wb={}){
  const uid=`${e.uid}_w${wi}`;
  const isActive=activeTab===uid;
  const dcls=e.crash?'crash':e.pid?'run':'stop';
  const label=e.displayName||(e.path||'').split('/').pop();
  const url=wb.url||'';
  const pill=el('div','tab tab-web'+(isActive?' active':''));
  pill.draggable=true;pill.dataset.tabUid=uid;pill.dataset.tip=`🌐 ${label} — ${url}`;
  const dot=el('div','tab-dot '+dcls);dot.dataset.pdot=e.uid;
  const nm=el('span','tab-name');nm.textContent=`🌐 ${label}`;nm.style.pointerEvents='none';
  const btnRef=el('button','tab-ibtn');btnRef.textContent='↺';btnRef.title='Reload';
  btnRef.onclick=ev=>{ev.stopPropagation();reloadWebTab(uid);};
  const susp=_suspendedTabs.has(uid);
  const btnSusp=el('button','tab-ibtn');
  btnSusp.textContent=susp?'▶':'⏸';btnSusp.title=susp?'Resume':'Suspend';
  btnSusp.onclick=ev=>{ev.stopPropagation();toggleSuspend(uid,url);};
  pill.append(dot,nm,btnRef,btnSusp);
  pill.onclick=g(()=>clickPill(uid,e,wb));
  pill.addEventListener('dragstart',ev=>{_dragTabUid=uid;ev.dataTransfer.effectAllowed='move';setTimeout(()=>pill.style.opacity='0.4',0);});
  pill.addEventListener('dragend',()=>{pill.style.opacity='';_dragTabUid=null;});
  pill.addEventListener('dragover',ev=>{ev.preventDefault();pill.classList.add('tab-drag-over');});
  pill.addEventListener('dragleave',()=>pill.classList.remove('tab-drag-over'));
  pill.addEventListener('drop',ev=>{ev.preventDefault();pill.classList.remove('tab-drag-over');if(_dragTabUid&&_dragTabUid!==uid)_onTabDrop(_dragTabUid,uid);});
  return pill;
}

async function _onTabDrop(srcUid,dstUid){
  const si=_tabOrder.indexOf(srcUid),di=_tabOrder.indexOf(dstUid);
  if(si>=0&&di>=0){[_tabOrder[si],_tabOrder[di]]=[_tabOrder[di],_tabOrder[si]];_saveTabOrder();renderPills();}
  const sm=srcUid.match(/^(.+)_w(\d+)$/),dm=dstUid.match(/^(.+)_w(\d+)$/);
  if(!sm||!dm) return;
  const[,se,sw]=sm,[,de,dw]=dm;
  if(se===de){
    const e=entries.find(x=>x.uid===se);if(!e)return;
    const wbs=[...e.webapps];[wbs[+sw],wbs[+dw]]=[wbs[+dw],wbs[+sw]];
    await api('POST','/api/update-entry',{idx:entries.indexOf(e),webapps:wbs});
  } else {
    const si2=entries.findIndex(x=>x.uid===se),di2=entries.findIndex(x=>x.uid===de);
    if(si2>=0&&di2>=0) await api('POST','/api/reorder',{a:si2,b:di2});
  }
  await poll();
}

function clickPill(uid,e,wb){
  if(activeTab===uid){reloadWebTab(uid);return;}
  activeTab=uid;activateTabContainer(uid,e,wb);renderPills();updateFooterForActiveTab();
  try{sessionStorage.setItem('mat',uid);}catch{}
}
function closePill(){
  activeTab=null;
  const tv=document.getElementById('tab-view');
  tv.classList.remove('active');tv.querySelectorAll('.tv-active').forEach(c=>c.classList.remove('tv-active'));
  renderGrid();renderPills();updateFooterForActiveTab();
  try{sessionStorage.removeItem('mat');}catch{}
}
function activateTabContainer(uid,e,wb){
  const tv=document.getElementById('tab-view');
  tv.classList.add('active');tv.querySelectorAll('.tv-active').forEach(c=>c.classList.remove('tv-active'));
  const url=wb.url||'';
  let cont=document.getElementById('tc-'+uid);
  if(!cont) cont=createWebContainer(uid,url,tv);
  cont.classList.add('tv-active');
  if(_tabReady.has(uid)){const ifr=tabIframes[uid];if(ifr&&(ifr.src==='about:blank'||!ifr.src))loadWebTab(uid,url);}
}
function renderTabView(){
  if(!activeTab){document.getElementById('tab-view').classList.remove('active');return;}
  const m=activeTab.match(/^(.+)_w(\d+)$/);if(!m){activeTab=null;return;}
  const e=entries.find(x=>x.uid===m[1])||{};
  const wb=(e.webapps||[])[parseInt(m[2])]||{};
  activateTabContainer(activeTab,e,wb);
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function renderSidebar(){
  const list=document.getElementById('lsb-scripts');
  const st=list.scrollTop;list.innerHTML='';
  entries.forEach((e,i)=>{
    if(e.status==='SEP'){
      const row=el('div','sep-row');
      const lbl=el('span','sep-label');lbl.textContent=`── ${e.displayName||e.path} ──`;
      row.appendChild(lbl);
      if(e.sepFile){
        const fvuid='fv_'+e.uid,isOpen=panels.includes(fvuid);
        const btn=el('button','sep-btn eye-btn'+(isOpen?'':' closed'));
        btn.textContent='👁';btn.dataset.tip=`${e.sepFile.split('/').pop()} — toggle panel`;
        btn.onclick=g(ev=>{ev.stopPropagation();toggleFvPanel(fvuid,e.sepFile);});
        row.appendChild(btn);
      }
      row.onclick=ev=>{ev.stopPropagation();selectRow(i);};
      list.appendChild(row);return;
    }
    const row=el('div','erow');row.dataset.uid=e.uid;
    // Unreachable: grey ✕ dot, muted name, no action buttons (only edit via row click)
    if(e.unreachable){
      const dot=el('div','dot stop');dot.style.cssText='border-radius:2px;font-size:9px;display:flex;align-items:center;justify-content:center;color:var(--txt3);background:none;border:1px solid var(--txt3)';dot.textContent='✕';
      const nm=el('div','ename');nm.textContent=e.displayName||(e.path||'').split('/').pop();nm.title=e.path||'';nm.style.opacity='.4';
      const right=el('div','eright');
      const met=el('div','emet');met.innerHTML='<span style="font-size:10px;color:var(--txt3)">missing</span>';
      right.append(met);row.append(dot,nm,right);
      row.onclick=ev=>{ev.stopPropagation();selectRow(i);};
      list.appendChild(row);return;
    }
    const dot=el('div','dot '+dotCls(e));dot.dataset.pdot=e.uid;
    const nm=el('div','ename');nm.textContent=e.displayName||(e.path||'').split('/').pop();nm.title=e.path||'';
    const right=el('div','eright');
    const met=el('div','emet');met.dataset.smet=e.uid;
    if(e.pid){
      const cpu=(e.metrics&&e.metrics.cpu)||0;
      const ram=(e.metrics&&e.metrics.ram)||0;
      met.innerHTML=_fmtStats(cpu,ram);
    }
    if(e.status==='ON'){const star=el('span','efav');star.textContent='★';met.appendChild(star);}
    const acts=el('div','eacts');
    const btns=[eab('▶','Launch',g(()=>doLaunch(e.uid)))];
    if(e.hasUpdate) btns.push(eab('⟳','Run update',g(()=>doUpdate(e.uid))));
    if(e.hasInfo)   btns.push(eab('ℹ','Info',g(()=>toggleInfoPanel('info_'+e.uid,e.infoFile))));
    const isOpen=panels.includes(e.uid);
    const eyeBtn=eab('👁',isOpen?'Hide panel':'Show panel',g(()=>togglePanel(e.uid)));
    eyeBtn.classList.add('eye-btn');if(!isOpen)eyeBtn.classList.add('closed');
    btns.push(eyeBtn);
    btns.push(eab('■','Kill',g(()=>doKill(e.uid))));
    acts.append(...btns);right.append(met,acts);row.append(dot,nm,right);
    row.onclick=ev=>{ev.stopPropagation();selectRow(i);};
    list.appendChild(row);
  });
  _updateMaestroEye();list.scrollTop=st;
}

function _updateMaestroEye(){
  const btn=document.getElementById('tb-mpanel');if(!btn)return;
  const isOpen=panels.includes(MAESTRO_UID);
  btn.classList.toggle('active',isOpen);btn.classList.toggle('closed',!isOpen);
}

function patchMeta(){
  if(!_pageVisible)return;
  entries.forEach(e=>{
    document.querySelectorAll(`[data-pdot="${e.uid}"]`).forEach(d=>d.className='dot '+dotCls(e));
    document.querySelectorAll(`[data-smet="${e.uid}"]`).forEach(m=>{
      const cpu=(e.metrics&&e.metrics.cpu)||0;
      const ram=(e.metrics&&e.metrics.ram)||0;
      let html=e.pid?_fmtStats(cpu,ram):'';
      if(e.status==='ON') html+=`<span class="efav">★</span>`;
      m.innerHTML=html;
    });
  });
}

function patchPanelTitles(){
  entries.forEach(e=>{
    const p=document.querySelector(`.tp[data-uid="${CSS.escape(e.uid)}"]`);if(!p)return;
    const t=p.querySelector('.tp-title');if(!t)return;
    const name=e.displayName||(e.path||'').split('/').pop()||'Process';
    if(t.textContent!==name){t.textContent=name;t.title=e.path||'';}
  });
}

function _patchPanelColors(){
  entries.forEach(e=>{
    const p=document.querySelector(`.tp[data-uid="${CSS.escape(e.uid)}"]`);if(!p)return;
    _applyPanelColor(p,e.termColor||'default');
  });
}
function _applyPanelColor(panel,colorId){
  const c=TERM_COLORS[colorId]||TERM_COLORS.default;
  panel.style.background=c.bg;
  if(!panel.classList.contains('pcrash'))panel.style.borderColor=c.border;
  const t=panel.querySelector('.tp-title');if(t)t.style.color=c.title;
}

function selectRow(i){ selIdx=i; openEdit(i); }
function selectByUid(uid){ const i=uidIdx(uid);if(i<0)return;selIdx=i;updatePanelMeta(); }
function eab(label,tip,fn){
  const b=el('button','eab');b.textContent=label;b.title=tip;b.dataset.tip=tip;
  b.onclick=ev=>{ev.stopPropagation();fn();};return b;
}

// ── Grid ──────────────────────────────────────────────────────────────────────
function togglePanel(uid)       { panels.includes(uid)?hidePanel(uid):openPanel(uid); }
function toggleFvPanel(uid,fp)  { panels.includes(uid)?hidePanel(uid):openFvPanel(uid,fp); }
function toggleInfoPanel(uid,fp){ panels.includes(uid)?hidePanel(uid):openInfoPanel(uid,fp); }

function openPanel(uid){
  if(!panels.includes(uid)){
    if(panels.filter(u=>u!==EXPLORER_UID).length>=MAX_PANELS){setStatus(`Max ${MAX_PANELS} panels`);return;}
    panels.push(uid);
  }
  // renderGrid first so panel DOM exists before SSE log events arrive
  renderGrid();_savePanels();renderSidebar();
  if(!isFvPanel(uid)&&!isInfoPanel(uid)&&!sseConns[uid]) connectSSE(uid);
}
async function openFvPanel(uid,fp){
  if(!panels.includes(uid)) panels.push(uid);
  renderGrid();
  const d=await api('GET',`/api/read-file?path=${encodeURIComponent(fp)}`);
  const ta=document.querySelector(`[data-fvuid="${uid}"]`);
  if(ta){ta.value=d.error?`Error: ${d.error}`:(d.content||'');ta.dataset.fvpath=fp;}
  _savePanels();renderSidebar();
}
async function openInfoPanel(uid,fp){
  if(!panels.includes(uid)) panels.push(uid);
  renderGrid();
  const d=await api('GET',`/api/read-file?path=${encodeURIComponent(fp)}`);
  const ta=document.querySelector(`[data-infoid="${uid}"]`);
  if(ta) ta.value=d.error?`Error: ${d.error}`:(d.content||'');
  _savePanels();renderSidebar();
}
function hidePanel(uid){
  panels=panels.filter(u=>u!==uid);
  if(sseConns[uid]){sseConns[uid].close();delete sseConns[uid];}
  document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.remove();
  _savePanels();renderGrid();renderSidebar();
}

// Wide panel: span 2 in dominant grid direction
function toggleWidePanel(uid){
  if(_widePanels[uid]) delete _widePanels[uid]; else _widePanels[uid]=true;
  _saveWide();renderGrid();
}

// Columns: neat square-root layout capped at 6
function _calcCols(n){
  if(n<=1) return 1;
  if(n<=2) return 2;
  if(n<=4) return 2;
  if(n<=6) return 3;
  return Math.min(6,Math.ceil(Math.sqrt(n)));
}

function renderGrid(){
  const main=document.getElementById('main');
  const tv=document.getElementById('tab-view');
  if(activeTab) tv.classList.add('active'); else tv.classList.remove('active');

  // Explorer panel
  const hasExp=panels.includes(EXPLORER_UID);
  if(hasExp){
    if(!main.querySelector('.tp-exp')) main.appendChild(makeExplorerPanel());
    main.querySelector('.tp-exp').style.display='flex';
  } else { main.querySelector('.tp-exp')?.remove(); }

  const gp=panels.filter(u=>u!==EXPLORER_UID); // grid panels

  if(gp.length===0){
    main.style.gridTemplateColumns='1fr';
    main.style.gridTemplateRows='1fr';
    main.style.gridAutoRows='';
    if(!main.querySelector('.empty-state')){
      const es=el('div','empty-state');
      es.innerHTML='<img src="/img/MaestroLogo.svg" class="empty-logo" onerror="this.style.display=\'none\'">' +
        '<div>No terminals open.<br><small style="font-size:11px">Launch a script to open its panel.</small></div>';
      main.appendChild(es);
    }
    main.querySelectorAll('.tp:not(.tp-exp)').forEach(p=>p.remove());
    return;
  }
  main.querySelector('.empty-state')?.remove();

  const n=gp.length;
  const cols=_calcCols(n);
  // Extra slots consumed by wide panels (each takes 2 col-slots or 2 row-slots)
  const wideCount=gp.filter(u=>_widePanels[u]).length;
  const totalSlots=n+wideCount;           // wide panels eat an extra slot
  const rows=Math.ceil(totalSlots/cols);
  main.style.gridTemplateColumns=`repeat(${cols},1fr)`;
  main.style.gridTemplateRows=`repeat(${rows},1fr)`;
  main.style.gridAutoRows='1fr'; // safety net for overflow rows (wide panels)

  // Remove DOM panels no longer in list
  main.querySelectorAll('.tp:not(.tp-exp)').forEach(p=>{
    if(!gp.includes(p.dataset.uid)) p.remove();
  });

  // Save scroll positions
  const scroll={};
  gp.forEach(uid=>{ const b=document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"] .tp-body`); if(b)scroll[uid]=b.scrollTop; });

  // Create or re-append panels (re-append preserves event listeners)
  gp.forEach(uid=>{
    let p=main.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`);
    if(!p){
      if(uid===MAESTRO_UID||(!isFvPanel(uid)&&!isInfoPanel(uid))) p=makeTermPanel(uid);
      else if(isFvPanel(uid)) p=makeFvPanel(uid);
      else p=makeInfoPanel(uid);
    }
    // Apply wide span
    const wide=!!_widePanels[uid];
    if(wide && cols>=2) { p.style.gridColumn='span 2'; p.style.gridRow=''; }
    else if(wide && cols<2) { p.style.gridRow='span 2'; p.style.gridColumn=''; }
    else { p.style.gridColumn=''; p.style.gridRow=''; }
    // Sync wide button
    const wb=p.querySelector('.tp-btn-wide');
    if(wb) wb.classList.toggle('active',wide);
    main.appendChild(p);
  });

  requestAnimationFrame(()=>{
    gp.forEach(uid=>{
      const b=document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"] .tp-body`);if(!b)return;
      if(scroll[uid]!==undefined) b.scrollTop=scroll[uid]; else b.scrollTop=b.scrollHeight;
    });
  });
}

function updatePanelMeta(){
  if(!_pageVisible)return;
  entries.forEach(e=>{
    document.querySelectorAll(`[data-pdot="${e.uid}"]`).forEach(d=>d.className='dot '+dotCls(e));
    document.querySelectorAll(`[data-pmet="${e.uid}"]`).forEach(m=>{
      if(e.pid&&e.metrics) m.innerHTML=_fmtStats(e.metrics.cpu||0,e.metrics.ram||0); else m.innerHTML='';
    });
    document.querySelectorAll(`[data-smet="${e.uid}"]`).forEach(m=>{
      const cpu=(e.metrics&&e.metrics.cpu)||0,ram=(e.metrics&&e.metrics.ram)||0;
      let html=e.pid?_fmtStats(cpu,ram):'';
      if(e.status==='ON') html+=`<span class="efav">★</span>`;
      m.innerHTML=html;
    });
    const p=document.querySelector(`.tp[data-uid="${CSS.escape(e.uid)}"]`);
    if(p) p.classList.toggle('pcrash',!!e.crash);
  });
}

// ── Terminal panels ───────────────────────────────────────────────────────────
function makeTermPanel(uid){
  const isMaestro=uid===MAESTRO_UID;
  const e=isMaestro?null:(entries.find(x=>x.uid===uid)||null);
  const name=isMaestro?'Maestro Log':(e?.displayName||(e?.path||'').split('/').pop()||'Process');
  const colorId=e?.termColor||'default';
  const c=TERM_COLORS[colorId]||TERM_COLORS.default;

  const panel=el('div','tp'+(e?.crash?' pcrash':''));
  panel.dataset.uid=uid;
  panel.style.background=c.bg;
  panel.style.borderColor=c.border;
  if(_widePanels[uid]) panel.style.gridColumn='span 2'; // restore if recreated

  panel.addEventListener('click',ev=>{if(!isMaestro&&!ev.target.closest('button'))selectByUid(uid);});

  const hdr=el('div','tp-hdr');hdr.draggable=true;
  const dot=el('div',isMaestro?'dot run':'dot '+dotCls(e||{}));dot.dataset.pdot=uid;
  const title=el('div','tp-title');title.textContent=name;title.title=e?.path||'';title.style.color=c.title;
  const met=el('div','tp-met');met.dataset.pmet=uid;
  if(e?.pid&&e?.metrics) met.innerHTML=_fmtStats(e.metrics.cpu||0,e.metrics.ram||0);

  hdr.append(dot,title,met,tpbtn('⌫','Clear log',()=>doClearLog(uid)));
  if(!isMaestro){
    hdr.append(
      tpbtn('▶','Launch',()=>{ const e2=entries.find(x=>x.uid===uid);if(e2)doLaunch(e2.uid); }),
      tpbtn('■','Kill',  ()=>{ const e2=entries.find(x=>x.uid===uid);if(e2)doKill(e2.uid); })
    );
    const wb=el('button','tp-btn tp-btn-wide');
    wb.textContent='⊞';wb.title='Toggle double width';wb.dataset.tip='Toggle double width (span 2)';
    if(_widePanels[uid]) wb.classList.add('active');
    wb.onclick=()=>toggleWidePanel(uid);wb.draggable=false;
    hdr.append(wb);
  }
  hdr.append(tpbtn('👁','Hide panel',()=>hidePanel(uid)));
  attachDrag(panel,hdr,uid);

  const body=el('div','tp-body');
  let ms=false;
  body.addEventListener('scroll',()=>{ms=(body.scrollHeight-body.scrollTop-body.clientHeight)>60;});
  body._ms=()=>ms;
  const pre=el('pre','tp-pre');pre.dataset.ppre=uid;
  body.appendChild(pre);panel.append(hdr,body);
  return panel;
}

function makeFvPanel(uid){
  const e=entries.find(x=>x.uid===uid.replace(/^fv_/,''))||{};
  const fp=e.sepFile||'';const fn=fp.split('/').pop()||'File';
  const panel=el('div','tp');panel.dataset.uid=uid;
  const hdr=el('div','tp-hdr nodrag');hdr.draggable=false;
  const title=el('div','tp-title');title.textContent=`📄 ${fn}`;title.title=fp;
  hdr.append(title,tpbtn('💾','Save',()=>saveFv(uid)),
    tpbtn('↗','Open external',()=>api('POST','/api/open-editor',{path:fp})),
    tpbtn('👁','Hide',()=>hidePanel(uid)));
  const ta=document.createElement('textarea');
  ta.className='fv-textarea';ta.dataset.fvuid=uid;ta.dataset.fvpath=fp;ta.spellcheck=false;
  panel.append(hdr,ta);
  if(fp) api('GET',`/api/read-file?path=${encodeURIComponent(fp)}`).then(d=>{ta.value=d.error?`Error: ${d.error}`:(d.content||'');});
  return panel;
}
async function saveFv(uid){
  const ta=document.querySelector(`[data-fvuid="${uid}"]`);if(!ta)return;
  const r=await api('POST','/api/write-file',{path:ta.dataset.fvpath,content:ta.value});
  setStatus(r.ok?'Saved.':'Failed: '+(r.error||'?'));
}

function makeInfoPanel(uid){
  const e=entries.find(x=>x.uid===uid.replace(/^info_/,''))||{};
  const fp=e.infoFile||'';const fn=fp.split('/').pop()||'Info';
  const panel=el('div','tp');panel.dataset.uid=uid;
  const hdr=el('div','tp-hdr nodrag');hdr.draggable=false;
  const title=el('div','tp-title');title.textContent=`ℹ ${fn}`;title.title=fp;
  hdr.append(title,tpbtn('↗','Open external',()=>api('POST','/api/open-editor',{path:fp})),
    tpbtn('👁','Hide',()=>hidePanel(uid)));
  const ta=document.createElement('textarea');
  ta.className='fv-textarea';ta.dataset.infoid=uid;ta.readOnly=true;ta.spellcheck=false;
  panel.append(hdr,ta);
  if(fp) api('GET',`/api/read-file?path=${encodeURIComponent(fp)}`).then(d=>{ta.value=d.error?`Error: ${d.error}`:(d.content||'');});
  return panel;
}

function makeExplorerPanel(){
  const panel=el('div','tp tp-exp');panel.dataset.uid=EXPLORER_UID;
  const hdr=el('div','tp-hdr nodrag');hdr.draggable=false;
  const title=el('div','tp-title');title.textContent='📁 File Explorer';
  hdr.append(title,
    tpbtn('📂','Open Thunar',()=>api('POST','/api/open-app',{app:'thunar',path:_explorerPath||'/home'})),
    tpbtn('✕','Close',()=>hidePanel(EXPLORER_UID)));
  const exp=el('div','tp-explorer');
  const lp=el('div','exp-pane');
  const lh=el('div','exp-header');lh.id='exp-lh';lh.textContent='Select a folder';
  const ll=el('div','exp-list');ll.id='exp-ll';lp.append(lh,ll);
  const rp=el('div','exp-pane');
  const rh=el('div','exp-header');rh.id='exp-rh';rh.textContent='Select a file';
  const rw=el('div','exp-file-wrap');
  const rta=document.createElement('textarea');rta.className='exp-file-ta';rta.id='exp-ta';rta.spellcheck=false;
  const rtbar=el('div','exp-toolbar');
  const bSave=el('button','exp-btn');bSave.textContent='💾 Save';bSave.dataset.tip='Save file';
  bSave.onclick=async()=>{const p=rta.dataset.path;if(!p)return;const r=await api('POST','/api/write-file',{path:p,content:rta.value});setStatus(r.ok?'Saved.':'Failed: '+(r.error||'?'));};
  const bExt=el('button','exp-btn');bExt.textContent='↗ Open External';bExt.dataset.tip='Open in text editor';
  bExt.onclick=()=>{const p=rta.dataset.path;if(p)api('POST','/api/open-editor',{path:p});};
  rtbar.append(bSave,bExt);rw.append(rta,rtbar);rp.append(rh,rw);exp.append(lp,rp);
  panel.append(hdr,exp);
  if(_treeCurrentPath) loadExplorerDir(_treeCurrentPath);
  return panel;
}

function tpbtn(lbl,tip,fn){
  const b=el('button','tp-btn');b.textContent=lbl;b.title=tip;b.dataset.tip=tip;b.onclick=fn;b.draggable=false;return b;
}
function attachDrag(panel,hdr,uid){
  hdr.addEventListener('dragstart',ev=>{dragSrc=uid;ev.dataTransfer.effectAllowed='move';});
  panel.addEventListener('dragover',ev=>{ev.preventDefault();panel.classList.add('pdrop');});
  panel.addEventListener('dragleave',()=>panel.classList.remove('pdrop'));
  panel.addEventListener('drop',ev=>{
    ev.preventDefault();panel.classList.remove('pdrop');
    if(dragSrc&&dragSrc!==uid){
      const ai=panels.indexOf(dragSrc),bi=panels.indexOf(uid);
      if(ai>=0&&bi>=0){[panels[ai],panels[bi]]=[panels[bi],panels[ai]];renderGrid();}
    }dragSrc=null;
  });
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(uid){
  if(sseConns[uid]) sseConns[uid].close();
  const es=new EventSource(`/api/logs/${uid}`);sseConns[uid]=es;
  let replay=true;
  setTimeout(()=>{
    replay=false;
    // After replay window closes, scroll panel to bottom via rAF to ensure DOM is painted
    requestAnimationFrame(()=>{
      const body=document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"] .tp-body`);
      if(body) body.scrollTop=body.scrollHeight;
    });
  },1500);
  es.addEventListener('log',  ev=>appendLog(uid,ev.data,'',replay));
  es.addEventListener('clear',()=>document.querySelector(`[data-ppre="${uid}"]`)?.replaceChildren());
  es.addEventListener('crash',ev=>{
    appendLog(uid,`[MAESTRO] ⚠ Crashed (exit ${ev.data})`,'lc');
    document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.classList.add('pcrash');
    notify('Maestro: Crashed',(entries.find(x=>x.uid===uid)?.displayName)||uid);
    _pollSoon();
  });
  es.addEventListener('close_panel',()=>{hidePanel(uid);_pollSoon();});
  es.addEventListener('restart',()=>{
    appendLog(uid,'[MAESTRO] ↺ Restarting…','lm');
    document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.classList.remove('pcrash');
    _pollSoon();
  });
  es.onerror=()=>{};
}

function appendLog(uid,line,cls,replay=false){
  const pre=document.querySelector(`[data-ppre="${uid}"]`);if(!pre)return;
  const s=el('span','ll'+(cls?' '+cls:'')+(line.startsWith('[MAESTRO]')?' lm':'')+(line.startsWith('[UPDATE]')?' lu':''));
  s.textContent=line;pre.appendChild(s);
  const body=pre.parentElement;if(!body)return;
  if(!replay&&!body._ms?.()) requestAnimationFrame(()=>{if(body)body.scrollTop=body.scrollHeight;});
}

function notify(t,b){
  if(!('Notification'in window))return;
  Notification.permission==='granted'?new Notification(t,{body:b})
    :Notification.permission!=='denied'&&Notification.requestPermission().then(p=>p==='granted'&&new Notification(t,{body:b}));
}
'Notification'in window&&Notification.permission==='default'&&Notification.requestPermission();

async function pingUntilUp(url,maxMs=30000){
  const start=Date.now();
  while(Date.now()-start<maxMs){
    try{await fetch(url,{mode:'no-cors',signal:AbortSignal.timeout(1500)});return true;}
    catch{await new Promise(r=>setTimeout(r,2000));}
  }return false;
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function doLaunch(uid){
  _checkStale();
  const e=entries.find(x=>x.uid===uid);if(!e)return;
  await api('POST','/api/launch',{uid});
  if(!panels.includes(uid)){
    panels.push(uid);
    if(!e.launchHidden){
      renderGrid();_savePanels();renderSidebar();
    }
    connectSSE(uid);
  }
  (e.webapps||[]).forEach(async(wb,wi)=>{
    const wurl=wb.url||'';if(!wurl)return;
    const wuid=`${uid}_w${wi}`;
    setStatus(`Waiting for ${wurl}…`);
    const up=await pingUntilUp(wurl,30000);
    setStatus(up?`${wurl} ready`:`${wurl} did not respond`);
    if(up){_tabReady.add(wuid);if(activeTab===wuid)loadWebTab(wuid,wurl);}
  });
  _pollSoon();
}

async function doKill(uid){
  _checkStale();
  const e=entries.find(x=>x.uid===uid);if(!e)return;
  const r=await api('POST','/api/kill',{uid});
  if(r.error){setStatus('Kill failed: '+r.error);return;}
  (e.webapps||[]).forEach((_,wi)=>{
    const wuid=`${uid}_w${wi}`;_tabReady.delete(wuid);
    const ifr=tabIframes[wuid];if(ifr){ifr.style.opacity='0';ifr.src='about:blank';}
    const ph=document.getElementById('ph-'+wuid);if(ph)ph.style.display='flex';
  });
  hidePanel(uid);
  if(sseConns[uid]){sseConns[uid].close();delete sseConns[uid];}
  if(activeTab&&(activeTab===uid||(e.webapps||[]).some((_,wi)=>activeTab===`${uid}_w${wi}`))) closePill();
  _pollSoon();
}

async function doRemove(uid){
  const e=entries.find(x=>x.uid===uid);if(!e)return;
  const idx=entries.indexOf(e);
  if(!confirm(`Remove "${e.displayName||e.path.split('/').pop()}"?`))return;
  (e.webapps||[]).forEach((_,wi)=>_tabReady.delete(`${uid}_w${wi}`));
  hidePanel(uid);hidePanel('fv_'+uid);hidePanel('info_'+uid);
  if(activeTab===uid||(e.webapps||[]).some((_,wi)=>activeTab===`${uid}_w${wi}`)) closePill();
  await api('POST','/api/remove',{idx});
  if(selIdx===idx)selIdx=-1;else if(selIdx>idx)selIdx--;
  closeEdit();await poll();
}

async function doClearLog(uid){
  document.querySelector(`[data-ppre="${uid}"]`)?.replaceChildren();
  await api('POST','/api/clear-log',{uid});
}

async function doLaunchAllFav(){
  showLoad();
  const favs=entries.filter(e=>e.status==='ON'&&!e.unreachable);
  for(const e of favs){
    await api('POST','/api/launch',{uid:e.uid});
    // For internal scripts: open panel and connect SSE now
    if(!e.openOutside&&!panels.includes(e.uid)){
      panels.push(e.uid);
      if(!e.launchHidden){ renderGrid();_savePanels(); }
      connectSSE(e.uid);
    }
    await new Promise(r=>setTimeout(r,800));
  }
  hideLoad();
  // Give daemon time to assign PIDs, then force full sidebar re-render
  await new Promise(r=>setTimeout(r,600));
  await forcePoll();
  // Second pass after another short wait to catch any lagging PIDs
  await new Promise(r=>setTimeout(r,1500));
  await forcePoll();
}

async function doKillAll(){
  for(const e of entries) if(e.pid) await api('POST','/api/kill',{uid:e.uid});
  entries.filter(e=>e.pid).forEach(e=>hidePanel(e.uid));
  _pollSoon();
}

async function doSwitchOff(){
  if(!confirm('Kill all processes and shut down Maestro?'))return;
  Object.values(sseConns).forEach(es=>es.close());sseConns={};sessionStorage.clear();
  await api('POST','/api/shutdown');
  document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#6b5040;font-family:monospace">Maestro stopped.</div>';
}

async function doMove(idx,dir){
  const t=idx+dir;if(t<0||t>=entries.length)return;
  await api('POST','/api/reorder',{a:idx,b:t});
  if(selIdx===idx)selIdx=t;else if(selIdx===t)selIdx=idx;
  await poll();openEdit(selIdx>=0?selIdx:idx);
}

async function doUpdate(uid){
  const e=entries.find(x=>x.uid===uid);if(!e||e.status==='SEP')return;
  const r=await api('POST','/api/update-script',{uid});
  if(r.ok){if(!panels.includes(uid))openPanel(uid);setStatus('Update launched in external terminal');}
  else setStatus('Error: '+(r.error||'?'));
}

function doAddSep(){
  const name=prompt('Separator label:');if(name===null)return;
  api('POST','/api/add-sep',{name:name.trim()||'Separator',after:selIdx>=0?selIdx:entries.length-1}).then(()=>poll());
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function _fetchSettings(){
  const d=await api('GET','/api/settings');
  if(d&&!d.error){
    _settings=d;
    // Apply explorer visibility immediately on every fetch
    _applyExplorerSetting(!!_settings.fileExplorer);
  }
  return _settings;
}

function openSettings(){
  _fetchSettings().then(s=>{
    const body=document.getElementById('settings-modal-body');
    body.innerHTML='';

    // ── General section ────────────────────────────────────────────────────────
    const secGen=_smSection('General');
    secGen.appendChild(_smToggle('File Explorer','fileExplorer',!!s.fileExplorer,async v=>{
      await api('POST','/api/settings',{fileExplorer:v});
      _applyExplorerSetting(v);
      await _fetchSettings();
    }));
    secGen.appendChild(_smToggle('Logs Recorder','logsRecorder',!!s.logsRecorder,async v=>{
      await api('POST','/api/settings',{logsRecorder:v});
      _loggingEnabled=v;
      await _fetchSettings();
    }));
    secGen.appendChild(_smToggle('Autostart with OS','autostart',!!s.autostart,async v=>{
      await api('POST','/api/settings',{autostart:v});
      await _fetchSettings();
    }));
    secGen.appendChild(_smToggle('Open browser at launch','openBrowserAtLaunch',!!s.openBrowserAtLaunch,async v=>{
      await api('POST','/api/settings',{openBrowserAtLaunch:v});
      await _fetchSettings();
    }));
    secGen.appendChild(_smToggle('Tray persistent','trayPersistent',s.trayPersistent!==false,async v=>{
      await api('POST','/api/settings',{trayPersistent:v});
      await _fetchSettings();
    }));
    secGen.appendChild(_smToggle('OS Notifications','systemNotifications',s.systemNotifications!==false,async v=>{
      await api('POST','/api/settings',{systemNotifications:v});
      await _fetchSettings();
    }));
    body.appendChild(secGen);

    // ── Server section ─────────────────────────────────────────────────────────
    const secSrv=_smSection('Server (restart required)');
    const hostInp=_smInput('Host','text',s.host||'127.0.0.1');
    const portInp=_smInput('Port','number',String(s.port||8865));
    const warnDiv=el('div','sm-warn');warnDiv.style.display='none';
    warnDiv.textContent='⚠ Exposing Maestro on the network — no authentication is active. Use on trusted networks only.';
    function _checkHostWarn(){warnDiv.style.display=(hostInp.value.trim()!=='127.0.0.1')?'block':'none';}
    hostInp.addEventListener('input',_checkHostWarn);_checkHostWarn();
    const saveSrvBtn=el('button','rbtn sec');saveSrvBtn.textContent='Save Server Settings';
    saveSrvBtn.style.marginTop='4px';
    saveSrvBtn.onclick=async()=>{
      await api('POST','/api/settings',{host:hostInp.value.trim(),port:parseInt(portInp.value)||8865});
      await _fetchSettings();
      setStatus('Server settings saved — restart to apply');
    };
    secSrv.append(hostInp,portInp,warnDiv,saveSrvBtn);
    body.appendChild(secSrv);

    // ── Links section ──────────────────────────────────────────────────────────
    const secLinks=_smSection('Links');
    const linksRow=el('div','sm-links-row');
    const gh=el('a','sm-link');gh.href='https://github.com/AIdevelopmentPit/Maestro';gh.target='_blank';gh.textContent='⭐ Open on GitHub';
    const coffee=el('a','sm-btn');coffee.href='#';coffee.textContent='☕ Buy me a coffee';
    linksRow.append(gh,coffee);
    secLinks.appendChild(linksRow);
    body.appendChild(secLinks);

    // ── Footer ────────────────────────────────────────────────────────────────
    const foot=el('div','sm-footer');
    foot.textContent='This is an opensource, light, offline and privacy first tool made with QoL mindset for peace of mind';
    body.appendChild(foot);
  });

  document.getElementById('settings-backdrop').classList.add('show');
}

function closeSettings(){
  document.getElementById('settings-backdrop').classList.remove('show');
}

// Settings modal helpers
function _smSection(title){
  const w=el('div','sm-section');
  const t=el('div','sm-section-title');t.textContent=title;
  w.appendChild(t);return w;
}
function _smToggle(label,key,checked,onChange){
  const row=el('div','trow');
  const l=el('label','');l.textContent=label;
  const t=el('div','tog'+(checked?' on':''));t.dataset.on=String(checked);
  t.onclick=()=>{const v=t.dataset.on!=='true';t.dataset.on=String(v);t.classList.toggle('on',v);onChange(v);};
  row.append(l,t);return row;
}
function _smInput(label,type,value){
  const w=el('div','field');
  const l=el('label','');l.textContent=label;
  const i=el('input','finput');i.type=type;i.value=value;
  if(type==='number'){i.min='1';i.max='65535';}
  w.append(l,i);return i;  // return input directly for access
}

// ── Explorer ──────────────────────────────────────────────────────────────────
async function loadExplorerDir(path){
  _explorerPath=path;
  const d=await api('GET',`/api/browse?path=${encodeURIComponent(path)}&all=1`);
  if(d.error)return;
  const lh=document.getElementById('exp-lh'),ll=document.getElementById('exp-ll');
  if(!lh||!ll)return;
  lh.textContent=d.path;ll.innerHTML='';
  if(d.parent){const r=el('div','exp-item tup');r.innerHTML='📁 ..';r.onclick=()=>loadExplorerDir(d.parent);ll.appendChild(r);}
  d.dirs.forEach(n=>{const r=el('div','exp-item tdir');r.innerHTML=`📁 ${n}`;r.onclick=()=>loadExplorerDir(d.path+'/'+n);ll.appendChild(r);});
  d.files.forEach(n=>{const r=el('div','exp-item tfile');r.innerHTML=`📄 ${n}`;
    r.onclick=()=>{ll.querySelectorAll('.exp-item').forEach(x=>x.classList.remove('tsel'));r.classList.add('tsel');loadExpFile(d.path+'/'+n);};ll.appendChild(r);});
}
async function loadExpFile(path){
  const d=await api('GET',`/api/read-file?path=${encodeURIComponent(path)}`);
  const rh=document.getElementById('exp-rh'),rta=document.getElementById('exp-ta');
  if(!rh||!rta)return;
  rh.textContent=d.name||path;rta.value=d.error?'Error: '+d.error:(d.content||'');rta.dataset.path=d.error?'':path;
}
async function loadTree(path){
  _treeCurrentPath=path;
  const d=await api('GET',`/api/browse?path=${encodeURIComponent(path)}`);
  if(!d||d.error)return;
  const tEl=document.getElementById('lsb-tree'),pEl=document.getElementById('lsb-tree-path');
  if(!tEl)return;pEl.textContent=d.path;tEl.innerHTML='';
  if(d.parent){const r=el('div','tree-item tup');r.innerHTML='📁 ..';r.onclick=()=>loadTree(d.parent);tEl.appendChild(r);}
  (d.dirs||[]).forEach(name=>{
    const r=el('div','tree-item tdir');r.innerHTML=`📁 ${name}`;
    r.onclick=()=>{loadTree(d.path+'/'+name);if(!panels.includes(EXPLORER_UID))openPanel(EXPLORER_UID);else loadExplorerDir(d.path+'/'+name);};
    tEl.appendChild(r);
  });
}
loadTree('/home');

// ── File Explorer toggle ───────────────────────────────────────────────────────
// Controls visibility of:
//   - #lsb-divider       (resize handle between script list and tree)
//   - #lsb-tree-wrap     (sidebar file tree panel)
//   - EXPLORER_UID panel (main area file explorer overlay)
// When disabled: hides tree UI and closes the explorer panel if open.
// When enabled:  shows tree UI and reloads the current tree path.
function _applyExplorerSetting(enabled){
  const divider  = document.getElementById('lsb-divider');
  const treeWrap = document.getElementById('lsb-tree-wrap');
  if(divider)  divider.style.display  = enabled ? '' : 'none';
  if(treeWrap) treeWrap.style.display = enabled ? '' : 'none';
  if(!enabled && panels.includes(EXPLORER_UID)){
    hidePanel(EXPLORER_UID);
  }
  if(enabled && treeWrap && treeWrap.style.display !== 'none'){
    // Reload tree at current path or default
    loadTree(_treeCurrentPath || '/home');
  }
}

// ── Resizers ──────────────────────────────────────────────────────────────────
(function(){
  const div=document.getElementById('lsb-divider'),wrap=document.getElementById('lsb-tree-wrap');
  let drag=false,sy=0,sh=0;
  div.addEventListener('mousedown',e=>{drag=true;sy=e.clientY;sh=wrap.offsetHeight;e.preventDefault();});
  document.addEventListener('mousemove',e=>{if(!drag)return;wrap.style.height=Math.max(60,Math.min(500,sh+(sy-e.clientY)))+'px';});
  document.addEventListener('mouseup',()=>drag=false);
})();

(function(){
  const res=document.getElementById('lsb-resizer');if(!res)return;
  let drag=false,startX=0,startW=0;
  const saved=parseInt(localStorage.getItem('lsbW')||'0');
  if(saved>=120&&saved<=600) document.documentElement.style.setProperty('--sbar',saved+'px');
  res.addEventListener('mousedown',e=>{
    drag=true;startX=e.clientX;
    startW=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sbar'))||280;
    res.classList.add('dragging');
    document.getElementById('tab-view').style.pointerEvents='none';
    document.getElementById('main').style.pointerEvents='none';
    e.preventDefault();
  });
  document.addEventListener('mousemove',e=>{
    if(!drag)return;
    document.documentElement.style.setProperty('--sbar',Math.max(120,Math.min(600,startW+(e.clientX-startX)))+'px');
  });
  document.addEventListener('mouseup',()=>{
    if(!drag)return;drag=false;res.classList.remove('dragging');
    document.getElementById('tab-view').style.pointerEvents='';
    document.getElementById('main').style.pointerEvents='';
    const w=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sbar'))||280;
    try{localStorage.setItem('lsbW',w);}catch{}
  });
})();

// ── File browser ──────────────────────────────────────────────────────────────
async function openFileBrowser(pathInput,insertAfter,filter='sh'){
  document.getElementById('_fb')?.remove();
  const wrap=el('div','');wrap.id='_fb';
  const pathDiv=el('div','fb-path');pathDiv.textContent='Loading…';
  const box=el('div','fb-box');wrap.append(pathDiv,box);
  insertAfter.parentNode.insertBefore(wrap,insertAfter.nextSibling);
  async function load(path){
    let url=`/api/browse?path=${encodeURIComponent(path)}`;
    if(filter==='all')url+='&all=1';else if(filter==='md')url+='&md=1';
    const d=await api('GET',url);box.innerHTML='';
    if(d.error){const m=el('div','fb-item fmsg');m.textContent=d.error;box.appendChild(m);return;}
    _fbPath=d.path;pathDiv.textContent=d.path;
    if(d.parent){const r=el('div','fb-item fup');r.innerHTML='📁 ..';r.onclick=()=>load(d.parent);box.appendChild(r);}
    (d.dirs||[]).forEach(n=>{const r=el('div','fb-item fdir');r.innerHTML=`📁 ${n}`;r.onclick=()=>load(d.path+'/'+n);box.appendChild(r);});
    (d.files||[]).forEach(n=>{const r=el('div','fb-item ffile');r.innerHTML=`📄 ${n}`;r.onclick=()=>{pathInput.value=d.path+'/'+n;wrap.remove();};box.appendChild(r);});
    if(!d.dirs?.length&&!d.files?.length){const m=el('div','fb-item fmsg');m.textContent='Empty folder';box.appendChild(m);}
  }
  load(_fbPath||_treeCurrentPath||'/home');
}

// ── Colour picker ─────────────────────────────────────────────────────────────
function makeColorPicker(current,onChange){
  const wrap=el('div','');
  const lbl=el('label','');lbl.style.cssText='font-size:10px;color:var(--txt3);text-transform:uppercase;letter-spacing:.8px';
  lbl.textContent='Terminal colour';
  const sw=el('div','color-swatches');
  Object.entries(TERM_COLORS).forEach(([id,c])=>{
    const s=el('div','color-swatch'+(id===(current||'default')?' sel':''));
    s.title=c.label;s.style.background=id==='default'?'var(--bg2)':c.bg;
    s.style.borderColor=id===(current||'default')?c.title:'transparent';s.dataset.colorId=id;
    s.onclick=()=>{sw.querySelectorAll('.color-swatch').forEach(x=>{x.classList.remove('sel');x.style.borderColor='transparent';});s.classList.add('sel');s.style.borderColor=TERM_COLORS[id].title;onChange(id);};
    sw.appendChild(s);
  });
  wrap.append(lbl,sw);return wrap;
}

// ── Edit / Add panels ─────────────────────────────────────────────────────────
function doAdd(){
  openRsb('Add Script');
  const body=document.getElementById('rsb-body');body.innerHTML='';
  const pathF=rfield('Full path to .sh script','text','r-add-path','');
  const pathInp=pathF.querySelector('input');
  const btnBrowse=el('button','rbtn sec');btnBrowse.textContent='📂 Browse';
  btnBrowse.onclick=()=>openFileBrowser(pathInp,btnBrowse,'sh');
  const wbList=el('div','');wbList.id='r-wb-add';
  const btnWb=el('button','rbtn sec');btnWb.textContent='＋ Add WebApp URL';btnWb.onclick=()=>addWbEntry(wbList);
  body.append(rfield('Display name','text','r-add-name',''),pathF,btnBrowse,
    rinfo('Browse or type the full path.'),rtog('Launch hidden','r-add-lh',false),
    rinfo('WebApp URLs:'),wbList,btnWb,hr());
  const btnAdd=el('button','rbtn');btnAdd.textContent='Add Script';
  btnAdd.onclick=g(async()=>{
    const path=pathInp.value.trim(),name=document.getElementById('r-add-name').value.trim();
    const lh=document.getElementById('r-add-lh').dataset.on==='true';
    const webapps=collectWbEntries(wbList);
    if(!path){setStatus('Enter a path');return;}
    const r=await api('POST','/api/add',{path,name,launchHidden:lh,webapps});
    if(r.error)setStatus('Error: '+r.error);else{await poll();closeEdit();setStatus('Script added');}
  });
  body.append(btnAdd,rbtn('Cancel',closeEdit,true));
}

function addWbEntry(container,url=''){
  const idx=container.querySelectorAll('.wb-entry').length;
  const wrap=el('div','wb-entry');const rm=el('button','wb-rm');rm.textContent='✕';rm.onclick=()=>wrap.remove();
  const uf=rfield('WebApp URL','text',`r-wbu-${idx}`,url);uf.querySelector('input').placeholder='http://127.0.0.1:8188';
  wrap.append(rm,uf);container.appendChild(wrap);
}
function collectWbEntries(container){
  const res=[];
  container.querySelectorAll('.wb-entry').forEach(w=>{const u=w.querySelector('input[id^="r-wbu-"]')?.value.trim()||'';if(u)res.push({url:u});});
  return res;
}

function openEdit(idx){
  const e=entries[idx];if(!e)return;
  openRsb(e.status==='SEP'?'Edit Separator':'Edit Entry');
  const body=document.getElementById('rsb-body');body.innerHTML='';
  if(e.status==='SEP'){
    const sfF=rfield('Attached text file','text','r-sep-sf',e.sepFile||'');
    const sfInp=sfF.querySelector('input');
    const btnSfB=el('button','rbtn sec');btnSfB.textContent='📂 Browse text file';btnSfB.onclick=()=>openFileBrowser(sfInp,btnSfB,'all');
    body.append(rfield('Label','text','r-sep-lbl',e.displayName||e.path),sfF,btnSfB,hr(),
      rbtn('▲ Move Up',async()=>doMove(idx,-1),true),rbtn('▼ Move Down',async()=>doMove(idx,1),true),hr(),
      rbtn('Save',g(async()=>{
        const lbl=document.getElementById('r-sep-lbl').value.trim();
        const sf=document.getElementById('r-sep-sf').value.trim();
        if(lbl){await api('POST','/api/update-entry',{idx,label:lbl,sepFile:sf});await poll();}
      })),hr(),
      rbtn('✕ Remove',g(async()=>{
        if(!confirm('Remove?'))return;
        await api('POST','/api/remove',{idx});
        if(selIdx===idx)selIdx=-1;else if(selIdx>idx)selIdx--;
        closeEdit();await poll();
      }),false,'dng'));return;
  }
  let _pc=e.termColor||'default';
  const updF=rfield('Update script path','text','r-upd',e.updateScript||'');updF.querySelector('input').placeholder='(default: update.sh)';
  const btnUpdB=el('button','rbtn sec');btnUpdB.textContent='📂 Browse';btnUpdB.onclick=()=>openFileBrowser(updF.querySelector('input'),btnUpdB,'sh');
  const infoF=rfield('Info file (.md)','text','r-info',e.infoFile||'');infoF.querySelector('input').placeholder='(optional)';
  const btnInfoB=el('button','rbtn sec');btnInfoB.textContent='📂 Browse .md';btnInfoB.onclick=()=>openFileBrowser(infoF.querySelector('input'),btnInfoB,'md');
  const wbList=el('div','');wbList.id='r-wb-edit';(e.webapps||[]).forEach(wb=>addWbEntry(wbList,wb.url||''));
  const btnWb=el('button','rbtn sec');btnWb.textContent='＋ Add WebApp URL';btnWb.onclick=()=>addWbEntry(wbList);
  const cp=makeColorPicker(_pc,id=>{ _pc=id; });
  body.append(rinfo(e.path),rfield('Display name','text','r-name',e.name||''),
    rfield('Launch delay (s)','number','r-delay',e.delay),
    rtog('Favourite (Launch All)','r-fav',e.status==='ON'),
    rtog('Auto-restart on crash','r-ar',!!e.autoRestart),
    rtog('Do not close panel on crash','r-ko',e.keepOpen!==false),
    rtog('Launch hidden','r-lh',!!e.launchHidden),
    rtog('Open outside Maestro','r-oo',!!e.openOutside),
    cp,hr(),rinfo('Update script'),updF,btnUpdB,hr(),rinfo('Info file (.md)'),infoF,btnInfoB,hr(),
    rinfo('WebApp URLs:'),wbList,btnWb,hr(),
    rbtn('Save',g(async()=>{
      const name=document.getElementById('r-name').value.trim();
      const delay=Math.max(0,parseInt(document.getElementById('r-delay').value)||0);
      const fav=document.getElementById('r-fav').dataset.on==='true';
      const ar=document.getElementById('r-ar').dataset.on==='true';
      const ko=document.getElementById('r-ko').dataset.on==='true';
      const lh=document.getElementById('r-lh').dataset.on==='true';
      const oo=document.getElementById('r-oo').dataset.on==='true';
      const us=document.getElementById('r-upd').value.trim();
      const inf=document.getElementById('r-info').value.trim();
      const webapps=collectWbEntries(wbList);
      await api('POST','/api/update-entry',{idx,name,delay,autoRestart:ar,keepOpen:ko,
        launchHidden:lh,openOutside:oo,termColor:_pc,webapps,updateScript:us,infoFile:inf,status:fav?'ON':'OFF'});
      await poll();
    })),
    rbtn('▲ Move Up',async()=>doMove(idx,-1),true),
    rbtn('▼ Move Down',async()=>doMove(idx,1),true),
    rbtn('UPDATE',g(()=>doUpdate(e.uid)),true),hr(),
    rbtn('✕ Remove Entry',g(async()=>doRemove(e.uid)),false,'dng'));
}

function openRsb(t){ document.getElementById('rsb-title').textContent=t;document.getElementById('rsb').classList.add('open');document.getElementById('rsb-backdrop').classList.add('show'); }
function closeEdit(){ document.getElementById('rsb').classList.remove('open');document.getElementById('rsb-backdrop').classList.remove('show'); }
function rfield(lbl,type,id,val){ const w=el('div','field');const l=el('label','');l.textContent=lbl;l.htmlFor=id;const i=el('input','finput');i.type=type;i.id=id;i.value=val;w.append(l,i);return w; }
function rinfo(txt){ const d=el('div','finfo');d.textContent=txt;return d; }
function rtog(lbl,id,checked){
  const w=el('div','trow');const l=el('label','');l.textContent=lbl;
  const t=el('div','tog'+(checked?' on':''));t.id=id;t.dataset.on=String(checked);
  t.onclick=()=>{const v=t.dataset.on!=='true';t.dataset.on=String(v);t.classList.toggle('on',v);};
  w.append(l,t);return w;
}
function rbtn(lbl,fn,sec=false,extra=''){ const b=el('button','rbtn'+(sec?' sec':'')+(extra?' '+extra:''));b.textContent=lbl;b.onclick=fn;return b; }
function hr(){ const h=document.createElement('hr');h.className='sep-ln';return h; }

// ── Wiring ────────────────────────────────────────────────────────────────────
document.getElementById('btn-launch-fav').onclick=g(doLaunchAllFav);
document.getElementById('btn-kill-all').onclick  =g(doKillAll);
document.getElementById('btn-off').onclick       =g(doSwitchOff);
document.getElementById('tb-add').onclick        =doAdd;
document.getElementById('tb-sep').onclick        =doAddSep;
document.getElementById('rsb-close').onclick     =closeEdit;
document.getElementById('rsb-backdrop').onclick  =closeEdit;
document.getElementById('tb-settings').onclick   =openSettings;
document.getElementById('settings-modal-close').onclick=closeSettings;
document.getElementById('settings-backdrop').addEventListener('click',ev=>{if(ev.target===ev.currentTarget)closeSettings();});
document.getElementById('tb-mpanel').onclick     =g(()=>{togglePanel(MAESTRO_UID);_updateMaestroEye();});
document.getElementById('btn-terms').onclick     =g(()=>{if(activeTab!==null)closePill();});

// ── Boot ──────────────────────────────────────────────────────────────────────
_fetchSettings();
try{const a=sessionStorage.getItem('mat');if(a)activeTab=a;}catch{}
renderGrid();
panels.forEach(uid=>{
  if(!isWebapp(uid)&&!isFvPanel(uid)&&!isInfoPanel(uid)&&uid!==EXPLORER_UID) connectSSE(uid);
});
if(activeTab) renderTabView();
