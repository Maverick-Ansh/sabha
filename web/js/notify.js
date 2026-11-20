// Toasts, a soft two-note chime, browser notifications and the document title count.
import { h, icon } from './dom.js';
import { subscribe, getState, missions, agentById, emit } from './store.js';
import { truncate } from './format.js';

let toastsEl = null;
let audio = null;
let muted = false;
let askedPermission = false;
try { muted = localStorage.getItem('sabha.mute') === '1'; askedPermission = localStorage.getItem('sabha.notifAsked') === '1'; } catch { /* ignore */ }

export const isMuted = () => muted;
export function setMuted(v) {
  muted = !!v;
  try { localStorage.setItem('sabha.mute', muted ? '1' : '0'); } catch { /* ignore */ }
  emit('mute', muted);
}

// toast('Title', { sub, tone: 'sun'|'mint'|'coral', actions: [{label, onClick}], timeout })
export function toast(title, opts = {}) {
  if (!toastsEl) { toastsEl = h('div.toasts', { role: 'status', 'aria-live': 'polite' }); document.body.appendChild(toastsEl); }
  for (const old of Array.from(toastsEl.children)) if (old.dataset.key === title) old.remove();  // the same note again replaces it
  const el = h('div', { class: 'toast ' + (opts.tone || ''), 'data-key': title, title: opts.sub ? `${title}: ${opts.sub}` : title },
    h('span.t-mark'),
    h('div.t-body', null, h('div.t-title', null, title), opts.sub ? h('div.t-sub', null, opts.sub) : null),
    (opts.actions || []).map((a) => h('button.btn.sm', { onclick: () => { a.onClick(); dismiss(); } }, a.label)),
    h('button.btn.ghost.sm.icon', { 'aria-label': 'Dismiss', onclick: () => dismiss() }, icon('x', 14)));
  let gone = false;
  function dismiss() {
    if (gone) return; gone = true;
    el.classList.add('out');
    setTimeout(() => el.remove(), 260);
  }
  toastsEl.appendChild(el);
  while (toastsEl.children.length > 3) toastsEl.firstChild.remove();
  const t = opts.timeout === 0 ? 0 : (opts.timeout || 6500);
  if (t) setTimeout(dismiss, t);
  return dismiss;
}

// Two gentle notes, sine with a soft envelope.
export function chime(kind = 'done') {
  if (muted) return;
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === 'suspended') audio.resume();
    const notes = kind === 'needs' ? [659.25, 523.25] : kind === 'failed' ? [440, 392] : [659.25, 987.77];
    const t0 = audio.currentTime + 0.02;
    notes.forEach((f, i) => {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = 'sine'; o.frequency.value = f;
      const s = t0 + i * 0.16;
      g.gain.setValueAtTime(0, s);
      g.gain.linearRampToValueAtTime(0.07, s + 0.025);
      g.gain.exponentialRampToValueAtTime(0.0008, s + 0.9);
      o.connect(g).connect(audio.destination);
      o.start(s); o.stop(s + 1);
    });
  } catch { /* audio unavailable */ }
}

function browserNotify(title, body) {
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;
  try { const n = new Notification(title, { body, tag: 'sabha', silent: true }); n.onclick = () => { focus(); n.close(); }; } catch { /* ignore */ }
}

// Ask politely, once, the first time a mission is dispatched.
export function firstDispatch() {
  if (askedPermission || !('Notification' in window) || Notification.permission !== 'default') return;
  askedPermission = true;
  try { localStorage.setItem('sabha.notifAsked', '1'); } catch { /* ignore */ }
  toast('Want a ping when missions finish?', {
    sub: 'Only when the tab is in the background.', timeout: 14000,
    actions: [{ label: 'Allow', onClick: () => Notification.requestPermission() }],
  });
}

let openMission = () => {};
export function setOpenMission(fn) { openMission = fn; }

function updateTitle() {
  const n = missions().filter((m) => m.status === 'awaiting_approval' || m.status === 'failed').length;
  const base = (getState().app && getState().app.name) || 'Sabha';
  document.title = n ? `(${n}) ${base} · needs you` : `${base} · in session`;
}

export function initNotify() {
  subscribe('reset', updateTitle);
  subscribe('missions', updateTitle);
  subscribe('transition', ({ mission: m, prev, local }) => {
    if (local || !prev || !getState().ready) return;
    const who = (agentById(m.agent_id) || {}).name || 'An agent';
    const title = truncate(m.title || m.prompt || 'Mission', 70);
    const open = { label: 'Open', onClick: () => openMission(m.id) };
    if (m.status === 'awaiting_approval') {
      toast(`${who} has a plan for you`, { sub: title, tone: 'sun', actions: [{ label: 'Review', onClick: () => openMission(m.id, 'plan') }], timeout: 12000 });
      chime('needs'); browserNotify(`${who} has a plan for you`, title);
    } else if (m.status === 'done') {
      toast(`${who} finished`, { sub: title, tone: 'mint', actions: [{ label: 'Report', onClick: () => openMission(m.id, 'report') }], timeout: 9000 });
      chime('done'); browserNotify(`${who} finished`, title);
    } else if (m.status === 'failed') {
      toast(`${who} hit a wall`, { sub: m.error ? truncate(m.error, 90) : title, tone: 'coral', actions: [open], timeout: 12000 });
      chime('failed'); browserNotify(`${who} hit a wall`, title);
    }
  });
  updateTitle();
}
