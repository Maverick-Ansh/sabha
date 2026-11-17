// Watch mode: roster | orbit stage | stream, with the session timeline along the bottom.
import { h, icon, setText, clear } from './dom.js';
import { subscribe, getState, agents, missions, setUI, agentRate } from './store.js';
import { hueOf, fmtNum, truncate, tone } from './format.js';
import { createOrbit } from './orbit.js';
import { createStream } from './stream.js';
import { createTimeline } from './timeline.js';
import { openComposerModal } from './composer.js';
import { get } from './api.js';
import { nav } from './nav.js';

// Research runs in its own process (sabha/research/run.py), not as crew missions, so Watch polls its overview and
// shows each research agent (one per track) in the roster and on the orbit's outer band.
const R_POLL_MS = 4000;
const R_STEP = { fetch: 'fetching', gate: 'checking', map: 'reading', reduce: 'writing the card', verify: 'checking quotes',
  scouting: 'searching', consolidating: 'folding cards into claims', report: 'writing its report' };
function rStation(s) {
  if (!s) return 'think';
  if (s.kind === 'worker') return s.step === 'fetch' ? 'web' : 'files';
  return s.step === 'scouting' ? 'web' : 'think';
}
function rNow(a) {
  const s = (a.now || [])[0];
  if (!s) return a.retired ? `retired after wave ${a.retired}` : a.state === 'saturated' ? 'searches ran dry' : 'waiting for papers';
  const what = R_STEP[s.step] || s.step || 'working';
  const more = a.now.length > 1 ? ` (+${a.now.length - 1})` : '';
  return s.kind === 'worker' ? `${what} · ${s.title || s.paper || ''}${more}` : `${what}${s.query ? ' · ' + s.query : ''}${more}`;
}

function agentStatus(agentId) {
  const ms = missions().filter((m) => m.agent_id === agentId);
  if (ms.some((m) => m.status === 'awaiting_approval')) return 'needs';
  if (ms.some((m) => m.status === 'running' || m.status === 'planning')) return 'running';
  const last = ms.sort((a, b) => (b.ended_ts || b.created_ts || 0) - (a.ended_ts || a.created_ts || 0))[0];
  if (last && last.status === 'failed') return 'failed';
  return 'idle';
}
// A stat rolls to its new value instead of jumping.
const calm = matchMedia('(prefers-reduced-motion: reduce)');
function countTo(el, to) {
  const from = el._n || 0;
  el._n = to;
  cancelAnimationFrame(el._raf);
  if (from === to || calm.matches || document.hidden) { setText(el, fmtNum(to)); return; }
  const t0 = performance.now();
  const step = (ts) => {
    const k = Math.min(1, Math.max(0, (ts - t0) / 700)), e = 1 - Math.pow(1 - k, 3);
    setText(el, fmtNum(Math.round(from + (to - from) * e)));
    if (k < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}
const STATUS_WORD = { needs: 'needs you', running: 'in session', failed: 'failed', idle: 'idle' };

export function createWatch() {
  const rows = new Map(); // agentId|'*' -> refs
  const rosterList = h('div.roster-list');
  const rLabel = h('div.panel-label.micro', { hidden: true }, 'research');
  const rList = h('div.roster-list');
  const roster = h('aside.roster.scroll', null, h('div.panel-label.micro', null, 'the council'), rosterList, rLabel, rList);
  let rData = null, rTimer = 0, rBusy = 0;

  const canvas = h('canvas.orbit-canvas', { 'aria-label': 'Agents in orbit around the mission pool', role: 'img' });
  const stat = (label) => { const v = h('div.stat-v.display.italic', null, '0'); return { el: h('div.stat', null, v, h('div.micro', null, label)), v }; };
  const stats = { running: stat('Running'), steps: stat('Steps'), tools: stat('Tool calls'), tokens: stat('Tokens') };
  const focusChip = h('button.chip.focus-chip', { hidden: true, onclick: () => focus(null) });
  const empty = h('div.stage-empty', { hidden: true },
    h('div.display.italic.empty-line', null, 'The sabha is quiet. Give it something to do.'),
    h('button.btn.brand', { onclick: () => openComposerModal() }, icon('plus', 15), 'Give it a task'));
  const stage = h('section.stage', null,
    h('div.orbit-wrap', null, canvas),
    h('div.stage-top', null, focusChip),
    h('div.stage-stats', null, Object.values(stats).map((s) => s.el)),
    empty);

  let timeline = null;
  const stream = createStream({ onLive: () => timeline && timeline.clearReplay() });
  timeline = createTimeline({ onJump: (seq) => stream.jumpTo(seq) });
  const streamPanel = h('section.stream-panel', null, stream.el);
  const el = h('div.view.watch', { 'data-view': 'watch' }, roster, stage, streamPanel, h('section.tl-panel', null, timeline.el));

  const orbit = createOrbit(canvas, { onFocus: (id) => focus(id), onResearch: () => nav.go('research') });

  // ---------- research (polled) ----------
  async function pollResearch() {
    if (!getState().system || !getState().system.research) { rData = null; paintResearch(); return; }
    try { rData = await get('/api/research'); } catch { rData = null; }
    paintResearch();
  }
  function paintResearch() {
    const d = rData;
    const running = !!(d && d.run && d.run.running && d.mission);
    const list = running ? (d.agents || []).filter((a) => !a.retired) : [];
    rBusy = list.filter((a) => a.now && a.now.length).length;
    orbit.setResearch(list.map((a) => ({ id: 'r:' + a.id, name: a.name, hue: a.hue, working: !!(a.now && a.now.length),
      now: rNow(a), station: rStation((a.now || [])[0]) })));
    rLabel.hidden = !running;
    clear(rList);
    if (running) {
      const m = d.mission, w = m.waves || {}, st = m.stats || {};
      const head = row('r:*', { hue: 'lilac' });
      head.btn.classList.add('all');
      setText(head.name, 'Research');
      setText(head.role, truncate(m.title || m.id, 60));
      const team = (d.team || []).map((t) => t.step).filter(Boolean);
      setText(head.now, (w.on ? `wave ${w.n}${team.length ? ' · ' + team[0] : ''} · ` : '') + `${st.cards || 0} papers · ${st.per_hour || 0}/h`);
      setText(head.statEl, st.left_m != null ? `${st.left_m}m left` : '');
      head.btn.dataset.status = 'running';
      rList.appendChild(head.btn);
      for (const a of list) {
        const r = row('r:' + a.id, { hue: a.hue });
        setText(r.name, a.name.replace(/^Track \d+:\s*/, ''));
        setText(r.role, `${a.counts.carded} card${a.counts.carded === 1 ? '' : 's'}`);
        setText(r.now, rNow(a)); r.now.title = rNow(a);
        r.btn.dataset.status = a.now && a.now.length ? 'running' : 'idle';
        rList.appendChild(r.btn);
      }
    }
    refreshAllRow();
    renderStats();
  }

  function focus(id) {
    setUI({ focusAgent: id || null, focusMission: null });
  }

  // ---------- roster ----------
  function row(id, a) {
    const ring = h('span.r-ring');
    const orb = h('span.r-orb', null, h('span.r-core'), ring);
    const name = h('span.r-name.ellipsis');
    const role = h('span.r-role.ellipsis');
    const now = h('div.r-now.ellipsis');
    const statEl = h('span.r-stat');
    const btn = h('button', { class: 'r-row' + (id === '*' ? ' all' : ''), 'data-hue': id === '*' ? 'sun' : hueOf(a), onclick: () => (String(id).startsWith('r:') ? nav.go('research') : focus(id === '*' ? null : id)) },
      orb, h('div.r-text', null, h('div.r-top', null, name, role), now), statEl);
    return { btn, name, role, now, statEl, ring };
  }
  function renderRoster() {
    clear(rosterList); rows.clear();
    const all = row('*');
    setText(all.name, 'Everyone'); setText(all.role, 'the whole floor');
    rows.set('*', all); rosterList.appendChild(all.btn);
    for (const a of agents()) {
      const r = row(a.id, a);
      setText(r.name, a.name); setText(r.role, a.role || '');
      rows.set(a.id, r); rosterList.appendChild(r.btn);
    }
    if (!agents().length) rosterList.appendChild(h('div.empty-note', null, 'No one is seated yet. Open Crew to add an agent.'));
    refreshAll();
  }
  function refreshRow(id) {
    const r = rows.get(id);
    if (!r) return;
    const st = getState();
    const s = agentStatus(id);
    r.btn.dataset.status = s;
    const act = st.act.agents[id];
    const m = missions().find((x) => x.agent_id === id && (x.status === 'running' || x.status === 'planning'));
    setText(r.now, act && act.now ? truncate(act.now, 120) : STATUS_WORD[s]);
    r.now.title = act && act.now ? act.now : '';
    const rate = agentRate(id);
    setText(r.statEl, rate > 0.5 ? `${Math.round(rate)} tok/s` : m && m.stats && m.stats.steps ? `step ${m.stats.steps}` : '');
    r.btn.classList.toggle('active', st.ui.focusAgent === id);
  }
  function refreshAllRow() {
    const r = rows.get('*');
    if (!r) return;
    const ms = missions();
    const run = ms.filter((m) => tone(m.status) === 'running').length;
    const need = ms.filter((m) => m.status === 'awaiting_approval').length;
    r.btn.dataset.status = need ? 'needs' : run ? 'running' : 'idle';
    const rr = rData && rData.run && rData.run.running ? ` · research: ${rBusy} of ${(rData.agents || []).filter((a) => !a.retired).length} agents busy` : '';
    if (rr && !run && !need) r.btn.dataset.status = 'running';
    setText(r.now, (run || need ? `${run} running${need ? ` · ${need} need${need === 1 ? 's' : ''} you` : ''}` : ms.length ? 'all quiet' : 'nothing yet') + rr);
    setText(r.statEl, '');
    r.btn.classList.toggle('active', !getState().ui.focusAgent);
  }
  function refreshAll() { for (const id of rows.keys()) id === '*' ? refreshAllRow() : refreshRow(id); }

  // ---------- stage ----------
  function renderStats() {
    const f = getState().ui.focusAgent;
    const ms = missions().filter((m) => !f || m.agent_id === f);
    let steps = 0, tools = 0, tok = 0;
    for (const m of ms) { const s = m.stats || {}; steps += s.steps || 0; tools += s.tool_calls || 0; tok += (s.prompt_tokens || 0) + (s.completion_tokens || 0); }
    countTo(stats.running.v, ms.filter((m) => tone(m.status) === 'running').length + (f ? 0 : rBusy));
    countTo(stats.steps.v, steps);
    countTo(stats.tools.v, tools);
    countTo(stats.tokens.v, tok);
    empty.hidden = missions().length > 0 || !getState().ready || !!(rData && rData.run && rData.run.running);
  }
  function renderFocus() {
    const { focusAgent: f, focusMission: fm } = getState().ui;
    const a = f && getState().agents[f];
    const m = fm && getState().missions[fm];
    focusChip.hidden = !a && !m;
    if (a || m) {
      clear(focusChip);
      focusChip.dataset.hue = hueOf(a);
      focusChip.append(h('span.dot'), m ? `Following “${truncate(m.title || m.prompt, 48)}”` : `Focused on ${a.name}`, icon('x', 12));
    }
    stream.setScope(m ? { mission: fm } : { agent: f || null });
  }

  subscribe('reset', () => { renderRoster(); renderStats(); renderFocus(); if (active) pollResearch(); });  // the snapshot says whether research is on
  subscribe('agents', () => { renderRoster(); renderFocus(); });
  subscribe('missions', ({ mission }) => { renderStats(); if (mission) refreshRow(mission.agent_id); refreshAllRow(); });
  subscribe('activity', (ev) => { if (ev && ev.agent_id) refreshRow(ev.agent_id); else refreshAll(); });
  subscribe('ui', () => { refreshAll(); renderStats(); renderFocus(); });
  setInterval(() => { if (active) refreshAll(); }, 2500);

  let active = false;
  return {
    el,
    setActive(on) {
      active = on; orbit.setActive(on); timeline.setVisible(on);
      clearInterval(rTimer); rTimer = 0;
      if (on) { refreshAll(); pollResearch(); rTimer = setInterval(() => { if (!document.hidden) pollResearch(); }, R_POLL_MS); }
    },
  };
}
