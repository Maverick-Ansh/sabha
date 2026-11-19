// The research report reader: a full-screen document for the report a run writes (PROTOCOL.md section 8).
// Three tabs: Report (REPORT.md: the living report while the run goes, the refined report when it ends), Notebook
// (NOTEBOOK.md: every paper as it was read and every note the model wrote) and Papers (every card, as a shelf).
// Citations like [2505.11893] become chips with the paper's short name; a chip opens a peek with the paper's line,
// its best number and its card. Sections get a table of contents with scroll spy. Download (.md) and Print (PDF).
import { h, icon, clear, setText } from './dom.js';
import { get, post, enc } from './api.js';
import { renderInto } from './md.js';
import { fmtAgo } from './format.js';
import { toast } from './notify.js';

const POLL_MS = 30000;
const CITE = /\[(\d{4}\.\d{4,5})(?:v\d+)?\]|\[unverified citation removed\]/g;
const CITE_ONE = new RegExp(CITE.source);
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const narrow = () => matchMedia('(max-width: 859px)').matches;
const slug = (s, used) => {
  let b = String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'part';
  let k = b, n = 2;
  while (used.has(k)) k = `${b}-${n++}`;
  used.add(k);
  return k;
};
// "RLAP: A Reinforcement ..." -> "RLAP"; "UniZero: Generalized ..." -> "UniZero"; else the first words
function shortName(title, aid) {
  const t = String(title || '').trim();
  if (!t) return aid;
  const c = t.split(/:\s/)[0];
  if (c.length <= 22 && c !== t) return c;
  const w = t.split(/\s+/).slice(0, 3).join(' ');
  return w.length > 26 ? w.slice(0, 25) + '…' : w;
}
const SECTION_KIND = [
  [/^the answer/i, 'answer'], [/^key findings/i, 'findings'], [/^how the papers compare|^papers so far/i, 'compare'],
  [/^experiments/i, 'exps'], [/^what is still open|^where we stand/i, 'open'], [/^how the run went/i, 'run'],
  [/^the papers/i, 'dossier'], [/^wave log/i, 'waves'], [/^from the lab notebook/i, 'notes'], [/^synthesis/i, 'synth'],
];

let current = null;
export function closeReport() { if (current) current.close(); }

// opts: { wanted: mission id or '', overview: () => latest /api/research data or null }
export function openReport(opts = {}) {
  closeReport();
  const wanted = opts.wanted || '';
  const mq = wanted ? '&mission=' + enc(wanted) : '';
  const st = { tab: 'report', docs: {}, papers: new Map(), tracks: [], timer: 0, spy: null, refineTimer: 0, filter: { q: '', track: '' } };
  const ov = () => (opts.overview && opts.overview()) || null;

  // ---------- frame ----------
  const tabBtns = [['report', 'Report'], ['notebook', 'Notebook'], ['papers', 'Papers']].map(([k, label]) =>
    h('button', { role: 'tab', 'aria-selected': 'false', 'data-tab': k, onclick: () => show(k) }, label));
  const tabs = h('div.rp-tabs', { role: 'tablist', 'aria-label': 'Report parts' }, tabBtns);
  const refineBtn = h('button.btn.sm.rp-refine', { hidden: true, onclick: () => refine() }, icon('spark', 14), h('span', null, 'Refine'));
  const dlBtn = h('button.btn.sm.ghost', { title: 'Download this page as markdown', onclick: () => download() }, icon('down', 14), h('span.rp-hide-s', null, '.md'));
  const printBtn = h('button.btn.sm.ghost', { title: 'Print, or save as PDF', onclick: () => print() }, icon('file', 14), h('span.rp-hide-s', null, 'PDF'));
  const closeBtn = h('button.btn.ghost.icon', { title: 'Close (Esc)', 'aria-label': 'Close the report', onclick: () => api.close() }, icon('x', 18));
  const barTitle = h('div.rp-bar-title.ellipsis');
  const bar = h('header.rp-bar', null, closeBtn, barTitle, tabs, h('div.rp-bar-act', null, refineBtn, dlBtn, printBtn));
  const scroll = h('div.rp-scroll');
  const peekEl = h('div.rp-peek', { hidden: true, role: 'dialog', 'aria-label': 'Paper' });
  const root = h('div.rp', { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Research report' }, bar, scroll, peekEl);
  document.body.appendChild(root);
  document.documentElement.classList.add('rp-open');
  void root.offsetWidth;
  root.classList.add('open');

  const onKey = (e) => {
    if (e.key !== 'Escape') return;
    if (!peekEl.hidden) { hidePeek(); return; }
    api.close();
  };
  addEventListener('keydown', onKey);
  const onDocClick = (e) => { if (!peekEl.hidden && !peekEl.contains(e.target) && !e.target.closest('.rp-cite')) hidePeek(); };
  document.addEventListener('pointerdown', onDocClick, true);
  scroll.addEventListener('scroll', () => { if (!peekEl.hidden && !peekEl.classList.contains('wide') && !narrow()) hidePeek(); }, { passive: true });

  // ---------- data ----------
  async function loadDoc(kind) {
    const r = await get(`/api/research/doc?kind=${kind}${mq}`);
    return { md: r.md || '', mtime: r.mtime || 0 };
  }
  async function loadPapers() {
    try {
      const r = await get(`/api/research/papers${wanted ? '?mission=' + enc(wanted) : ''}`);
      st.tracks = r.tracks || [];
      st.papers = new Map((r.papers || []).map((p) => [p.id, p]));
    } catch { /* an older hub: chips fall back to ids */ }
  }
  const trackOf = (tid) => st.tracks.find((t) => t.id === tid) || null;

  // ---------- report & notebook ----------
  function splitHead(src) {
    const lines = src.split('\n');
    let i = 0, title = '';
    while (i < lines.length && !lines[i].trim()) i++;
    if (/^# /.test(lines[i] || '')) title = lines[i++].slice(2).trim();
    const pre = [];
    while (i < lines.length && !/^## /.test(lines[i])) pre.push(lines[i++]);
    let meta = '', intro = [];
    for (const l of pre) {
      const mm = l.match(/^_(.*(?:report|pass|notebook|Everything).*)_\s*$/);
      if (mm && !meta) meta = mm[1]; else intro.push(l);
    }
    const introTxt = intro.join('\n').trim();
    const qm = introTxt.match(/^\*\*The question\.\*\*\s*([\s\S]*)$/);
    return { title, meta, question: qm ? qm[1].trim() : '', intro: qm ? '' : introTxt, body: lines.slice(i).join('\n') };
  }

  function hero(head, kind) {
    const o = ov();
    const m = o && o.mission;
    const finished = /finished report/i.test(head.meta);
    const living = !finished && kind === 'report';
    const eyebrow = h('div.rp-eyebrow', null,
      h('span.rp-pill' + (finished ? '.done' : living ? '.live' : ''), null, h('i'), kind === 'notebook' ? 'Lab notebook' : finished ? 'Finished report' : 'Living report'),
      living && m && m.status === 'running' ? h('span.micro.dim', null, 'grows as the agents write') : null);
    const metaBits = / · /.test(head.meta) ? head.meta.split(/\s+·\s+/).filter((x) => x && !/^(finished|living) report$/i.test(x.trim())) : [];
    const sub = !metaBits.length && head.meta ? h('p.rp-sub', null, head.meta) : null;
    const stats = metaBits.length ? h('div.rp-meta', null, metaBits.map((b) => h('span', null, b.replace(/^refined /, 'refined ').replace(/^updated (\S+)/, (_, t) => 'updated ' + (Date.parse(t) ? fmtAgo(Date.parse(t) / 1000) : t))))) : null;
    let q = null;
    if (head.question) {
      const qBody = h('div.md.rp-q-md');
      renderInto(qBody, head.question.replace(/!\[\[[^\]]*\]\]/g, ''));
      const more = h('button.rp-q-more', { onclick: () => { const open = q.classList.toggle('open'); setText(more, open ? 'Show less' : 'Read the whole question'); } }, 'Read the whole question');
      q = h('div.rp-q', null, h('div.micro', null, 'the question'), qBody, more);
      requestAnimationFrame(() => { if (qBody.scrollHeight <= qBody.clientHeight + 4) more.hidden = true; });
    }
    return h('section.rp-hero', null, eyebrow, h('h1.rp-title', null, head.title || (m && m.title) || 'Research report'), sub, stats, q,
      head.intro ? (() => { const d = h('div.md.rp-intro'); renderInto(d, head.intro); return d; })() : null);
  }

  function group(parent, tag, cls, stopTags) {
    const kids = Array.from(parent.children);
    let box = null;
    for (const k of kids) {
      const t = k.tagName;
      if (t === tag) { box = h('div.' + cls); parent.insertBefore(box, k); box.appendChild(k); continue; }
      if (stopTags.includes(t)) { box = null; continue; }
      if (box) box.appendChild(k);
    }
  }

  function citeChips(el) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement.closest('code, pre, a, .rp-cite') ? NodeFilter.FILTER_REJECT : CITE_ONE.test(n.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const n of nodes) {
      const frag = document.createDocumentFragment();
      let last = 0;
      const s = n.data;
      const re = new RegExp(CITE.source, 'g');
      let m;
      while ((m = re.exec(s))) {
        if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
        if (m[1]) {
          const aid = m[1];
          const p = st.papers.get(aid);
          const tr = p && p.tracks && p.tracks[0] ? trackOf(p.tracks[0]) : null;
          frag.appendChild(h('button.rp-cite', { type: 'button', 'data-hue': (p && p.hue) || (tr && tr.hue) || null, 'data-aid': aid,
            title: p ? p.title : aid, onclick: (e) => { e.stopPropagation(); peek(aid, e.currentTarget); } },
          p ? shortName(p.title, aid) : aid));
        } else frag.appendChild(h('span.rp-cite.bad', { title: 'The model cited a paper the team never read: the citation was removed' }, 'unverified'));
        last = re.lastIndex;
      }
      if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
      n.parentNode.replaceChild(frag, n);
    }
  }

  function enhance(body, kind) {
    for (const t of body.querySelectorAll('table')) { const w = h('div.rp-tablewrap'); t.replaceWith(w); w.appendChild(t); }
    const used = new Set();
    const secs = [];
    let sec = null;
    for (const k of Array.from(body.children)) {
      if (k.tagName === 'H2') {
        const title = k.textContent.trim();
        const kindCls = (SECTION_KIND.find(([re]) => re.test(title)) || [null, 'plain'])[1];
        sec = h('section.rp-sec', { id: 'rp-' + slug(title, used), 'data-kind': kind === 'notebook' ? 'nb-wave' : kindCls });
        body.insertBefore(sec, k);
        sec.appendChild(k);
        secs.push({ el: sec, title });
        continue;
      }
      if (sec) sec.appendChild(k);
    }
    for (const s of secs) {
      const k = s.el.dataset.kind;
      if (k === 'dossier') { group(s.el, 'H4', 'rp-paper', ['H3', 'H2']); }
      else if (k === 'exps') group(s.el, 'H3', 'rp-exp', ['H2']);
      else if (k === 'notes' || k === 'nb-wave') group(s.el, 'H3', 'rp-entry', ['H2']);
      else if (k === 'waves') {
        group(s.el, 'H3', 'rp-wave', ['H2']);
        for (const w of s.el.querySelectorAll('.rp-wave')) {
          const head = w.firstElementChild;
          const d = h('details.rp-wave-d', null, h('summary', null, head.textContent));
          while (w.children.length > 1) d.appendChild(w.children[1]);
          head.remove();
          w.appendChild(d);
        }
      }
      if (k === 'answer') {
        for (const p of s.el.querySelectorAll('p')) {
          const mm = p.textContent.match(/^\s*Confidence:\s*(high|medium|low)\b[,:\s]*(.*)$/i);
          if (mm) { p.className = 'rp-conf'; p.dataset.level = mm[1].toLowerCase(); clear(p).append(h('b', null, mm[1] + ' confidence'), mm[2] ? h('span', null, mm[2].replace(/^because\s*/i, 'because ')) : null); }
        }
      }
    }
    for (const e of body.querySelectorAll('.rp-entry')) {
      const t = (e.firstElementChild && e.firstElementChild.textContent) || '';
      e.dataset.kind = /· read /.test(t) ? 'paper' : /· note/.test(t) ? 'note' : /closed/.test(t) ? 'wave' : 'other';
      const mm = t.match(/^(\d\d:\d\d) · (.*)$/);
      if (mm) { clear(e.firstElementChild).append(h('span.rp-when', null, mm[1]), h('span', null, mm[2])); }
    }
    citeChips(body);
    return secs;
  }

  function toc(secs) {
    if (secs.length < 2) return null;
    const links = secs.map((s) => h('a.rp-toc-a', { href: '#' + s.el.id, onclick: (e) => { e.preventDefault(); s.el.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' }); } },
      s.title.replace(/\s*\(.*\)$/, '')));
    const nav = h('nav.rp-toc', { 'aria-label': 'Contents' }, h('div.micro.rp-toc-h', null, 'contents'), links);
    if (st.spy) st.spy.disconnect();
    st.spy = new IntersectionObserver((ents) => {
      for (const en of ents) {
        if (!en.isIntersecting) continue;
        const i = secs.findIndex((s) => s.el === en.target);
        links.forEach((a, j) => a.classList.toggle('on', j === i));
        if (narrow() && links[i]) links[i].scrollIntoView({ block: 'nearest', inline: 'center' });
      }
    }, { root: scroll, rootMargin: '-15% 0px -70% 0px' });
    secs.forEach((s) => st.spy.observe(s.el));
    return nav;
  }

  async function paintDoc(kind, keepScroll) {
    const top = keepScroll ? scroll.scrollTop : 0;
    let d = st.docs[kind];
    if (!d) {
      clear(scroll).appendChild(h('div.rp-wrap', null, h('div.rp-skel', null, h('i'), h('i'), h('i'), h('i'))));
      try { d = st.docs[kind] = await loadDoc(kind === 'report' ? 'living' : 'notebook'); }
      catch (e) {
        if (st.tab !== kind) return;
        clear(scroll).appendChild(h('div.rp-wrap', null, h('div.rp-empty', null, h('h2', null, kind === 'notebook' ? 'No notebook yet' : 'No report yet'),
          h('p.dim', null, kind === 'notebook' ? 'The notebook starts with the first paper the agents read.' : 'The report appears once the first papers are read.'), h('p.micro.dim', null, e.message))));
        return;
      }
    }
    if (st.tab !== kind) return;
    const head = splitHead(d.md);
    setText(barTitle, head.title || (kind === 'notebook' ? 'Lab notebook' : 'Report'));
    const body = h('div.md.rp-md' + (kind === 'notebook' ? '.nb' : ''));
    renderInto(body, head.body);
    const secs = enhance(body, kind);
    const article = h('article.rp-article', null, hero(head, kind), body,
      h('footer.rp-foot.micro.dim', null, kind === 'notebook' ? 'Written by the agents while they read. Numbers come from the paper cards.'
        : 'Every number comes from a quote in the paper (open a citation, then Read the card). Citations the team never read are marked unverified.'));
    clear(scroll).appendChild(h('div.rp-wrap', null, h('div.rp-layout', null, toc(secs), article)));
    scroll.scrollTop = top;
  }

  // ---------- papers ----------
  function paintPapers() {
    setText(barTitle, 'Papers');
    const all = Array.from(st.papers.values());
    const search = h('input.input.rp-search', { type: 'search', placeholder: 'Search titles, methods, numbers', value: st.filter.q, 'aria-label': 'Search the papers',
      oninput: (e) => { st.filter.q = e.target.value; paintGrid(); } });
    const chip = (tid, label, hue) => h('button.rp-fchip', { 'aria-pressed': String(st.filter.track === tid), 'data-hue': hue || null,
      onclick: () => { st.filter.track = tid; chips.querySelectorAll('.rp-fchip').forEach((b) => b.setAttribute('aria-pressed', String(b === chipEls[tid]))); paintGrid(); } },
    hue ? h('i') : null, label);
    const chipEls = {};
    const chips = h('div.rp-fchips', null, [['', 'All', null], ...st.tracks.map((t) => [t.id, t.name, t.hue])].map(([tid, label, hue]) => (chipEls[tid] = chip(tid, label, hue))));
    const grid = h('div.rp-grid');
    const count = h('div.micro.dim');
    function paintGrid() {
      const q = st.filter.q.trim().toLowerCase();
      const rows = all.filter((p) => (!st.filter.track || (p.tracks || []).includes(st.filter.track))
        && (!q || [p.title, p.one_line, p.method, p.best, p.id].join(' ').toLowerCase().includes(q)));
      setText(count, `${rows.length} of ${all.length} papers`);
      clear(grid).append(...rows.map((p) => tile(p)));
      if (!rows.length) grid.appendChild(h('div.rp-empty', null, h('p.dim', null, all.length ? 'No paper matches.' : 'No papers read yet.')));
    }
    function tile(p) {
      const tr = p.tracks && p.tracks[0] ? trackOf(p.tracks[0]) : null;
      return h('button.rp-tile', { 'data-hue': p.hue || null, onclick: () => openCard(p.id) },
        h('div.rp-tile-top', null, h('span.mono', null, p.id), p.submitted ? h('span.micro.dim', null, p.submitted.replace(/^\w{3}, /, '')) : null,
          h('span.spacer'), p.repo ? h('span.rp-badge', { title: 'Official code' }, 'code') : null, p.grade ? h('span.rp-badge.g', null, p.grade) : null),
        h('div.rp-tile-title', null, p.title || p.id),
        h('div.rp-tile-line', null, p.one_line),
        p.best ? h('div.rp-best', null, h('i'), p.best) : null,
        tr ? h('div.micro.rp-tile-tr', null, tr.name) : null);
    }
    clear(scroll).appendChild(h('div.rp-wrap', null, h('div.rp-papers', null,
      h('div.rp-papers-head', null, h('h1.rp-title.sm', null, 'Every paper the team read'), count),
      h('div.rp-ptools', null, search, chips), grid)));
    paintGrid();
  }

  // ---------- peek ----------
  function hidePeek() { peekEl.hidden = true; peekEl.classList.remove('wide'); clear(peekEl); }
  function peek(aid, anchor) {
    const p = st.papers.get(aid);
    clear(peekEl);
    peekEl.classList.remove('wide');
    peekEl.dataset.hue = (p && p.hue) || '';
    const tr = p && p.tracks && p.tracks[0] ? trackOf(p.tracks[0]) : null;
    peekEl.append(
      h('div.rp-peek-top', null, h('span.mono', null, aid), tr ? h('span.micro', null, tr.name) : null, h('span.spacer'),
        h('button.btn.ghost.sm.icon', { 'aria-label': 'Close', onclick: hidePeek }, icon('x', 14))),
      h('div.rp-peek-title', null, p ? p.title : 'A paper the team read'),
      p && p.one_line ? h('p.rp-peek-line', null, p.one_line) : null,
      p && p.best ? h('div.rp-best', null, h('i'), p.best) : null,
      p && p.limits && p.limits[0] ? h('p.rp-peek-lim', null, h('b', null, 'Limitation. '), p.limits[0]) : null,
      h('div.rp-peek-act', null,
        h('button.btn.sm.brand', { onclick: () => openCard(aid) }, icon('file', 13), 'Read the card'),
        h('a.btn.sm', { href: `https://arxiv.org/abs/${aid}`, target: '_blank', rel: 'noopener noreferrer' }, 'arXiv'),
        p && p.repo ? h('a.btn.sm', { href: p.repo, target: '_blank', rel: 'noopener noreferrer' }, 'Code') : null));
    peekEl.hidden = false;
    if (narrow() || !anchor) { peekEl.style.left = peekEl.style.top = ''; peekEl.classList.add('sheet'); return; }
    peekEl.classList.remove('sheet');
    const r = anchor.getBoundingClientRect();
    const w = Math.min(380, innerWidth - 32);
    const left = Math.max(16, Math.min(r.left - 12, innerWidth - w - 16));
    peekEl.style.left = left + 'px';
    peekEl.style.top = '0px';
    const ph = peekEl.offsetHeight;
    const below = r.bottom + 10 + ph < innerHeight - 12;
    peekEl.style.top = (below ? r.bottom + 10 : Math.max(12, r.top - ph - 10)) + 'px';
  }
  async function openCard(aid) {
    const p = st.papers.get(aid);
    clear(peekEl);
    peekEl.classList.add('wide');
    peekEl.classList.remove('sheet');
    peekEl.style.left = peekEl.style.top = '';
    peekEl.dataset.hue = (p && p.hue) || '';
    const body = h('div.md.rp-card-md', null, h('div.rp-skel', null, h('i'), h('i'), h('i')));
    peekEl.append(h('div.rp-peek-top', null, h('span.mono', null, aid), h('span.spacer'),
      h('a.btn.sm', { href: `https://arxiv.org/abs/${aid}`, target: '_blank', rel: 'noopener noreferrer' }, 'arXiv'),
      h('button.btn.ghost.sm.icon', { 'aria-label': 'Close', onclick: hidePeek }, icon('x', 14))),
    h('div.rp-card-scroll', null, body));
    peekEl.hidden = false;
    try { const r = await get(`/api/research/card/${enc(aid)}${wanted ? '?mission=' + enc(wanted) : ''}`); renderInto(body, r.md || ''); citeChips(body); }
    catch (e) { clear(body).appendChild(h('p.dim', null, e.message)); }
  }

  // ---------- actions ----------
  function download() {
    const kind = st.tab === 'notebook' ? 'notebook' : 'report';
    let text = st.docs[kind] && st.docs[kind].md;
    if (st.tab === 'papers') {
      text = '# Papers\n\n' + Array.from(st.papers.values()).map((p) => `- [${p.id}] **${p.title}**: ${p.one_line}${p.best ? ' (' + p.best + ')' : ''} https://arxiv.org/abs/${p.id}`).join('\n') + '\n';
    }
    if (!text) { toast('Nothing to download yet', { tone: 'sun' }); return; }
    const o = ov();
    const name = `sabha-${st.tab}-${(o && o.mission && o.mission.id) || 'research'}.md`;
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
    const a = h('a', { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }
  function print() {
    for (const d of scroll.querySelectorAll('details')) d.open = true;
    document.documentElement.classList.add('rp-printing');
    const done = () => { document.documentElement.classList.remove('rp-printing'); removeEventListener('afterprint', done); };
    addEventListener('afterprint', done);
    window.print();
    setTimeout(done, 1500);
  }
  function paintRefine() {
    const o = ov();
    const m = o && o.mission;
    const r = m && m.refining;
    const ended = m && m.status === 'done';
    refineBtn.hidden = !ended || st.tab === 'papers';
    refineBtn.disabled = !!(r && r.running);
    refineBtn.classList.toggle('brand', !!(ended && !m.has_final));
    setText(refineBtn.lastChild, r && r.running ? 'Refining…' : m && m.has_final ? 'Refine again' : 'Write the finished report');
    refineBtn.title = r && r.running ? 'The model is rewriting the report from the notebook and the cards (a few minutes)'
      : 'Rewrite the finished report from the notebook, the cards and the state of knowledge (uses the box\'s models, a few minutes)';
  }
  async function refine() {
    const o = ov();
    const m = o && o.mission;
    try {
      await post('/api/research/refine', { mission: (m && m.id) || wanted || '' });
      toast('Refining the report', { sub: 'The model writes the answer, the key findings and the experiments; the tables and the paper dossier come from the cards. A few minutes.', tone: 'mint', timeout: 7000 });
      if (m) m.refining = { running: true };
      paintRefine();
      clearInterval(st.refineTimer);
      st.refineTimer = setInterval(async () => {
        try {
          const d = await get(`/api/research${wanted ? '?mission=' + enc(wanted) : ''}`);
          const rf = d.mission && d.mission.refining;
          if (m) { m.refining = rf; m.has_final = d.mission.has_final; }
          paintRefine();
          if (!rf || !rf.running) {
            clearInterval(st.refineTimer);
            if (rf && rf.error) toast('The report was not refined', { sub: rf.error, tone: 'coral', timeout: 9000 });
            else { toast('The finished report is ready', { tone: 'mint' }); st.docs = {}; await loadPapers(); show(st.tab === 'papers' ? 'report' : st.tab); }
          }
        } catch { /* try again next tick */ }
      }, 10000);
    } catch (e) { toast('Could not refine', { sub: e.message, tone: 'coral', timeout: 8000 }); }
  }

  // ---------- tabs & polling ----------
  function show(k) {
    st.tab = k;
    tabBtns.forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === k)));
    hidePeek();
    paintRefine();
    if (k === 'papers') paintPapers(); else paintDoc(k, false);
  }
  async function poll() {
    if (document.hidden) return;
    const o = ov();
    if (!o || !o.mission || o.mission.status === 'done') return;
    try {
      const kind = st.tab === 'notebook' ? 'notebook' : 'report';
      const d = await loadDoc(kind === 'report' ? 'living' : 'notebook');
      const old = st.docs[kind];
      if (!old || old.md !== d.md) {
        st.docs[kind] = d;
        await loadPapers();
        if (st.tab === kind && peekEl.hidden) paintDoc(kind, true);
      }
    } catch { /* keep what is shown */ }
  }

  const api = {
    close() {
      if (current !== api) return;
      current = null;
      clearInterval(st.timer); clearInterval(st.refineTimer);
      if (st.spy) st.spy.disconnect();
      removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onDocClick, true);
      root.classList.remove('open');
      document.documentElement.classList.remove('rp-open');
      setTimeout(() => root.remove(), reduced() ? 0 : 380);
    },
  };
  current = api;
  loadPapers().then(() => show(opts.tab || 'report'));
  st.timer = setInterval(poll, POLL_MS);
  return api;
}
