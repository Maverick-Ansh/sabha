// Council mode: the crew reads one paper in parts, argues it out, and hands back a verdict.
import { openExchanges } from './exchanges.js';
import { h, icon, setText, clear, autogrow, copyText } from './dom.js';
import { get, post, del, enc } from './api.js';
import { subscribe, getState, agents, agentById, upsert, remove } from './store.js';
import { hueOf, fmtAgo, fmtNum } from './format.js';
import { renderInto } from './md.js';
import { toast } from './notify.js';

const ACTIVE = ['fetching', 'reading', 'discussing', 'verdict'];
const STEPS = [['fetching', 'Fetch'], ['reading', 'Read'], ['discussing', 'Discuss'], ['verdict', 'Verdict']];
const isActive = (c) => !!c && ACTIVE.includes(c.status);
const sorted = () => Object.values(getState().councils).sort((a, b) => (b.created_ts || 0) - (a.created_ts || 0));

export function createCouncil() {
  let currentId = null, rounds = 2, lastBlock = '', briefGrid = null, pickedInit = false;
  const rendered = new Set();   // message ids on screen
  const live = new Map();       // message_id -> streaming entry
  const picked = new Set();     // agent ids chosen for a new council
  let linked = (location.hash.match(/^#\/council\/(c_\w+)/) || [])[1] || null;   // #/council/<id> opens that council

  // ---------- rail ----------
  const railList = h('div.rail-list.scroll');
  const rail = h('aside.talk-rail', null,
    h('div.rail-head', null, h('span.micro', null, 'councils'),
      h('button.btn.ghost.sm.icon', { title: 'New council', onclick: () => open(null) }, icon('plus', 15))),
    railList);

  function renderRail() {
    clear(railList);
    const list = sorted();
    if (!list.length) railList.appendChild(h('div.empty-note', null, 'No councils yet.'));
    for (const c of list) {
      let armed = false;
      const delBtn = h('button.rail-del', { title: 'Delete council', onclick: async (e) => {
        e.stopPropagation();
        if (!armed) { armed = true; delBtn.classList.add('armed'); setText(delBtn, 'delete?'); setTimeout(() => { armed = false; delBtn.classList.remove('armed'); clear(delBtn).appendChild(icon('x', 12)); }, 2500); return; }
        try { await del(`/api/councils/${enc(c.id)}`); remove('councils', c.id); if (c.id === currentId) open(null); } catch (err) { toast('Could not delete', { sub: err.message, tone: 'coral' }); }
      } }, icon('x', 12));
      railList.appendChild(h('div', { class: 'rail-item' + (c.id === currentId ? ' on' : ''), role: 'button', tabindex: 0, onclick: () => open(c.id) },
        h('span', { class: 'c-railmark ' + c.status }),
        h('div.rail-text', null, h('div.rail-title.ellipsis', null, c.title || 'Council'),
          h('div.rail-sub.micro', null, `${statusText(c)} · ${fmtAgo(c.created_ts)}`)),
        isActive(c) ? h('span.rail-busy') : null, delBtn));
    }
  }

  // ---------- new council ----------
  const srcIn = h('input.input.c-src', { placeholder: 'arXiv id or link, for example 2412.06769', 'aria-label': 'Paper', autocomplete: 'off', spellcheck: 'false' });
  const qIn = h('textarea.textarea', { rows: 2, placeholder: 'What do you want to know about it? (optional)', 'aria-label': 'Question' });
  const textIn = h('textarea.textarea.mono', { rows: 6, placeholder: 'Paste the paper text here.', 'aria-label': 'Paper text' });
  const chipRow = h('div.c-members');
  const roundBtns = [1, 2, 3].map((n) => h('button', { 'aria-pressed': String(n === rounds), onclick: () => { rounds = n; roundBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(i + 1 === n))); } }, String(n)));
  const lookChk = h('input', { type: 'checkbox', checked: true });
  const goBtn = h('button.btn.brand.c-go', { onclick: convene }, 'Convene', icon('send', 14));
  const formErr = h('div.c-err', { role: 'alert' });
  const hero = h('div.c-hero.scroll', null, h('div.c-hero-col', null,
    h('div.micro.c-eyebrow', null, 'council'),
    h('h1.display.c-h1', null, 'Read a paper', h('br'), h('span.greet-2', null, 'together.')),
    h('p.dim.c-lede', null, 'Give the council a paper. Each member reads one part. Then they argue it out, look things up on the web when they need to, and hand you a verdict.'),
    h('div.card.c-form', null,
      srcIn, qIn,
      h('div.c-row', null, h('span.micro.c-rowlabel', null, 'members'), chipRow),
      h('div.c-row', null, h('span.micro.c-rowlabel', null, 'rounds'), h('div.seg.seg-sm', null, roundBtns),
        h('label.check', null, lookChk, 'web lookups'), h('span.spacer'), goBtn),
      h('details.c-paste', null, h('summary', null, 'No link? Paste the text instead'), textIn),
      formErr)));
  srcIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); convene(); } });

  function renderChips() {
    const list = agents();
    for (const id of Array.from(picked)) if (!agentById(id)) picked.delete(id);
    if (!pickedInit && list.length) { pickedInit = true; list.slice(0, 4).forEach((a) => picked.add(a.id)); }
    clear(chipRow);
    if (!list.length) chipRow.appendChild(h('span.dim', null, 'Seat some agents in Crew first.'));
    for (const a of list) {
      chipRow.appendChild(h('button', { class: 'agent-chip sm' + (picked.has(a.id) ? ' on' : ''), 'data-hue': hueOf(a), 'aria-pressed': String(picked.has(a.id)),
        onclick: () => { if (picked.has(a.id)) picked.delete(a.id); else picked.add(a.id); renderChips(); } }, h('span.dot'), a.name));
    }
  }

  async function convene() {
    const ids = agents().filter((a) => picked.has(a.id)).map((a) => a.id);
    formErr.textContent = '';
    if (!srcIn.value.trim() && !textIn.value.trim()) { formErr.textContent = 'Give a paper: an arXiv id, a link, or pasted text.'; srcIn.focus(); return; }
    if (ids.length < 2 || ids.length > 6) { formErr.textContent = 'Pick 2 to 6 members.'; return; }
    goBtn.disabled = true;
    try {
      const c = await post('/api/councils', { source: srcIn.value.trim(), text: textIn.value, question: qIn.value.trim(), agent_ids: ids, rounds, lookups: lookChk.checked });
      upsert('councils', c);
      srcIn.value = ''; qIn.value = ''; textIn.value = '';
      open(c.id);
    } catch (e) { formErr.textContent = e.message; } finally { goBtn.disabled = false; }
  }

  // ---------- a session ----------
  const titleEl = h('h1.display.c-title');
  const subEl = h('div.c-sub.dim');
  const statusEl = h('span.status');
  const noteEl = h('span.c-note.dim');
  const stepsEl = h('div.c-steps', { 'aria-hidden': 'true' });
  const convBtn = h('button.btn.sm.ghost.c-conv', { title: 'Every exchange in full: what each member was told and asked, which model answered, the whole answer',
    onclick: () => { if (!currentId) return; const c = getState().councils && getState().councils[currentId];
      openExchanges({ title: `Conversations · ${(c && c.title) || 'council'}`, scope: 'council', id: currentId,
        who: (name) => { const a = Object.values(getState().agents || {}).find((x) => x.name === name); return { name: name || 'member', hue: a ? hueOf(a) : null }; } }); } },
    icon('chat', 14), h('span', null, 'Conversations'));
  const seatsEl = h('div.c-seats');
  const errEl = h('div.mcard-err', { hidden: true });
  const flow = h('div.c-flow');
  const scroller = h('div.c-scroll.scroll', null, h('div.c-col', null,
    h('div.c-head', null, h('div.c-headline', null, statusEl, noteEl, h('span.spacer'), convBtn, stepsEl), titleEl, subEl),
    seatsEl, errEl, flow));
  const ta = h('textarea.talk-input', { rows: 1, placeholder: 'Say something to the council…', 'aria-label': 'Message to the council' });
  const fit = autogrow(ta, 180);
  const sendBtn = h('button.btn.ink.icon.send', { title: 'Send (Enter)', onclick: say }, icon('up', 17));
  const stopBtn = h('button.btn.icon.stop', { title: 'Stop the council', hidden: true, onclick: stop }, icon('stop', 15));
  const hintEl = h('span.micro.c-hint');
  const session = h('div.c-session', { hidden: true }, scroller,
    h('div.talk-foot', null, h('div.talk-composer.card', null, ta, h('div.tc-row', null, hintEl, h('span.spacer'), stopBtn, sendBtn))));
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); say(); } });

  const el = h('div.view.council', { 'data-view': 'council' }, rail, h('main.c-main', null, hero, session));

  function statusText(c) {
    if (c.status === 'discussing') return `round ${c.round} of ${Math.max(c.rounds, c.round)}`;
    return { fetching: 'getting the paper', reading: 'reading', verdict: 'writing the verdict', done: 'done', failed: 'failed', stopped: 'stopped' }[c.status] || c.status;
  }

  function renderHead(c) {
    setText(titleEl, c.title || 'Council');
    const p = c.paper;
    clear(subEl);
    if (p) {
      const who = (p.authors || []).slice(0, 4).join(', ') + ((p.authors || []).length > 4 ? ' and others' : '');
      if (who) subEl.append(who, ' · ');
      if (p.url) subEl.append(h('a', { href: p.url, target: '_blank', rel: 'noopener' }, p.url.replace(/^https?:\/\//, '')), ' · ');
      subEl.append(`${fmtNum(p.chars)} characters in ${p.sections.length} sections`);
    } else if (c.source) subEl.append(c.source);
    statusEl.className = 'status ' + (isActive(c) ? 'running' : c.status === 'done' ? 'done' : c.status === 'failed' ? 'failed' : 'queued');
    setText(statusEl, statusText(c));
    setText(noteEl, isActive(c) ? (c.note || '') : '');
    errEl.hidden = !c.error; setText(errEl, c.error || '');

    let at = ACTIVE.indexOf(c.status);
    if (at < 0) at = c.status === 'done' || c.verdict ? 4 : c.round > 0 ? 2 : p ? 1 : 0;
    clear(stepsEl);
    STEPS.forEach(([, label], i) => stepsEl.appendChild(h('span', { class: 'c-step' + (i < at ? ' done' : i === at && isActive(c) ? ' now' : '') }, h('i'), label)));

    clear(seatsEl);
    for (const s of c.seats || []) {
      const a = agentById(s.agent_id);
      const state = s.state === 'reading' ? 'reading' : s.state === 'speaking' ? 'speaking' : '';
      seatsEl.appendChild(h('button', { class: 'c-seat card ' + state, 'data-hue': hueOf(a), title: s.section_ids.length ? 'Show this part of the paper' : '', onclick: () => { if (s.section_ids.length) showPart(c, s); } },
        h('div.c-seat-top', null, h('span.dot'), h('span.c-seat-name', null, a ? a.name : 'gone'),
          s.agent_id === c.chair_id ? h('span.micro.c-chair', null, 'chair') : null,
          h('span.spacer'), h('span.micro.c-seat-state', null, state || '')),
        h('div.c-seat-part', null, s.part || (p ? 'member at large' : 'waiting for the paper')),
        h('div.micro.c-seat-meta', null, s.chars ? `${fmtNum(s.chars)} chars${s.cut ? ' · trimmed to fit' : ''}` : (a && a.role) || ''),
        h('div.c-seat-bar', null, h('span'))));
    }
    stopBtn.hidden = !isActive(c);
    setText(hintEl, isActive(c) ? 'they will see it on the next turn' : c.paper ? 'this starts one more round' : '');
  }

  // ---------- messages ----------
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  function toBottom(force) { if (force || nearBottom()) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; }); }

  function place(kind, round) {
    const key = kind === 'brief' ? 'briefs' : kind === 'verdict' ? 'verdict' : kind === 'say' || kind === 'lookup' ? `round ${round}` : lastBlock || 'notes';
    if (key !== lastBlock) {
      lastBlock = key;
      flow.appendChild(h('div.c-label', null, h('span.micro', null, key)));
      briefGrid = key === 'briefs' ? flow.appendChild(h('div.c-briefs')) : null;
    }
    return kind === 'brief' && briefGrid ? briefGrid : flow;
  }

  function entry(m, streaming) {
    const a = agentById(m.agent_id);
    const seat = ((getState().councils[currentId] || {}).seats || []).find((s) => s.agent_id === m.agent_id);
    const body = h('div.md.c-md');
    let node;
    if (m.kind === 'brief') {
      node = h('article.card.c-brief', { 'data-hue': hueOf(a) },
        h('div.msg-who', null, h('span.dot'), h('span.c-who', null, a ? a.name : 'member'), h('span.micro.ellipsis', null, seat ? seat.part : '')), body);
    } else if (m.kind === 'verdict') {
      node = h('article.c-verdict', null, h('div.c-verdict-in', null,
        h('div.msg-who', null, h('span.c-orb'), h('span.c-who', null, 'The verdict'), h('span.micro', null, a ? `written by ${a.name}` : ''), h('span.spacer'),
          h('button.btn.ghost.sm', { onclick: () => { copyText(body.textContent || ''); toast('Verdict copied', { tone: 'mint' }); } }, 'Copy')), body));
    } else if (m.kind === 'lookup') {
      node = h('div.c-lookup', { 'data-hue': hueOf(a) },
        h('div.msg-who', null, icon('search', 14), h('span.c-who', null, (m.meta && m.meta.query) || 'web search'),
          h('span.micro', null, `${a ? a.name + ' asked' : 'asked'}${m.meta && m.meta.engine ? ' · ' + m.meta.engine : ''}`)), body);
    } else if (m.kind === 'human') {
      node = h('div.msg.user', null, h('div.bubble', null, m.content || ''));
    } else if (m.kind === 'note') {
      node = h('div.msg.system', null, h('span.micro', null, m.content || ''));
    } else {
      node = h('div.c-say', { 'data-hue': hueOf(a) }, h('div.msg-who', null, h('span.dot'), h('span.c-who', null, a ? a.name : 'member')), body);
    }
    node.classList.add('rise');
    if (streaming) node.classList.add('streaming', 'waiting');
    place(m.kind, m.round).appendChild(node);
    return { node, body, text: '', agent_id: m.agent_id, raf: 0 };
  }

  function paint(b) {
    if (b.raf) return;
    b.raf = requestAnimationFrame(() => { b.raf = 0; renderInto(b.body, b.text); toBottom(false); });
  }

  function addMessage(m, isLive) {
    if (!m || rendered.has(m.id)) return;
    rendered.add(m.id);
    for (const [id, b] of live) if (id !== m.id && b.agent_id === m.agent_id && !b.text) { b.node.remove(); live.delete(id); } // a turn that failed before it said anything
    let b = live.get(m.id);
    if (b) live.delete(m.id); else b = entry(m, false);
    b.node.classList.remove('streaming', 'waiting');
    if (m.kind !== 'human' && m.kind !== 'note') { b.text = m.content || ''; renderInto(b.body, b.text); }
    if (m.kind === 'lookup') for (const link of b.body.querySelectorAll('a')) { link.target = '_blank'; link.rel = 'noopener'; }
    if (isLive) toBottom(false);
  }

  subscribe('cdelta', (ev) => {
    const d = ev.data || {};
    if (d.council_id !== currentId || rendered.has(d.message_id)) return;
    let b = live.get(d.message_id);
    if (!b) { b = entry({ id: d.message_id, kind: d.mkind, round: d.round, agent_id: ev.agent_id }, true); live.set(d.message_id, b); toBottom(false); }
    if (d.text) { b.text += d.text; b.node.classList.remove('waiting'); paint(b); }
  });
  subscribe('cmessage', (ev) => { const d = ev.data || {}; if (d.council_id === currentId) addMessage(d.message, true); });
  subscribe('councils', (c) => {
    renderRail();
    if (c && c.removed && c.id === currentId) { open(null); return; }
    if (c && c.id === currentId) renderHead(c);
  });
  subscribe('agents', () => { renderChips(); const c = getState().councils[currentId]; if (c) renderHead(c); });
  subscribe('reset', () => {
    renderChips();
    if (currentId && !getState().councils[currentId]) currentId = null;
    if (!currentId) { let saved = linked; linked = null; try { saved = saved || localStorage.getItem('sabha.council'); } catch { /* ignore */ } if (saved && getState().councils[saved]) { open(saved); return; } const newest = sorted()[0]; if (isActive(newest)) { open(newest.id); return; } }
    if (currentId) open(currentId); else renderRail();
  });

  async function open(id) {
    currentId = id;
    try { localStorage.setItem('sabha.council', id || ''); } catch { /* ignore */ }
    for (const b of live.values()) cancelAnimationFrame(b.raf);
    rendered.clear(); live.clear(); lastBlock = ''; briefGrid = null;
    clear(flow);
    renderRail();
    hero.hidden = !!id; session.hidden = !id;
    if (!id) { renderChips(); return; }
    requestAnimationFrame(fit);   // the box was hidden when it was first measured
    const known = getState().councils[id];
    if (known) renderHead(known);
    try {
      const c = await get(`/api/councils/${enc(id)}`);
      if (currentId !== id) return;
      if (!known) renderHead(c);
      for (const m of c.messages || []) addMessage(m, false);
      toBottom(true);
    } catch (e) { flow.appendChild(h('div.empty-note', null, `Could not open this council: ${e.message}`)); }
  }

  async function say() {
    const text = ta.value.trim();
    if (!text || !currentId) return;
    ta.value = ''; fit();
    try { await post(`/api/councils/${enc(currentId)}/say`, { text }); toBottom(true); } catch (e) { ta.value = text; fit(); toast('Could not send', { sub: e.message, tone: 'coral' }); }
  }
  async function stop() {
    if (!currentId) return;
    try { await post(`/api/councils/${enc(currentId)}/stop`); } catch (e) { toast('Could not stop', { sub: e.message, tone: 'coral' }); }
  }

  async function showPart(c, seat) {
    const body = h('div.c-part-body.scroll', null, h('div.micro', null, 'opening…'));
    const close = () => { overlay.remove(); modal.remove(); removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const overlay = h('div.overlay', { onclick: close });
    const modal = h('div.modal.c-part', { role: 'dialog', 'aria-label': 'Part of the paper' },
      h('div.modal-head', null, h('div.display.modal-title.ellipsis', null, seat.part), h('button.btn.ghost.icon', { title: 'Close', onclick: close }, icon('x', 16))), body);
    document.body.append(overlay, modal);
    addEventListener('keydown', onKey);
    try {
      const parts = await Promise.all(seat.section_ids.map((sid) => get(`/api/councils/${enc(c.id)}/section?id=${enc(sid)}`)));
      clear(body);
      for (const p of parts) body.append(h('h3.c-part-h', null, p.title), h('div.c-part-text', null, p.text));
    } catch (e) { clear(body); body.append(h('div.empty-note', null, e.message)); }
  }

  renderChips(); renderRail();
  return { el, setActive(on) { if (on) { renderRail(); if (!currentId) requestAnimationFrame(() => srcIn.focus()); } } };
}
