// Watch mode: roster | orbit stage | stream, with the session timeline along the bottom.
import { h, icon, setText, clear } from './dom.js';
import { subscribe, getState, agents, missions, setUI, agentRate } from './store.js';
import { hueOf, fmtNum, truncate, tone } from './format.js';
import { createOrbit } from './orbit.js';
import { createStream } from './stream.js';
import { createTimeline } from './timeline.js';
import { openComposerModal } from './composer.js';

function agentStatus(agentId) {
  const ms = missions().filter((m) => m.agent_id === agentId);
  if (ms.some((m) => m.status === 'awaiting_approval')) return 'needs';
  if (ms.some((m) => m.status === 'running' || m.status === 'planning')) return 'running';
  const last = ms.sort((a, b) => (b.ended_ts || b.created_ts || 0) - (a.ended_ts || a.created_ts || 0))[0];
  if (last && last.status === 'failed') return 'failed';
  return 'idle';
}
const STATUS_WORD = { needs: 'needs you', running: 'in session', failed: 'failed', idle: 'idle' };

export function createWatch() {
  const rows = new Map(); // agentId|'*' -> refs
  const rosterList = h('div.roster-list');
  const roster = h('aside.roster.scroll', null, h('div.panel-label.micro', null, 'the council'), rosterList);

  const canvas = h('canvas.orbit-canvas', { 'aria-label': 'Agents in orbit around the mission pool', role: 'img' });
  const stat = (label) => { const v = h('div.stat-v.display.italic', null, '0'); return { el: h('div.stat', null, v, h('div.micro', null, label)), v }; };
  const stats = { running: stat('missions running'), steps: stat('steps'), tools: stat('tool calls'), tokens: stat('tokens') };
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

  const orbit = createOrbit(canvas, { onFocus: (id) => focus(id) });

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
    const btn = h('button', { class: 'r-row' + (id === '*' ? ' all' : ''), 'data-hue': id === '*' ? 'sun' : hueOf(a), onclick: () => focus(id === '*' ? null : id) },
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
    setText(r.now, run || need ? `${run} running${need ? ` · ${need} need${need === 1 ? 's' : ''} you` : ''}` : ms.length ? 'all quiet' : 'nothing yet');
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
    setText(stats.running.v, String(ms.filter((m) => tone(m.status) === 'running').length));
    setText(stats.steps.v, fmtNum(steps));
    setText(stats.tools.v, fmtNum(tools));
    setText(stats.tokens.v, fmtNum(tok));
    empty.hidden = missions().length > 0 || !getState().ready;
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

  subscribe('reset', () => { renderRoster(); renderStats(); renderFocus(); });
  subscribe('agents', () => { renderRoster(); renderFocus(); });
  subscribe('missions', ({ mission }) => { renderStats(); if (mission) refreshRow(mission.agent_id); refreshAllRow(); });
  subscribe('activity', (ev) => { if (ev && ev.agent_id) refreshRow(ev.agent_id); else refreshAll(); });
  subscribe('ui', () => { refreshAll(); renderStats(); renderFocus(); });
  setInterval(() => { if (active) refreshAll(); }, 2500);

  let active = false;
  return {
    el,
    setActive(on) { active = on; orbit.setActive(on); timeline.setVisible(on); if (on) refreshAll(); },
  };
}
