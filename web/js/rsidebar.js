// The Research sidebar (PROTOCOL.md section 17): New research, a search over every report and notebook, and every
// research grouped by when it ran, newest first, the running one on top. Collapsible on a wide screen (remembered),
// a drawer on a phone. Titles filter as you type; after a pause the hub searches the reports' full text and the
// matches come back with a snippet.
import { h, icon, clear, setText } from './dom.js';
import { get, enc } from './api.js';

const KEY = 'sabha.research.side';
const narrow = () => matchMedia('(max-width: 859px)').matches;
const ts = (e) => { const t = Date.parse(e.started || e.saved_at || ''); return Number.isFinite(t) ? t : 0; };
function groupOf(t) {
  if (!t) return 'Earlier';
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  const day = 86400000;
  if (t >= d0.getTime()) return 'Today';
  if (t >= d0.getTime() - day) return 'Yesterday';
  if (t >= d0.getTime() - 6 * day) return 'Previous 7 days';
  if (t >= d0.getTime() - 29 * day) return 'Previous 30 days';
  return new Date(t).toLocaleDateString([], { month: 'long', year: 'numeric' });
}
function mark(text, words) {  // the matched words in bold, as nodes (never HTML)
  if (!words.length) return [text];
  const re = new RegExp('(' + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'ig');
  return String(text).split(re).map((p, i) => (i % 2 ? h('b', null, p) : p));
}

// opts: { onNew(), onOpen(entry), onBrowse(), activeId() }
export function createSidebar(opts = {}) {
  let entries = [], q = '', timer = 0, seq = 0, hits = null;
  let collapsed = false;
  try { collapsed = localStorage.getItem(KEY) === 'closed'; } catch { /* private window */ }

  const search = h('input.rs-search-in', { type: 'search', placeholder: 'Search your research', 'aria-label': 'Search your research', autocomplete: 'off',
    oninput: (e) => { q = e.target.value; paint(); clearTimeout(timer); timer = setTimeout(deep, 320); },
    onkeydown: (e) => { if (e.key === 'Escape' && q) { e.stopPropagation(); q = ''; search.value = ''; hits = null; paint(); } } });
  const list = h('div.rs-list', { role: 'list' });
  const ghLine = h('div.rs-gh');
  const el = h('aside.rs', { 'aria-label': 'Your research' },
    h('div.rs-top', null,
      h('div.rs-brand', null, h('span.rs-dot'), h('span', null, 'Research')),
      h('button.btn.ghost.icon.sm.rs-fold', { title: 'Hide the sidebar', 'aria-label': 'Hide the sidebar', onclick: () => setCollapsed(true) }, icon('panel', 16))),
    h('button.rs-new', { onclick: () => { close(); opts.onNew && opts.onNew(); } }, 'New research'),
    h('label.rs-search', null, icon('search', 15), search),
    list,
    h('div.rs-foot', null,
      h('button.rs-browse', { onclick: () => { close(); opts.onBrowse && opts.onBrowse(); } }, icon('layers', 15), 'Browse the library'),
      ghLine));
  const scrim = h('div.rs-scrim', { onclick: () => close() });
  const toggle = h('button.btn.sm.rs-toggle', { title: 'Your research', 'aria-label': 'Show your research', onclick: () => (narrow() ? openDrawer() : setCollapsed(false)) },
    icon('panel', 15), h('span', null, 'Research'));

  function setCollapsed(v) {
    if (narrow()) { if (v) close(); return; }
    collapsed = v;
    try { localStorage.setItem(KEY, v ? 'closed' : 'open'); } catch { /* fine */ }
    apply();
  }
  function apply() { document.documentElement.classList.toggle('rs-collapsed', collapsed && !narrow()); }
  function openDrawer() { document.documentElement.classList.add('rs-drawer'); setTimeout(() => search.focus(), 250); }
  function close() { document.documentElement.classList.remove('rs-drawer'); }
  addEventListener('resize', apply);
  apply();

  function item(e, snippet = '', words = []) {
    const running = e.live_mission && e.status === 'running';
    const on = opts.activeId && (opts.activeId() === e.live_mission && e.live_mission);
    const when = ts(e) ? new Date(ts(e)).toLocaleDateString([], { day: 'numeric', month: 'short' }) : '';
    return h('button.rs-item' + (on ? '.on' : '') + (running ? '.running' : ''), { role: 'listitem', title: e.question || e.title, 'data-lid': e.lid,
      onclick: () => { close(); opts.onOpen && opts.onOpen(e); } },
    h('div.rs-item-title', null, running ? h('i.rs-pulse', { 'aria-hidden': 'true' }) : null, mark(e.title || e.mission || e.lid, words)),
    snippet ? h('div.rs-snip', null, mark(snippet, words)) : h('div.rs-item-meta', null,
      [when, e.papers ? `${e.papers} papers` : null].filter(Boolean).join(' · '),
      e.in_github ? h('span.rs-kept', { title: 'Kept in GitHub' }, icon('check', 10)) : null));
  }

  function paint() {
    clear(list);
    const words = (q.toLowerCase().match(/\w{2,}/g) || []).slice(0, 8);
    if (words.length) {
      const quick = entries.filter((e) => words.every((w) => [e.title, e.question, e.one_line].join(' ').toLowerCase().includes(w)));
      const seen = new Set(quick.map((e) => e.lid));
      const deepHits = (hits && hits.q === q ? hits.hits : []).filter((x) => !seen.has(x.lid));
      if (quick.length) { list.appendChild(h('div.rs-group', null, 'Titles')); quick.forEach((e) => list.appendChild(item(e, '', words))); }
      if (deepHits.length) {
        list.appendChild(h('div.rs-group', null, 'Inside the reports'));
        for (const x of deepHits) list.appendChild(item({ ...(entries.find((e) => e.lid === x.lid) || {}), ...x }, x.snippet, words));
      }
      for (const x of (hits && hits.q === q ? hits.hits : [])) {  // a title match that also matched inside: show where
        const node = list.querySelector(`.rs-item[data-lid="${CSS.escape(x.lid)}"]`);
        if (node && x.snippet && !node.querySelector('.rs-snip')) node.appendChild(h('div.rs-snip', null, mark(x.snippet, words)));
      }
      if (!quick.length && !deepHits.length) list.appendChild(h('div.rs-none', null, hits && hits.q === q ? 'Nothing matches.' : 'Searching…'));
      return;
    }
    if (!entries.length) { list.appendChild(h('div.rs-none', null, 'No research yet. Your first one shows up here.')); return; }
    const running = entries.filter((e) => e.live_mission && e.status === 'running');
    const rest = entries.filter((e) => !running.includes(e)).sort((a, b) => ts(b) - ts(a));
    if (running.length) { list.appendChild(h('div.rs-group', null, 'Running')); running.forEach((e) => list.appendChild(item(e))); }
    let g = '';
    for (const e of rest) {
      const name = groupOf(ts(e));
      if (name !== g) { g = name; list.appendChild(h('div.rs-group', null, name)); }
      list.appendChild(item(e));
    }
  }
  async function deep() {
    const mine = ++seq;
    const query = q;
    if (!(query.match(/\w{2,}/g) || []).length) { hits = null; return; }
    try {
      const r = await get('/api/library/search?q=' + enc(query));
      if (mine === seq) { hits = { q: query, hits: r.hits || [] }; paint(); }
    } catch { if (mine === seq) { hits = { q: query, hits: [] }; paint(); } }
  }
  async function load() {
    try {
      const r = await get('/api/library');
      entries = r.entries || [];
      const gh = r.github || {};
      clear(ghLine).append(h('i', { class: gh.on ? (gh.error ? 'warn' : 'on') : '' }),
        gh.on ? (gh.error ? 'GitHub: ' + gh.error : 'Kept in GitHub · ' + gh.repo) : 'Only on this box · connect GitHub in the library');
      paint();
    } catch { /* an older hub: the sidebar stays empty */ }
  }
  return { el, scrim, toggle, load, paint, close };
}
