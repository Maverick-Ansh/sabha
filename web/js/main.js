// Bootstrap: token, auth gate, snapshot, WebSocket, router, shortcuts.
import { initToken, setTokenFrom, setUnauthorizedHandler, get, connect } from './api.js';
import * as store from './store.js';
import * as log from './events.js';
import { setClockOffset } from './format.js';
import { h, icon } from './dom.js';
import { createTopbar, MODES } from './shell.js';
import { createWatch } from './watch.js';
import { createTalk } from './talk.js';
import { createWork } from './work.js';
import { openDrawer } from './drawer.js';
import { openCrew } from './crew.js';
import { openPalette } from './palette.js';
import { initNotify, setOpenMission } from './notify.js';
import { nav } from './nav.js';

const root = document.getElementById('app');
let helloHead = 0, helloSeen = false, pongSeen = false, conn = null, gateEl = null, mode = null;

// ---------- views ----------
const views = { watch: createWatch(), talk: createTalk(), work: createWork() };
const topbar = createTopbar({ onMode: (m) => setMode(m), onCrew: () => openCrew(), onPalette: () => openPalette() });
const viewsEl = h('main.views', null, Object.values(views).map((v) => v.el));
root.append(topbar.el, viewsEl);
for (const [k, v] of Object.entries(views)) { v.el.hidden = true; v.el.dataset.view = k; }

function setMode(next) {
  if (!MODES.includes(next)) next = 'watch';
  if (next === mode) return;
  const prev = mode;
  mode = next;
  store.setUI({ mode });
  topbar.setMode(mode);
  try { localStorage.setItem('sabha.mode', mode); } catch { /* ignore */ }
  if (location.hash !== '#/' + mode) history.replaceState(null, '', '#/' + mode);
  const inEl = views[mode].el;
  if (prev) {
    const outEl = views[prev].el;
    views[prev].setActive(false);
    outEl.classList.add('leaving');
    setTimeout(() => { if (mode !== prev) { outEl.hidden = true; } outEl.classList.remove('leaving'); }, 200);
  }
  inEl.hidden = false;
  inEl.classList.remove('entering'); void inEl.offsetWidth; inEl.classList.add('entering');
  views[mode].setActive(true);
}
const modeFromHash = () => { const m = location.hash.match(/^#\/(watch|talk|work)\b/); return m ? m[1] : null; };
addEventListener('hashchange', () => { const m = modeFromHash(); if (m) setMode(m); });

nav.go = setMode;
nav.openMission = (id, tab) => { setMode('work'); openDrawer(id, tab); };
nav.watchMission = (id) => {
  const m = store.getState().missions[id];
  store.setUI({ focusAgent: m ? m.agent_id : null, focusMission: id });
  setMode('watch');
};
nav.openCrew = (tab) => openCrew(tab);
nav.openPalette = () => openPalette();
setOpenMission((id, tab) => nav.openMission(id, tab));

// ---------- shortcuts ----------
const typing = (t) => t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  if (document.querySelector('.modal, .drawer, .slideover')) return;
  const i = ['1', '2', '3'].indexOf(e.key);
  if (i >= 0) { e.preventDefault(); setMode(MODES[i]); }
});

// ---------- auth gate ----------
function showGate() {
  store.setConn('auth');
  if (gateEl) return;
  const input = h('input.input', { placeholder: 'https://…/#token=…  or the token itself', 'aria-label': 'Hub link or token', autocomplete: 'off' });
  const err = h('div.err');
  const btn = h('button.btn.brand', { type: 'submit' }, 'Take a seat', icon('send', 14));
  gateEl = h('div.gate', null, h('div.gate-card.card', null,
    h('div.gate-sun', { 'aria-hidden': 'true' }),
    h('h1', null, 'The council is in session.'),
    h('p', null, 'Paste your hub link or token to come in. The hub prints the link when it starts.'),
    h('form', { onsubmit: async (e) => {
      e.preventDefault();
      if (!input.value.trim()) { input.focus(); return; }
      setTokenFrom(input.value);
      btn.disabled = true; err.textContent = '';
      try {
        const snap = await get('/api/state');
        store.reset(snap);
        gateEl.remove(); gateEl = null;
        if (conn) conn.reconnect(); else startSocket();
      } catch (ex) {
        err.textContent = ex.status === 401 ? 'That key did not open the door. Check the link the hub printed.' : `The hub did not answer: ${ex.message}`;
      } finally { btn.disabled = false; }
    } }, input, btn),
    err));
  document.body.appendChild(gateEl);
  requestAnimationFrame(() => input.focus());
}

// ---------- socket ----------
function startSocket() {
  conn = connect({
    getSince: () => log.lastSeq(),
    onStatus: (s) => { store.setConn(s); if (s === 'auth') showGate(); },
    onHello: (ev) => {
      const first = !helloSeen;
      helloSeen = true;
      const prevMax = log.lastSeq();
      helloHead = ev.seq_head || 0;
      if (ev.state) store.reset(ev.state);
      if (first) log.loadHistory(helloHead);
      else if (helloHead - prevMax > 4900) log.loadRange(prevMax, helloHead - 4900).catch(() => {});
    },
    onEvent: (ev) => {
      if (!ev || !ev.type) return;
      if (!pongSeen && ev.type === 'sys.telemetry' && ev.ts) setClockOffset(ev.ts - Date.now() / 1000);
      const live = !ev.seq || ev.seq > helloHead;
      if (ev.seq && !log.ingest(ev)) return; // duplicate
      store.apply(ev, live);
    },
    onPong: (ev, mid) => { if (ev.ts) { pongSeen = true; setClockOffset(ev.ts - mid); } },
  });
}

// ---------- boot ----------
async function boot() {
  initToken();
  setUnauthorizedHandler(showGate);
  initNotify();
  let initial = modeFromHash();
  if (!initial) { try { initial = localStorage.getItem('sabha.mode'); } catch { /* ignore */ } }
  setMode(initial || 'watch');
  try {
    const snap = await get('/api/state');
    store.reset(snap);
  } catch (e) {
    if (e.status === 401) { showGate(); return; }
    store.setConn('reconnecting');
  }
  startSocket();
}
boot();
