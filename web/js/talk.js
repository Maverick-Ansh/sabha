// Talk mode: one column of conversation with an agent, live-streamed replies, mission cards.
import { h, icon, setText, clear, autogrow } from './dom.js';
import { get, post, del, enc } from './api.js';
import { subscribe, getState, agents, agentById, defaultAgent, upsert, remove } from './store.js';
import { hueOf, greeting, fmtAgo, truncate, statusLabel, tone, planProgress } from './format.js';
import { md, renderInto } from './md.js';
import { toast } from './notify.js';
import { dispatchMission } from './composer.js';
import { nav } from './nav.js';

const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
const SUGGEST = ['What has our research found so far?', 'Which papers fit my taste best, and why?', 'What did the last council conclude?', 'Plan the next experiment with me'];
const STEP_ICON = { search_memory: 'search', read_memory: 'file', web_search: 'search', read_page: 'eye' };

export function createTalk() {
  let agentId = null, threadId = null, mode = 'chat', railOpen = true, busyLocal = false;
  const rendered = new Set();               // message ids on screen
  const bubbles = new Map();                // message_id -> streaming bubble
  const cards = new Map();                  // mission_id -> Set(update fn)
  const narrow = () => matchMedia('(max-width: 760px)').matches;
  try { railOpen = !narrow() && localStorage.getItem('sabha.rail') !== '0'; } catch { /* ignore */ }

  // ---------- DOM ----------
  const railList = h('div.rail-list.scroll');
  const rail = h('aside.talk-rail', null,
    h('div.rail-head', null, h('span.micro', null, 'threads'),
      h('button.btn.ghost.sm.icon', { title: 'New thread', onclick: () => newThread() }, icon('plus', 15)),
      h('button.btn.ghost.sm.icon', { title: 'Hide threads', onclick: () => setRail(false) }, icon('chev-left', 15))),
    railList);
  const chips = h('div.talk-agents');
  const railBtn = h('button.btn.ghost.sm.icon.rail-open', { title: 'Show threads', onclick: () => setRail(true) }, icon('panel', 15));
  const col = h('div.talk-col');
  const scroller = h('div.talk-scroll.scroll', null, col);

  const ta = h('textarea.talk-input', { rows: 1, placeholder: 'Ask anything…', 'aria-label': 'Message' });
  const fit = autogrow(ta, 220);
  const modeBtns = ['chat', 'mission'].map((m) => h('button', { 'aria-pressed': String(m === mode), onclick: () => setMode(m) }, m === 'chat' ? 'Chat' : 'Mission'));
  const planChk = h('input', { type: 'checkbox' });
  const planLbl = h('label.check.plan-first', { hidden: true }, planChk, 'plan first');
  const mic = Speech ? h('button.btn.ghost.icon.mic', { title: 'Speak', onclick: toggleMic }, icon('mic', 17)) : null;
  const sendBtn = h('button.btn.ink.icon.send', { title: 'Send (Enter)', onclick: send }, icon('up', 17));
  const stopBtn = h('button.btn.icon.stop', { title: 'Stop the reply', hidden: true, onclick: stopReply }, icon('stop', 15));
  const composer = h('div.talk-composer.card', null, ta,
    h('div.tc-row', null, h('div.seg.seg-sm', null, modeBtns), planLbl, h('span.spacer'), mic, stopBtn, sendBtn));
  const main = h('main.talk-main', null, h('div.talk-top', null, railBtn, chips), scroller, h('div.talk-foot', null, composer));
  const el = h('div', { class: 'view talk' + (railOpen ? '' : ' rail-closed'), 'data-view': 'talk' }, rail, main);

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });

  function setRail(open) { railOpen = open; el.classList.toggle('rail-closed', !open); if (!narrow()) { try { localStorage.setItem('sabha.rail', open ? '1' : '0'); } catch { /* ignore */ } } }
  function setMode(m) {
    mode = m;
    modeBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(['chat', 'mission'][i] === m)));
    planLbl.hidden = m !== 'mission';
    composer.classList.toggle('mission', m === 'mission');
    placeholder();
  }
  function placeholder() {
    const a = agentById(agentId);
    const n = a ? a.name : 'the council';
    ta.placeholder = mode === 'mission' ? `Describe a mission for ${n}…` : `Ask ${n} anything…`;
  }

  // ---------- agents + threads ----------
  function renderChips() {
    clear(chips);
    for (const a of agents()) {
      chips.appendChild(h('button', { class: 'agent-chip' + (a.id === agentId ? ' on' : ''), 'data-hue': hueOf(a), onclick: () => selectAgent(a.id) },
        h('span.dot'), a.name));
    }
  }
  function threadsSorted() { return Object.values(getState().threads).sort((a, b) => (b.updated_ts || 0) - (a.updated_ts || 0)); }
  function renderRail() {
    clear(railList);
    const list = threadsSorted();
    if (!list.length) railList.appendChild(h('div.empty-note', null, 'No threads yet.'));
    for (const t of list) {
      const a = agentById(t.agent_id);
      let armed = false;
      const delBtn = h('button.rail-del', { title: 'Delete thread', onclick: async (e) => {
        e.stopPropagation();
        if (!armed) { armed = true; delBtn.classList.add('armed'); setText(delBtn, 'delete?'); setTimeout(() => { armed = false; delBtn.classList.remove('armed'); clear(delBtn).appendChild(icon('x', 12)); }, 2500); return; }
        try { await del(`/api/threads/${enc(t.id)}`); remove('threads', t.id); if (t.id === threadId) openThread(null); } catch (err) { toast('Could not delete', { sub: err.message, tone: 'coral' }); }
      } }, icon('x', 12));
      railList.appendChild(h('div', { class: 'rail-item' + (t.id === threadId ? ' on' : ''), 'data-hue': hueOf(a), role: 'button', tabindex: 0, onclick: () => { selectAgent(t.agent_id, false); openThread(t.id); if (narrow()) setRail(false); } },
        h('span.dot'), h('div.rail-text', null, h('div.rail-title.ellipsis', null, t.title || 'New thread'), h('div.rail-sub.micro', null, `${a ? a.name : '?'} · ${fmtAgo(t.updated_ts)}`)), t.busy ? h('span.rail-busy') : null, delBtn));
    }
  }
  function selectAgent(id, pickThread = true) {
    agentId = id;
    renderChips(); placeholder();
    if (pickThread) {
      const t = threadsSorted().find((x) => x.agent_id === id);
      openThread(t ? t.id : null);
    }
  }
  function newThread() { openThread(null); ta.focus(); }

  async function openThread(id) {
    threadId = id;
    rendered.clear(); bubbles.clear(); cards.clear();
    clear(col);
    renderRail(); updateBusy();
    if (!id) { renderGreeting(); return; }
    col.appendChild(h('div.talk-loading.micro', null, 'opening thread…'));
    try {
      const t = await get(`/api/threads/${enc(id)}`);
      if (threadId !== id) return;
      clear(col);
      for (const m of t.messages || []) addMessage(m, false);
      if (!(t.messages || []).length) renderGreeting();
      toBottom(true);
    } catch (e) {
      clear(col); col.appendChild(h('div.empty-note', null, `Could not open this thread: ${e.message}`));
    }
  }
  function renderGreeting() {
    const [a, b] = greeting();
    const ag = agentById(agentId);
    col.appendChild(h('div.greeting', null,
      h('h1.display.italic', null, a, h('br'), h('span.greet-2', null, b)),
      h('p.dim', null, ag ? `You are talking with ${ag.name}${ag.role ? ', who ' + ag.role : ''}. Talk is where you think out loud with an agent. `
        + 'It remembers everything this box has done (research runs, paper cards, councils, missions, earlier chats) and searches that, or the web, before it answers. '
        + 'Flip to Mission when you want it to go off and do long work.' : 'Seat an agent in Crew to begin.'),
      h('div.suggest', null, SUGGEST.map((s) => h('button.chip', { onclick: () => { ta.value = s; fit(); ta.focus(); } }, s)))));
  }

  // ---------- messages ----------
  function addMessage(m, live) {
    if (!m || rendered.has(m.id)) return;
    rendered.add(m.id);
    col.querySelector('.greeting')?.remove();
    if (m.mission_id) { col.appendChild(missionCard(m, live)); return; }
    if (m.role === 'user') { col.appendChild(h('div.msg.user', { 'data-id': m.id }, h('div.bubble', null, m.content || ''))); return; }
    if (m.role === 'system') { col.appendChild(h('div.msg.system', null, h('span.micro', null, m.content || ''))); return; }
    const b = assistantBubble(m.id);
    b.thinking = m.thinking || ''; b.text = m.content || '';
    for (const st of m.steps || []) addStep(b, st);
    paintBubble(b, true);
  }
  function assistantBubble(id) {
    let b = bubbles.get(id);
    if (b) return b;
    const a = agentById(agentId);
    const thinkBody = h('div.thought-body');
    const thinkHead = h('button.thought-head', { onclick: () => { b.userToggled = true; b.think.classList.toggle('open'); } }, h('span.micro', null, 'thought'), h('span.thought-live'), icon('chev-down', 13));
    const think = h('div.thought', { hidden: true }, thinkHead, thinkBody);
    const content = h('div.md.answer');
    const stepsEl = h('div.steps', { hidden: true });
    const elx = h('div.msg.agent', { 'data-hue': hueOf(a), 'data-id': id }, h('div.msg-who', null, h('span.dot'), h('span.micro', null, a ? a.name : 'agent')), think, stepsEl, content);
    col.appendChild(elx);
    b = { id, el: elx, think, thinkBody, content, stepsEl, nSteps: 0, text: '', thinking: '', timer: 0, userToggled: false };
    bubbles.set(id, b);
    rendered.add(id);
    return b;
  }
  // one line per lookup the agent made (memory or web), live while it works and kept with the message
  function addStep(b, st) {
    if (!st) return;
    b.stepsEl.hidden = false; b.nSteps += 1;
    b.stepsEl.appendChild(h('div.step', { title: st.id || st.url || '' }, icon(STEP_ICON[st.tool] || 'search', 13),
      h('span', null, st.label || st.tool), st.result ? h('span.dim', null, ` · ${st.result}`) : null));
  }
  function paintBubble(b, final) {
    if (b.thinking) { b.think.hidden = false; b.thinkBody.textContent = b.thinking; }
    if (!b.userToggled) b.think.classList.toggle('open', !b.text && !final);
    b.el.classList.toggle('streaming', !final);
    b.el.classList.toggle('waiting', !final && !b.text && !b.thinking);
    if (final) { clearTimeout(b.timer); b.timer = 0; renderInto(b.content, b.text); return; }
    if (!b.timer) b.timer = setTimeout(() => { b.timer = 0; renderInto(b.content, b.text); toBottom(); }, 140);
  }

  function missionCard(msg, live) {
    const mid = msg.mission_id;
    const isReport = Array.from(col.querySelectorAll('.mcard')).some((c) => c.dataset.mission === mid);
    const statusEl = h('span.status');
    const title = h('div.mcard-title.display');
    const bar = h('div.mcard-bar', null, h('span'));
    const progLbl = h('span.micro');
    const now = h('div.mcard-now.ellipsis');
    const body = h('div.mcard-body');
    const agentChip = h('span.chip.hue');
    const card = h('div', { class: 'mcard' + (isReport ? ' report' : ''), 'data-mission': mid },
      h('div.mcard-head', null, h('span.micro', null, isReport ? 'report' : 'mission'), statusEl, agentChip),
      title,
      isReport ? null : h('div.mcard-prog', null, bar, progLbl),
      isReport ? null : now,
      body,
      h('div.mcard-actions', null,
        h('button.btn.sm', { onclick: () => nav.watchMission(mid) }, icon('orbit', 14), 'Watch'),
        h('button.btn.sm', { onclick: () => nav.openMission(mid, isReport ? 'report' : 'plan') }, icon('layers', 14), 'Open')));
    const update = () => {
      const m = getState().missions[mid];
      const a = agentById(m ? m.agent_id : agentId);
      card.dataset.hue = hueOf(a);
      clear(agentChip).append(h('span.dot'), a ? a.name : 'agent');
      statusEl.className = 'status ' + tone(m ? m.status : 'queued');
      setText(statusEl, m ? statusLabel(m.status) : 'gone');
      setText(title, truncate((m && m.title) || msg.content || 'Mission', 120));
      if (!isReport) {
        const p = planProgress(m && m.plan);
        bar.firstChild.style.width = Math.round(p.frac * 100) + '%';
        setText(progLbl, p.total ? `${p.done} / ${p.total} steps` : m && m.stats ? `${m.stats.steps || 0} steps · ${m.stats.tool_calls || 0} tools` : '');
        const act = getState().act.missions[mid];
        setText(now, m && tone(m.status) === 'running' && act ? act.now : m && m.status === 'awaiting_approval' ? 'waiting for your approval' : '');
        now.hidden = !now.textContent;
      }
      const hasLater = !isReport && Array.from(col.querySelectorAll('.mcard.report')).some((c) => c.dataset.mission === mid);
      const text = isReport ? (msg.content || (m && m.result_text) || '') : (!hasLater && m && m.status === 'done' ? m.result_text : '');
      const err = m && m.status === 'failed' && m.error && !hasLater ? m.error : '';
      if (body._t !== text + '|' + err) {
        body._t = text + '|' + err;
        clear(body);
        if (text) body.appendChild(md(text, 'mcard-md'));
        if (err) body.appendChild(h('div.mcard-err', null, err));
      }
    };
    if (!cards.has(mid)) cards.set(mid, new Set());
    cards.get(mid).add(update);
    update();
    const m = getState().missions[mid];
    if (live && (isReport || (m && m.status === 'done'))) celebrate(card);
    // a report arriving means the dispatch card should drop its inline result
    if (isReport) setTimeout(() => cards.get(mid) && cards.get(mid).forEach((f) => f()), 0);
    return card;
  }
  function celebrate(card) {
    card.classList.add('celebrate');
    card.appendChild(h('span.sunburst', { 'aria-hidden': 'true' }));
    setTimeout(() => card.classList.remove('celebrate'), 2400);
  }

  // ---------- sending ----------
  async function ensureThread() {
    if (threadId) return threadId;
    const t = await post('/api/threads', { agent_id: agentId });
    upsert('threads', t);
    threadId = t.id;
    renderRail();
    return t.id;
  }
  async function send() {
    const text = ta.value.trim();
    if (!text) return;
    if (!agentId) { toast('Seat an agent first', { sub: 'Open Crew to add one.', tone: 'sun' }); return; }
    ta.value = ''; fit();
    try {
      const tid = await ensureThread();
      if (mode === 'mission') {
        await dispatchMission({ prompt: text, agent_id: agentId, require_plan_approval: planChk.checked, thread_id: tid });
      } else {
        busyLocal = true; updateBusy();
        const r = await post(`/api/threads/${enc(tid)}/messages`, { content: text });
        if (r && r.user) addMessage(r.user, true);
        if (r && r.assistant_id && !bubbles.has(r.assistant_id)) paintBubble(assistantBubble(r.assistant_id), false);
      }
      toBottom(true);
    } catch (e) {
      busyLocal = false; updateBusy();
      ta.value = text; fit();
      toast('Not sent', { sub: e.message, tone: 'coral' });
    }
  }
  async function stopReply() {
    if (!threadId) return;
    try { await post(`/api/threads/${enc(threadId)}/stop`); } catch (e) { toast('Could not stop', { sub: e.message, tone: 'coral' }); }
  }
  function updateBusy() {
    const t = threadId && getState().threads[threadId];
    const busy = busyLocal || !!(t && t.busy);
    stopBtn.hidden = !busy; sendBtn.hidden = busy;
  }

  let rec = null;
  function toggleMic() {
    if (rec) { rec.stop(); return; }
    rec = new Speech();
    rec.lang = navigator.language || 'en-US';
    rec.interimResults = true;
    const base = ta.value ? ta.value.replace(/\s*$/, ' ') : '';
    rec.onresult = (e) => { let s = ''; for (const r of e.results) s += r[0].transcript; ta.value = base + s; fit(); };
    rec.onend = () => { rec = null; mic.classList.remove('listening'); ta.focus(); };
    rec.onerror = () => { rec = null; mic.classList.remove('listening'); };
    mic.classList.add('listening');
    rec.start();
  }

  function atBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; }
  function toBottom(force) { if (force || atBottom()) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; }); }

  // ---------- live ----------
  subscribe('delta', (ev) => {
    if (ev.thread_id !== threadId) return;
    const d = ev.data || {};
    const stick = atBottom();
    const b = assistantBubble(d.message_id);
    if (d.thinking) b.thinking += d.thinking;
    if (d.text) b.text += d.text;
    if (d.step) addStep(b, d.step);
    paintBubble(b, false);
    if (stick) toBottom(true);
  });
  subscribe('chatdone', (ev) => {
    const b = bubbles.get((ev.data || {}).message_id);
    if (b) paintBubble(b, true);
    if (ev.thread_id === threadId || !ev.thread_id) { busyLocal = false; updateBusy(); }
  });
  subscribe('message', (ev) => {
    const m = (ev.data || {}).message;
    if (!m || ev.thread_id !== threadId) return;
    const b = bubbles.get(m.id);
    if (b) { b.text = m.content || b.text; b.thinking = m.thinking || b.thinking; if (!b.nSteps) for (const st of m.steps || []) addStep(b, st); paintBubble(b, true); if (m.role === 'assistant') { busyLocal = false; updateBusy(); } return; }
    const stick = atBottom();
    addMessage(m, true);
    if (m.role === 'assistant') { busyLocal = false; updateBusy(); }
    if (stick) toBottom(true);
  });
  const refreshCards = (id) => { const s = cards.get(id); if (s) s.forEach((f) => f()); };
  subscribe('missions', ({ mission, id }) => refreshCards(mission ? mission.id : id));
  subscribe('activity', (ev) => { if (ev && ev.mission_id) refreshCards(ev.mission_id); });
  subscribe('threads', () => { renderRail(); updateBusy(); });
  subscribe('agents', () => { renderChips(); placeholder(); });
  subscribe('reset', () => {
    if (!agentId || !agentById(agentId)) { const a = defaultAgent(); agentId = a ? a.id : null; }
    renderChips(); placeholder(); renderRail();
    if (!col.firstChild) { const t = threadsSorted().find((x) => x.agent_id === agentId); openThread(t ? t.id : null); }
  });

  return {
    el,
    setActive(on) { if (on) { requestAnimationFrame(() => ta.focus({ preventScroll: true })); updateBusy(); } },
  };
}
