// Research mode: the research daemon's mission on one screen. Agent tiles (one per track), a sheet
// per agent (now doing, summary, cards, instructions, clear context, pause), and a new-mission composer.
// Data comes from GET /api/research every 3 s; commands go to POST /api/research/command (PROTOCOL section 8).
import { h, icon, setText, clear, autogrow } from './dom.js';
import { get, post, enc } from './api.js';
import { fmtAgo, fmtDur, serverNow } from './format.js';
import { renderInto } from './md.js';
import { toast } from './notify.js';
import { createTaste } from './taste.js';

const POLL_MS = 3000;
const SHEET_MS = 4000;
const STEPS = ['fetch', 'gate', 'map', 'reduce', 'verify'];
const STEP_LABEL = { fetch: 'Fetch', gate: 'Gate', map: 'Read', reduce: 'Write card', verify: 'Verify' };
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const sig = (v) => JSON.stringify(v == null ? null : v);

// ---------- words ----------
function verb(s) {
  const step = s.step || 'idle';
  if (s.kind === 'worker') {
    if (step === 'map') return s.n ? `Reading part ${Math.min((s.i || 0) + 1, s.n)} of ${s.n}` : 'Reading';
    return { fetch: 'Fetching the paper', gate: 'Checking if it is worth a full read', reduce: 'Writing the card', verify: 'Checking quotes and code links' }[step] || step;
  }
  if (step === 'scouting') return s.query ? `Searching "${s.query}"` : 'Planning the next searches';
  return { consolidating: 'Updating the claims ledger', report: 'Rewriting the report', synthesis: 'Writing the synthesis',
    seeds: 'Resolving seed papers', snowballing: 'Sorting cited papers' }[step] || step;
}
function nowLine(a, alive) {
  if (a.now && a.now.length) {
    const s = a.now[0];
    const more = a.now.length > 1 ? `  +${a.now.length - 1} more` : '';
    return (s.kind === 'worker' ? `${verb(s)} · ${s.title || s.paper || ''}` : verb(s)) + more;
  }
  if (!alive) return 'The daemon is not running';
  if (a.paused) return 'Paused. No new papers, no searching.';
  if (a.saturated) return 'Searches stopped finding new papers';
  if (a.last_card) return `Last card ${fmtAgo(a.last_card.t)} · ${a.last_card.title}`;
  return a.counts.queued ? 'Waiting for a free reader' : 'Waiting for papers';
}
function stateLabel(a, alive) {
  if (a.now && a.now.length) {
    const readers = a.now.filter((s) => s.kind === 'worker').length;
    if (readers) return readers > 1 ? `reading ${readers}` : 'reading';
    return { scouting: 'searching', consolidating: 'thinking', report: 'writing' }[a.now[0].step] || 'working';
  }
  if (!alive) return 'offline';
  return a.state === 'working' ? 'working' : a.state;
}
const leftText = (m) => (m == null ? '–' : m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`);

// Count a number up or down to its new value.
function setNum(el, v) {
  const to = Number(v) || 0;
  const from = Number(el.dataset.v || 0);
  el.dataset.v = String(to);
  if (from === to || reduced() || !el.isConnected || el.dataset.init !== '1') { el.dataset.init = '1'; el.textContent = String(to); return; }
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 480);
    const e = 1 - Math.pow(1 - k, 3);
    if (Number(el.dataset.v) !== to) return;
    el.textContent = String(Math.round(from + (to - from) * e));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function createResearch() {
  let data = null, active = false, timer = 0, busy = false, wanted = null, sheet = null, lastMission = '', built = '';
  const tiles = new Map();

  // ---------- hero ----------
  const liveDot = h('span.r-livedot');
  const eyebrowText = h('span.micro', null, 'research');
  const missionSel = h('select.select.r-missions', { hidden: true, 'aria-label': 'Mission', onchange: () => { wanted = missionSel.value || null; tick(); } });
  const titleEl = h('h1.display.r-title');
  const ledeEl = h('p.r-lede', { onclick: () => ledeEl.classList.toggle('open') });
  const pulseEl = h('div.r-pulse');
  const nCards = h('span.r-stat-n'), nQueued = h('span.r-stat-n'), nRate = h('span.r-stat-n'), nLeft = h('span.r-stat-n');
  const stat = (n, label) => h('div.r-stat', null, n, h('span.micro', null, label));
  const stats = h('div.r-stats.card', null, stat(nCards, 'cards'), stat(nQueued, 'queued'), stat(nRate, 'per hour'), stat(nLeft, 'left'));
  const rawEl = h('button.r-raw.mono', { title: 'The daemon\'s own status line', onclick: () => rawEl.classList.toggle('open') });
  const synthBtn = h('button.btn', { hidden: true, onclick: () => openDoc('Synthesis', 'synthesis') }, icon('layers', 15), 'Synthesis');
  const newBtn = h('button.btn.brand', { onclick: () => openMission() }, icon('plus', 15), 'New mission');
  const actions = h('div.r-actions', null,
    newBtn,
    h('button.btn', { onclick: () => openDoc('Status', 'status') }, icon('log', 15), 'Status'),
    synthBtn);
  const hero = h('header.r-hero', null,
    h('div.r-eyebrow', null, liveDot, eyebrowText, h('span.spacer'), missionSel),
    titleEl, ledeEl, pulseEl, stats, rawEl, actions);

  // ---------- body ----------
  const banner = h('div.r-banner', { hidden: true, role: 'status' });
  const grid = h('div.r-grid');
  const allIn = h('textarea.textarea.r-input', { rows: 1, placeholder: 'An instruction for every agent…', 'aria-label': 'Instruction for every agent' });
  const allFit = autogrow(allIn, 140);
  const allBtn = h('button.btn.ink.icon.r-send', { title: 'Send to every agent', onclick: () => sendAll() }, icon('up', 17));
  const allList = h('div.r-list');
  const cmdList = h('div.r-list');
  const feedList = h('div.r-list');
  const allCard = h('section.card.r-card', null,
    h('div.r-card-head', null, h('h2.r-h2', null, 'Tell every agent'), h('span.micro', null, 'standing instruction')),
    h('div.r-compose', null, allIn, allBtn), allList);
  const cmdCard = h('section.card.r-card', { hidden: true }, h('div.r-card-head', null, h('h2.r-h2', null, 'Your commands')), cmdList);
  const feedCard = h('section.card.r-card', { hidden: true }, h('div.r-card-head', null, h('h2.r-h2', null, 'Latest')), feedList);
  const empty = h('div.r-empty', { hidden: true });
  const bodyEl = h('div.r-body', null, banner, grid, h('div.r-lower', null, allCard, cmdCard, feedCard));
  const taste = createTaste({ onChange: () => setTimeout(tick, 400) });
  const el = h('div.view.research', { 'data-view': 'research' }, h('div.r-scroll.scroll', null, h('div.r-col', null, hero, taste.el, bodyEl, empty)));
  allIn.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendAll(); } });

  // ---------- tiles ----------
  function makeTile(a, i) {
    const r = {};
    r.el = h('button.r-tile.card', { 'data-hue': a.hue, style: { '--i': String(i) }, onclick: () => openAgent(a.id) },
      h('div.r-tile-top', null, r.orb = h('span.r-orb'), h('span.micro.r-tid', null, a.id), r.flags = h('span.r-flags'), h('span.spacer'), r.state = h('span.r-state')),
      r.name = h('div.r-name'),
      r.now = h('div.r-now'),
      h('div.r-tile-foot', null,
        h('div.r-count', null, r.cards = h('span.r-num'), h('span.r-unit', null, 'cards')),
        r.meta = h('div.r-meta')),
      h('span.r-sweep', { 'aria-hidden': 'true' }, h('span')));
    return r;
  }
  function patchTile(r, a, alive) {
    const working = !!(a.now && a.now.length);
    r.el.className = 'r-tile card ' + (working ? 'working' : alive ? a.state : 'offline');
    setText(r.name, a.name);
    setText(r.now, nowLine(a, alive));
    setText(r.state, stateLabel(a, alive));
    setNum(r.cards, a.counts.carded);
    const bits = [`${a.counts.queued} queued`];
    if (a.counts.reading) bits.push(`${a.counts.reading} reading`);
    if (a.counts.failed) bits.push(`${a.counts.failed} failed`);
    setText(r.meta, bits.join(' · '));
    const flags = sig([a.instructions, a.focus, a.pending]);
    if (r.flagSig !== flags) {
      r.flagSig = flags;
      clear(r.flags);
      if (a.instructions) r.flags.appendChild(h('span.r-flag', { title: 'standing instructions' }, icon('pen', 11), String(a.instructions)));
      if (a.focus) r.flags.appendChild(h('span.r-flag', { title: 'priority topics' }, icon('zap', 11), String(a.focus)));
      if (a.pending) r.flags.appendChild(h('span.r-flag.wait', { title: 'commands waiting for the daemon' }, 'sending'));
    }
    r.el.setAttribute('aria-label', `${a.name}: ${stateLabel(a, alive)}, ${a.counts.carded} cards`);
  }

  // ---------- render ----------
  function render() {
    const d = data;
    if (!d) return;
    taste.update(d);
    const m = d.mission;
    bodyEl.hidden = !m; hero.classList.toggle('bare', !m);
    stats.hidden = !m; rawEl.hidden = !m; pulseEl.hidden = !m;
    if (!d.available || !m) {
      empty.hidden = false;
      clear(empty);
      setText(eyebrowText, 'research');
      liveDot.className = 'r-livedot';
      if (!d.available) {
        actions.hidden = true; ledeEl.hidden = true; missionSel.hidden = true;
        setText(titleEl, 'Research is not connected.');
        empty.append(h('p.dim', null, d.hint || 'This hub has no research data repo.'),
          h('p.mono.dim', null, 'python -m sabha --research-repo /kaggle/working/sabha-research'));
      } else {
        actions.hidden = true; ledeEl.hidden = true;
        setText(titleEl, 'No research yet.');
        empty.append(h('p.dim', null, 'Write your research taste and a question above, then press Start research. The agents split it into tracks, read papers for hours and report back here.'));
      }
      return;
    }
    empty.hidden = true; actions.hidden = false; ledeEl.hidden = false;
    const alive = d.daemon && d.daemon.alive;
    newBtn.hidden = !alive || !!(d.run && d.run.mission); // queued briefs are for a git-driven daemon; app runs start from the taste form
    const st = m.stats || {};

    // hero (a mission that is done is finished, not offline: no daemon is expected any more)
    const done = m.status === 'done';
    setText(eyebrowText, m.status === 'running' && alive ? 'research · live' : done ? 'research · finished' : `research · ${alive ? m.status : 'daemon offline'}`);
    liveDot.className = 'r-livedot ' + (alive && m.status === 'running' ? 'live' : alive || done ? 'idle' : 'off');
    setText(titleEl, m.title || m.id);
    setText(ledeEl, m.one_line || '');
    const working = d.agents.filter((a) => a.now && a.now.length).length;
    const bits = [`${working} of ${d.agents.length} agents working`];
    if (d.workers && d.workers.total) bits.push(`${d.workers.busy} of ${d.workers.total} readers busy`);
    for (const t of d.team || []) bits.push(verb(t).toLowerCase());
    if (st.errors) bits.push(`${st.errors} errors`);
    setText(pulseEl, alive ? bits.join(' · ') : done ? 'Finished. Read the synthesis, each agent\'s report and the cards, or download everything above.' : 'The research daemon is not reporting.');
    setNum(nCards, st.cards); setNum(nQueued, st.queued); setNum(nRate, st.per_hour);
    setText(nLeft, leftText(st.left_m));
    setText(rawEl, m.status_line || 'no status line yet');
    synthBtn.hidden = !m.has_synthesis;

    // mission switcher (only when there is more than one)
    const ms = d.missions || [];
    const msig = sig(ms.map((x) => [x.id, x.status])) + m.id;
    if (missionSel.dataset.sig !== msig) {
      missionSel.dataset.sig = msig;
      clear(missionSel);
      for (const x of ms) missionSel.appendChild(h('option', { value: x.id, selected: x.id === m.id }, `${x.id} · ${x.status}`));
      missionSel.hidden = ms.length < 2;
    }

    // banner
    const dead = !alive && !done;
    banner.hidden = !dead && !m.error;
    if (dead) setText(banner, `The research daemon has not reported${d.daemon.age_s != null ? ' for ' + fmtDur(d.daemon.age_s, true) : ''}. You can still read everything. Commands wait in its inbox until it runs.`);
    else if (m.error) setText(banner, m.error);

    // tiles
    const ids = m.id + '|' + d.agents.map((a) => a.id).join(',');
    if (built !== ids) {
      built = ids; tiles.clear(); clear(grid);
      d.agents.forEach((a, i) => { const r = makeTile(a, i); tiles.set(a.id, r); grid.appendChild(r.el); });
      if (!d.agents.length) grid.appendChild(h('div.empty-note', null, m.status === 'waiting' ? 'This mission is waiting for the daemon to plan it.' : 'No agents yet: the daemon is planning the mission.'));
    }
    for (const a of d.agents) patchTile(tiles.get(a.id), a, alive || done);
    if (lastMission !== m.id) { lastMission = m.id; if (sheet && sheet.kind === 'agent') closeSheet(); }

    // standing instructions for everyone
    const allSig = sig([d.standing, (d.commands.pending || []).filter((c) => c.track === 'all')]);
    if (allList.dataset.sig !== allSig) {
      allList.dataset.sig = allSig;
      clear(allList);
      for (const x of (d.standing && d.standing.instructions) || []) allList.appendChild(row('pen', x.text, fmtAgo(tsOf(x.ts))));
      for (const x of (d.standing && d.standing.focus) || []) allList.appendChild(row('zap', `Priority topic: ${x.topic}`, fmtAgo(tsOf(x.ts))));
      for (const c of (d.commands.pending || []).filter((c) => c.track === 'all' && (c.cmd === 'instruct' || c.cmd === 'focus'))) allList.appendChild(row('send', c.text || c.topic, 'sending…', 'wait'));
    }

    // command log
    const cs = sig(d.commands);
    if (cmdList.dataset.sig !== cs) {
      cmdList.dataset.sig = cs;
      clear(cmdList);
      for (const c of d.commands.pending || []) cmdList.appendChild(row('send', `${c.cmd} ${c.track || ''} ${c.text || c.topic || c.title || ''}`.trim(), 'waiting for the daemon', 'wait'));
      for (const c of d.commands.applied || []) cmdList.appendChild(row(c.ok ? 'check' : 'x', `${c.cmd} ${c.track || ''}`.trim(), c.result, c.ok ? '' : 'bad'));
      cmdCard.hidden = !cmdList.firstChild;
    }

    // feed
    const fs = sig((d.feed || []).map((e) => [e.t, e.kind, e.id]));
    if (feedList.dataset.sig !== fs) {
      feedList.dataset.sig = fs;
      clear(feedList);
      for (const e of d.feed || []) feedList.appendChild(feedRow(e, d.agents));
      feedCard.hidden = !feedList.firstChild;
    }
  }

  const tsOf = (iso) => { const t = Date.parse(iso || ''); return Number.isFinite(t) ? t / 1000 : 0; };
  function row(ic, text, sub, cls) {
    return h('div', { class: 'r-row' + (cls ? ' ' + cls : '') }, h('span.r-row-ic', null, icon(ic, 13)),
      h('div.r-row-text', null, h('div', null, text || ''), sub ? h('div.r-row-sub', null, sub) : null));
  }
  function feedRow(e, agents) {
    const a = agents && agents.find((x) => x.id === e.track);
    const node = h(e.kind === 'carded' && e.id ? 'button' : 'div', { class: 'r-row feed' + (e.ok === false || e.kind === 'error' ? ' bad' : ''), 'data-hue': a ? a.hue : null },
      h('span.r-row-ic', null, a ? h('span.dot') : icon(e.kind === 'command' ? 'send' : 'log', 13)),
      h('div.r-row-text', null, h('div', null, e.text), h('div.r-row-sub', null, [a ? a.name : null, fmtAgo(e.t)].filter(Boolean).join(' · '))));
    if (e.kind === 'carded' && e.id) node.addEventListener('click', () => openCardSheet(e.id));
    return node;
  }

  // ---------- polling ----------
  const q = () => (wanted ? `?mission=${enc(wanted)}` : '');
  async function tick() {
    if (busy) return;
    busy = true;
    try { data = await get('/api/research' + q()); render(); } catch (e) {
      if (e.status !== 401 && !data) { data = { available: false, hint: `The hub did not answer: ${e.message}` }; render(); data = null; }
    } finally { busy = false; }
  }
  function start() { stop(); tick(); timer = setInterval(() => { if (!document.hidden) tick(); }, POLL_MS); }
  function stop() { clearInterval(timer); timer = 0; }
  document.addEventListener('visibilitychange', () => { if (active && !document.hidden) tick(); });

  // ---------- commands ----------
  async function command(body, okTitle) {
    try {
      const r = await post('/api/research/command', body);
      toast(okTitle, { sub: r.note || '', tone: r.daemon_alive === false && r.applied_by !== 'hub' ? 'sun' : 'mint', timeout: 4500 });
      tick();
      if (sheet && sheet.reload) sheet.reload();
      return r;
    } catch (e) { toast('Not sent', { sub: e.message, tone: 'coral' }); return null; }
  }
  async function sendAll() {
    const text = allIn.value.trim();
    if (!text) { allIn.focus(); return; }
    allBtn.disabled = true;
    const r = await command({ cmd: 'instruct', track: 'all', text }, 'Sent to every agent');
    allBtn.disabled = false;
    if (r) { allIn.value = ''; allFit(); }
  }

  // ---------- sheet shell (bottom sheet on a phone, side panel on a wide screen) ----------
  function closeSheet() {
    const s = sheet;
    if (!s) return;
    sheet = null;
    clearInterval(s.timer);
    removeEventListener('keydown', s.onKey);
    s.el.classList.remove('open'); s.overlay.classList.remove('open');
    setTimeout(() => { s.el.remove(); s.overlay.remove(); }, reduced() ? 0 : 420);
  }
  function openSheet(kind, hue, titleNode, bodyNode) {
    closeSheet();
    const s = { kind, timer: 0, reload: null };
    s.onKey = (e) => { if (e.key === 'Escape') { if (s.back && !s.docEl.hidden) s.back(); else closeSheet(); } };
    s.overlay = h('div.r-overlay', { onclick: closeSheet });
    const head = h('header.r-sheet-head', null, titleNode, h('button.btn.ghost.icon.r-close', { title: 'Close', 'aria-label': 'Close', onclick: closeSheet }, icon('x', 18)));
    s.docTitle = h('div.r-doc-title.ellipsis');
    s.docLink = h('a.btn.sm', { target: '_blank', rel: 'noopener noreferrer', hidden: true }, 'arXiv');
    s.docBody = h('div.md.r-doc-md');
    s.docEl = h('div.r-doc', { hidden: true },
      h('div.r-doc-head', null, h('button.btn.ghost.r-back', { onclick: () => s.back() }, icon('chev-left', 16), 'Back'), s.docTitle, s.docLink),
      h('div.r-doc-scroll.scroll', null, s.docBody));
    s.main = h('div.r-sheet-body.scroll', null, bodyNode);
    s.back = () => { s.docEl.hidden = true; s.main.hidden = false; };
    s.showDoc = async (title, path, link) => {
      setText(s.docTitle, title);
      s.docLink.hidden = !link; if (link) s.docLink.href = link;
      clear(s.docBody).appendChild(h('div.r-skel', null, h('i'), h('i'), h('i')));
      s.main.hidden = true; s.docEl.hidden = false;
      try { const r = await get(path); renderInto(s.docBody, r.md || ''); if (r.url) { s.docLink.href = r.url; s.docLink.hidden = false; } }
      catch (e) { clear(s.docBody).appendChild(h('div.empty-note', null, e.message)); }
    };
    s.el = h('aside.r-sheet', { role: 'dialog', 'aria-modal': 'true', 'data-hue': hue || null }, h('span.r-grab', { 'aria-hidden': 'true' }), head, s.main, s.docEl);
    // drag the head down to close (phone layout only)
    let y0 = null, dy = 0;
    const phone = () => matchMedia('(max-width: 859px)').matches;
    head.addEventListener('touchstart', (e) => { if (!phone()) return; y0 = e.touches[0].clientY; dy = 0; s.el.style.transition = 'none'; }, { passive: true });
    head.addEventListener('touchmove', (e) => { if (y0 == null) return; dy = Math.max(0, e.touches[0].clientY - y0); s.el.style.transform = `translateY(${dy}px)`; }, { passive: true });
    const end = () => { if (y0 == null) return; y0 = null; s.el.style.transition = ''; s.el.style.transform = ''; if (dy > 110) closeSheet(); };
    head.addEventListener('touchend', end); head.addEventListener('touchcancel', end);
    document.body.append(s.overlay, s.el);
    addEventListener('keydown', s.onKey);
    void s.el.offsetWidth;
    s.el.classList.add('open'); s.overlay.classList.add('open');
    sheet = s;
    return s;
  }
  const section = (title, aside, ...kids) => h('section.r-sec', null, h('div.r-sec-head', null, h('h3.r-h3', null, title), aside || null), kids);

  // ---------- documents ----------
  function openDoc(title, kind, track) {
    const s = openSheet('doc', null, h('div.r-sheet-title', null, h('h2.r-h2', null, title), h('div.r-sheet-sub', null, data && data.mission ? data.mission.id : '')), h('div'));
    s.back = closeSheet;
    s.showDoc(title, `/api/research/doc?kind=${enc(kind)}${track ? '&track=' + enc(track) : ''}${wanted ? '&mission=' + enc(wanted) : ''}`);
  }
  function openCardSheet(aid) {
    const s = openSheet('doc', null, h('div.r-sheet-title', null, h('h2.r-h2', null, 'Paper card'), h('div.r-sheet-sub.mono', null, aid)), h('div'));
    s.back = closeSheet;
    s.showDoc(aid, `/api/research/card/${enc(aid)}${q()}`, `https://arxiv.org/abs/${aid}`);
  }

  // ---------- one agent ----------
  function openAgent(tid) {
    const known = data && data.agents.find((a) => a.id === tid);
    const nameEl = h('h2.r-h2', null, known ? known.name : tid);
    const subEl = h('div.r-sheet-sub');
    const orb = h('span.r-orb.lg');
    const oneEl = h('p.r-one', null, known ? known.one_line : '');
    const nowEl = h('div.r-nowlist');
    const kpis = { carded: h('span.r-kpi-n'), queued: h('span.r-kpi-n'), rejected: h('span.r-kpi-n'), failed: h('span.r-kpi-n') };
    const kpiEl = h('div.r-kpis', null, Object.entries(kpis).map(([k, n]) => h('div.r-kpi', null, n, h('span.micro', null, k === 'carded' ? 'cards' : k))));

    // summary
    const sumText = h('p.r-sum');
    const sumMeta = h('div.r-row-sub');
    const sumBtn = h('button.btn.r-big', { onclick: summarise }, icon('zap', 15), 'Summarise');
    let sumBusy = false, sumShown = '';
    async function summarise() {
      if (sumBusy) return;
      sumBusy = true; sumBtn.disabled = true;
      clear(sumText).appendChild(h('span.r-skel', null, h('i'), h('i'), h('i')));
      setText(sumMeta, 'Asking the reader model. This can take a minute while it is busy with papers.');
      try { showSummary(await post(`/api/research/summary/${enc(tid)}${q()}`), true); }
      catch (e) { sumShown = ''; setText(sumText, ''); setText(sumMeta, `No summary: ${e.message}`); }
      finally { sumBusy = false; sumBtn.disabled = false; }
    }
    function showSummary(s, force) {
      if (sumBusy && !force) return;
      if (!s) { if (!sumShown) { setText(sumText, 'No summary yet. Ask for one and the reader model writes a few sentences from this agent\'s cards and events.'); sumText.classList.add('dim'); } return; }
      const key = s.t + s.text;
      if (key === sumShown) { setText(sumMeta, metaOf(s)); return; }
      sumShown = key;
      sumText.classList.remove('dim');
      setText(sumText, s.text);
      setText(sumMeta, metaOf(s));
    }
    const metaOf = (s) => (s.fallback ? (s.error || 'Built from counts: the reader model did not answer.') : `Written by ${s.model} ${fmtAgo(s.t)}${s.cached ? ' · kept for 5 minutes' : ''}`);

    // composer
    let mode = 'instruct';
    const ta = h('textarea.textarea.r-input', { rows: 2, 'aria-label': 'Message to this agent' });
    const fit = autogrow(ta, 160);
    const modeBtns = [['instruct', 'Instruction'], ['focus', 'Priority topic']].map(([k, label]) => h('button', { 'aria-pressed': String(k === mode), onclick: () => setMode(k) }, label));
    const hint = h('div.r-row-sub');
    function setMode(k) {
      mode = k;
      modeBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(['instruct', 'focus'][i] === k)));
      ta.placeholder = k === 'instruct' ? 'For example: prefer papers with released code, and note the compute each one needs.' : 'A topic to chase, for example: object-centric world models';
      setText(hint, k === 'instruct' ? 'Stays in force. The agent sees it in everything it reads, searches and writes from now on.' : 'Its next searches look for this topic and matching papers jump the queue.');
    }
    const sendBtn = h('button.btn.brand.r-big', { onclick: send }, 'Send', icon('send', 15));
    async function send() {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      sendBtn.disabled = true;
      const r = await command(mode === 'focus' ? { cmd: 'focus', track: tid, topic: text } : { cmd: 'instruct', track: tid, text }, mode === 'focus' ? `Topic sent to ${tid}` : `Instruction sent to ${tid}`);
      sendBtn.disabled = false;
      if (r) { ta.value = ''; fit(); }
    }
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } });

    const standEl = h('div.r-list');
    const cardsEl = h('div.r-list');
    const queueEl = h('div.r-list');
    const eventsEl = h('div.r-list');
    const boardEl = h('div.r-list');
    const boardSec = section('Open questions for it', null, boardEl);
    const queueSec = section('Next in its queue', null, queueEl);
    const reportBtn = h('button.btn.r-big', { onclick: () => s.showDoc('Report', `/api/research/doc?kind=report&track=${enc(tid)}${wanted ? '&mission=' + enc(wanted) : ''}`) }, icon('file', 15), 'Report');
    const digestBtn = h('button.btn.r-big', { onclick: () => s.showDoc('Digest: what it believes so far', `/api/research/doc?kind=track_digest&track=${enc(tid)}${wanted ? '&mission=' + enc(wanted) : ''}`) }, icon('layers', 15), 'Digest');
    const taskBtn = h('button.btn.r-big', { onclick: () => s.showDoc('Assignment', `/api/research/doc?kind=assignment&track=${enc(tid)}${wanted ? '&mission=' + enc(wanted) : ''}`) }, icon('board', 15), 'Assignment');

    // controls
    const pauseBtn = h('button.btn.r-big', { onclick: async () => { pauseBtn.disabled = true; await command({ cmd: paused ? 'resume' : 'pause', track: tid }, paused ? `Resuming ${tid}` : `Pausing ${tid}`); pauseBtn.disabled = false; } });
    let paused = false, armed = false, armTimer = 0;
    const clearBtn = h('button.btn.danger.r-big', { onclick: async () => {
      if (!armed) { armed = true; clearBtn.classList.add('armed'); setText(clearLabel, 'Tap again to clear'); armTimer = setTimeout(disarm, 3500); return; }
      disarm(); clearBtn.disabled = true;
      await command({ cmd: 'clear', track: tid }, `Clearing the context of ${tid}`);
      clearBtn.disabled = false;
    } });
    const clearLabel = h('span', null, 'Clear context');
    clearBtn.append(icon('refresh', 15), clearLabel);
    function disarm() { armed = false; clearTimeout(armTimer); clearBtn.classList.remove('armed'); setText(clearLabel, 'Clear context'); }

    const body = h('div.r-agent', null,
      oneEl,
      section('Now doing', null, nowEl),
      kpiEl,
      section('Summary', sumBtn, sumText, sumMeta),
      section('Tell this agent', h('div.seg.seg-sm', null, modeBtns), ta, h('div.r-compose-foot', null, hint, sendBtn)),
      section('Standing instructions', null, standEl),
      section('Recent cards', null, cardsEl),
      queueSec,
      section('What it did', null, eventsEl),
      boardSec,
      section('Its memory', null, h('div.r-btnrow', null, reportBtn, digestBtn, taskBtn)),
      section('Controls', null, h('div.r-btnrow', null, pauseBtn, clearBtn),
        h('p.r-note', null, 'Clear context drops this agent\'s standing instructions and priority topics, rebuilds its digest from the claims ledger and resets its search counters, so it starts from a clean slate. Paper cards and the claims ledger are kept. Instructions sent to every agent stay until you clear them from all agents.'),
        h('p.r-note', null, 'Pause stops new papers and searching for this agent. A paper already being read is finished.')));
    const s = openSheet('agent', known ? known.hue : 'sky', h('div.r-sheet-title', null, orb, h('div', { style: { minWidth: '0' } }, nameEl, subEl)), body);
    setMode('instruct');
    requestAnimationFrame(fit);
    const seen = {};
    const changed = (k, v) => { const x = sig(v); if (seen[k] === x) return false; seen[k] = x; return true; };

    function paint(a) {
      if (sheet !== s) return;
      const alive = a.daemon_alive;
      s.el.dataset.hue = a.hue;
      setText(nameEl, a.name);
      setText(subEl, `${a.id} · ${stateLabel(a, alive)}${a.weight !== 1 && !a.paused ? ' · weight ' + a.weight : ''}`);
      orb.className = 'r-orb lg ' + (a.now.length ? 'working' : alive ? a.state : 'offline');
      setText(oneEl, a.one_line);
      for (const k in kpis) setNum(kpis[k], a.counts[k]);

      if (changed('now', [a.now.map((x) => [x.slot, x.step, x.i, x.n, x.paper, x.query]), alive, a.state])) {
        clear(nowEl);
        for (const x of a.now) {
          const at = STEPS.indexOf(x.step);
          nowEl.appendChild(h('div.r-nowitem', null,
            h('div.r-now-title', null, x.kind === 'worker' ? (x.title || x.paper) : verb(x)),
            h('div.r-row-sub', null, x.kind === 'worker' ? `${verb(x)} · ${x.paper}` : 'the mission\'s deep model', x.since ? ` · ${fmtDur(serverNow() - x.since, true)}` : ''),
            x.kind === 'worker' ? h('div.r-steps', null, STEPS.map((k, i) => h('span', { class: 'r-step' + (i < at ? ' done' : i === at ? ' now' : '') }, h('i'), STEP_LABEL[k]))) : null,
            x.kind === 'worker' && x.step === 'map' && x.n ? h('div.r-bar', null, h('span', { style: { width: Math.round(100 * (x.i || 0) / x.n) + '%' } })) : null));
        }
        if (!a.now.length) nowEl.appendChild(h('div.r-idle', null, nowLine(a, alive)));
      }
      showSummary(a.summary);

      const pend = (a.pending_commands || []).filter((c) => c.cmd === 'instruct' || c.cmd === 'focus');
      if (changed('stand', [a.standing, pend])) {
        clear(standEl);
        for (const x of a.standing.instructions) standEl.appendChild(row('pen', x.text, `${x.track === 'all' ? 'every agent' : 'this agent'} · ${fmtAgo(tsOf(x.ts))}`));
        for (const x of a.standing.focus) standEl.appendChild(row('zap', `Priority topic: ${x.topic}`, `${x.track === 'all' ? 'every agent' : 'this agent'} · ${fmtAgo(tsOf(x.ts))}`));
        for (const c of pend) standEl.appendChild(row('send', c.text || c.topic, 'sending… the daemon picks it up within 15 seconds', 'wait'));
        if (!standEl.firstChild) standEl.appendChild(h('div.r-idle', null, 'None. This agent follows only its assignment.'));
      }
      if (changed('cards', a.cards.map((c) => [c.id, c.grade]))) {
        clear(cardsEl);
        for (const c of a.cards) {
          cardsEl.appendChild(h('button.r-cardrow', { onclick: () => s.showDoc(c.title || c.id, `/api/research/card/${enc(c.id)}${q()}`, `https://arxiv.org/abs/${c.id}`) },
            h('div.r-cardrow-top', null, h('span.r-cardrow-title', null, c.title || c.id), h('span', { class: 'r-grade ' + (c.grade === 'ok' ? 'ok' : 'weak') }, c.grade || '?')),
            h('div.r-cardrow-line', null, c.one_line || ''),
            h('div.r-row-sub', null, [c.id, fmtAgo(c.t), c.quote_pass != null ? `quotes ${Math.round(c.quote_pass * 100)}%` : null, c.repo ? 'code found' : null].filter(Boolean).join(' · '))));
        }
        if (!a.cards.length) cardsEl.appendChild(h('div.r-idle', null, 'No cards yet.'));
      }
      if (changed('queue', a.queue)) {
        clear(queueEl);
        for (const x of a.queue) queueEl.appendChild(row('file', x.title, [x.id, x.via].filter(Boolean).join(' · ')));
        queueSec.hidden = !a.queue.length;
      }
      if (changed('events', a.events.map((e) => [e.t, e.kind, e.id]))) {
        clear(eventsEl);
        for (const e of a.events) eventsEl.appendChild(row(e.kind === 'carded' ? 'check' : e.kind === 'command' ? 'send' : e.kind === 'scouted' ? 'search' : 'log', e.text, fmtAgo(e.t), e.ok === false || /fail/.test(e.kind) ? 'bad' : ''));
        if (!a.events.length) eventsEl.appendChild(h('div.r-idle', null, 'Nothing logged yet.'));
      }
      if (changed('board', a.board)) {
        clear(boardEl);
        for (const b of a.board) boardEl.appendChild(row('chat', b.q, `${b.id} · asked by ${b.from}${a.board_open > a.board.length ? ` · ${a.board_open} open in all` : ''}`));
        boardSec.hidden = !a.board.length;
      }
      reportBtn.disabled = !a.report_chars; digestBtn.disabled = !a.digest;
      paused = !!a.paused;
      if (changed('pause', paused)) { clear(pauseBtn).append(icon(paused ? 'play' : 'stop', 15), paused ? 'Resume' : 'Pause'); }
    }
    async function load() {
      try { paint(await get(`/api/research/agent/${enc(tid)}${q()}`)); }
      catch (e) { if (sheet === s && e.status === 404) { toast('That agent is gone', { sub: e.message, tone: 'coral' }); closeSheet(); } }
    }
    s.reload = load;
    load();
    s.timer = setInterval(() => { if (!document.hidden) load(); }, SHEET_MS);
  }

  // ---------- new mission ----------
  function openMission() {
    let mode = 'queue';
    const running = !!(data && data.mission && data.mission.status === 'running' && data.daemon && data.daemon.alive);
    const titleIn = h('input.input.r-input', { placeholder: 'A short name, for example: Memory in world models', 'aria-label': 'Mission title', maxlength: 80, autocomplete: 'off' });
    const briefIn = h('textarea.textarea.r-input.r-brief', { rows: 8, 'aria-label': 'Brief', placeholder: 'What do you want to find out? Plain sentences or markdown.\n\nTo set the agents yourself, add sections like\n## Agent 1: name\nwhat this agent must find out, and the papers to read first.' });
    const hoursIn = h('input.input.r-input.r-hours', { type: 'number', min: '0.5', max: '12', step: '0.5', value: '6', 'aria-label': 'Hours', inputmode: 'decimal' });
    const modeBtns = [['queue', 'Queue'], ['preempt', 'Start now']].map(([k, label]) => h('button', { 'aria-pressed': String(k === mode), onclick: () => setMode(k) }, label));
    const hint = h('div.r-row-sub');
    const err = h('div.c-err', { role: 'alert' });
    function setMode(k) {
      mode = k;
      modeBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(['queue', 'preempt'][i] === k)));
      setText(hint, !running ? 'No mission is running, so the daemon starts this one as soon as it is up.'
        : k === 'queue' ? 'Waits its turn. It starts when the current mission ends.'
          : 'The daemon finishes the papers in hand (up to about 15 minutes), saves, and switches to this mission. The current one resumes afterwards.');
    }
    const go = h('button.btn.brand.r-big', { onclick: async () => {
      err.textContent = '';
      if (!titleIn.value.trim()) { err.textContent = 'Give the mission a title.'; titleIn.focus(); return; }
      if (briefIn.value.trim().length < 20) { err.textContent = 'Write a brief: a few sentences on what to find out.'; briefIn.focus(); return; }
      go.disabled = true;
      const r = await command({ cmd: 'mission', title: titleIn.value.trim(), brief: briefIn.value, mode, hours: Number(hoursIn.value) || 6 }, 'Mission sent');
      go.disabled = false;
      if (r) closeSheet();
    } }, 'Send mission', icon('send', 15));
    const body = h('div.r-agent', null,
      h('p.r-one', null, 'The research daemon turns the brief into agents (tracks), each with its questions and first searches, then reads papers until the time is up.'),
      section('Title', null, titleIn),
      section('Brief', null, briefIn),
      section('When', h('div.seg.seg-sm', null, modeBtns), hint),
      section('Hours', null, h('div.r-compose-foot', null, h('div.r-row-sub', null, 'Planned length, 0.5 to 12. The box session deadline still wins.'), hoursIn)),
      err, h('div.r-btnrow', null, go));
    openSheet('mission', null, h('div.r-sheet-title', null, h('span.r-orb.lg.core'), h('div', null, h('h2.r-h2', null, 'New mission'), h('div.r-sheet-sub', null, 'a research brief'))), body);
    setMode('queue');
    requestAnimationFrame(() => titleIn.focus());
  }

  return {
    el,
    setActive(on) { active = on; if (on) start(); else { stop(); closeSheet(); } },
  };
}

// True when the hub has research data: main.js makes Research the first view then.
export async function researchAvailable() {
  try { const r = await get('/api/research'); return !!(r && r.available && r.mission); } catch { return false; }
}
