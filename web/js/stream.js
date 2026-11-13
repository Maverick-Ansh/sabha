// The live transcript. Windowed: at most ~MAX events mounted; older load on scroll-up,
// replay jumps load a window around a seq. Used by Watch (all / agent) and the mission drawer.
import { h, icon, setText, clear } from './dom.js';
import { md } from './md.js';
import * as log from './events.js';
import { subscribe, agentById, getState, toolForCall } from './store.js';
import { fmtClock, kindOf, inputSummary, asText, statusLine, prettyJSON, hueOf, fmtDur, fmtBytes, truncate } from './format.js';

const MAX = 600, PAGE = 150, VIEW_CAP = 20000;
const FILTERS = ['all', 'thoughts', 'tools', 'results', 'errors'];
const FILTER_MATCH = {
  all: (k) => k !== 'entity',
  thoughts: (k) => k === 'think' || k === 'text' || k === 'final' || k === 'plan' || k === 'human',
  tools: (k, t) => t === 'run.tool_call' || t === 'run.tool_result',
  results: (k, t) => t === 'run.tool_result',
  errors: (k) => k === 'err',
};

export function createStream(opts = {}) {
  const st = { agent: opts.agent || null, mission: opts.mission || null, filter: 'all', raw: false,
    ready: false, detached: false, replay: false, stick: true, pending: 0, count: 0, lastSeq: 0,
    hasOlder: false, busy: false, gen: 0, partial: true };
  const callRows = new Map(), looseResults = new Map();
  let queue = [], flushTimer = 0;

  // ---------- DOM ----------
  const scopeEl = h('div.stream-scope.ellipsis');
  const liveEl = h('span.stream-live', null, 'live');
  const filterBtns = FILTERS.map((f) => h('button', { 'aria-pressed': String(f === 'all'), onclick: () => setFilter(f) }, f));
  const rawBtn = h('button.btn.ghost.sm.raw-toggle', { title: 'Show the exact JSON of every event', 'aria-pressed': 'false', onclick: () => setRaw(!st.raw) }, icon('braces', 14), 'raw');
  const sentinel = h('div.stream-sentinel');
  const rowsEl = h('div.stream-rows');
  const scroller = h('div.stream-list.scroll', null, sentinel, rowsEl);
  const emptyEl = h('div.stream-empty', null, h('div.display.italic', null, 'Nothing on the floor yet.'), h('div.micro', null, 'events appear here as agents work'));
  const pill = h('button.stream-pill', { hidden: true, onclick: onPill });
  const root = h('div', { class: 'stream' + (opts.compact ? ' compact' : '') },
    h('div.stream-head', null, h('div.stream-title', null, scopeEl, liveEl),
      h('div.stream-tools', null, h('div.seg.seg-sm.stream-filters', null, filterBtns), rawBtn)),
    h('div.stream-body', null, scroller, emptyEl, pill));

  // ---------- matching ----------
  const match = (e) => (!st.mission || e.m === st.mission) && (!st.agent || e.a === st.agent) && FILTER_MATCH[st.filter](e.k, e.type);
  const matchEv = (ev) => match({ k: kindOf(ev), type: ev.type, a: ev.agent_id, m: ev.mission_id });

  // ---------- rendering ----------
  const showWho = () => !st.agent && !st.mission;
  function shell(ev, cls, ...body) {
    const a = agentById(ev.agent_id);
    const peek = h('button.ev-peek', { title: 'Show raw JSON', onclick: (e) => { e.stopPropagation(); togglePeek(row); } }, icon('braces', 12));
    const row = h('div', { class: 'ev ' + cls, 'data-hue': hueOf(a) },
      h('div.ev-time', null, fmtClock(ev.ts), showWho() && a ? h('span.ev-who', null, a.name) : null),
      h('div.ev-main', null, body), st.raw ? null : peek);
    row._seqs = [ev.seq]; row._evs = [ev]; row._kind = cls.split(' ')[0]; row._m = ev.mission_id;
    return row;
  }
  function togglePeek(row) {
    let pre = row.querySelector(':scope > .ev-main > .ev-env');
    if (pre) { pre.remove(); return; }
    pre = h('pre.ev-env', null, row._evs.map((e) => prettyJSON(e)).join('\n\n'));
    row.querySelector('.ev-main').appendChild(pre);
  }
  function clampBlock(text, cls, limit) {
    const shown = text.length > VIEW_CAP ? text.slice(0, VIEW_CAP) : text;
    const body = h('div', { class: cls }, shown);
    const long = text.length > limit || text.split('\n').length > 6;
    if (!long) return body;
    body.classList.add('clamp');
    const btn = h('button.ev-more', { onclick: () => {
      const open = body.classList.toggle('clamp');
      if (!open && text.length > VIEW_CAP && body.textContent.length < text.length) body.textContent = text;
      setText(btn, open ? `show more · ${fmtBytes(text.length)}` : 'show less');
    } }, `show more · ${fmtBytes(text.length)}`);
    return h('div.ev-clampwrap', null, body, btn);
  }
  function sep(ev, label, cls = '') {
    const row = shell(ev, 'sep ' + cls, h('div.ev-sep', null, h('span.ev-sepline'), h('span.ev-seplabel', null, label), h('span.ev-sepline')));
    return row;
  }
  function resultEl(ev, callTs) {
    const d = ev.data || {};
    const bad = d.status === 'error' || !!d.error;
    const out = asText(d.result);
    const dur = callTs ? ev.ts - callTs : null;
    return h('div', { class: 'ev-result ' + (bad ? 'err' : 'ok') },
      h('div.ev-reshead', null, h('span.ev-tick', null, icon(bad ? 'x' : 'check', 12, 2.2)),
        h('span', null, bad ? 'error' : 'completed'),
        out ? h('span.faint', null, fmtBytes(out.length)) : null,
        dur != null && dur >= 0 ? h('span.faint', null, dur < 1 ? `${Math.round(dur * 1000)} ms` : fmtDur(dur)) : null),
      d.error ? h('div.ev-errline', null, asText(d.error)) : null,
      out ? clampBlock(out, 'ev-out', 520) : null);
  }

  // Render one event; merges / attaches when it can. `into` is a fragment or rowsEl.
  function render(ev, into, prepend, animate) {
    st.count++;
    if (ev.seq > st.lastSeq) st.lastSeq = ev.seq;
    const d = ev.data || {};
    const prev = prepend ? into.lastChild : (into.lastChild || (into !== rowsEl ? rowsEl.lastChild : null));
    if (st.raw) return add(shell(ev, 'rawrow', h('div.ev-rawhead', null, h('span.chip.mono', null, ev.type), h('span.faint.mono', null, `seq ${ev.seq}`)), h('pre.ev-env.open', null, prettyJSON(ev))));
    const mergeable = (kind) => prev && prev._kind === kind && prev._m === ev.mission_id && ev.ts - prev._evs[prev._evs.length - 1].ts < 2.5;
    switch (ev.type) {
      case 'run.thinking': case 'run.text': {
        const kind = ev.type === 'run.thinking' ? 'think' : 'text';
        if (mergeable(kind) && prev._text.length < 40000) {
          prev._text += d.text || ''; prev._seqs.push(ev.seq); prev._evs.push(ev);
          const slot = prev.querySelector('.ev-main');
          const old = slot.querySelector('.ev-clampwrap, .ev-think, .ev-text');
          old.replaceWith(clampBlock(prev._text, 'ev-' + kind, kind === 'think' ? 360 : 1400));
          return;
        }
        const row = shell(ev, kind, clampBlock(d.text || '', 'ev-' + kind, kind === 'think' ? 360 : 1400));
        row._text = d.text || '';
        return add(row);
      }
      case 'run.tool_call': {
        const json = h('pre.ev-json', { hidden: true }, prettyJSON(d.input));
        const durEl = h('span.ev-dur');
        const head = h('button.ev-callhead', { onclick: () => { json.hidden = !json.hidden; head.classList.toggle('open', !json.hidden); } },
          h('span.chip.mono.tool', null, d.tool || 'tool'), h('span.ev-sum.ellipsis', null, truncate(inputSummary(d.tool, d.input), 300)),
          durEl, h('span.ev-pend'), icon('chev-down', 14));
        const slot = h('div.ev-results');
        const row = shell(ev, 'call', head, json, slot);
        row._slot = slot; row._dur = durEl; row._callId = d.call_id;
        if (d.call_id) {
          callRows.set(d.call_id, row);
          const loose = looseResults.get(d.call_id);
          if (loose && prepend) { // result already mounted on its own: pull it under its call
            looseResults.delete(d.call_id);
            attach(row, loose._evs[0]);
            row._seqs.push(...loose._seqs); row._evs.push(...loose._evs);
            loose.remove();
          }
        }
        return add(row);
      }
      case 'run.tool_result': {
        const call = d.call_id && callRows.get(d.call_id);
        if (call && st.filter !== 'results') { attach(call, ev); call._seqs.push(ev.seq); call._evs.push(ev); return; }
        const tool = toolForCall(d.call_id);
        const row = shell(ev, 'result loose', h('div.ev-orphan.micro', null, `result · ${tool || d.call_id || 'tool'}`), resultEl(ev));
        if (d.call_id) { row._callId = d.call_id; looseResults.set(d.call_id, row); }
        return add(row);
      }
      case 'run.status': {
        const label = statusLine(d);
        if (prev && prev._kind === 'sep' && prev._status && prev._m === ev.mission_id) {
          setText(prev.querySelector('.ev-seplabel'), label); prev._seqs.push(ev.seq); prev._evs.push(ev); return;
        }
        const row = sep(ev, label, 'sep-status'); row._status = true; return add(row);
      }
      case 'run.started': return add(sep(ev, `run ${d.run || 1} · ${d.harness || 'harness'} · ${d.resumed ? 'resumed' : 'started'}`, 'sep-run'));
      case 'run.ended': return add(sep(ev, `run ${d.run || ''} ended · exit ${d.exit_code ?? '?'}${d.duration_s ? ' · ' + fmtDur(d.duration_s) : ''}`, d.ok === false ? 'sep-run sep-bad' : 'sep-run'));
      case 'run.waiting': return add(sep(ev, `waiting · ${d.name || 'the model'} is still loading`, 'sep-run'));
      case 'human.approved': return add(sep(ev, `plan approved · ${(d.steps || []).length} steps`, 'sep-human'));
      case 'human.stopped': return add(sep(ev, 'stopped by you', 'sep-human sep-bad'));
      case 'human.note': return add(shell(ev, 'note', h('div.micro', null, d.note && d.note.interrupt ? 'note from you · interrupt' : 'note from you'), h('div.ev-text', null, (d.note && d.note.text) || '')));
      case 'plan.proposed': return add(shell(ev, 'plan', h('div.micro', null, 'proposed plan'), h('ol.ev-steps', null, (d.steps || []).map((s) => h('li', null, typeof s === 'string' ? s : s.text || '')))));
      case 'run.final': {
        const reason = d.reason && (d.reason.kind || d.reason);
        return add(shell(ev, 'final', h('div.ev-finalhead', null, h('span.micro', null, 'final report'), reason ? h('span.chip.mono', null, String(reason)) : null), md(d.text || '')));
      }
      case 'run.error': return add(shell(ev, 'error', h('div.micro', null, 'error'), h('div.ev-text', null, d.message || '')));
      case 'run.stderr': {
        if (prev && prev._kind === 'stderr' && prev._m === ev.mission_id && prev._evs.length < 40) {
          prev.querySelector('.ev-stderr').append('\n' + (d.line || '')); prev._seqs.push(ev.seq); prev._evs.push(ev); return;
        }
        return add(shell(ev, 'stderr', h('div.ev-stderr', null, d.line || '')));
      }
      case 'run.raw': return add(shell(ev, 'stderr', h('div.ev-stderr', null, 'raw · ' + truncate(asText(d.raw).replace(/\s+/g, ' '), 600))));
      default: return add(shell(ev, 'stderr', h('div.ev-stderr', null, ev.type)));
    }
    function add(row) { if (animate) row.classList.add('rise'); into.appendChild(row); }
  }
  function attach(callRow, ev) {
    const callTs = callRow._evs[0].ts;
    callRow._slot.appendChild(resultEl(ev, callTs));
    callRow.classList.add('has-result');
    if ((ev.data || {}).status === 'error' || (ev.data || {}).error) callRow.classList.add('bad');
    const dur = ev.ts - callTs;
    if (dur >= 0) setText(callRow._dur, dur < 1 ? `${Math.round(dur * 1000)} ms` : fmtDur(dur));
  }

  function dropRow(row) {
    st.count -= row._seqs.length;
    if (row._callId) { if (callRows.get(row._callId) === row) callRows.delete(row._callId); if (looseResults.get(row._callId) === row) looseResults.delete(row._callId); }
    row.remove();
  }
  function clearRows() { clear(rowsEl); callRows.clear(); looseResults.clear(); st.count = 0; st.lastSeq = 0; }
  function trimTop() {
    if (st.count <= MAX) return;
    const before = scroller.scrollHeight;
    while (st.count > MAX && rowsEl.firstChild) dropRow(rowsEl.firstChild);
    st.hasOlder = true;
    if (!st.stick) scroller.scrollTop -= before - scroller.scrollHeight;
  }
  function trimBottom() {
    while (st.count > MAX && rowsEl.lastChild) dropRow(rowsEl.lastChild);
    st.lastSeq = 0;
    for (const r of rowsEl.children) for (const s of r._seqs) if (s > st.lastSeq) st.lastSeq = s;
    st.detached = true;
  }
  const firstSeq = () => (rowsEl.firstChild ? rowsEl.firstChild._seqs[0] : null);
  const atBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };
  function renderList(evs, prepend, animate) {
    const frag = document.createDocumentFragment();
    for (const ev of evs) render(ev, frag, prepend, animate);
    return frag;
  }

  // ---------- windows ----------
  async function resetTail() {
    const gen = ++st.gen;
    st.ready = false; queue = [];
    const list = log.query(match, { limit: 240 });
    const evs = await log.fetchFull(list, st.mission);
    if (gen !== st.gen) return;
    clearRows();
    rowsEl.appendChild(renderList(evs, false, false));
    const last = list.length ? list[list.length - 1].seq : 0;
    const newer = log.query(match, { after: last, limit: MAX }).map((e) => log.full(e.seq)).filter(Boolean);
    if (newer.length) rowsEl.appendChild(renderList(newer, false, false));
    st.hasOlder = !!(list.length && log.query(match, { before: list[0].seq, limit: 1 }).length);
    st.partial = !log.isHistoryDone();
    Object.assign(st, { ready: true, detached: false, replay: false, pending: 0, stick: true });
    updateChrome();
    toBottom();
    requestAnimationFrame(toBottom);
  }

  async function loadOlder() {
    const first = firstSeq();
    if (st.busy || !st.hasOlder || first == null) return;
    st.busy = true; sentinel.classList.add('busy');
    const gen = st.gen;
    const list = log.query(match, { before: first, limit: PAGE });
    const evs = await log.fetchFull(list, st.mission);
    st.busy = false; sentinel.classList.remove('busy');
    if (gen !== st.gen) return;
    if (!list.length) { st.hasOlder = false; updateChrome(); return; }
    const h0 = scroller.scrollHeight, t0 = scroller.scrollTop;
    rowsEl.insertBefore(renderList(evs, true, false), rowsEl.firstChild);
    scroller.scrollTop = t0 + (scroller.scrollHeight - h0);
    st.hasOlder = log.query(match, { before: list[0].seq, limit: 1 }).length > 0;
    if (st.count > MAX) trimBottom();
    updateChrome();
  }

  async function loadNewer() {
    if (st.busy || !st.detached) return;
    st.busy = true;
    const gen = st.gen;
    const list = log.query(match, { after: st.lastSeq, limit: PAGE });
    const evs = await log.fetchFull(list, st.mission);
    st.busy = false;
    if (gen !== st.gen) return;
    if (evs.length) {
      rowsEl.appendChild(renderList(evs, false, false));
      const keep = st.stick; st.stick = false; trimTop(); st.stick = keep;
    }
    if (list.length < PAGE) { st.detached = false; st.pending = 0; }
    updateChrome();
  }

  async function jumpTo(seq) {
    const gen = ++st.gen;
    st.ready = false; queue = [];
    const before = log.query(match, { before: seq, limit: 60 });
    const after = log.query(match, { after: seq - 1, limit: 260 });
    const evs = await log.fetchFull(before.concat(after), st.mission);
    if (gen !== st.gen) return;
    clearRows();
    rowsEl.appendChild(renderList(evs, false, false));
    st.hasOlder = !!(before.length && log.query(match, { before: before[0].seq, limit: 1 }).length);
    const last = after.length ? after[after.length - 1].seq : seq;
    Object.assign(st, { ready: true, replay: true, stick: false, pending: 0, detached: log.query(match, { after: last, limit: 1 }).length > 0 });
    updateChrome();
    const target = Array.from(rowsEl.children).find((r) => r._seqs.some((s) => s >= seq));
    if (target) {
      requestAnimationFrame(() => { scroller.scrollTop = Math.max(0, target.offsetTop - 40); });
      target.classList.add('flash');
      setTimeout(() => target.classList.remove('flash'), 1600);
    }
  }

  // ---------- live ----------
  function push(ev) {
    if (!st.ready || !matchEv(ev)) return;
    if (st.detached) { st.pending++; updateChrome(); return; }
    queue.push(ev);
    if (!flushTimer) flushTimer = setTimeout(flush, 60);
  }
  function flush() {
    flushTimer = 0;
    let evs = queue.filter((e) => e.seq > st.lastSeq || !e.seq);
    queue = [];
    if (!evs.length || !st.ready) return;
    if (st.detached) { st.pending += evs.length; updateChrome(); return; }
    if (evs.length > MAX) { clearRows(); evs = evs.slice(-MAX); st.hasOlder = true; }
    const stick = st.stick;
    rowsEl.appendChild(renderList(evs, false, !document.hidden && evs.length < 30));
    if (stick) { trimTop(); toBottom(); }
    else { st.pending += evs.length; if (st.count > MAX + 300) st.detached = true; }
    updateChrome();
  }
  function onPill() {
    if (st.detached || st.replay) { resetTail(); opts.onLive && opts.onLive(); }
    else { st.pending = 0; st.stick = true; toBottom(); trimTop(); updateChrome(); }
  }

  scroller.addEventListener('scroll', () => {
    const bottom = atBottom();
    st.stick = bottom && !st.detached;
    if (bottom && !st.detached && st.pending) { st.pending = 0; trimTop(); updateChrome(); }
    if (scroller.scrollTop < 160) loadOlder();
    if (st.detached && bottom) loadNewer();
  }, { passive: true });

  function updateChrome() {
    const a = st.agent && agentById(st.agent);
    const m = st.mission && getState().missions[st.mission];
    setText(scopeEl, opts.title || (m ? m.title : a ? a.name : 'Everyone'));
    liveEl.className = 'stream-live' + (st.replay || st.detached ? ' off' : '');
    setText(liveEl, st.replay ? 'replay' : st.detached ? 'history' : 'live');
    filterBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(FILTERS[i] === st.filter)));
    rawBtn.setAttribute('aria-pressed', String(st.raw));
    root.classList.toggle('raw', st.raw);
    sentinel.classList.toggle('more', st.hasOlder);
    setText(sentinel, st.hasOlder ? 'earlier events' : st.count ? 'beginning of the record' : '');
    emptyEl.hidden = st.count > 0 || !st.ready;
    if (st.detached || st.replay) { pill.hidden = false; replacePill(st.pending ? `${st.pending} new · back to live` : 'back to live', 'down'); }
    else if (!st.stick && st.pending) { pill.hidden = false; replacePill(`${st.pending} new`, 'down'); }
    else pill.hidden = true;
  }
  function replacePill(label, ic) { clear(pill); pill.append(label, icon(ic, 13)); }

  function setFilter(f) { if (st.filter !== f) { st.filter = f; resetTail(); } }
  function setRaw(v) { st.raw = !!v; resetTail(); }
  function setScope({ agent = null, mission = null } = {}) {
    if (st.agent === agent && st.mission === mission && st.ready) return;
    st.agent = agent; st.mission = mission; resetTail();
  }

  const unsubs = [
    subscribe('run', ({ ev }) => push(ev)),
    subscribe('tail', () => resetTail()),
    subscribe('index', () => { if (st.partial && log.isHistoryDone() && !st.detached && st.count < 80) resetTail(); }),
    subscribe('missions', () => { if (st.mission) setText(scopeEl, opts.title || (getState().missions[st.mission] || {}).title || ''); }),
  ];
  if (log.isTailReady()) resetTail(); else updateChrome();

  return {
    el: root, setScope, setFilter, jumpTo, resetTail,
    get replaying() { return st.replay; },
    destroy() { st.gen++; unsubs.forEach((u) => u()); clearTimeout(flushTimer); root.remove(); },
  };
}
