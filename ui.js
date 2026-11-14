'use strict';
// Path: ui.js

const MAESTRO_UID  = '__maestro__';
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
let entries=[],selIdx=-1,panels=[],activeTab=null,dragSrc=null,_dragTabUid=null;
let _prevHash='',_loggingEnabled=true;
let _settings={};
let _pageVisible=!document.hidden;
const _seenTabs=new Set();   // webapp tab uids whose process is running and already handled by renderPills (lazy-tab bookkeeping)

// ── Log stream state (single multiplexed SSE, see connectLogStream) ────────────
// ONE EventSource (/api/logs-all) carries the logs of every entry. Lines are also
// kept client-side per uid so a panel opened later can be filled instantly.
const LOG_BUF_MAX    = 1000;   // lines kept per uid (matches the server's BUF_LINES)
const LOG_STALE_MS   = 15000;  // no event (log/ping) for this long = stream considered dead
const LOG_MIN_RETRY  = 3000;   // never reconnect the stream more often than this
let _logMux      = null;       // the single EventSource
let _lastLogMsg  = Date.now(); // timestamp of the last event received on it
let _logConnAt   = 0;          // timestamp of the last connectLogStream() call
const _logBuf    = {};         // uid -> [{t:line, c:cssClass}]
const _logPartial= {};         // uid -> text of the line being rewritten with '\r' (progress output), if any

// ── Stale connection watchdog state ─────────────────────────────────────────────
// Chromium (and Chromium-based browsers like Brave) throttles or pauses timers
// on background tabs regardless of Memory Saver settings, since it targets any
// non-focused tab. This silently stalls poll() and can drop SSE connections,
// making the UI look "frozen" (clicks on Launch/Kill appear to do nothing) until
// the page is manually refreshed. This watchdog detects the stall and recovers.
//
// Recovery is PERSISTENT, not "try 3 times and give up": real-world testing
// showed a single reload right after wake often still fails (the browser's
// network stack itself needs a beat to fully recover), and the working manual
// workaround was "refresh / stop / refresh a few times" — i.e. keep retrying.
// So the watchdog does a short fast-retry burst, then falls back to a slow
// indefinite retry (every SLOW_RETRY_MS) for as long as the tab stays visible,
// with a clickable footer message for an immediate manual retry at any point.
const STALE_MS       = 6000;   // ms without a successful poll before we consider the UI stale
const API_TIMEOUT_MS = 6000;   // hard ceiling for every api() fetch, via AbortSignal.timeout —
                                // without this a fetch riding a half-dead post-resume connection
                                // could hang far longer than STALE_MS and never let the watchdog
                                // even start retrying.
const FAST_TRIES     = 4;      // quick attempts right after staleness is detected
const FAST_GAP_MS    = 1500;   // gap between fast attempts
const SLOW_RETRY_MS  = 15000;  // gap between attempts once in the persistent slow phase
const NOTIFY_AFTER_MS= 30000;  // OS notification only for interruptions at least this long
const QUICK_PROBE_MS = 2500;   // silent first attempt before any overlay is shown
let _lastPollOk = Date.now();    // timestamp of the last successful /api/state response
let _lastPollNetErr = false;     // true = last poll failed with a real fetch error; false = just stale (throttled timer)
let _staleActive = false;        // guards against overlapping reconnect attempts
let _staleNotified = false;      // guards against spamming an OS notification on every slow-retry attempt
let _reconnectTimer = null;      // setTimeout id for the current pending reconnect attempt
let _staleStartedAt = 0;         // when the current interruption started

let _widePanels={};
try{ _widePanels=JSON.parse(localStorage.getItem('tpw')||'{}'); }catch{}
function _saveWide(){ try{localStorage.setItem('tpw',JSON.stringify(_widePanels));}catch{} }

let _tabOrder=[];
try{ _tabOrder=JSON.parse(localStorage.getItem('mto')||'[]'); }catch{}
function _saveTabOrder(){ try{localStorage.setItem('mto',JSON.stringify(_tabOrder));}catch{} }

function _savePanels(){ try{sessionStorage.setItem('mp',JSON.stringify(panels));}catch{} }
function _loadPanels(){ try{const p=sessionStorage.getItem('mp');if(p)panels=JSON.parse(p).filter(u=>u!=='__explorer__');}catch{} }  // drop the removed explorer panel from old sessions
_loadPanels();

document.addEventListener('visibilitychange',()=>{
  _pageVisible=!document.hidden;
  if(_pageVisible){ _ensureLogStream(false); poll(); _checkStale(); }
});

// ── Tab-alive: background keepalive (silent audio) + screen wake lock ─────────
// Chromium freezes a hidden tab after ~5 min (JS, timers and sockets all stop);
// an audio-playing tab is exempt, which is what the near-silent oscillator below
// is for (S1). Browsers refuse to START audio before a user gesture, so the
// context is created at load and resumed on the first click/key/touch, and
// re-armed on pageshow/focus/visibilitychange. Nothing in JS survives a real OS
// sleep, and no in-page trick can keep a frozen tab running — the wake lock (S3)
// prevents the machine from idling to sleep WHILE the tab is visible; the browser
// releases it automatically when the tab hides, so it is re-requested on return.
// Both behaviours are user-controlled (Settings: tabKeepalive / screenWakeLock)
// and their state is shown by the dot in the footer (#ftr-ka).
let _kaCtx=null,_kaOsc=null,_kaWanted=false,_kaHinted=false;
let _wlSentinel=null,_wlWanted=false,_wlPending=false;
let _episodes=[];                       // today's connection interruptions (see _recordEpisode)
try{ _episodes=JSON.parse(localStorage.getItem('mep')||'[]'); }catch{}

function _kaState(){
  if(!_kaWanted) return 'off';
  if(!(window.AudioContext||window.webkitAudioContext)) return 'unsupported';
  return (_kaCtx&&_kaCtx.state==='running')?'active':'pending';
}
function _kaStart(){
  if(!_kaWanted) return;
  if(_kaCtx&&_kaCtx.state==='running') return;   // hot path: key/click listeners call this constantly
  try{
    const AC=window.AudioContext||window.webkitAudioContext;
    if(!AC){ _updateKaIndicator(); return; }
    if(!_kaCtx){
      _kaCtx=new AC();
      _kaCtx.onstatechange=_updateKaIndicator;
      const osc=_kaCtx.createOscillator(),gain=_kaCtx.createGain();
      gain.gain.value=0.0001;     // effectively silent, non-zero on purpose: avoids any
                                  // "no audible output" auto-suspend heuristic
      osc.frequency.value=20000;  // also outside the normal hearing range
      osc.connect(gain).connect(_kaCtx.destination);
      osc.start();_kaOsc=osc;
    }
    if(_kaCtx.state==='suspended') _kaCtx.resume().catch(()=>{});
  }catch(e){ _kaCtx=null; _kaOsc=null; }
  _updateKaIndicator();
}
function _kaStop(){
  try{ _kaOsc&&_kaOsc.stop(); }catch{}
  try{ _kaCtx&&_kaCtx.close(); }catch{}
  _kaCtx=null;_kaOsc=null;_updateKaIndicator();
}

async function _wlAcquire(){
  if(!_wlWanted||_wlSentinel||_wlPending||document.hidden) return;
  if(!('wakeLock' in navigator)) return;          // unsupported, or not a secure context
  _wlPending=true;
  try{
    const sent=await navigator.wakeLock.request('screen');
    _wlSentinel=sent;
    sent.addEventListener('release',()=>{ if(_wlSentinel===sent) _wlSentinel=null; _updateKaIndicator(); });
  }catch(e){ /* refused (e.g. battery saver): retried on the next visibility change */ }
  _wlPending=false;
  _updateKaIndicator();
}
function _wlRelease(){
  const sent=_wlSentinel;_wlSentinel=null;
  if(sent) sent.release().catch(()=>{});
  _updateKaIndicator();
}

function _episodeSummary(){
  const day=new Date().toDateString();
  const today=_episodes.filter(x=>new Date(x.t).toDateString()===day);
  if(!today.length) return 'No connection interruptions today';
  const last=new Date(today[today.length-1].t);
  const hh=String(last.getHours()).padStart(2,'0')+':'+String(last.getMinutes()).padStart(2,'0');
  return `Today: ${today.length} interruption${today.length>1?'s':''} (${today.filter(x=>x.s).length} visible), last ${hh}`;
}
function _recordEpisode(ms,shown){
  const day=new Date().toDateString();
  _episodes=_episodes.filter(x=>new Date(x.t).toDateString()===day);
  _episodes.push({t:Date.now(),ms,s:shown?1:0});
  try{ localStorage.setItem('mep',JSON.stringify(_episodes.slice(-50))); }catch{}
  _updateKaIndicator();
}

function _updateKaIndicator(){
  const d=document.getElementById('ftr-ka');if(!d) return;
  const st=_kaState();
  d.className=st==='active'?'ok':(st==='pending'?'warn':'');
  const kaTxt={
    active:'active (an inaudible tone keeps this tab from being frozen in the background)',
    pending:'NOT active yet — click once anywhere in this tab',
    off:'off (Settings)',unsupported:'not supported by this browser'}[st];
  const wlTxt=!_wlWanted?'off (Settings)':!('wakeLock' in navigator)?'unavailable (needs http://127.0.0.1 or https)':(_wlSentinel?'held':'not held');
  d.dataset.tip=`Tab keepalive: ${kaTxt} · Screen wake lock: ${wlTxt} · ${_episodeSummary()}`;
  if(st==='pending'&&!_kaHinted){ _kaHinted=true; setStatus('Click once in this tab to enable the background keepalive'); }
}

// Applied every time settings are (re)fetched, so toggles take effect immediately.
function _applyTabAliveSettings(){
  _kaWanted=_settings.tabKeepalive!==false;
  _wlWanted=_settings.screenWakeLock!==false;
  if(_kaWanted) _kaStart(); else _kaStop();
  if(_wlWanted) _wlAcquire(); else _wlRelease();
  _updateKaIndicator();
}
['click','keydown','touchstart','pointerdown'].forEach(ev=>document.addEventListener(ev,_kaStart,{passive:true}));
window.addEventListener('pageshow',()=>{ _kaStart(); _wlAcquire(); });
window.addEventListener('focus',()=>{ _kaStart(); _wlAcquire(); });
document.addEventListener('visibilitychange',()=>{ _kaStart(); if(!document.hidden) _wlAcquire(); });

// DOM refs needed by poll callbacks — must be initialized before any async poll result
const fQ=document.getElementById('ftr-quote');
const fUrl=document.getElementById('ftr-url');
const fT=document.getElementById('ftr-tip');
const fOv=document.getElementById('ftr-overlay');

// ── Utilities ─────────────────────────────────────────────────────────────────
function g(fn){ let t=0; return function(...a){const n=Date.now();if(n-t<500)return;t=n;fn(...a);}; }
function el(tag,cls){ const e=document.createElement(tag); if(cls)e.className=cls; return e; }
function dotCls(e){ return e.crash?'crash':e.pid?'run':'stop'; }
function uidIdx(uid){ return entries.findIndex(e=>e.uid===uid); }
function isAlwaysValid(uid){ return uid===MAESTRO_UID; }
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
    // signal: AbortSignal.timeout(...) guarantees this promise always settles within
    // API_TIMEOUT_MS, even if the underlying connection is stalled — see the watchdog
    // state comment above for why this matters after a browser tab suspend/resume.
    const o={method,headers:{'Content-Type':'application/json'},signal:AbortSignal.timeout(API_TIMEOUT_MS)};
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
  // Per-process RAM has no system total to compare against; the colour scale
  // uses a fixed 4 GB reference (purely cosmetic).
  const pct=Math.min(v/4096*100,100);
  return `RAM ${_resNum(_toGB(v),pct)}`;
}
function _fmtStats(cpu,ram){ return `${_fmtCPU(cpu)} | ${_fmtRAM(ram)}`; }

// ── HTML escaping (Fix #5 — stored XSS via unescaped innerHTML) ───────────────
// Any value that can come from server/API data (webapp URLs, file/dir names,
// tooltip text) must be escaped before being interpolated into an innerHTML
// template literal. Plain-text-only insertions use textContent instead and
// don't need this.
function _esc(s){
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

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
  if(d&&d.entries){ _lastPollOk=Date.now(); _lastPollNetErr=false; _setConnText('— CONNECTED —'); }
  else { _lastPollNetErr=true; _checkStale(); }   // no-op until STALE_MS of silence; also covers a visible tab
  if(!d||!d.entries) return;
  entries=d.entries;
  if(d.loggingEnabled!==undefined) _loggingEnabled=d.loggingEnabled;

  const h=_mkHash(entries);
  if(h!==_prevHash){ _prevHash=h; renderSidebar(); }
  else { patchMeta(); }

  _autoSuspendIdleTabs();
  renderPills();
  updatePanelMeta();
  patchPanelTitles();
  _patchPanelColors();
  updateFooterForActiveTab();
  _updateMaestroEye();

  // Remove panels whose entry no longer exists in config at all
  const validUIDs=new Set([
    ...entries.map(e=>e.uid),
    ...entries.flatMap(e=>(e.webapps||[]).map((_,wi)=>`${e.uid}_w${wi}`)),
    MAESTRO_UID,
    ...entries.filter(e=>e.sepFile).map(e=>'fv_'+e.uid),
    ...entries.filter(e=>e.infoFile).map(e=>'info_'+e.uid),
  ]);
  // Use a soft remove that doesn't close SSE — just clean panels array
  // hidePanel would close SSE prematurely on first poll after browser refresh
  const stalePanels=panels.filter(uid=>!isAlwaysValid(uid)&&!validUIDs.has(uid));
  if(stalePanels.length){
    stalePanels.forEach(uid=>{
      panels=panels.filter(u=>u!==uid);
      document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.remove();
    });
    _savePanels();
    renderGrid();
    renderSidebar();
  }

  // Self-heal the single log stream (no-op while it is healthy).
  _ensureLogStream(false);
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
  if(d&&d.entries){ _lastPollOk=Date.now(); _lastPollNetErr=false; _setConnText('— CONNECTED —'); } else { _lastPollNetErr=true; }
  if(!d||!d.entries) return;
  entries=d.entries;
  if(d.loggingEnabled!==undefined) _loggingEnabled=d.loggingEnabled;
  // Force full re-render ignoring hash
  _prevHash='';
  renderSidebar();
  renderPills();
  updatePanelMeta();
  patchPanelTitles();
  _patchPanelColors();
  updateFooterForActiveTab();
  _updateMaestroEye();
}

setInterval(()=>{ if(_pageVisible)poll(); },2000);
poll();



// ── Footer ────────────────────────────────────────────────────────────────────

function ftrDefault(){ fQ.classList.remove('hide');fT.classList.remove('show');if(!activeTab||!isWebapp(activeTab))fUrl.classList.remove('show'); }
// Fix #5: ftrTip is only ever given plain text (tooltip strings from
// data-tip attributes, which are static in ui.html/ui.js) — use textContent,
// not innerHTML, so this can never become an XSS vector even if a future
// caller passes untrusted text.
function ftrTip(text){ fQ.classList.add('hide');fUrl.classList.remove('show');fT.textContent=text;fT.classList.add('show'); }
function setStatus(m){
  const s=document.getElementById('ftr-status');
  s.textContent=m;s._tmp=true;clearTimeout(s._t);
  s._t=setTimeout(()=>{s._tmp=false;s.textContent='';updateFooterForActiveTab();},3500);
}

// ── Real connection status text (replaces the old static footer quote) ──────
// Shown in #ftr-quote whenever no connection-notification overlay is active
// and no webapp-tab URL/tooltip is being displayed — so the user can tell
// at a glance, before clicking anything, whether the tab is actually live.
function _setConnText(text){ fQ.textContent=text; }
_setConnText('— CONNECTING… —');

// ── Full-footer overlay (traffic-light connection notifications) ────────────
// Covers #ftr-inner completely with a flat, bold, uppercase, centered message
// in a color that maps to severity — green=reconnected, yellow=tab
// suspended/reconnecting, red=server unreachable/give-up-and-retry. Replaces
// the old small colored text in the corner (#ftr-status), which was too easy
// to miss entirely.
function _connOverlay(msg,{color='red',clickable=false,duration=0,onClick=null}={}){
  clearTimeout(fOv._t);
  fOv.className='show ov-'+color+(clickable?' clickable':'');
  fOv.textContent=msg;
  fOv.onclick=clickable?onClick:null;
  if(duration>0){
    fOv._t=setTimeout(()=>{ _hideConnOverlay(); },duration);
  }
}
function _hideConnOverlay(){
  clearTimeout(fOv._t);
  fOv.className='';fOv.textContent='';fOv.onclick=null;
}

// (old _footerAlert — small colored corner text — removed; connection-state
// notifications now use the full-footer _connOverlay() above. #ftr-status
// remains only for short one-off action feedback via setStatus(), e.g.
// "Saved.", "Launch failed: ...".)

// ── Stale connection watchdog ─────────────────────────────────────────────────
// Called on tab visibility return and before Launch/Kill actions. Checks the
// age of the last successful poll; if too old, alerts the user in the footer
// and starts a persistent reconnect cycle (see _reconnectCycle below).
async function _checkStale(){
  if(_staleActive) return;
  if(Date.now()-_lastPollOk<STALE_MS) return;
  _staleActive=true;
  _staleStartedAt=Date.now();
  _ensureLogStream(false);
  // Silent first attempt (S5): right after a wake/resume a single fresh snapshot
  // usually clears the problem, and then no red/yellow flash is ever shown.
  await Promise.race([forcePoll(),new Promise(r=>setTimeout(r,QUICK_PROBE_MS))]);
  if(Date.now()-_lastPollOk<STALE_MS){
    _staleActive=false;
    _recordEpisode(Date.now()-_staleStartedAt,false);
    return;
  }
  const msg=_lastPollNetErr
    ?'SERVER NON RAGGIUNGIBILE — RICONNESSIONE…'
    :'TAB SOSPESA DAL BROWSER — RICONNESSIONE…';
  _connOverlay(msg,{color:_lastPollNetErr?'red':'yellow'});
  await _reconnectCycle(0);
}

// One step of the reconnect cycle. `attempt` is 0-based during the fast phase;
// once attempt >= FAST_TRIES we're in the slow, indefinite phase.
//
// Fast phase (attempt 0..FAST_TRIES-1): quick attempts FAST_GAP_MS apart — the
// first fetch right after a browser un-suspends a tab often still rides a
// half-dead connection, a couple of quick retries usually clears that.
//
// Slow phase (attempt >= FAST_TRIES): retries every SLOW_RETRY_MS indefinitely,
// with a clickable footer message for an immediate manual retry. The OS
// notification fires only once the episode has lasted NOTIFY_AFTER_MS (S5):
// shorter interruptions just update the footer.
//
// The loop pauses (does not give up) if the tab goes back to background —
// _staleActive is released so a fresh _checkStale() restarts it on return.
async function _reconnectCycle(attempt){
  if(!_pageVisible){ _staleActive=false; return; }
  await forcePoll();
  const recovered=(Date.now()-_lastPollOk)<STALE_MS;
  if(recovered){ _onReconnected(); return; }

  if(attempt<FAST_TRIES-1){
    _reconnectTimer=setTimeout(()=>_reconnectCycle(attempt+1),FAST_GAP_MS);
    return;
  }
  if(!_staleNotified&&Date.now()-_staleStartedAt>=NOTIFY_AFTER_MS){
    _staleNotified=true;
    notify('Maestro — connessione persa',
      'La tab non risponde da un po\u2019. Riconnessione automatica in corso in background.');
  }
  _connOverlay('RICONNESSIONE IN CORSO — CLIC PER RIPROVARE SUBITO',{
    color:'red',clickable:true,
    onClick:()=>{ clearTimeout(_reconnectTimer); _reconnectCycle(FAST_TRIES); },
  });
  _reconnectTimer=setTimeout(()=>_reconnectCycle(attempt+1),SLOW_RETRY_MS);
}

function _onReconnected(){
  clearTimeout(_reconnectTimer);_reconnectTimer=null;
  // Only notify "reconnected" if "connection lost" was notified in this episode.
  if(_staleNotified) notify('Maestro — riconnesso', 'La connessione è stata ripristinata.');
  _staleNotified=false;
  // The log stream is a single connection that heals itself (see
  // _ensureLogStream): nothing more to do for it here.
  // Webapp iframes (Jellyfin, Tunarr, ComfyUI, ...) don't use SSE and can't
  // self-heal from Maestro's side: a dead connection inside one of them just sits
  // there broken. Reload only the tab the user is looking at; every other loaded
  // tab goes back to the paused state and reloads when clicked, so a reconnection
  // never triggers several heavy page loads at once.
  Object.keys(tabIframes).forEach(uid=>{
    if(!isWebTabLoaded(uid)) return;
    const m=uid.match(/^(.+)_w(\d+)$/);if(!m)return;
    const e=entries.find(x=>x.uid===m[1]);
    if(!e||!e.pid) return;
    if(uid===activeTab) reloadWebTab(uid);
    else pauseWebTab(uid,tabIframes[uid].dataset.origUrl||'');
  });
  // Green only now: forcePoll() has just returned fresh data.
  _connOverlay('RICONNESSO!',{color:'green',duration:2500});
  _staleActive=false;
  _recordEpisode(Date.now()-_staleStartedAt,true);
}

function updateFooterForActiveTab(){
  // BUGFIX: this runs every ~2s via poll()'s loop, completely independent of
  // mouse state. It used to unconditionally hide #ftr-tip and restore the
  // default quote/webapp-url text — so a tooltip shown via ftrTip() while
  // hovering would get yanked away mid-hover the next time poll() fired,
  // even though the mouse never left the element (mouseout never fired).
  // Skip entirely while a tooltip is actively shown; the mouseout handler
  // is what's responsible for restoring the footer once the hover ends.
  if(fT.classList.contains('show')) return;
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
  // Lazy webapp tabs: a tab whose process just started is PAUSED — nothing is loaded
  // (and no request reaches the webapp) until the user clicks it or its ▶. A tab whose
  // process stopped is blanked and forgotten, so its next launch starts paused again.
  [..._seenTabs].filter(u=>!cur.has(u)).forEach(u=>{ _seenTabs.delete(u); forgetWebTab(u); });
  pills.forEach(p=>{
    if(_seenTabs.has(p.uid)) return;
    _seenTabs.add(p.uid);
    if(p.uid===activeTab) _wakeTab(p.uid,p.wb.url||'');   // active tab restored after a page refresh: load it
    else{ _suspendedTabs.add(p.uid); _autoPaused.add(p.uid); }
  });
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
  const pill=el('div','tab tab-web'+(isActive?' active':'')+(_suspendedTabs.has(uid)?' tab-paused':''));
  pill.draggable=true;pill.dataset.tabUid=uid;pill.dataset.tip=`🌐 ${label} — ${url}`;
  const dot=el('div','tab-dot '+dcls);dot.dataset.pdot=e.uid;
  const nm=el('span','tab-name');nm.textContent=`🌐 ${label}`;nm.style.pointerEvents='none';
  const btnRef=el('button','tab-ibtn');btnRef.textContent='↺';btnRef.title='Reload';
  btnRef.onclick=ev=>{ev.stopPropagation();reloadWebTab(uid);};
  const susp=_suspendedTabs.has(uid);
  const btnSusp=el('button','tab-ibtn');
  btnSusp.textContent=susp?'▶':'⏸';btnSusp.title=susp?(_autoPaused.has(uid)?'Load (paused)':'Resume'):'Suspend';
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

// Load a paused / not-yet-loaded webapp tab (ping until the app answers, then set the iframe src).
function _wakeTab(uid,url){ _autoPaused.delete(uid); _resumeTab(uid,url); }

// Auto-pause: a loaded webapp tab that has not been the visible tab for
// webAppAutoSuspendMin minutes goes back to the paused state (its page is blanked,
// freeing the memory the page held); clicking the tab loads it again. 0 = never.
// Runs from poll(), i.e. only while the Maestro tab itself is visible.
const _tabLastSeen={};   // webuid -> timestamp it was last the visible tab
function _autoSuspendIdleTabs(){
  const now=Date.now();
  if(activeTab) _tabLastSeen[activeTab]=now;
  const mins=Number(_settings.webAppAutoSuspendMin??10);
  if(!(mins>0)) return;
  Object.keys(tabIframes).forEach(uid=>{
    if(uid===activeTab||!isWebTabLoaded(uid)||_suspendedTabs.has(uid)) return;
    if(!_tabLastSeen[uid]){ _tabLastSeen[uid]=now; return; }
    if(now-_tabLastSeen[uid]>=mins*60000){
      pauseWebTab(uid,tabIframes[uid].dataset.origUrl||'');
      setStatus(`⏸ A webapp tab was paused after ${mins} min unused — click it to load it again`);
    }
  });
}

function clickPill(uid,e,wb){
  if(activeTab===uid){
    if(_suspendedTabs.has(uid)){ if(_autoPaused.has(uid)) _wakeTab(uid,wb.url||''); return; }  // manual suspend: use its Resume button
    reloadWebTab(uid);return;
  }
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
  // First click on a paused tab loads it (never loaded before the click)
  if(_autoPaused.has(uid)||(!_suspendedTabs.has(uid)&&!isWebTabLoaded(uid))) _wakeTab(uid,url);
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
    if(e.pid&&!e.openOutside){
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
      let html=(e.pid&&!e.openOutside)?_fmtStats(cpu,ram):'';
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
    if(panels.length>=MAX_PANELS){setStatus(`Max ${MAX_PANELS} panels`);return;}
    panels.push(uid);
  }
  // The panel is filled from the client-side log buffer on creation (makeTermPanel)
  renderGrid();_savePanels();renderSidebar();
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

  const gp=panels.slice(); // grid panels

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
    main.querySelectorAll('.tp').forEach(p=>p.remove());
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
  main.querySelectorAll('.tp').forEach(p=>{
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
      if(e.pid&&e.metrics&&!e.openOutside) m.innerHTML=_fmtStats(e.metrics.cpu||0,e.metrics.ram||0); else m.innerHTML='';
    });
    document.querySelectorAll(`[data-smet="${e.uid}"]`).forEach(m=>{
      const cpu=(e.metrics&&e.metrics.cpu)||0,ram=(e.metrics&&e.metrics.ram)||0;
      let html=(e.pid&&!e.openOutside)?_fmtStats(cpu,ram):'';
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
  if(e?.pid&&e?.metrics&&!e?.openOutside) met.innerHTML=_fmtStats(e.metrics.cpu||0,e.metrics.ram||0);

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
  const hb=_logBuf[uid];
  if(hb&&hb.length){
    const frag=document.createDocumentFragment();
    hb.forEach(x=>frag.appendChild(_mkLogSpan(x.t,x.c)));
    pre.appendChild(frag);
  }
  if(_logPartial[uid]){ const sp=el('span','ll lp');sp.textContent=_logPartial[uid];pre.appendChild(sp); }
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

// ── SSE (single multiplexed log stream) ───────────────────────────────────────
// ONE EventSource (/api/logs-all) for every entry, instead of one per open panel:
// browsers allow only 6 concurrent HTTP/1.1 connections per origin, and with 6+
// open panels the SSE connections used to starve /api/state (the "constant
// disconnect/reconnect" symptom — see README "Connection resilience").
// Event data is JSON {u: uid, d: payload}. The server sends `reset` before every
// (re)subscription replay of a uid, so a reconnect can never duplicate lines.
function _mkLogSpan(line,cls){
  const s=el('span','ll'+(cls?' '+cls:'')+(line.startsWith('[MAESTRO]')?' lm':'')+(line.startsWith('[UPDATE]')?' lu':''));
  s.textContent=line;return s;
}
function _bufPush(uid,line,cls){
  const b=_logBuf[uid]||(_logBuf[uid]=[]);
  b.push({t:line,c:cls||''});
  if(b.length>LOG_BUF_MAX+200) b.splice(0,b.length-LOG_BUF_MAX); // trim in batches, not on every line
}

// The line a program is rewriting with '\r' (x1 → x2 → x3 …) is ONE span, always the last
// child of the panel's <pre>, whose text is replaced on every update — exactly what a
// terminal shows. Finished lines are inserted before it; when the program ends the line
// the server drops the partial and sends it as a normal `log` line.
function _partialSpan(pre){
  const l=pre.lastElementChild;
  return (l&&l.classList.contains('lp'))?l:null;
}
function setPartial(uid,txt){
  if(txt) _logPartial[uid]=txt; else delete _logPartial[uid];
  const pre=document.querySelector(`[data-ppre="${uid}"]`);if(!pre) return;
  let sp=_partialSpan(pre);
  if(!txt){ sp?.remove(); return; }
  if(!sp){ sp=el('span','ll lp'); pre.appendChild(sp); }
  sp.textContent=txt;
  const body=pre.parentElement;
  if(body&&!body._ms?.()) requestAnimationFrame(()=>{ if(body) body.scrollTop=body.scrollHeight; });
}

function connectLogStream(){
  if(_logMux){ try{_logMux.close();}catch{} }
  _logConnAt=Date.now();_lastLogMsg=Date.now();
  const es=new EventSource('/api/logs-all');_logMux=es;
  let replay=true;
  setTimeout(()=>{
    if(_logMux!==es) return;
    replay=false;
    // After the replay window closes, scroll panels to the bottom once painted
    requestAnimationFrame(()=>{
      document.querySelectorAll('.tp .tp-body').forEach(b=>{ if(!b._ms?.()) b.scrollTop=b.scrollHeight; });
    });
  },1500);
  const on=(name,fn)=>es.addEventListener(name,ev=>{
    _lastLogMsg=Date.now();
    if(name==='ping') return;
    let m=null;try{m=JSON.parse(ev.data);}catch{}
    if(m&&typeof m.u==='string') fn(m.u,m.d);
  });
  es.addEventListener('ping',()=>{_lastLogMsg=Date.now();});
  on('reset',uid=>{ _logBuf[uid]=[];delete _logPartial[uid];document.querySelector(`[data-ppre="${uid}"]`)?.replaceChildren(); });
  on('gone', uid=>{ delete _logBuf[uid];delete _logPartial[uid]; });
  on('partial',(uid,txt)=>setPartial(uid,txt));
  on('log',  (uid,line)=>appendLog(uid,line,'',replay));
  on('crash',(uid,rc)=>{
    appendLog(uid,`[MAESTRO] ⚠ Crashed (exit ${rc})`,'lc');
    document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.classList.add('pcrash');
    // Browser notification only for panels the user has open (the tray already
    // notifies natively for every crash)
    if(panels.includes(uid)) notify('Maestro: Crashed',(entries.find(x=>x.uid===uid)?.displayName)||uid);
    _pollSoon();
  });
  on('finished',()=>{ _pollSoon(); });   // clean exit (code 0): not a crash, the server already logged it
  on('close_panel',uid=>{ if(panels.includes(uid)) hidePanel(uid); _pollSoon(); });
  on('restart',uid=>{
    appendLog(uid,'[MAESTRO] ↺ Restarting…','lm');
    document.querySelector(`.tp[data-uid="${CSS.escape(uid)}"]`)?.classList.remove('pcrash');
    _pollSoon();
  });
  es.onerror=()=>{};   // EventSource retries by itself; _ensureLogStream covers a stream that stays dead
}

function _closeLogStream(){
  if(_logMux){ try{_logMux.close();}catch{} _logMux=null; }
}

// Reconnect the stream only when it is actually unhealthy (or `force`): closed,
// or silent for LOG_STALE_MS (the server pings every ~5s when idle, so silence
// means a dead socket, e.g. after a system sleep/resume). Reconnecting replays
// every buffer, so it must not happen needlessly.
function _ensureLogStream(force){
  if(Date.now()-_logConnAt<LOG_MIN_RETRY) return;
  const dead=!_logMux||_logMux.readyState===2||(Date.now()-_lastLogMsg)>LOG_STALE_MS;
  if(force||dead) connectLogStream();
}

function appendLog(uid,line,cls,replay=false){
  _bufPush(uid,line,cls);
  const pre=document.querySelector(`[data-ppre="${uid}"]`);if(!pre)return;
  const sp=_partialSpan(pre),span=_mkLogSpan(line,cls);
  if(sp) pre.insertBefore(span,sp); else pre.appendChild(span);
  // Cap the panel DOM: one <span> per line is the main RAM cost of a chatty process
  // left running for days. Trimmed in batches, same limit as the line buffers.
  if(pre.childElementCount>LOG_BUF_MAX+200){ for(let i=0;i<200;i++) pre.removeChild(pre.firstChild); }
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
  const r=await api('POST','/api/launch',{uid});
  if(r&&r.error){setStatus('Launch failed: '+r.error);return;}
  // Optimistic UI update: right after a tab wakes from background sleep, the
  // browser's per-origin connection pool can still be draining/reopening SSE
  // sockets, so the very next /api/state poll's
  // fetch can queue behind them and resolve a beat late. Without this, the
  // sidebar/panel dot stayed grey until a SECOND click happened to land after
  // that delayed poll finally came back — even though the launch itself
  // already succeeded server-side on the first click. Set a truthy
  // placeholder PID immediately so the dot flips to "run" right away; the
  // next real poll overwrites entries[] wholesale with the true state anyway
  // (correcting back to stopped if the launch actually failed server-side
  // after this point).
  e.pid=e.pid||-1;
  document.querySelectorAll(`[data-pdot="${uid}"]`).forEach(d=>d.className='dot run');
  if(!panels.includes(uid)){
    panels.push(uid);
    if(!e.launchHidden){
      renderGrid();_savePanels();renderSidebar();
    }
  }
  // Webapp tabs always (re)start PAUSED: nothing is loaded, and no request is sent
  // to the webapp, until the user clicks the tab. If the user is looking at one of
  // them right now (relaunch), it reloads instead.
  (e.webapps||[]).forEach((wb,wi)=>{
    const wuid=`${uid}_w${wi}`;
    _seenTabs.add(wuid);
    if(activeTab===wuid){ _suspendedTabs.delete(wuid);_autoPaused.delete(wuid);reloadWebTab(wuid); }
    else pauseWebTab(wuid,wb.url||'');
  });
  _pollSoon();
}

async function doKill(uid){
  _checkStale();
  const e=entries.find(x=>x.uid===uid);if(!e)return;
  const r=await api('POST','/api/kill',{uid});
  if(r.error){setStatus('Kill failed: '+r.error);return;}
  // Optimistic UI update — same reasoning as doLaunch() above.
  e.pid=null;e.crash=false;
  document.querySelectorAll(`[data-pdot="${uid}"]`).forEach(d=>d.className='dot stop');
  (e.webapps||[]).forEach((_,wi)=>{
    const wuid=`${uid}_w${wi}`;_seenTabs.delete(wuid);forgetWebTab(wuid);
  });
  hidePanel(uid);
  if(activeTab&&(activeTab===uid||(e.webapps||[]).some((_,wi)=>activeTab===`${uid}_w${wi}`))) closePill();
  _pollSoon();
}

async function doRemove(uid){
  const e=entries.find(x=>x.uid===uid);if(!e)return;
  const idx=entries.indexOf(e);
  if(!confirm(`Remove "${e.displayName||e.path.split('/').pop()}"?`))return;
  (e.webapps||[]).forEach((_,wi)=>{ const wuid=`${uid}_w${wi}`; _seenTabs.delete(wuid); forgetWebTab(wuid); });
  hidePanel(uid);hidePanel('fv_'+uid);hidePanel('info_'+uid);
  if(activeTab===uid||(e.webapps||[]).some((_,wi)=>activeTab===`${uid}_w${wi}`)) closePill();
  const r=await api('POST','/api/remove',{idx});
  if(r&&r.error){setStatus('Remove failed: '+r.error);return;}
  if(selIdx===idx)selIdx=-1;else if(selIdx>idx)selIdx--;
  closeEdit();await poll();
}

async function doClearLog(uid){
  document.querySelector(`[data-ppre="${uid}"]`)?.replaceChildren();
  _logBuf[uid]=[];delete _logPartial[uid];
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
  _closeLogStream();sessionStorage.clear();
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

// ── Update Maestro (Settings modal) ───────────────────────────────────────────
// Applies a full Maestro update from GitHub — either a channel ("release":
// the latest tagged GitHub Release, labeled "Latest" in the UI; or "main":
// main branch HEAD, labeled "Pre-release") or an explicit tag (any released
// version, including older ones — used by CHECK ALL VERSIONS for rollbacks).
// See main.py's /api/update-maestro and tray.py's _perform_update()/_reexec_self()
// for the server-side download/stage/swap/restart sequence. This stops ALL
// running managed scripts (full-stack restart, by design).
async function _runMaestroInstall(payload,label){
  // NOTE: this deliberately does NOT use api() — api()'s AbortSignal.timeout
  // is hardcoded to API_TIMEOUT_MS (6s), tuned for ordinary fast calls. The
  // server-side work here (download + extract + validate a GitHub zip) can
  // legitimately take well beyond 6s, and a client-side abort at 6s was
  // silently killing every update attempt with no visible error — the fetch
  // rejected, the fallback status message flashed for only 3.5s in the small
  // footer text, and it looked exactly like "clicking Update does nothing".
  _connOverlay(`DOWNLOAD ${label.toUpperCase()} IN CORSO… PUÒ RICHIEDERE FINO A UN MINUTO`,
    {color:'yellow'});

  let r;
  try{
    const res=await fetch('/api/update-maestro',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload),
      signal:AbortSignal.timeout(120000),
    });
    r=await res.json();
  }catch(ex){
    console.error('Update Maestro request failed:',ex);
    _connOverlay('AGGIORNAMENTO FALLITO: '+String(ex),{color:'red',duration:8000});
    return;
  }

  if(r&&r.ok){
    _closeLogStream();sessionStorage.clear();
    document.body.innerHTML=
      '<div style="display:flex;align-items:center;justify-content:center;height:100vh;'+
      'color:#6b5040;font-family:monospace;text-align:center;line-height:2">'+
      'Updating Maestro…<br><small style="font-size:12px">'+
      'Restarting to apply the update — reload this page in a few seconds.</small></div>';
  } else {
    console.error('Update Maestro failed:',r);
    _connOverlay('AGGIORNAMENTO FALLITO: '+((r&&r.error)||'?'),{color:'red',duration:8000});
  }
}

async function doUpdateMaestro(channel){
  const label=channel==='release'?'Latest (tagged release)':'Pre-release (main branch)';
  if(!confirm(
    `Update Maestro to ${label}?\n\n`+
    `This will stop ALL running scripts and restart Maestro. `+
    `No automatic backup is kept.`
  )) return;
  await _runMaestroInstall({channel},channel);
}

// Tiny client-side version compare (mirrors main.py's _cmp_versions) — only
// used to warn the user when CHECK ALL VERSIONS would install an OLDER tag.
function _verT(v){const p=String(v||'').replace(/^[vV]/,'').split(/[.\-+]/);return [0,1,2].map(i=>{const n=parseInt(p[i],10);return Number.isFinite(n)?n:0;});}
function _cmpVer(a,b){const ta=_verT(a),tb=_verT(b);for(let i=0;i<3;i++){if(ta[i]!==tb[i])return ta[i]<tb[i]?-1:1;}return 0;}

// CHECK ALL VERSIONS row: first click fetches every installable GitHub release
// (GET /api/update-check-all, sorted newest-first) and turns the row into a
// version dropdown + an "INSTALL THIS VERSION" button; clicking it again
// installs the selected tag — even an older one (rollback).
function _buildAllVersionsRow(){
  const wrap=el('div','');wrap.style.cssText='display:flex;flex-direction:column;gap:4px';
  const btn=el('button','sm-btn');
  btn.textContent='CHECK ALL VERSIONS';
  btn.dataset.state='check';
  const selWrap=el('div','');selWrap.style.display='none';
  const sel=el('select','finput');
  let localVer='';
  selWrap.appendChild(sel);

  btn.onclick=async()=>{
    if(btn.dataset.state==='install'){
      const tag=(sel.value||'').trim();
      if(!tag) return;
      const rollback=localVer&&_cmpVer(tag,localVer)<0;
      if(!confirm(
        `Install Maestro v${tag}?\n\n`+
        (rollback?'⚠ This is a ROLLBACK to an older version.\n\n':'')+
        `This will stop ALL running scripts and restart Maestro. No automatic backup is kept.`
      )) return;
      await _runMaestroInstall({tag},'v'+tag);
      return;
    }
    btn.disabled=true;btn.textContent='Checking…';
    try{
      const r=await api('GET','/api/update-check-all');
      if(!r||!r.ok) throw new Error((r&&r.error)||'unknown error');
      localVer=r.localVersion||'';
      sel.innerHTML='';
      (r.releases||[]).forEach(rel=>{
        const o=document.createElement('option');
        o.value=rel.tag;
        let suffix='';
        if(rel.isCurrent) suffix+=' (current)';
        if(rel.prerelease) suffix+=' (pre-release)';
        o.textContent=`v${rel.version}${suffix}`;
        sel.appendChild(o);
      });
      // List is sorted newest-first, so the default selection is the newest.
      btn.dataset.state='install';
      btn.textContent='INSTALL THIS VERSION';
      selWrap.style.display='block';
    }catch(ex){
      console.error('All-versions check failed:',ex);
      btn.textContent='CHECK ALL VERSIONS';
      _connOverlay('CHECK ALL VERSIONS FALLITO: '+String(ex),{color:'red',duration:6000});
    }finally{
      btn.disabled=false;
    }
  };

  wrap.append(btn,selWrap);
  return wrap;
}


// Builds one row of the Update Maestro section: a single button that starts
// as "CHECK <LABEL>", runs GET /api/update-check?channel=... on click (using
// main.py's server-side githubToken — works against a private repo, unlike
// a client-side GitHub API call), shows the result message below, and — only
// if an update is actually available — turns into a highlighted "UPDATE NOW"
// button that triggers the real update on its next click. Nothing runs
// automatically; the check only fires on the user's first click, per-channel.
function _buildUpdateRow(channel,label){
  const wrap=el('div','');wrap.style.cssText='display:flex;flex-direction:column;gap:4px';
  const btn=el('button','sm-btn');
  btn.textContent=`CHECK ${label.toUpperCase()}`;
  btn.dataset.state='check';
  const msg=el('div','finfo');msg.textContent='';

  btn.onclick=async()=>{
    if(btn.dataset.state==='update'){ doUpdateMaestro(channel); return; }
    const prevLabel=btn.textContent;
    btn.disabled=true;btn.textContent='Checking…';
    try{
      const r=await api('GET',`/api/update-check?channel=${channel}`);
      if(r&&r.ok){
        msg.textContent=r.message||'';
        if(r.available){
          btn.dataset.state='update';
          btn.textContent='⬆ UPDATE NOW';
          btn.style.background='var(--grn)';btn.style.color='#0a1a0a';btn.style.borderColor='var(--grn)';
        }else{
          btn.dataset.state='check';
          btn.textContent=`CHECK ${label.toUpperCase()}`;
          btn.style.background='';btn.style.color='';btn.style.borderColor='';
        }
      }else{
        console.error('Update check failed:',r);
        msg.textContent='Check failed: '+((r&&r.error)||'?');
        btn.textContent=prevLabel;
      }
    }catch(ex){
      console.error('Update check failed:',ex);
      msg.textContent='Check failed: '+String(ex);
      btn.textContent=prevLabel;
    }finally{
      btn.disabled=false;
    }
  };

  wrap.append(btn,msg);
  return wrap;
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function _fetchSettings(){
  const d=await api('GET','/api/settings');
  if(d&&!d.error){
    _settings=d;
    _applyTabAliveSettings();
  }
  return _settings;
}

function openSettings(){
  _fetchSettings().then(s=>{
    const body=document.getElementById('settings-modal-body');
    body.innerHTML='';

    // ── Support section (moved to the top, no section title, per project owner's request) ──
    const secSupport=el('div','sm-section');
    const supportRow=el('div','sm-links-row');
    const btnSupport=el('a','sm-btn');
    // Opens in a new browser tab; rel=noopener keeps the external page from touching this one.
    btnSupport.href='https://liberapay.com/pellaDev/donate';btnSupport.target='_blank';btnSupport.rel='noopener noreferrer';
    btnSupport.textContent='💛 Donate on Liberapay';
    const btnStar=el('a','sm-btn');
    btnStar.href='https://github.com/pellaDev/Maestro';btnStar.target='_blank';btnStar.rel='noopener noreferrer';
    btnStar.textContent='⭐ Star me on GitHub';
    // NOTE: GitHub does not offer any URL/deep-link that stars a repo on
    // click — starring is an authenticated POST action with no anonymous
    // equivalent. This opens the repo page (where the visitor can star it
    // themselves in one click) rather than actually starring it automatically.
    supportRow.append(btnSupport,btnStar);
    const supportDesc=el('div','sm-footer');
    supportDesc.textContent='Maestro is developed and maintained by only one person, please consider to donate if you are using it on a daily basis.';
    secSupport.append(supportRow,supportDesc);
    body.appendChild(secSupport);

    // ── General section ────────────────────────────────────────────────────────
    const secGen=_smSection('General');
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
    secGen.appendChild(_smToggle('Background tab keepalive','tabKeepalive',s.tabKeepalive!==false,async v=>{
      await api('POST','/api/settings',{tabKeepalive:v});
      await _fetchSettings();
    }));
    secGen.appendChild(_smToggle('Keep screen awake while visible','screenWakeLock',s.screenWakeLock!==false,async v=>{
      await api('POST','/api/settings',{screenWakeLock:v});
      await _fetchSettings();
    }));
    const autoSusp=_smInput('Auto-pause unused webapp tabs after (minutes, 0 = never)','number',String(s.webAppAutoSuspendMin??10));
    autoSusp.min='0';autoSusp.max='1440';   // _smInput defaults to the port range
    autoSusp.addEventListener('change',async()=>{
      const v=Math.max(0,Math.min(1440,parseInt(autoSusp.value)||0));
      autoSusp.value=String(v);
      const r=await api('POST','/api/settings',{webAppAutoSuspendMin:v});
      if(r&&r.error){setStatus('Save failed: '+r.error);return;}
      await _fetchSettings();
    });
    secGen.appendChild(autoSusp._wrap);
    const tabTip=el('div','finfo');
    tabTip.textContent='Browser tip: pin this tab (right-click the tab → Pin tab) and add 127.0.0.1 to the browser\u2019s Memory Saver exclusions (Chrome/Brave: chrome://settings/performance) so it is never frozen or discarded in the background.';
    secGen.appendChild(tabTip);
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
      const r=await api('POST','/api/settings',{host:hostInp.value.trim(),port:parseInt(portInp.value)||8865});
      if(r&&r.error){setStatus('Save failed: '+r.error);return;}
      await _fetchSettings();
      setStatus('Server settings saved — restart to apply');
    };
    secSrv.append(hostInp._wrap,portInp._wrap,warnDiv,saveSrvBtn);
    body.appendChild(secSrv);

    // ── Update Maestro section ─────────────────────────────────────────────────
    // Latest (tagged release) and Pre-release (main branch) each get their
    // own independent check — nothing runs automatically on modal open; the
    // check for a channel only fires the first time its button is clicked
    // (see _buildUpdateRow above).
    const secUpd=_smSection('Update Maestro');
    // Fix #2: the server never sends the real token back (see main.py's
    // /api/settings GET handler) — only a `githubTokenSet` boolean. Show a
    // placeholder reflecting that instead of expecting/echoing the real value,
    // and don't overwrite a saved token with a blank unless the user actually
    // typed something new.
    const tokenInp=_smInput('GitHub Token (only needed while the repo is private)','password','');
    tokenInp.placeholder = s.githubTokenSet
      ? '•••••••• (already set — leave blank to keep)'
      : 'ghp_… (leave empty once the repo is public)';
    const btnSaveToken=el('button','rbtn sec');btnSaveToken.textContent='Save Token';
    btnSaveToken.style.marginTop='4px';
    btnSaveToken.onclick=async()=>{
      const v=tokenInp.value.trim();
      if(!v && s.githubTokenSet){ setStatus('Token unchanged.'); return; }
      await api('POST','/api/settings',{githubToken:v});
      await _fetchSettings();
      setStatus('GitHub token saved.');
    };
    const updWarn=el('div','sm-warn');
    updWarn.textContent='⚠ Updating stops ALL running scripts and restarts Maestro. No automatic backup is kept.';
    // Auto-update: at startup, if this setting is on, tray.py compares the local
    // badge version against the latest tagged GitHub Release ("release" channel
    // only) and applies it when local is older — see tray.py's
    // _maybe_auto_update(). It never auto-installs pre-releases.
    const autoUpdTog=_smToggle('Auto-update to latest stable at startup','autoUpdate',!!s.autoUpdate,async v=>{
      const r=await api('POST','/api/settings',{autoUpdate:v});
      if(r&&r.error){setStatus('Save failed: '+r.error);return;}
      await _fetchSettings();
    });
    secUpd.append(
      tokenInp._wrap,btnSaveToken,updWarn,autoUpdTog,
      _buildUpdateRow('release','Latest'),
      _buildUpdateRow('main','Pre-release'),
      _buildAllVersionsRow(),
    );
    body.appendChild(secUpd);

    // ── Backup / Restore section ───────────────────────────────────────────────
    // Export/import of the full Maestro state: script list (with SEP rows) and
    // settings.json. Import replaces everything, stops all running scripts, and
    // starts every imported entry as OFF — see main.py's /api/export and
    // /api/import handlers.
    const secBak=_smSection('Backup / Restore');
    const bakDesc=el('div','');
    bakDesc.style.cssText='font-size:11px;color:var(--txt2);line-height:1.5';
    bakDesc.textContent='Export or import the full Maestro state: script list (with SEP rows) and settings. Import replaces everything, stops all running scripts, and every imported entry starts as OFF.';
    const btnExp=el('button','rbtn sec');btnExp.textContent='⬇ EXPORT STATE';
    const btnImp=el('button','rbtn sec');btnImp.textContent='⬆ IMPORT STATE';
    const bakBtns=el('div','');bakBtns.style.cssText='display:flex;gap:8px';
    bakBtns.append(btnExp,btnImp);
    const fileInp=el('input','');fileInp.type='file';fileInp.accept='.json,application/json';fileInp.style.display='none';
    btnExp.onclick=async()=>{
      try{
        const res=await fetch('/api/export');
        const r=await res.json();
        if(!r||r.app!=='Maestro') throw new Error('export failed');
        const blob=new Blob([JSON.stringify(r,null,2)],{type:'application/json'});
        const url=URL.createObjectURL(blob);
        const a=document.createElement('a');
        const d=new Date();
        const stamp=`${d.getFullYear()}${String(d.getMonth()+1).padStart(2,'0')}${String(d.getDate()).padStart(2,'0')}`;
        a.href=url;a.download='maestro-backup-'+stamp+'.json';
        document.body.appendChild(a);a.click();a.remove();
        URL.revokeObjectURL(url);
      }catch(ex){
        console.error('Export state failed:',ex);
        setStatus('Export failed: '+String(ex));
      }
    };
    btnImp.onclick=()=>fileInp.click();
    fileInp.onchange=async()=>{
      const f=fileInp.files&&fileInp.files[0];
      if(!f) return;
      let bundle;
      try{ bundle=JSON.parse(await f.text()); }catch(ex){ setStatus('Import failed: not valid JSON'); return; }
      if(!bundle||bundle.app!=='Maestro'){ setStatus('Import failed: not a Maestro backup file'); return; }
      if(!confirm(
        'Import this Maestro state?\n\n'+
        'This REPLACES the current script list and settings, stops ALL running scripts, and every imported entry starts as OFF.'
      )){ fileInp.value=''; return; }
      try{
        const r=await api('POST','/api/import',bundle);
        if(!r||!r.error){
          _closeLogStream();sessionStorage.clear();
          _prevHash='';
          await _fetchSettings();
          closeSettings();
          poll();
        } else {
          setStatus('Import failed: '+r.error);
        }
      }catch(ex){
        console.error('Import state failed:',ex);
        setStatus('Import failed: '+String(ex));
      }
      fileInp.value='';
    };
    secBak.append(bakDesc,bakBtns,fileInp);
    body.appendChild(secBak);
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
// Toggle rows use a <span> for the label, not <label>, because the actual
// control is a custom <div class="tog"> — not a real labelable form element
// (input/select/textarea/button). A <label for="..."> pointing at a <div>
// is invalid HTML and shows up as a Chrome DevTools "Issues" accessibility
// warning ("No label associated with a form field"). A <span> makes the
// same visual result without the false association.
function _smToggle(label,key,checked,onChange){
  const row=el('div','trow');
  const l=el('span','trow-label');l.textContent=label;
  const t=el('div','tog'+(checked?' on':''));t.dataset.on=String(checked);
  t.onclick=()=>{const v=t.dataset.on!=='true';t.dataset.on=String(v);t.classList.toggle('on',v);onChange(v);};
  row.append(l,t);return row;
}
function _smInput(label,type,value){
  const w=el('div','field');
  const l=el('label','');l.textContent=label;
  const i=el('input','finput');i.type=type;i.value=value;
  // Real <input> here, so give it an id and associate the label properly —
  // this is what was missing and triggering the accessibility warning.
  i.id='sm-inp-'+label.toLowerCase().replace(/[^a-z0-9]+/g,'-');
  l.htmlFor=i.id;
  if(type==='number'){i.min='1';i.max='65535';}
  w.append(l,i);
  i._wrap=w;  // BUGFIX: w (containing the visible label) was created but never
              // returned/appended anywhere — callers only ever got the bare
              // input back, so every _smInput field rendered with no visible
              // label at all. Host/Port got away with it because position and
              // typical values made them guessable; the new GitHub Token field
              // did not. Callers must append `i._wrap`, not `i` itself, to get
              // the labeled version. `i` is still returned directly so
              // `.value` reads/writes at call sites keep working unchanged.
  return i;
}

// ── Resizers ──────────────────────────────────────────────────────────────────
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

// ── Native file / folder picker ───────────────────────────────────────────────
// A web page cannot read the real path of a file chosen with <input type=file>, and
// Maestro needs real paths. So main.py opens the operating system's own file dialog
// (POST /api/pick-path) and returns the chosen path. Deliberately NOT via api(): the
// dialog stays open as long as the user likes, so the 6s API timeout must not apply.
let _lastPickDir='';
try{ _lastPickDir=localStorage.getItem('lpd')||''; }catch{}

async function pickPath(kind,{title='',filter='any',start=''}={}){
  let r;
  try{
    const res=await fetch('/api/pick-path',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind,title,filter,start:start||_lastPickDir})});
    r=await res.json();
  }catch(ex){ _connOverlay('FILE DIALOG FAILED: '+String(ex),{color:'red',duration:6000}); return null; }
  if(!r||!r.ok){ _connOverlay((r&&r.error)||'FILE DIALOG FAILED',{color:'red',duration:9000}); return null; }
  if(r.cancelled||!r.path) return null;
  try{
    _lastPickDir=kind==='folder'?r.path:r.path.replace(/[\\/][^\\/]*$/,'');
    localStorage.setItem('lpd',_lastPickDir);
  }catch{}
  return r.path;
}

// Replaces a "type the path" text field: a button that opens the OS dialog, the chosen
// path shown below it, and a hidden <input id=...> that keeps the value so existing
// code can keep reading document.getElementById(id).value. `filter` is one of
// 'launchable' | 'sh' | 'md' | 'any' (see main.py _PICK_FILTERS).
function makePathPicker(label,id,value,{kind='file',filter='any',optional=false}={}){
  const w=el('div','field');
  const l=el('label','');l.textContent=label;
  const hid=el('input','');hid.type='hidden';hid.id=id;hid.value=value||'';
  const row=el('div','');row.style.cssText='display:flex;gap:6px;align-items:stretch';
  const btn=el('button','rbtn sec');btn.style.flex='1';
  const clr=el('button','rbtn sec');clr.textContent='✕';clr.title='Clear';clr.style.cssText='width:auto;flex-shrink:0;padding:8px 12px';
  const shown=el('div','finfo');
  const noun=kind==='folder'?'folder':'file';
  function refresh(){
    btn.textContent=hid.value?`📂 Change ${noun}…`:`📂 Choose ${noun}…`;
    shown.textContent=hid.value||'(none selected)';shown.title=hid.value;
    clr.style.display=(optional&&hid.value)?'':'none';
  }
  btn.onclick=async()=>{
    btn.disabled=true;btn.textContent='Waiting for the file dialog…';
    const p=await pickPath(kind,{title:label,filter,start:hid.value||''});
    btn.disabled=false;
    if(p) hid.value=p;
    refresh();
  };
  clr.onclick=()=>{ hid.value='';refresh(); };
  row.append(btn,clr);w.append(l,row,shown,hid);
  refresh();return w;
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
  const pathPk=makePathPicker('Script or executable','r-add-path','',{kind:'file',filter:'launchable'});
  const wbList=el('div','');wbList.id='r-wb-add';
  const btnWb=el('button','rbtn sec');btnWb.textContent='＋ Add WebApp URL';btnWb.onclick=()=>addWbEntry(wbList);
  body.append(rfield('Display name','text','r-add-name',''),pathPk,
    rinfo('Supported: .sh, .AppImage, .run, .bin, .py, .jar, .exe, .bat, .cmd, .ps1 (what runs depends on the OS).'),rtog('Launch hidden','r-add-lh',false),
    rinfo('WebApp URLs:'),wbList,btnWb,hr());
  const btnAdd=el('button','rbtn');btnAdd.textContent='Add Script';
  btnAdd.onclick=g(async()=>{
    const path=document.getElementById('r-add-path').value.trim(),name=document.getElementById('r-add-name').value.trim();
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
    const sfPk=makePathPicker('Attached text file','r-sep-sf',e.sepFile||'',{filter:'any',optional:true});
    body.append(rfield('Label','text','r-sep-lbl',e.displayName||e.path),sfPk,hr(),
      rbtn('▲ Move Up',async()=>doMove(idx,-1),true),rbtn('▼ Move Down',async()=>doMove(idx,1),true),hr(),
      rbtn('Save',g(async()=>{
        const lbl=document.getElementById('r-sep-lbl').value.trim();
        const sf=document.getElementById('r-sep-sf').value.trim();
        if(!lbl)return;
        const r=await api('POST','/api/update-entry',{idx,label:lbl,sepFile:sf});
        if(r&&r.error){setStatus('Save failed: '+r.error);return;}
        await poll();setStatus('Saved.');
      })),hr(),
      rbtn('✕ Remove',g(async()=>{
        if(!confirm('Remove?'))return;
        const r=await api('POST','/api/remove',{idx});
        if(r&&r.error){setStatus('Remove failed: '+r.error);return;}
        if(selIdx===idx)selIdx=-1;else if(selIdx>idx)selIdx--;
        closeEdit();await poll();
      }),false,'dng'));return;
  }
  let _pc=e.termColor||'default';
  const updPk=makePathPicker('Update script (default: update.sh next to the script)','r-upd',e.updateScript||'',{filter:'sh',optional:true});
  const infoPk=makePathPicker('Info file (.md)','r-info',e.infoFile||'',{filter:'md',optional:true});
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
    cp,hr(),updPk,hr(),infoPk,hr(),
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
      const r=await api('POST','/api/update-entry',{idx,name,delay,autoRestart:ar,keepOpen:ko,
        launchHidden:lh,openOutside:oo,termColor:_pc,webapps,updateScript:us,infoFile:inf,status:fav?'ON':'OFF'});
      if(r&&r.error){setStatus('Save failed: '+r.error);return;}
      await poll();setStatus('Saved.');
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
  const w=el('div','trow');const l=el('span','trow-label');l.textContent=lbl;
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
connectLogStream();
if(activeTab) renderTabView();
