// Conversations: every exchange between the agents and the models, in full. One panel for a research run
// (/api/research/calls), a council and a Talk thread (/api/exchanges/{scope}/{id}): who asked, for what, which model on
// which box answered, the system prompt, the whole input and the whole answer, tokens, seconds, failures.
import { h, icon, setText, clear } from './dom.js';
import { get } from './api.js';
import { renderInto } from './md.js';

const fmtT = (t) => new Date((t || 0) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const kfmt = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n || 0));

// opts: { listUrl(params: URLSearchParams) -> url, fullUrl(n) -> url, who(track) -> {name, hue}, title, sub, scroll (element) }
export function exchangePanel(opts) {
  const c = { q: '', track: '', step: '', endpoint: '', errors: false, rows: [], more: false, total: 0, matched: 0, open: new Set() };
  const who = opts.who || ((t) => ({ name: t || 'team', hue: null }));
  const head = h('div.cv-head');
  const tools = h('div.cv-tools');
  const list = h('div.cv-list');
  const el = h('div.rp-papers', null,
    h('div.rp-papers-head', null, h('h1.rp-title.sm', null, opts.title || 'Every exchange, in full'),
      h('div.micro.dim', null, opts.sub || 'What each agent was told and asked, which model on which box answered, and the whole answer. Newest at the bottom.')),
    head, tools, list);
  const scroll = () => opts.scroll || el.closest('.rp-scroll, .xo-scroll') || el.parentElement;
  let tmr = 0;

  function url(before) {
    const p = new URLSearchParams();
    for (const k of ['track', 'step', 'endpoint', 'q']) if (c[k]) p.set(k, c[k]);
    if (c.errors) p.set('errors', '1');
    if (before) p.set('before', String(before));
    p.set('limit', '120');
    return opts.listUrl(p);
  }
  function take(r) { c.total = r.total; c.matched = r.matched; c.endpoints = r.endpoints || []; c.steps = r.steps || []; c.tracks = r.tracks || []; }
  const sel = (key, label, items) => h('select.input.cv-sel', { 'aria-label': label, onchange: (e) => { c[key] = e.target.value; reload(); } },
    h('option', { value: '' }, label), items.map((o) => h('option', { value: o.v, selected: c[key] === o.v ? '' : null }, o.l)));
  function paintTools() {
    clear(tools).append(
      h('input.input.rp-search', { type: 'search', placeholder: 'Search what was asked and answered', value: c.q,
        oninput: (e) => { c.q = e.target.value; clearTimeout(tmr); tmr = setTimeout(reload, 350); } }),
      sel('track', 'Every agent', (c.tracks || []).map((t) => ({ v: t, l: who(t).name }))),
      sel('step', 'Every step', (c.steps || []).map((x) => ({ v: x, l: x }))),
      sel('endpoint', 'Every model', (c.endpoints || []).map((e) => ({ v: e.endpoint, l: e.endpoint }))),
      h('label.cv-err', null, h('input', { type: 'checkbox', checked: c.errors ? '' : null, onchange: (e) => { c.errors = e.target.checked; reload(); } }), 'failures only'));
  }
  function paintHead() {
    const tot = (c.endpoints || []).reduce((a, e) => a + e.calls, 0) || 1;
    clear(head).append(h('div.cv-stats', null,
      h('div.cv-stat', null, h('b', null, kfmt(c.total)), h('span.micro', null, 'exchanges')),
      ...(c.endpoints || []).map((e) => h('div.cv-ep', { title: `${e.calls} calls, ${e.errors} failed, ${kfmt(e.tok)} tokens out, ${Math.round(e.secs)} s of model time` },
        h('div.cv-ep-top', null, h('span.cv-where', null, e.remote ? 'other box' : 'this box'), h('b.ellipsis', null, e.endpoint)),
        h('div.cv-bar', null, h('i', { style: { width: `${Math.max(2, (e.calls / tot) * 100)}%` } })),
        h('div.micro.mono', null, `${e.calls} calls · ${kfmt(e.tok)} tok · ${e.calls ? (e.secs / e.calls).toFixed(1) : 0} s avg${e.errors ? ` · ${e.errors} failed` : ''}`)))));
  }
  async function fillBody(r, body) {
    clear(body).appendChild(h('p.micro.dim', null, 'loading…'));
    try {
      const f = await get(opts.fullUrl(r.n));
      const block = (label, text, asMd) => {
        const d = h('div.cv-block', null, h('div.micro.cv-label', null, label));
        let t = String(text || '');
        if (!asMd) { try { if (/^\s*[{[]/.test(t)) t = JSON.stringify(JSON.parse(t), null, 2); } catch { /* as is */ } d.appendChild(h('pre.cv-pre', null, t || '(empty)')); }
        else { const m = h('div.md'); renderInto(m, t || '_(empty)_'); d.appendChild(m); }
        return d;
      };
      clear(body).append(
        h('div.micro.mono.cv-meta', null, [`#${f.n}`, `${f.model || f.role} via ${f.endpoint}${f.remote ? ' (another box)' : ''}`,
          f.round != null ? `round ${f.round}` : '', f.attempt ? `try ${f.attempt + 1}` : '', f.max_tokens ? `max ${f.max_tokens} tok` : '',
          f.json ? 'JSON schema' : '', f.looped ? 'LOOPED (retried)' : '', f.paper ? 'paper ' + f.paper : ''].filter(Boolean).join(' · ')),
        block('system: what the agent was told it is', f.system, false),
        block(`input (${kfmt((f.input || '').length)} chars)`, f.input, false),
        f.thinking ? block('thinking', f.thinking, false) : null,
        (f.tool_calls || []).length ? block('tool calls', JSON.stringify(f.tool_calls, null, 2), false) : null,
        f.error ? block('error', f.error, false) : block(`answer (${f.tok} tokens, ${f.secs} s)`, f.output, !f.json));
    } catch (e) { clear(body).appendChild(h('p.dim', null, e.message)); }
  }
  function row(r) {
    const w = who(r.track);
    const body = h('div.cv-body', { hidden: !c.open.has(r.n) });
    const node = h('div.cv-row' + (r.error ? '.err' : ''), { 'data-hue': w.hue || null },
      h('button.cv-line', { onclick: async () => {
        if (c.open.has(r.n)) { c.open.delete(r.n); body.hidden = true; return; }
        c.open.add(r.n); body.hidden = false;
        if (!body.firstChild) await fillBody(r, body);
      } },
        h('span.cv-t.mono', null, fmtT(r.t)),
        h('span.cv-who', null, h('i'), w.name),
        h('span.cv-step', null, (r.step || 'model call') + (r.round ? ` · r${r.round}` : '')),
        h('span.cv-arrow.micro', null, r.asked ? `asked ${r.asked}${r.role && r.role !== r.asked ? ' → ' + r.role : ''}` : ''),
        h('span.cv-ep.ellipsis', { title: r.endpoint }, r.endpoint),
        h('span.cv-num.mono', null, r.error ? 'failed' : `${kfmt(r.prompt_tok)}→${kfmt(r.tok)} · ${r.secs}s`)),
      h('div.cv-prev', null, r.error ? r.error : (r.title ? `[${r.title.slice(0, 70)}] ` : '') + (r.output || '')),
      body);
    if (c.open.has(r.n)) fillBody(r, body);
    return node;
  }
  function paintList() {
    clear(list);
    if (c.more) list.appendChild(h('button.btn.sm.cv-older', { onclick: async () => {
      const r = await get(url(c.rows.length ? c.rows[0].n : 0)); take(r); c.rows = [...r.rows, ...c.rows]; c.more = r.more; paintList();
    } }, `Show older (${c.matched - c.rows.length} more)`));
    if (!c.rows.length) list.appendChild(h('div.rp-empty', null, h('p.dim', null, c.total ? 'No exchange matches.' : 'No exchange yet: they appear as the agents start working.')));
    for (const r of c.rows) list.appendChild(row(r));
  }
  async function reload() {
    try { const r = await get(url(0)); take(r); c.rows = r.rows; c.more = r.more; }
    catch (e) { clear(list).appendChild(h('p.dim', null, e.message)); return; }
    paintTools(); paintHead(); paintList();
  }
  async function refresh() {  // live: new exchanges at the bottom, keeping what is open
    const sc = scroll();
    const atBottom = sc ? sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 80 : false;
    const last = c.rows.length ? c.rows[c.rows.length - 1].n : 0;
    try {
      const r = await get(url(0));
      const fresh = (r.rows || []).filter((x) => x.n > last);
      take(r);
      if (fresh.length) {
        if (!c.rows.length) clear(list);
        c.rows = [...c.rows, ...fresh]; for (const x of fresh) list.appendChild(row(x));
        paintHead(); if (atBottom && sc) sc.scrollTop = sc.scrollHeight;
      }
    } catch { /* next tick */ }
  }
  return { el, reload, refresh };
}

// A full-screen reader for one council or Talk thread: the panel, live while open, Esc to close.
let open = null;
export function openExchanges({ title, sub, scope, id, who }) {
  if (open) open();
  const enc = encodeURIComponent;
  const base = `/api/exchanges/${enc(scope)}/${enc(id)}`;
  const scrollEl = h('div.rp-scroll.xo-scroll');
  const panel = exchangePanel({ title: 'Every exchange, in full', sub, who, scroll: scrollEl,
    listUrl: (p) => `${base}?${p.toString()}`, fullUrl: (n) => `${base}/${n}` });
  const closeBtn = h('button.btn.ghost.icon', { title: 'Close (Esc)', 'aria-label': 'Close', onclick: () => close() }, icon('x', 18));
  const root = h('div.rp.xo', { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Conversations' },
    h('header.rp-bar', null, closeBtn, h('div.rp-bar-title.ellipsis', null, title || 'Conversations')),
    scrollEl);
  scrollEl.appendChild(h('div.rp-wrap', null, panel.el));
  document.body.appendChild(root);
  void root.offsetWidth;
  root.classList.add('open');
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  addEventListener('keydown', onKey);
  const timer = setInterval(() => { if (!document.hidden) panel.refresh(); }, 4000);
  panel.reload().then(() => { scrollEl.scrollTop = scrollEl.scrollHeight; });
  function close() {
    clearInterval(timer); removeEventListener('keydown', onKey);
    root.classList.remove('open'); setTimeout(() => root.remove(), 380);
    open = null;
  }
  open = close;
  return close;
}
void setText;
