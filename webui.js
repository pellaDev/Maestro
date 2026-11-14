'use strict';
// Path: webui.js
// WebUI tab management — iframe lifecycle, dark placeholder, suspend/resume.
// Webapp tabs are LAZY: a tab whose process just started is 'auto-paused' — no iframe
// is loaded (and no request is made to the webapp) until the user clicks it or its ▶.
// Depends on ui.js globals: api, el, setStatus, renderPills,
//                           updateFooterForActiveTab, pingUntilUp

const tabIframes     = {};          // webuid -> iframe element
const _suspendedTabs = new Set();   // webuid of suspended tabs (manual or auto-paused)
const _resuming       = new Set();   // webuids currently waiting for their webapp to answer a ping
const _autoPaused    = new Set();   // subset of _suspendedTabs: paused by default, never loaded since the process started

// ── HTML escaping (Fix #5 — stored XSS via unescaped innerHTML) ───────────────
// Every url interpolated into an innerHTML template below is attacker-settable
// via the unauthenticated /api/add or /api/update-entry endpoints, so it must
// never be inserted raw. Always route through _esc() before using ${...}
// inside an innerHTML template literal.
function _esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// ── Placeholder / state helpers ───────────────────────────────────────────────
function _phText(uid, html) {
  const ph = document.getElementById('ph-' + uid);
  if (!ph) return;
  ph.style.display = 'flex';
  const d = ph.querySelector('div');
  if (d) d.innerHTML = html;
}

// True when the iframe currently holds a real page (not blank / not yet created).
function isWebTabLoaded(uid) {
  const ifr = tabIframes[uid];
  return !!(ifr && ifr.src && ifr.src !== 'about:blank');
}

// Put a tab back into the default paused state: blank iframe (frees the page's
// memory), no manual-suspend overlay, loads again on the next click.
function pauseWebTab(uid, url) {
  const iframe = tabIframes[uid];
  if (iframe) {
    iframe.src = 'about:blank';
    iframe.style.opacity = '0'; iframe.style.transition = '';
  }
  document.getElementById('tc-' + uid)?.querySelector('.tv-suspended')?.remove();
  _suspendedTabs.add(uid);
  _autoPaused.add(uid);
  _phText(uid, `⏸ Paused — click to load <span style="color:var(--acc4)">${_esc(url || '')}</span>`);
}

// The webapp's process stopped (or the entry was removed): blank the page and
// forget every paused/suspended flag so the next launch starts paused again.
function forgetWebTab(uid) {
  _suspendedTabs.delete(uid);
  _autoPaused.delete(uid);
  const iframe = tabIframes[uid];
  if (iframe) { iframe.style.opacity = '0'; iframe.src = 'about:blank'; }
  document.getElementById('tc-' + uid)?.querySelector('.tv-suspended')?.remove();
  _phText(uid, 'Process not running');
}

// ── Create container (iframe starts blank) ────────────────────────────────────
function createWebContainer(uid, url, tv) {
  const cont = document.createElement('div');
  cont.id = 'tc-' + uid;
  cont.style.cssText = 'position:absolute;inset:0;background:#0e0808;';

  // Placeholder shown while connecting or suspended
  const ph = document.createElement('div');
  ph.id = 'ph-' + uid;
  ph.style.cssText = [
    'position:absolute','inset:0','z-index:2','display:flex','flex-direction:column',
    'align-items:center','justify-content:center','background:#0e0808',
    'color:var(--txt3)','font-size:12px','gap:14px','pointer-events:none'
  ].join(';');
  ph.innerHTML = `
    <img src="/img/MaestroLogo.svg" style="width:80px;height:80px;opacity:.06;pointer-events:none">
    <div style="color:var(--txt3);font-size:11px">
      Connecting to <span style="color:var(--acc4)">${_esc(url)}</span>…
    </div>`;
  cont.appendChild(ph);

  // Iframe — always starts blank; src set by loadWebTab() after ping
  const iframe = document.createElement('iframe');
  iframe.allow = 'fullscreen';
  iframe.dataset.origUrl = url;
  iframe.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;border:none;opacity:0;';
  iframe.addEventListener('load', () => {
    if (iframe.src && iframe.src !== 'about:blank') {
      iframe.style.transition = 'opacity .25s';
      iframe.style.opacity = '1';
      ph.style.display = 'none';
    }
  });
  iframe.src = 'about:blank';
  cont.appendChild(iframe);
  tabIframes[uid] = iframe;
  tv.appendChild(cont);
  return cont;
}

// ── Load iframe (called after ping succeeds) ──────────────────────────────────
function loadWebTab(uid, url) {
  const iframe = tabIframes[uid];
  if (!iframe || _suspendedTabs.has(uid)) return;
  const ph = document.getElementById('ph-' + uid);
  if (ph) { ph.style.display = 'flex'; ph.querySelector('div').innerHTML =
    `Connecting to <span style="color:var(--acc4)">${_esc(url)}</span>…`; }
  iframe.style.opacity = '0'; iframe.style.transition = '';
  iframe.dataset.origUrl = url;
  iframe.src = url;
}

// ── Reload (show placeholder → re-ping → load) ────────────────────────────────
function reloadWebTab(uid) {
  if (_suspendedTabs.has(uid)) return;   // paused/suspended tabs load only when resumed
  const iframe = tabIframes[uid]; if (!iframe) return;
  const url = iframe.dataset.origUrl || ''; if (!url) return;
  const ph = document.getElementById('ph-' + uid);
  if (ph) {
    ph.innerHTML = `
      <img src="/img/MaestroLogo.svg" style="width:80px;height:80px;opacity:.06;pointer-events:none">
      <div style="color:var(--txt3);font-size:11px">
        Reconnecting to <span style="color:var(--acc4)">${_esc(url)}</span>…
      </div>`;
    ph.style.display = 'flex';
  }
  iframe.style.opacity = '0'; iframe.style.transition = '';
  iframe.src = 'about:blank';
  if (typeof pingUntilUp === 'function') {
    pingUntilUp(url, 20000).then(up => {
      if (up) loadWebTab(uid, url);
      else if (typeof setStatus === 'function') setStatus(`${url} did not respond`);
    });
  } else {
    setTimeout(() => loadWebTab(uid, url), 500);
  }
}

// ── Suspend / Resume ──────────────────────────────────────────────────────────
function toggleSuspend(uid, url) {
  _suspendedTabs.has(uid) ? _resumeTab(uid, url) : _suspendTab(uid, url);
  if (typeof renderPills === 'function') renderPills();
  if (typeof updateFooterForActiveTab === 'function') updateFooterForActiveTab();
}

function _suspendTab(uid, url) {
  _suspendedTabs.add(uid);
  _autoPaused.delete(uid);
  const iframe = tabIframes[uid];
  if (iframe) {
    iframe.dataset.origUrl = iframe.dataset.origUrl || url;
    iframe.src = 'about:blank';
    iframe.style.opacity = '0'; iframe.style.transition = '';
  }
  const cont = document.getElementById('tc-' + uid);
  if (cont && !cont.querySelector('.tv-suspended')) {
    const ov = document.createElement('div');
    ov.className = 'tv-suspended';
    ov.style.cssText = [
      'position:absolute','inset:0','z-index:3','display:flex','flex-direction:column',
      'align-items:center','justify-content:center','background:#0e0808','gap:14px'
    ].join(';');
    ov.innerHTML = `
      <img src="/img/MaestroLogo.svg" style="width:70px;height:70px;opacity:.07;pointer-events:none">
      <div style="color:var(--txt3);font-size:11px">
        ⏸ Suspended — <span style="color:var(--acc4)">${_esc(url)}</span>
      </div>`;
    const btn = document.createElement('button');
    btn.textContent = '▶ Resume';
    btn.style.cssText = [
      'background:var(--bg2)','border:1px solid var(--acc3)','color:var(--acc4)',
      'padding:7px 18px','border-radius:2px','cursor:pointer',
      'font-family:inherit','font-size:12px','margin-top:4px'
    ].join(';');
    btn.onclick = () => toggleSuspend(uid, url);
    ov.appendChild(btn);
    cont.appendChild(ov);
  }
}

function _resumeTab(uid, url) {
  _suspendedTabs.delete(uid);
  _autoPaused.delete(uid);
  const cont = document.getElementById('tc-' + uid);
  cont?.querySelector('.tv-suspended')?.remove();
  // A paused tab that was never opened has no container yet: create it hidden
  // (only .tv-active containers are displayed), so ▶ can load it in the background.
  if (!tabIframes[uid]) {
    const tv = document.getElementById('tab-view');
    if (tv) createWebContainer(uid, url, tv);
  }
  const targetUrl = tabIframes[uid]?.dataset.origUrl || url;
  if (!targetUrl) return;
  _phText(uid, `Connecting to <span style="color:var(--acc4)">${_esc(targetUrl)}</span>…`);
  if (_resuming.has(uid)) return;                   // a ping for this tab is already in flight
  if (typeof pingUntilUp === 'function') {
    _resuming.add(uid);
    pingUntilUp(targetUrl, 20000).then(up => {
      _resuming.delete(uid);
      if (_suspendedTabs.has(uid)) return;          // suspended again while waiting
      if (up) loadWebTab(uid, targetUrl);
      else {
        _phText(uid, `<span style="color:var(--acc4)">${_esc(targetUrl)}</span> did not respond — use ↺ to retry`);
        if (typeof setStatus === 'function') setStatus(`${targetUrl} did not respond`);
      }
    });
  } else {
    loadWebTab(uid, targetUrl);
  }
}
