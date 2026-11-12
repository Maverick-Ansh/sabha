// Event log: a compact index of every persisted event (for the timeline and stream windows)
// plus a bounded cache of full events. Older full events are fetched on demand.
import { get, enc } from './api.js';
import { kindOf, eventSummary } from './format.js';
import { noteActivity, toolForCall, emit } from './store.js';

const CACHE_MAX = 12000;
const idx = [];            // { seq, ts, type, a, m, k, s } sorted by seq
const cache = new Map();   // seq -> full event (insertion order ~ recency)
let maxSeq = 0;
let tailReady = false;
let historyDone = false;

export const entries = () => idx;
export const lastSeq = () => maxSeq;
export const isTailReady = () => tailReady;
export const isHistoryDone = () => historyDone;
export const full = (seq) => cache.get(seq);

function entryOf(ev) {
  const run = ev.type.startsWith('run.') || ev.type.startsWith('plan.') || ev.type.startsWith('human.');
  return {
    seq: ev.seq, ts: ev.ts, type: ev.type, a: ev.agent_id || null, m: ev.mission_id || null,
    k: kindOf(ev),
    s: run ? eventSummary(ev, ev.type === 'run.tool_result' ? toolForCall(ev.data && ev.data.call_id) : null).slice(0, 140) : '',
  };
}
function remember(ev) {
  if (cache.has(ev.seq)) cache.delete(ev.seq);
  cache.set(ev.seq, ev);
  if (cache.size > CACHE_MAX) {
    const drop = cache.size - CACHE_MAX;
    let i = 0;
    for (const k of cache.keys()) { if (i++ >= drop) break; cache.delete(k); }
  }
}
function lowerBound(seq) { // first index with idx[i].seq >= seq
  let lo = 0, hi = idx.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (idx[mid].seq < seq) lo = mid + 1; else hi = mid; }
  return lo;
}
export function has(seq) { const i = lowerBound(seq); return i < idx.length && idx[i].seq === seq; }

// Live (or replayed) persisted event. Returns false if it was a duplicate.
export function ingest(ev) {
  if (!ev.seq) return true;
  if (ev.seq > maxSeq) {
    idx.push(entryOf(ev));
    maxSeq = ev.seq;
  } else if (has(ev.seq)) {
    return false;
  } else {
    idx.splice(lowerBound(ev.seq), 0, entryOf(ev));
  }
  remember(ev);
  scheduleNotify();
  return true;
}

// A sorted batch from REST (history). Merged in place; activity updated; nothing streamed.
export function addBatch(events) {
  const fresh = [];
  for (const ev of events) {
    if (!ev || !ev.seq) continue;
    noteActivity(ev);
    if (ev.seq <= maxSeq && has(ev.seq)) { remember(ev); continue; }
    fresh.push(entryOf(ev));
    remember(ev);
  }
  if (!fresh.length) return;
  fresh.sort((a, b) => a.seq - b.seq);
  if (!idx.length || fresh[0].seq > idx[idx.length - 1].seq) {
    for (const e of fresh) idx.push(e);
  } else {
    const at = lowerBound(fresh[0].seq);
    if (at === idx.length || fresh[fresh.length - 1].seq < idx[at].seq) idx.splice(at, 0, ...fresh);
    else { for (const e of fresh) idx.push(e); idx.sort((a, b) => a.seq - b.seq); }
  }
  maxSeq = Math.max(maxSeq, idx[idx.length - 1].seq);
  scheduleNotify();
}

let notifyTimer = 0;
function scheduleNotify() {
  if (notifyTimer) return;
  notifyTimer = setTimeout(() => { notifyTimer = 0; emit('index', idx); }, 250);
}

// Startup: fetch the most recent slice first (stream + "now" lines), then the full history (timeline).
export async function loadHistory(head) {
  const tailFrom = Math.max(0, head - 1500);
  try {
    let since = tailFrom;
    for (let guard = 0; guard < 3; guard++) {
      const r = await get(`/api/events?since=${since}&limit=2000`);
      const evs = (r && r.events) || [];
      addBatch(evs);
      if (!r.more || !evs.length) break;
      since = evs[evs.length - 1].seq;
    }
  } catch (e) { console.warn('[events] tail load failed', e); }
  tailReady = true;
  emit('activity', null);
  emit('tail', null);
  if (tailFrom > 0) {
    try { await loadRange(0, tailFrom); } catch (e) { console.warn('[events] history load failed', e); }
  }
  historyDone = true;
  emit('index', idx);
}

// Fill (from, to] from REST in pages, yielding between pages.
export async function loadRange(from, to) {
  let since = from;
  for (let guard = 0; guard < 400 && since < to; guard++) {
    const r = await get(`/api/events?since=${since}&limit=2000`);
    const evs = ((r && r.events) || []).filter((e) => e.seq <= to);
    addBatch(evs);
    if (!r.more || !evs.length) break;
    since = evs[evs.length - 1].seq;
    await new Promise((res) => setTimeout(res, 30));
  }
}

// Query the index. match(entry) -> bool. Returns up to `limit` entries:
//   before: seq -> newest matching entries with seq < before (returned oldest first)
//   after:  seq -> oldest matching entries with seq > after
//   neither -> the newest `limit`
export function query(match, { before, after, limit = 200 } = {}) {
  const out = [];
  if (after != null) {
    for (let i = lowerBound(after + 1); i < idx.length && out.length < limit; i++) if (match(idx[i])) out.push(idx[i]);
    return out;
  }
  const start = before != null ? lowerBound(before) - 1 : idx.length - 1;
  for (let i = start; i >= 0 && out.length < limit; i--) if (match(idx[i])) out.push(idx[i]);
  return out.reverse();
}
export function countMatching(match, { before, after }) {
  const r = query(match, { before, after, limit: 1 });
  return r.length;
}

// Full events for these entries, fetching any that fell out of the cache.
export async function fetchFull(list, missionId) {
  const missing = list.filter((e) => !cache.has(e.seq));
  if (missing.length) {
    let since = missing[0].seq - 1;
    const last = missing[missing.length - 1].seq;
    for (let guard = 0; guard < 6 && since < last; guard++) {
      const span = missionId ? missing.length + 50 : Math.min(2000, last - since);
      const path = missionId
        ? `/api/missions/${enc(missionId)}/events?since=${since}&limit=${Math.min(2000, span)}`
        : `/api/events?since=${since}&limit=${span}`;
      let r;
      try { r = await get(path); } catch (e) { console.warn('[events] fetch failed', e); break; }
      const evs = (r && r.events) || [];
      for (const ev of evs) if (ev.seq) remember(ev);
      if (!r.more || !evs.length) break;
      since = evs[evs.length - 1].seq;
    }
  }
  return list.map((e) => cache.get(e.seq)).filter(Boolean);
}

// Seq of the first event at or after time ts (binary search on ts; ts is ~monotonic in seq).
export function seqAtTime(ts, match) {
  let lo = 0, hi = idx.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (idx[mid].ts < ts) lo = mid + 1; else hi = mid; }
  for (let i = lo; i < idx.length; i++) if (!match || match(idx[i])) return idx[i].seq;
  for (let i = lo - 1; i >= 0; i--) if (!match || match(idx[i])) return idx[i].seq;
  return null;
}
export function indexOfTime(ts) {
  let lo = 0, hi = idx.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (idx[mid].ts < ts) lo = mid + 1; else hi = mid; }
  return lo;
}
