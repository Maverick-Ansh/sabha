// Observable store. Topic based so views update only what changed.
import { eventSummary, kindOf, stationFor, serverNow } from './format.js';

const state = {
  ready: false,
  app: {}, system: {},
  models: {}, agents: {}, missions: {}, threads: {}, councils: {},
  seqHead: 0,
  telemetry: null, telemetryAt: 0,
  conn: 'connecting',
  ui: { mode: 'watch', focusAgent: null, focusMission: null },
  // live activity, derived from run events: what each agent / mission is doing right now
  act: { agents: {}, missions: {} },
};

const subs = new Map(); // topic -> Set(fn)
export function subscribe(topic, fn) {
  if (!subs.has(topic)) subs.set(topic, new Set());
  subs.get(topic).add(fn);
  return () => subs.get(topic).delete(fn);
}
export function emit(topic, payload) {
  const set = subs.get(topic);
  if (!set) return;
  for (const fn of Array.from(set)) {
    try { fn(payload, state); } catch (e) { console.error(`[store] ${topic} handler`, e); }
  }
}
export const getState = () => state;

const byId = (arr) => { const o = {}; for (const x of arr || []) if (x && x.id) o[x.id] = x; return o; };

export function reset(snap) {
  if (!snap) return;
  state.app = snap.app || {};
  state.system = snap.system || {};
  state.models = byId(snap.models);
  state.agents = byId(snap.agents);
  state.missions = byId(snap.missions);
  state.threads = byId(snap.threads);
  state.councils = byId(snap.councils);
  state.seqHead = Math.max(state.seqHead, snap.seq_head || 0);
  state.ready = true;
  emit('reset', state);
}

export function setConn(c) { if (state.conn !== c) { state.conn = c; emit('conn', c); } }

export function setUI(patch) {
  let changed = false;
  for (const k in patch) if (state.ui[k] !== patch[k]) { state.ui[k] = patch[k]; changed = true; }
  if (changed) emit('ui', state.ui);
}

// --- token rate per mission (from stats deltas), summed per agent ---
const rate = new Map(); // mission_id -> { tok, ts, rate }
function trackRate(m) {
  const tok = (m.stats && m.stats.completion_tokens) || 0;
  const now = serverNow();
  const r = rate.get(m.id);
  if (!r) { rate.set(m.id, { tok, ts: now, rate: 0 }); return; }
  const dt = now - r.ts;
  if (dt < 0.5) return;
  const inst = Math.max(0, (tok - r.tok) / dt);
  r.rate = r.rate ? r.rate * 0.5 + inst * 0.5 : inst;
  r.tok = tok; r.ts = now;
}
export function agentRate(agentId) {
  let sum = 0;
  const now = serverNow();
  for (const m of Object.values(state.missions)) {
    if (m.agent_id !== agentId || m.status !== 'running') continue;
    const r = rate.get(m.id);
    if (r && now - r.ts < 12) sum += r.rate;
  }
  return sum;
}

// --- activity ("now" lines) ---
const ACT_TYPES = new Set(['run.thinking', 'run.text', 'run.tool_call', 'run.tool_result', 'run.final',
  'run.error', 'run.started', 'run.waiting', 'run.ended', 'plan.proposed', 'human.note', 'human.approved', 'human.stopped']);
const callTools = new Map(); // call_id -> tool (for result summaries)
export const toolForCall = (id) => callTools.get(id);

export function noteActivity(ev) {
  if (ev.type === 'run.tool_call' && ev.data) callTools.set(ev.data.call_id, ev.data.tool);
  if (!ACT_TYPES.has(ev.type)) return false;
  const d = ev.data || {};
  const tool = ev.type === 'run.tool_call' ? d.tool : ev.type === 'run.tool_result' ? callTools.get(d.call_id) : null;
  const entry = {
    ts: ev.ts, type: ev.type, kind: kindOf(ev), mission_id: ev.mission_id, tool,
    station: tool ? stationFor(tool) : ev.type === 'run.thinking' ? 'think' : ev.type === 'run.text' ? 'talk' : null,
    now: eventSummary(ev, tool),
  };
  let changed = false;
  if (ev.agent_id) {
    const cur = state.act.agents[ev.agent_id];
    if (!cur || cur.ts <= ev.ts) { state.act.agents[ev.agent_id] = entry; changed = true; }
  }
  if (ev.mission_id) {
    const cur = state.act.missions[ev.mission_id];
    if (!cur || cur.ts <= ev.ts) { state.act.missions[ev.mission_id] = entry; changed = true; }
  }
  return changed;
}

// Apply one event. `live` = arrived after the hello snapshot (entity events before it are stale).
export function apply(ev, live) {
  const d = ev.data || {};
  const t = ev.type;
  if (t === 'sys.telemetry') {
    state.telemetry = d; state.telemetryAt = Date.now() / 1000;
    emit('telemetry', d);
    return;
  }
  if (t === 'chat.delta') return emit('delta', ev);
  if (t === 'chat.done') return emit('chatdone', ev);
  if (t === 'model.log') return emit('modellog', ev);
  if (t === 'council.delta') return emit('cdelta', ev);
  if (t.startsWith('nirmana.')) return emit('nirmana', ev);

  if (t.startsWith('run.') || t.startsWith('plan.') || t.startsWith('human.')) {
    if (noteActivity(ev)) emit('activity', ev);
    emit('run', { ev, live });
    return;
  }
  if (!live) return; // stale entity event replayed after a newer snapshot

  switch (t) {
    case 'model.updated': if (d.model) { state.models[d.model.id] = d.model; emit('models', d.model); } break;
    case 'model.removed': delete state.models[d.id]; emit('models', { id: d.id, removed: true }); break;
    case 'agent.updated': if (d.agent) { state.agents[d.agent.id] = d.agent; emit('agents', d.agent); } break;
    case 'agent.removed': delete state.agents[d.id]; emit('agents', { id: d.id, removed: true }); break;
    case 'mission.updated': {
      const m = d.mission;
      if (!m) break;
      const prev = state.missions[m.id];
      state.missions[m.id] = m;
      trackRate(m);
      emit('missions', { mission: m, prev });
      if (!prev || prev.status !== m.status) emit('transition', { mission: m, prev });
      break;
    }
    case 'mission.removed': delete state.missions[d.id]; rate.delete(d.id); emit('missions', { id: d.id, removed: true }); break;
    case 'thread.updated': if (d.thread) {
      const prev = state.threads[d.thread.id];
      state.threads[d.thread.id] = Object.assign({}, prev, d.thread);
      emit('threads', state.threads[d.thread.id]);
    } break;
    case 'thread.removed': delete state.threads[d.id]; emit('threads', { id: d.id, removed: true }); break;
    case 'thread.message': emit('message', ev); break;
    case 'council.updated': if (d.council) {
      state.councils[d.council.id] = Object.assign({}, state.councils[d.council.id], d.council);
      emit('councils', state.councils[d.council.id]);
    } break;
    case 'council.removed': delete state.councils[d.id]; emit('councils', { id: d.id, removed: true }); break;
    case 'council.message': emit('cmessage', ev); break;
    default: break;
  }
}

// Optimistic upsert from a REST response (the event that follows is still the source of truth).
export function upsert(kind, obj) {
  if (!obj || !obj.id) return;
  const coll = state[kind];
  const prev = coll[obj.id];
  coll[obj.id] = kind === 'threads' ? Object.assign({}, prev, obj) : obj;
  if (kind === 'missions') { emit('missions', { mission: obj, prev }); if (!prev || prev.status !== obj.status) emit('transition', { mission: obj, prev, local: true }); }
  else emit(kind, coll[obj.id]);
}
export function remove(kind, id) {
  delete state[kind][id];
  emit(kind, { id, removed: true });
}

// Helpers
export const agents = () => Object.values(state.agents);
export const missions = () => Object.values(state.missions);
// research agents ('r:<track>', 'r:team') are not crew: watch.js registers their names and hues for the stream
const researchAgents = {};
export function setResearchAgents(map) { for (const k in researchAgents) delete researchAgents[k]; Object.assign(researchAgents, map); }
export const agentById = (id) => state.agents[id] || researchAgents[id];
export function defaultAgent() {
  const list = agents();
  return list.find((a) => /^lead$/i.test(a.name)) || list[0] || null;
}
export function sessionWindow() {
  const tel = state.telemetry, sys = state.system || {};
  const limit = (tel && tel.session_limit_s) || sys.session_limit_s || 12 * 3600;
  let start = sys.started_ts || serverNow();
  if (tel && Number.isFinite(tel.uptime_s)) start = state.telemetryAt + offsetGuess() - tel.uptime_s;
  return { start, end: start + limit, limit };
}
function offsetGuess() { return serverNow() - Date.now() / 1000; }
