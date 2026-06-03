'use strict';
// Path: /disks/linux/EXT4/ENVIRONMENTS/MaestroStable/webui.js
// WebUI tab management — iframe lifecycle, dark placeholder, suspend/resume.
// Depends on ui.js globals: api, el, setStatus, renderPills,
//                           updateFooterForActiveTab, pingUntilUp

const tabIframes     = {};          // webuid -> iframe element
const _suspendedTabs = new Set();   // webuid of suspended tabs

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
      Connecting to <span style="color:var(--acc4)">${url}</span>…
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
    `Connecting to <span style="color:var(--acc4)">${url}</span>…`; }
  iframe.style.opacity = '0'; iframe.style.transition = '';
  iframe.dataset.origUrl = url;
  iframe.src = url;
}

// ── Reload (show placeholder → re-ping → load) ────────────────────────────────
function reloadWebTab(uid) {
  const iframe = tabIframes[uid]; if (!iframe) return;
  const url = iframe.dataset.origUrl || ''; if (!url) return;
  const ph = document.getElementById('ph-' + uid);
  if (ph) {
    ph.innerHTML = `
      <img src="/img/MaestroLogo.svg" style="width:80px;height:80px;opacity:.06;pointer-events:none">
      <div style="color:var(--txt3);font-size:11px">
        Reconnecting to <span style="color:var(--acc4)">${url}</span>…
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
        ⏸ Suspended — <span style="color:var(--acc4)">${url}</span>
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
  const cont = document.getElementById('tc-' + uid);
  cont?.querySelector('.tv-suspended')?.remove();
  const targetUrl = tabIframes[uid]?.dataset.origUrl || url;
  if (targetUrl && typeof pingUntilUp === 'function') {
    pingUntilUp(targetUrl, 20000).then(up => { if (up) loadWebTab(uid, targetUrl); });
  } else if (targetUrl) {
    loadWebTab(uid, targetUrl);
  }
}
