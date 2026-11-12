// Work mode: a composer on top and a four-column board of missions. Cards update in place.
import { h, icon, setText, clear } from './dom.js';
import { subscribe, getState, missions, agentById } from './store.js';
import { hueOf, fmtDur, elapsed, planProgress, statusLabel, tone, truncate } from './format.js';
import { createComposer } from './composer.js';
import { openDrawer } from './drawer.js';

const COLUMNS = [
  { id: 'needs', title: 'Needs you', hint: 'plans to approve, runs that failed', has: (s) => s === 'awaiting_approval' || s === 'failed' },
  { id: 'running', title: 'Running', hint: 'on the floor now', has: (s) => s === 'running' || s === 'planning' },
  { id: 'queued', title: 'Queued', hint: 'waiting for a seat', has: (s) => s === 'queued' },
  { id: 'done', title: 'Done', hint: 'finished or stopped', has: (s) => s === 'done' || s === 'stopped' },
];
const colOf = (status) => (COLUMNS.find((c) => c.has(status)) || COLUMNS[2]).id;
const DONE_LIMIT = 40;

export function createWork() {
  const composer = createComposer({ compact: true });
  const cols = {};
  const board = h('div.board');
  for (const c of COLUMNS) {
    const count = h('span.col-count');
    const list = h('div.col-list');
    const more = h('button.btn.ghost.sm.col-more', { hidden: true, onclick: () => { showAllDone = true; renderAll(); } }, 'show all');
    const el = h('section', { class: 'col col-' + c.id },
      h('header.col-head', null, h('span.col-title', null, c.title), count, h('span.micro.col-hint', null, c.hint)),
      list, more,
      h('div.col-empty', null, c.id === 'needs' ? 'Nothing needs you. Lovely.' : c.id === 'running' ? 'No one is working right now.' : c.id === 'queued' ? 'The queue is clear.' : 'Finished work lands here.'));
    cols[c.id] = { el, list, count, more };
    board.appendChild(el);
  }
  let showAllDone = false;
  const el = h('div.view.work', { 'data-view': 'work' },
    h('div.work-inner', null, h('div.work-head', null,
      h('div', null, h('h1.display.italic.work-title', null, 'The floor'), h('div.dim.work-sub', null, 'Dispatch missions, approve plans, read reports.')),
      composer.el), board));

  const cards = new Map(); // mission_id -> refs

  function card(m) {
    const statusEl = h('span.status');
    const title = h('div.wc-title');
    const agentChip = h('span.chip.hue');
    const bar = h('div.wc-bar', null, h('span'));
    const prog = h('span.wc-prog.micro');
    const meta = h('div.wc-meta.mono');
    const now = h('div.wc-now.ellipsis');
    const el = h('button', { class: 'wcard card', onclick: () => openDrawer(m.id) },
      h('div.wc-top', null, agentChip, statusEl), title, h('div.wc-progrow', null, bar, prog), now, meta);
    return { el, statusEl, title, agentChip, bar, prog, meta, now, col: null };
  }
  function paint(r, m) {
    const a = agentById(m.agent_id);
    r.el.dataset.hue = hueOf(a);
    r.el.dataset.tone = tone(m.status);
    clear(r.agentChip).append(h('span.dot'), a ? a.name : 'unknown agent');
    r.statusEl.className = 'status ' + tone(m.status);
    setText(r.statusEl, statusLabel(m.status));
    setText(r.title, truncate(m.title || m.prompt || 'Mission', 140));
    const p = planProgress(m.plan);
    r.bar.firstChild.style.width = Math.round(p.frac * 100) + '%';
    r.bar.parentElement.hidden = !p.total;
    setText(r.prog, p.total ? `${p.done}/${p.total}` : '');
    const s = m.stats || {};
    const dur = elapsed(m);
    setText(r.meta, [dur ? fmtDur(dur, true) : 'not started', `${s.steps || 0} steps`, `${s.tool_calls || 0} tools`, m.runs > 1 ? `${m.runs} runs` : ''].filter(Boolean).join(' · '));
    const act = getState().act.missions[m.id];
    const line = m.status === 'awaiting_approval' ? 'plan ready for your review'
      : m.status === 'failed' ? (m.error || 'failed')
        : act && tone(m.status) === 'running' ? act.now : m.status === 'done' && m.result_text ? m.result_text.split('\n').find((l) => l.trim()) || '' : '';
    setText(r.now, truncate(line.replace(/^#+\s*/, ''), 160));
    r.now.hidden = !line;
  }
  function sortKey(colId, m) {
    if (colId === 'queued') return m.created_ts || 0;
    if (colId === 'done') return -(m.ended_ts || m.created_ts || 0);
    return -(m.started_ts || m.created_ts || 0);
  }
  function place(m) {
    let r = cards.get(m.id);
    if (!r) { r = card(m); cards.set(m.id, r); }
    paint(r, m);
    const cid = colOf(m.status);
    const list = cols[cid].list;
    // insert at the sorted position
    const key = sortKey(cid, m);
    let before = null;
    for (const child of list.children) {
      if (child === r.el) continue;
      const om = getState().missions[child.dataset.id];
      if (om && sortKey(cid, om) > key) { before = child; break; }
    }
    r.el.dataset.id = m.id;
    if (r.el.parentElement !== list || r.el.nextSibling !== before) {
      const moved = r.col && r.col !== cid;
      list.insertBefore(r.el, before);
      if (moved) { r.el.classList.remove('rise'); void r.el.offsetWidth; r.el.classList.add('rise'); }
    }
    r.col = cid;
  }
  function counts() {
    for (const c of COLUMNS) {
      const n = cols[c.id].list.children.length;
      setText(cols[c.id].count, n ? String(n) : '');
      cols[c.id].el.classList.toggle('empty', !n);
    }
    const done = missions().filter((m) => colOf(m.status) === 'done').length;
    cols.done.more.hidden = showAllDone || done <= DONE_LIMIT;
    if (!cols.done.more.hidden) setText(cols.done.more, `show all ${done}`);
  }
  function renderAll() {
    for (const c of COLUMNS) clear(cols[c.id].list);
    cards.clear();
    const list = missions();
    const done = list.filter((m) => colOf(m.status) === 'done').sort((a, b) => (b.ended_ts || 0) - (a.ended_ts || 0));
    const keepDone = new Set((showAllDone ? done : done.slice(0, DONE_LIMIT)).map((m) => m.id));
    for (const m of list) if (colOf(m.status) !== 'done' || keepDone.has(m.id)) place(m);
    counts();
  }

  subscribe('reset', renderAll);
  subscribe('missions', ({ mission, id, removed }) => {
    if (removed) { const r = cards.get(id); if (r) { r.el.remove(); cards.delete(id); } counts(); return; }
    if (mission) { place(mission); counts(); }
  });
  subscribe('activity', (ev) => {
    if (!ev || !ev.mission_id) return;
    const r = cards.get(ev.mission_id), m = getState().missions[ev.mission_id];
    if (r && m) paint(r, m);
  });
  subscribe('agents', () => { for (const [id, r] of cards) { const m = getState().missions[id]; if (m) paint(r, m); } });
  let active = false;
  setInterval(() => {
    if (!active) return;
    for (const [id, r] of cards) { const m = getState().missions[id]; if (m && tone(m.status) === 'running') paint(r, m); }
  }, 5000);

  return { el, setActive(on) { active = on; }, focusComposer: () => composer.focus() };
}
