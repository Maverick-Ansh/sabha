// Boot strip: while the box launcher (sabha/box.py) sets up tools and models, one line under the top bar
// shows each step (GET /api/boot, PROTOCOL.md section 9). Hidden when the hub was not started by the launcher.
import { h, setText, clear } from './dom.js';
import { get } from './api.js';
import { fmtDur } from './format.js';

const WORD = { running: 'installing', downloading: 'downloading', starting: 'warming up', waiting: 'waiting', ready: 'ready', done: 'done', failed: 'failed' };

export function createBoot() {
  const steps = h('div.b-steps');
  const note = h('div.b-note');
  const el = h('div.b-strip', { hidden: true, role: 'status', 'aria-live': 'polite' },
    h('span.b-title', null, 'Setting up your box'), steps, note);
  let timer = 0, readySince = 0;

  function paint(b) {
    if (!b || !b.present) { el.hidden = true; return; }
    const now = Date.now();
    if (b.ready && !readySince) readySince = now;
    el.hidden = b.ready && now - readySince > 8000;
    el.classList.toggle('ready', !!b.ready);
    el.classList.toggle('failed', b.stage === 'failed');
    clear(steps);
    for (const s of b.steps || []) {
      const live = ['running', 'downloading', 'starting'].includes(s.status);
      const t = live && s.secs ? ` ${fmtDur(s.secs, true)}${s.expect_s ? ' of ~' + fmtDur(s.expect_s, true) : ''}` : '';
      steps.appendChild(h('span', { class: 'b-step ' + (s.status || ''), title: [s.label, s.detail].filter(Boolean).join(': ') },
        h('span.b-dot'), `${s.short || s.id}: ${WORD[s.status] || s.status}${t}`));
    }
    const busy = (b.steps || []).find((s) => ['running', 'downloading', 'starting'].includes(s.status));
    const anyReady = (b.steps || []).some((s) => s.status === 'ready');
    setText(note, b.note || (b.ready ? 'All set. Talk, Council, Work and Research are ready.'
      : !b.alive ? 'The setup helper is not reporting (run the notebook cell again).'
      : busy && busy.id === 'tools' ? busy.detail || ''
      : busy ? `Models load, compile and warm up: about 10 minutes the first time.${anyReady ? ' The ready one already answers in Talk, Council and Work.' : ''}` : ''));
  }
  async function tick() {
    let b = null;
    try { b = await get('/api/boot'); } catch { /* older hub or offline: keep the last state */ return; }
    paint(b);
    clearTimeout(timer);
    if (b && b.present && !(b.ready && Date.now() - readySince > 8000)) timer = setTimeout(tick, 4000);
    else timer = setTimeout(tick, 60000);
  }
  return { el, start: tick };
}
